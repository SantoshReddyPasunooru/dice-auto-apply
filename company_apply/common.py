"""
Company-level batch job applicator — Phase 1
=============================================
Applies to all matching jobs at a company's careers page.

Phase 1 ATS support: Greenhouse · Lever · Ashby · Workday

Usage:
  python company_apply.py                            — interactive
  python company_apply.py --company "Anthropic"      — apply at Anthropic
  python company_apply.py --company "Anthropic" \\
         --profile santoshpasunoorureddy@gmail.com
  python company_apply.py --keywords "python,ai,ml"  — keyword filter
  python company_apply.py --dry-run                  — list jobs without applying
  python company_apply.py --list                     — list all companies in DB
  python company_apply.py --add                      — add a company to the DB
  python company_apply.py --setup                    — add phone/LinkedIn to profile
"""

import argparse
import asyncio
import csv
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Frame, BrowserContext
try:
    import gmail_sender as _gmail_sender
except ImportError:
    _gmail_sender = None

load_dotenv()

# ── Live Gmail verification-code watcher (background asyncio task per email) ──
_code_queues:   dict[str, asyncio.Queue] = {}
_watcher_stops: dict[str, asyncio.Event] = {}

# ── Paths ──────────────────────────────────────────────────────────────────────

_HERE            = Path(__file__).parent.parent  # dice_auto_apply/ root
COMPANY_DB_PATH       = _HERE / "company_careers_db.json"
APPLIED_LOG_PATH      = _HERE / "external_applied_jobs.csv"
PROFILES_JSON         = _HERE / "profiles.json"
RESUMES_JSON          = _HERE / "resumes.json"
CUSTOM_ANSWERS_PATH   = _HERE / "custom_answers.json"
SAVED_FILTERS_PATH    = _HERE / "saved_filters.json"

APPLIED_LOG_FIELDS = [
    "timestamp", "profile_email", "company", "ats",
    "job_title", "location", "job_url", "status", "notes",
]

SUBMIT_WAIT = 6      # seconds to wait after clicking submit before checking confirmation
BETWEEN_JOBS = 4     # seconds between each application

OLLAMA_URL   = "http://localhost:11434/api/generate"

# Experience-level synonyms: expands a single requested term to all equivalent title tokens.
# Allows "junior" to match "Associate SWE", "Entry Level Engineer", "New Grad", etc.
_EXP_SYNONYMS: dict[str, list[str]] = {
    "principal": ["principal", "distinguished", "fellow", "vp engineering"],
    "staff":     ["staff"],
    "senior":    ["senior", "sr"],
    "lead":      ["lead"],
    "mid":       ["mid", "intermediate", " ii ", " 2 "],
    "junior":    ["junior", "jr", "entry", "associate", "new grad",
                  "early career", " i ", " 1 ", "level 1", "l3",
                  "intern", "internship"],
    "intern":    ["intern", "internship", "co-op", "coop", "co op"],
}
OLLAMA_MODEL = "gemma2:2b"  # override via OLLAMA_MODEL env var; auto-detected at startup

# ── Resume text cache ─────────────────────────────────────────────────────────
# Keyed by absolute file path → extracted plain text (up to 3000 chars).
# Populated once per resume file on first use; reused across all Ollama calls.
_RESUME_TEXT_CACHE: dict[str, str] = {}

def _extract_resume_text(resume_path) -> str:
    """
    Extract plain text from a .pdf or .docx resume file.
    Returns up to 3000 characters — enough for Ollama context without bloating the prompt.
    Caches the result so each file is parsed at most once per run.
    """
    if resume_path is None:
        return ""
    key = str(resume_path)
    if key in _RESUME_TEXT_CACHE:
        return _RESUME_TEXT_CACHE[key]

    text = ""
    try:
        path = Path(key)
        if not path.exists():
            return ""
        suffix = path.suffix.lower()

        if suffix == ".docx":
            import docx as _docx
            doc = _docx.Document(str(path))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())

        elif suffix == ".pdf":
            import pdfplumber as _pdf
            with _pdf.open(str(path)) as pdf:
                pages = []
                for page in pdf.pages[:6]:   # first 6 pages is plenty
                    t = page.extract_text()
                    if t:
                        pages.append(t)
                text = "\n".join(pages)

        # Trim and cache
        text = text.strip()[:3000]
    except Exception:
        text = ""

    _RESUME_TEXT_CACHE[key] = text
    return text

def _detect_ollama_model() -> str:
    """Return the best available Ollama model, preferring larger/smarter ones."""
    import urllib.request as _ur, json as _j
    _PREF = ["llama3.2", "llama3.1", "llama3", "llama2", "mistral",
             "gemma2", "gemma2:2b", "gemma", "phi3", "phi", "qwen", "deepseek"]
    try:
        with _ur.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            models = [m["name"] for m in _j.loads(r.read()).get("models", [])]
        if not models:
            return OLLAMA_MODEL
        for pref in _PREF:
            for m in models:
                if m.startswith(pref):
                    return m
        return models[0]  # whatever is installed
    except Exception:
        return OLLAMA_MODEL

_OLLAMA_MODEL_LIVE = _detect_ollama_model()
print(f"  Ollama model : {_OLLAMA_MODEL_LIVE}", flush=True)

# Country names that appear as checkbox labels on compliance/equal-opportunity forms.
# Matching is case-insensitive on the stripped label.
_COUNTRY_CHECKBOX_NAMES: frozenset[str] = frozenset({
    "afghanistan", "albania", "algeria", "angola", "argentina", "armenia",
    "australia", "austria", "azerbaijan", "bahrain", "bangladesh", "belarus",
    "belgium", "bolivia", "brazil", "bulgaria", "cambodia", "cameroon",
    "canada", "chile", "china", "colombia", "congo", "costa rica", "croatia",
    "cuba", "cyprus", "czech republic", "czechia", "denmark", "ecuador",
    "egypt", "ethiopia", "finland", "france", "georgia", "germany", "ghana",
    "greece", "guatemala", "honduras", "hong kong", "hungary", "india",
    "indonesia", "iran", "iraq", "ireland", "israel", "italy", "jamaica",
    "japan", "jordan", "kazakhstan", "kenya", "kuwait", "latvia", "lebanon",
    "libya", "lithuania", "luxembourg", "malaysia", "mexico", "morocco",
    "mozambique", "myanmar", "nepal", "netherlands", "new zealand", "nigeria",
    "north korea", "norway", "oman", "pakistan", "panama", "peru",
    "philippines", "poland", "portugal", "qatar", "romania", "russia",
    "rwanda", "saudi arabia", "senegal", "serbia", "singapore", "slovakia",
    "somalia", "south africa", "south korea", "south sudan", "spain",
    "sri lanka", "sudan", "sweden", "switzerland", "syria", "taiwan",
    "tajikistan", "tanzania", "thailand", "tunisia", "turkey", "turkmenistan",
    "ukraine", "united arab emirates", "uae", "united kingdom", "uk",
    "united states", "united states of america", "us", "usa", "uruguay",
    "uzbekistan", "venezuela", "vietnam", "yemen", "zambia", "zimbabwe",
})


# ── Company DB ─────────────────────────────────────────────────────────────────

def load_company_db() -> dict:
    if COMPANY_DB_PATH.exists():
        try:
            raw = json.loads(COMPANY_DB_PATH.read_text())
            # Skip any non-dict entries (e.g. _note comment keys)
            return {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            pass
    return {}


def save_company_db(db: dict):
    COMPANY_DB_PATH.write_text(json.dumps(db, indent=2))


def find_company(query: str, db: dict) -> Optional[dict]:
    q = query.lower().strip()
    # Exact key
    if q in db:
        return db[q]
    # Name field or key substring
    for key, rec in db.items():
        if rec.get("name", "").lower() == q:
            return rec
        if q in rec.get("name", "").lower() or q in key:
            return rec
    return None


def add_company_interactive() -> Optional[dict]:
    print("\n── Add Company ─────────────────────────────────────────────────")
    name = input("  Company name (e.g. Stripe): ").strip()
    if not name:
        return None

    print("  ATS type:")
    print("    1 = Greenhouse  (boards.greenhouse.io)")
    print("    2 = Lever       (jobs.lever.co)")
    print("    3 = Ashby       (jobs.ashbyhq.com)")
    print("    4 = Other / Generic")
    choice = input("  Choice [1]: ").strip() or "1"
    ats_map = {"1": "greenhouse", "2": "lever", "3": "ashby", "4": "generic"}
    ats = ats_map.get(choice, "greenhouse")

    slug = input(
        f"  {ats.capitalize()} slug (e.g. 'anthropic' → boards.greenhouse.io/anthropic): "
    ).strip().lower().replace(" ", "")

    if ats == "greenhouse":
        careers_url = f"https://boards.greenhouse.io/{slug}"
    elif ats == "lever":
        careers_url = f"https://jobs.lever.co/{slug}"
    elif ats == "ashby":
        careers_url = f"https://jobs.ashbyhq.com/{slug}"
    else:
        careers_url = input("  Full careers URL: ").strip()

    override = input(f"  Careers URL [{careers_url}]: ").strip()
    if override:
        careers_url = override

    rec = {"name": name, "ats": ats, "slug": slug, "careers_url": careers_url}
    db  = load_company_db()
    key = re.sub(r"[^a-z0-9]", "_", name.lower()).strip("_")
    db[key] = rec
    save_company_db(db)
    print(f"\n  ✓ Added: {name} ({ats}, slug={slug})")
    print(f"    Careers: {careers_url}\n")
    return rec


# ── Job listing via public APIs ────────────────────────────────────────────────

def _get_json(url: str) -> Optional[dict | list]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [API] {e}  —  {url}")
        return None


def greenhouse_list_jobs(slug: str) -> list[dict]:
    """Fetch all open jobs from Greenhouse public API."""
    data = _get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    )
    if not isinstance(data, dict):
        return []
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "title":     j.get("title", "").strip(),
            "location":  j.get("location", {}).get("name", ""),
            "url":       j.get("absolute_url", ""),
            "id":        str(j.get("id", "")),
            "ats":       "greenhouse",
            "posted_at": j.get("updated_at", ""),   # ISO-8601 string
        })
    return jobs


def stripe_list_jobs() -> list[dict]:
    """
    Fetch all open jobs from Stripe's custom careers page.
    Stripe uses Next.js SSR with __NEXT_DATA__ containing all listings.
    Apply URL pattern: https://stripe.com/careers/apply/{slug}/{greenhouseId}
    """
    try:
        req = urllib.request.Request(
            "https://stripe.com/careers/search",
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [Stripe] Could not fetch careers page: {e}")
        return []

    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html, re.S
    )
    if not m:
        print("  [Stripe] __NEXT_DATA__ not found in page HTML")
        return []

    try:
        data      = json.loads(m.group(1))
        job_index = data["props"]["pageProps"]["jobIndexData"]
        listings  = job_index.get("listings", [])
        locations = job_index.get("filters", {}).get("locations", [])
    except Exception as e:
        print(f"  [Stripe] Parse error: {e}")
        return []

    jobs = []
    for j in listings:
        gh_id = j.get("greenhouseId")
        slug  = j.get("slug", "")
        title = j.get("title", "").strip()
        if not gh_id or not slug or not title:
            continue
        loc_indices = j.get("locationIndices", [])
        loc_names   = [locations[i]["name"] for i in loc_indices if i < len(locations)]
        loc_str     = ", ".join(loc_names)
        jobs.append({
            "title":     title,
            "location":  loc_str,
            "url":       f"https://stripe.com/careers/apply/{slug}/{gh_id}",
            "id":        str(gh_id),
            "ats":       "stripe",
            "posted_at": "",
        })
    return jobs


def lever_list_jobs(slug: str) -> list[dict]:
    """Fetch all open jobs from Lever public API."""
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return []
    jobs = []
    for j in data:
        hosted = j.get("hostedUrl", "") or j.get("applyUrl", "")
        apply_url = hosted.rstrip("/") + "/apply" if hosted and "/apply" not in hosted else hosted
        # Lever timestamps are Unix ms
        ts_ms = j.get("createdAt", 0) or 0
        posted_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat() if ts_ms else ""
        jobs.append({
            "title":     j.get("text", "").strip(),
            "location":  j.get("categories", {}).get("location", ""),
            "team":      j.get("categories", {}).get("team", ""),
            "url":       apply_url,
            "id":        j.get("id", ""),
            "ats":       "lever",
            "posted_at": posted_iso,
        })
    return jobs


def workday_list_jobs(
    tenant: str,
    shard: int,
    portal: str,
    search: str = "",
    limit: int = 100,
) -> list[dict]:
    """
    Fetch jobs from Workday's undocumented CXS JSON API (no browser needed).
    Returns normalized job dicts compatible with filter_jobs().
    """
    base = f"https://{tenant}.wd{shard}.myworkdayjobs.com"
    url  = f"{base}/wday/cxs/{tenant}/{portal}/jobs"
    jobs: list[dict] = []
    offset = 0
    while True:
        payload = json.dumps({
            "appliedFacets": {},
            "limit": min(limit, 20),
            "offset": offset,
            "searchText": search,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent":   "Mozilla/5.0 (compatible; JobBot/1.0)",
                "Accept":       "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            print(f"  [Workday] API error: {e}")
            break
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            ext_path = j.get("externalPath", "")
            apply_url = f"{base}/en-US/{portal}{ext_path}/apply"
            jobs.append({
                "title":     j.get("title", "").strip(),
                "location":  j.get("locationsText", ""),
                "url":       apply_url,
                "id":        ext_path.split("-")[-1] if ext_path else "",
                "ats":       "workday",
                "posted_at": "",
            })
        offset += len(postings)
        if offset >= data.get("total", 0):
            break
        if len(jobs) >= limit:
            break
    return jobs


async def ashby_list_jobs(page: Page, slug: str) -> list[dict]:
    """Scrape Ashby job board (no public REST API)."""
    url = f"https://jobs.ashbyhq.com/{slug}"
    try:
        await page.goto(url, wait_until="networkidle", timeout=0)
        await asyncio.sleep(2)
    except Exception as e:
        print(f"  [Ashby] Could not load {url}: {e}")
        return []

    jobs = []
    seen: set[str] = set()

    # Job cards are <a> tags that contain the title and link to /jobs/{id}
    for link in await page.locator("a[href*='/jobs/']").all():
        try:
            href  = (await link.get_attribute("href") or "").strip()
            title = (await link.inner_text()).strip()
            if not href or not title or href in seen or len(title) < 3:
                continue
            seen.add(href)
            if href.startswith("/"):
                href = f"https://jobs.ashbyhq.com{href}"
            jobs.append({"title": title, "location": "", "url": href, "ats": "ashby"})
        except Exception:
            pass
    return jobs


async def generic_list_jobs(page: Page, careers_url: str) -> list[dict]:
    """Fallback: scrape any careers page for job-looking links."""
    try:
        await page.goto(careers_url, wait_until="networkidle", timeout=0)
        await asyncio.sleep(2)
    except Exception as e:
        print(f"  [Generic] Could not load {careers_url}: {e}")
        return []

    job_pattern = re.compile(
        r"/(job|jobs|career|careers|position|posting|opening|role|apply)[s]?[/_\-]",
        re.I,
    )
    parsed_base = urlparse(careers_url)
    jobs: list[dict] = []
    seen: set[str] = set()

    for link in await page.locator("a[href]").all():
        try:
            href  = (await link.get_attribute("href") or "").strip()
            title = (await link.inner_text()).strip()
            if not href or len(title) < 4 or href in seen:
                continue
            if not job_pattern.search(href):
                continue
            seen.add(href)
            if href.startswith("/"):
                href = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
            jobs.append({"title": title, "location": "", "url": href, "ats": "generic"})
        except Exception:
            pass

    return jobs[:60]


# ── Job filtering ───────────────────────────────────────────────────────────────

# Major US tech-hub cities that Workday lists without a state abbreviation
_US_CITIES = {
    "san jose", "san francisco", "new york", "seattle", "austin", "boston",
    "chicago", "los angeles", "denver", "atlanta", "dallas", "houston",
    "phoenix", "portland", "san diego", "washington", "miami", "minneapolis",
    "salt lake city", "raleigh", "charlotte", "nashville", "philadelphia",
    "detroit", "pittsburgh", "columbus", "indianapolis", "sacramento",
    "san antonio", "orlando", "tampa", "las vegas", "new orleans",
    "boise", "richmond", "baltimore", "st. louis", "kansas city",
    "albuquerque", "tucson", "fresno", "memphis", "louisville",
    "palo alto", "menlo park", "mountain view", "sunnyvale", "santa clara",
    "bellevue", "redmond", "kirkland", "irvine", "santa monica", "culver city",
    "new york city", "brooklyn", "manhattan", "jersey city",
    "cambridge", "somerville", "waltham",
}

_US_STATE_ABBREVS = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in",
    "ia","ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv",
    "nh","nj","nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn",
    "tx","ut","vt","va","wa","wv","wi","wy","dc",
}

# Canadian province/territory abbreviations that overlap with US state detection
_CA_PROVINCE_ABBREVS = {
    "ab","bc","mb","nb","nl","ns","nt","nu","on","pe","qc","sk","yt",
}

# Canadian city/province keywords — if any appear the job is Canada-based
_CA_KEYWORDS = {
    "canada", "ontario", "british columbia", "alberta", "quebec", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland", "labrador",
    "prince edward island", "northwest territories", "nunavut", "yukon",
    "toronto", "vancouver", "montreal", "calgary", "edmonton", "ottawa",
    "winnipeg", "hamilton", "kitchener", "london ontario", "halifax",
    "victoria bc", "mississauga", "brampton", "surrey bc",
}

def _is_canada_location(loc: str) -> bool:
    """Return True if the location string is clearly Canadian."""
    if not loc:
        return False
    l = loc.lower().strip()
    for kw in _CA_KEYWORDS:
        if kw in l:
            return True
    # "Toronto, ON" — last comma-token is a Canadian province abbrev
    parts = [p.strip() for p in l.split(",")]
    if parts[-1] in _CA_PROVINCE_ABBREVS:
        return True
    return False


def _is_us_location(loc: str) -> bool:
    if not loc or not loc.strip():
        return True  # no location info — include
    # Canadian locations are explicitly excluded — check first so "Remote, Canada" is rejected
    if _is_canada_location(loc):
        return False
    l = loc.lower().strip()
    if "united states" in l or l in ("usa", "us") or " usa" in l or "u.s.a" in l:
        return True
    if "remote" in l:
        return True
    # "San Francisco, CA" — last comma-token is a US state abbrev
    parts = [p.strip() for p in l.split(",")]
    if parts[-1] in _US_STATE_ABBREVS:
        return True
    # Workday bare city names (no state code, e.g. "San Jose", "San Francisco")
    if l in _US_CITIES:
        return True
    # Workday "N Locations" — multiple locations, can't tell without clicking through;
    # include optimistically since Workday jobs are typically posted for US + elsewhere
    import re as _re
    if _re.match(r"^\d+\s+locations?$", l):
        return True
    return False


def filter_jobs(
    jobs: list[dict],
    keywords: list[str],
    applied_urls: set[str],
    locations:   Optional[list[str]] = None,
    posted_days: Optional[int]       = None,
    experience:  Optional[list[str]] = None,
    work_type:   Optional[list[str]] = None,
    us_only:     bool                = False,
) -> list[dict]:
    """
    Filter jobs by:
      • applied_urls — already applied (always excluded)
      • keywords     — any keyword in job title (case-insensitive substring)
      • locations    — any token in job location  (e.g. "remote", "austin")
      • posted_days  — posted within the last N days
      • experience   — any token in job title     (e.g. "senior", "staff")
      • work_type    — any token in title/location (e.g. "intern", "contract")
    """
    kw_lower  = [k.lower() for k in keywords]
    loc_lower = [l.lower() for l in (locations  or [])]
    wt_lower  = [w.lower() for w in (work_type  or [])]
    cutoff    = (datetime.now(tz=timezone.utc) - timedelta(days=posted_days)
                 if posted_days else None)

    # Expand each requested experience term to all its synonyms so that
    # "junior" matches "Associate SWE", "New Grad Engineer", etc.
    exp_lower = [e.lower() for e in (experience or [])]
    exp_expanded: list[str] = []
    for e in exp_lower:
        exp_expanded.append(e)
        exp_expanded.extend(_EXP_SYNONYMS.get(e, []))
    exp_expanded = list(dict.fromkeys(exp_expanded))  # dedupe, preserve order

    result: list[dict] = []

    for j in jobs:
        if j["url"] in applied_urls:
            continue

        title_lower = j["title"].lower()
        loc_field   = j.get("location", "").lower()
        title_loc   = f" {title_lower} {loc_field} "  # padded for whole-word synonyms

        # keyword filter
        if kw_lower and not any(k in title_lower for k in kw_lower):
            continue

        # location filter
        if loc_lower and not any(l in loc_field for l in loc_lower):
            continue

        # US-only filter
        if us_only and not _is_us_location(j.get("location", "")):
            continue

        # date-posted filter
        if cutoff:
            raw = j.get("posted_at", "")
            if raw:
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if dt < cutoff:
                        continue
                except Exception:
                    pass  # unparseable date → include the job

        # experience-level filter (uses expanded synonyms)
        if exp_expanded and not any(e in title_loc for e in exp_expanded):
            continue

        # work-type filter
        if wt_lower:
            combined = title_lower + " " + loc_field
            if not any(w in combined for w in wt_lower):
                continue

        result.append(j)

    # If nothing matched, print a per-filter breakdown so the user knows why
    if not result and jobs:
        _log_zero_match_diagnosis(jobs, kw_lower, loc_lower, exp_expanded, cutoff, us_only)

    return result


def _log_zero_match_diagnosis(
    jobs: list[dict],
    kw_lower: list[str],
    loc_lower: list[str],
    exp_expanded: list[str],
    cutoff,
    us_only: bool,
):
    """Print a per-filter breakdown when 0 jobs pass all filters."""
    total = len(jobs)

    def _passes_kw(j):
        t = j["title"].lower()
        return not kw_lower or any(k in t for k in kw_lower)

    def _passes_loc(j):
        lf = j.get("location", "").lower()
        return not loc_lower or any(l in lf for l in loc_lower)

    def _passes_exp(j):
        tl = f" {j['title'].lower()} "
        return not exp_expanded or any(e in tl for e in exp_expanded)

    def _passes_date(j):
        if not cutoff:
            return True
        raw = j.get("posted_at", "")
        if not raw:
            return True
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")) >= cutoff
        except Exception:
            return True

    def _passes_us(j):
        return not us_only or _is_us_location(j.get("location", ""))

    rows = [
        ("keywords",    kw_lower,     _passes_kw),
        ("location",    loc_lower,    _passes_loc),
        ("experience",  exp_expanded, _passes_exp),
        ("date-posted", [cutoff],     _passes_date),
        ("us-only",     [us_only],    _passes_us),
    ]

    print("\n  ⚠  0 roles matched — per-filter breakdown:")
    for name, active, pred in rows:
        if not any(active):
            continue
        n = sum(1 for j in jobs if pred(j))
        bar = "✓" if n == total else ("~" if n > 0 else "✗")
        print(f"     {bar} {name:<14}: {n:>4}/{total} roles pass")
    print()
    print("  Tips:")
    if kw_lower:
        print(f"    • Keywords '{', '.join(kw_lower)}' may be too specific — leave blank to apply to all roles")
    if exp_expanded:
        print(f"    • Experience filter uses: {', '.join(exp_expanded[:6])}")
        print(f"      If 0 pass, this company may not use these terms in job titles")
    if cutoff:
        print(f"    • Try increasing days (e.g. 30 or 60) — older postings may still be open")
    print()


# ── Apply log ───────────────────────────────────────────────────────────────────

def _init_log():
    if not APPLIED_LOG_PATH.exists():
        with open(APPLIED_LOG_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=APPLIED_LOG_FIELDS).writeheader()
        return
    # Migrate: if existing CSV is missing the location column, rewrite it
    try:
        with open(APPLIED_LOG_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames and "location" not in reader.fieldnames:
                rows = list(reader)
        if "location" not in (open(APPLIED_LOG_PATH).readline()):
            with open(APPLIED_LOG_PATH, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=APPLIED_LOG_FIELDS)
                writer.writeheader()
                for row in rows:
                    writer.writerow({field: row.get(field, "") for field in APPLIED_LOG_FIELDS})
    except Exception:
        pass


_MAX_ERROR_RETRIES = 3   # skip a job permanently after this many consecutive errors

def load_applied_urls(email: str = "") -> set[str]:
    """
    Returns URLs to skip on the next run:
      • Successfully applied/submitted (never retry)
      • Errored >= _MAX_ERROR_RETRIES times without ever succeeding (give up)
    """
    _init_log()
    try:
        with open(APPLIED_LOG_PATH, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f)
                    if r.get("job_url")
                    and (not email or r.get("profile_email", "").lower() == email.lower())]
    except Exception:
        return set()

    from collections import Counter, defaultdict
    successes: set[str] = set()
    error_counts: Counter = Counter()

    for r in rows:
        url    = r["job_url"]
        status = r.get("status", "").lower()
        if any(w in status for w in ("applied", "submitted")):
            successes.add(url)
        elif "error" in status:
            error_counts[url] += 1

    # URLs that errored too many times and never succeeded → give up on them
    exhausted = {url for url, n in error_counts.items()
                 if n >= _MAX_ERROR_RETRIES and url not in successes}

    skip_urls = successes | exhausted
    if exhausted:
        print(f"  ⚠  {len(exhausted)} job(s) skipped — errored {_MAX_ERROR_RETRIES}+ times "
              f"(likely require a cover letter / portfolio — not retrying).")
    return skip_urls


def log_applied(company: str, ats: str, title: str, job_url: str,
                status: str, email: str, notes: str = "", location: str = ""):
    _init_log()
    with open(APPLIED_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=APPLIED_LOG_FIELDS).writerow({
            "timestamp":     datetime.now().isoformat(timespec="seconds"),
            "profile_email": email,
            "company":       company,
            "ats":           ats,
            "job_title":     title,
            "location":      location[:60],
            "job_url":       job_url,
            "status":        status,
            "notes":         notes[:100],
        })


# ── Profile helpers ─────────────────────────────────────────────────────────────

_EXTRA_FIELDS = [
    ("phone",        "Phone number (e.g. +1-512-555-0123): "),
    ("linkedin_url", "LinkedIn profile URL: "),
    ("github_url",   "GitHub URL (press Enter to skip): "),
    ("website_url",  "Portfolio/website URL (press Enter to skip): "),
]


def load_all_profiles() -> dict:
    if not PROFILES_JSON.exists():
        return {}
    try:
        return json.loads(PROFILES_JSON.read_text())
    except Exception:
        return {}


def ensure_profile_complete(profile: dict, email: str) -> dict:
    """Prompt for missing contact fields and save back. Empty input = keep blank."""
    changed = False
    # Only prompt for fields that are truly absent (key missing, not just empty string)
    missing = [f for f in _EXTRA_FIELDS if f[0] not in profile]
    for key, prompt in missing:
        try:
            val = input(f"  {prompt}").strip()
        except EOFError:
            val = ""
        if val:
            profile[key] = val
            changed = True
        else:
            profile[key] = ""
    if changed:
        all_p = load_all_profiles()
        all_p[email] = {k: v for k, v in profile.items() if k != "email"}
        PROFILES_JSON.write_text(json.dumps(all_p, indent=2))
        print("  ✓ Profile saved.\n")
    return profile


_RESUME_STOPWORDS = {"a","an","the","for","at","in","of","and","or","with","to",
                     "is","be","as","on","by","from","this","that","are","was"}

def _extract_resume_text(path: Path) -> str:
    """Extract plain text from a PDF or DOCX file."""
    try:
        if path.suffix.lower() == ".pdf":
            import pdfplumber
            with pdfplumber.open(str(path)) as pdf:
                return " ".join(
                    page.extract_text() or "" for page in pdf.pages
                ).lower()
        elif path.suffix.lower() == ".docx":
            from docx import Document
            doc = Document(str(path))
            return " ".join(p.text for p in doc.paragraphs).lower()
    except Exception:
        pass
    return ""


# Cache extracted resume text so we only read each file once per run
_RESUME_TEXT_CACHE: dict[Path, str] = {}


def get_resume(profile: dict, email: str, job_title: str = "") -> Optional[Path]:
    """
    Pick the best resume for the given job title by scoring each file's
    CONTENT (not just filename) against the job title keywords.
    Falls back to filename scoring, then default, then first file.
    """
    if not RESUMES_JSON.exists():
        return None
    try:
        rd = json.loads(RESUMES_JSON.read_text())
        entry   = rd.get(email, {})
        folder  = entry.get("resume_folder", "")
        default = entry.get("default_resume", "")

        all_resumes: list[Path] = []
        if folder:
            fp = Path(folder).expanduser()
            if fp.is_dir():
                all_resumes = sorted(list(fp.rglob("*.pdf")) + list(fp.rglob("*.docx")))

        default_path: Optional[Path] = None
        if default:
            dp = Path(default).expanduser()
            if dp.exists():
                default_path = dp

        if not all_resumes:
            return default_path

        if not job_title:
            return default_path or all_resumes[0]

        title_words = [
            w for w in re.sub(r"[^a-z0-9 ]", " ", job_title.lower()).split()
            if len(w) >= 2 and w not in _RESUME_STOPWORDS
        ]
        if not title_words:
            return default_path or all_resumes[0]

        best_score, best_path = -1, None
        for p in all_resumes:
            # Read and cache file content
            if p not in _RESUME_TEXT_CACHE:
                _RESUME_TEXT_CACHE[p] = _extract_resume_text(p)
            content = _RESUME_TEXT_CACHE[p]

            # Score: content keyword matches (weighted 2x) + filename matches (1x)
            path_text = p.as_posix().lower()
            content_score  = sum(2 for w in title_words if w in content)
            filename_score = sum(1 for w in title_words if w in path_text)
            score = content_score + filename_score

            if score > best_score:
                best_score, best_path = score, p

        if best_score > 0:
            return best_path

        return default_path or all_resumes[0]
    except Exception:
        return None


# ── Answer mapper: profile → form field value ──────────────────────────────────

def answer_for(label: str, profile: dict, email: str) -> str:
    """Return the best answer for a form field, given its label text."""
    l = label.lower().strip()

    name_parts = profile.get("name", "").split()
    first = name_parts[0] if name_parts else ""
    last  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # ── Name ──
    if re.search(r"first.?name|given.?name|forename", l):
        return first
    if re.search(r"last.?name|family.?name|surname", l):
        return last
    if re.search(r"^name$|full.?name|your.?name", l):
        return profile.get("name", "")

    # ── Contact ──
    if "email" in l:
        return email
    if re.search(r"phone|mobile|cell|tel", l):
        return profile.get("phone", "")

    # ── Links ──
    if "linkedin" in l:
        return profile.get("linkedin_url", "")
    if "github" in l:
        return profile.get("github_url", "")
    if re.search(r"website|portfolio|personal.*url|url.*personal", l):
        return profile.get("website_url", "")

    # ── Work auth — MUST come before location checks (long question text can match location patterns) ──
    if re.search(r"authorized.*(work|us)|eligible.*(work|us)|legally.*work|work.*author", l):
        return "Yes"
    if re.search(r"require.*sponsor|need.*sponsor|visa.*sponsor|sponsor.*visa|require.*company.*sponsor", l):
        return "Yes" if profile.get("needs_sponsorship") else "No"
    if re.search(r"work.*auth.*type|visa.*type|visa.*status|immigration.*status", l):
        return profile.get("work_auth", "OPT")

    # ── Location ──
    # "where.*you.*located" — only match "where are you located?", NOT "where the job is located"
    if re.search(r"^city$|^location$|current.*location|where.*you.*located|city.*state", l):
        return profile.get("location", "")
    if re.search(r"^state$", l):
        parts = profile.get("location", "").split(",")
        return parts[-1].strip() if len(parts) > 1 else ""
    # Match country-of-residence/location questions — narrow enough to avoid "country where job is located"
    if re.search(
        r"^country[\s\*:]*$"
        r"|country\s+of\s+(residence|citizenship|birth|work|origin)"
        r"|country\s+where\s+you\s+(currently\s+)?(reside|live|are\s+located)"
        r"|where\s+do\s+you\s+(currently\s+)?reside"
        r"|your\s+country\s+of\s+residence",
        l,
    ):
        return "United States"
    if re.search(r"zip|postal", l):
        return ""

    # ── Education ──
    if re.search(r"^school$|^university$|^college$|^institution$|school.*name|university.*name|"
                 r"where.*study|where.*attend|education.*institution|name.*school", l):
        return profile.get("school", "")
    if re.search(r"^degree$|degree.*type|type.*degree|highest.*degree|level.*education|"
                 r"education.*level|field.*study|area.*study|major", l):
        return profile.get("degree", "")
    if re.search(r"graduation.*year|year.*graduation|grad.*year|expected.*graduation", l):
        return profile.get("graduation_year", "")

    # ── Experience ──
    if re.search(r"years.*experience|experience.*years|how many years", l):
        return str(profile.get("years_experience", ""))

    # ── Current role ──
    if re.search(r"current.*title|job.*title|position.*title|role.*title", l):
        return profile.get("current_title", "")
    if re.search(r"current.*company|current.*employer|where.*work|previous.*employer|current.*or.*previous.*employer", l):
        # Return profile value if set; otherwise return empty so Ollama handles it
        return profile.get("current_company", "")

    # ── Salary ──
    if re.search(r"salary|compensation|pay|rate|expected", l):
        return profile.get("expected_salary", "Open to discussion")

    # ── Remote / work arrangement ──
    if re.search(r"remote|onsite|hybrid|work.*type|work.*arrangement|preferred.*work", l):
        return profile.get("preferred_work", "Any")

    # ── Disability — hardcoded: decline to self-identify ──
    if re.search(r"disabilit", l):
        return "I don't wish to answer"

    # ── Veteran — hardcoded to exact Greenhouse option text ──
    if re.search(r"veteran", l):
        return "I am not a protected veteran"

    # ── EEO / demographic ──────────────────────────────────────────────────────
    if re.search(r"\brace\b|ethnicity|racial", l):
        return profile.get("race", "")
    if re.search(r"gender|eeo|diversity", l):
        return profile.get("gender", "")

    # ── Cover letter / additional — skip ──
    if re.search(r"cover.?letter|additional.*info|anything.*else|tell.*us.*more", l):
        return ""

    return ""


def answer_bool(label: str, profile: dict) -> bool:
    """Return True/Yes for a yes/no question."""
    l = label.lower()
    if re.search(r"authorized|eligible.*work|legally.*work", l):
        return True
    if re.search(r"require.*sponsor|need.*sponsor", l):
        return bool(profile.get("needs_sponsorship"))
    if re.search(r"relocat", l):
        return bool(profile.get("open_to_relocation", True))
    if re.search(r"remote", l):
        return True
    return True   # default Yes for unknown yes/no questions


# ── CAPTCHA detection ───────────────────────────────────────────────────────────

async def has_captcha(page: Page) -> bool:
    """
    Detect an *actual* interactive CAPTCHA the user must solve.
    Only checks iframe URLs from known CAPTCHA providers — no element/text checks
    that cause false positives on pages mentioning "captcha" in accessibility text
    or that embed Cloudflare/reCAPTCHA for analytics without showing a challenge.
    """
    _CAPTCHA_ORIGINS = [
        "recaptcha.net/recaptcha/api2/bframe",   # reCAPTCHA interactive frame
        "google.com/recaptcha/api2/bframe",
        "hcaptcha.com/captcha/challenge",         # hCaptcha challenge frame
        "challenges.cloudflare.com/cdn-cgi/challenge-platform",  # CF Turnstile
    ]
    try:
        for frame in page.frames:
            url = frame.url.lower()
            if any(o in url for o in _CAPTCHA_ORIGINS):
                return True
    except Exception:
        pass
    return False


async def pause_for_captcha(page: Page):
    print("\n  ⚠  CAPTCHA detected — solve it in the browser window.")
    print("  Press Enter here once you've solved it...", end="", flush=True)
    await asyncio.to_thread(input, "")
    print()


# ── Custom-answers store (per profile, survives across runs) ───────────────────

def load_custom_answers() -> dict:
    """Load {email: {norm_label: answer}} from disk."""
    try:
        return json.loads(CUSTOM_ANSWERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_custom_answers(db: dict):
    CUSTOM_ANSWERS_PATH.write_text(
        json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_saved_filters(email: str) -> dict:
    try:
        data = json.loads(SAVED_FILTERS_PATH.read_text(encoding="utf-8"))
        return data.get(email, {})
    except Exception:
        return {}


def save_filters(email: str, filters: dict):
    try:
        data = json.loads(SAVED_FILTERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data[email] = filters
    SAVED_FILTERS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def check_gmail_confirmation(company: str, job_title: str, window_minutes: int = 10) -> bool:
    """
    Check Gmail for an application confirmation from `company` received
    in the last `window_minutes` minutes.  Returns True if found.
    """
    if _gmail_sender is None:
        return False
    try:
        service = _gmail_sender.get_gmail_service()
        if not service:
            return False
        after_ts = int((datetime.now(tz=timezone.utc).timestamp()) - window_minutes * 60)
        company_kw = company.lower().replace(" ", "")
        query = (
            f"(subject:application OR subject:received OR subject:confirmation OR subject:thank) "
            f"after:{after_ts}"
        )
        result = service.users().messages().list(
            userId="me", q=query, maxResults=10
        ).execute()
        msgs = result.get("messages", [])
        for ref in msgs:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            frm  = headers.get("From",    "").lower()
            subj = headers.get("Subject", "").lower()
            if company_kw in frm or company_kw in subj or company.lower() in frm or company.lower() in subj:
                return True
    except Exception:
        pass
    return False


def _ollama_generate_answer(question: str, profile: dict, job_title: str = "", company: str = "",
                            max_retries: int = 3, retry_delay: float = 3.0,
                            resume_path=None) -> str:
    """
    Use Ollama to generate a concise answer for an open-ended textarea question.
    When resume_path is provided, the resume text is included as primary context.
    Retries up to max_retries times with retry_delay seconds between attempts.
    Returns an empty string if Ollama is unavailable after all retries.
    """
    import time as _time
    model = __import__("os").environ.get("OLLAMA_MODEL", _OLLAMA_MODEL_LIVE)
    summary  = profile.get("summary", "")
    skills   = profile.get("skills", "")
    title    = profile.get("current_title", "")
    yoe      = profile.get("years_experience", "")
    location = profile.get("location", "")
    ctx_parts = []
    if job_title:
        ctx_parts.append(f"Applying for: {job_title} at {company}")

    # Resume text is the richest context — use it if available
    resume_text = _extract_resume_text(resume_path)
    if resume_text:
        ctx_parts.append(f"Candidate resume:\n{resume_text}")
    elif summary:
        ctx_parts.append(f"Candidate background: {summary}")
    else:
        ctx_parts.append(
            f"Candidate: {title}, {yoe} years experience, "
            f"skills: {skills}, based in {location}"
        )
    context = "\n".join(ctx_parts)

    prompt = (
        f"{context}\n\n"
        f"Job application question: \"{question}\"\n\n"
        f"Write a concise, professional answer in 2-4 bullet points or short sentences. "
        f"Base the answer strictly on the candidate's actual background above. "
        f"Do NOT add fake accomplishments. Be specific and honest. "
        f"Reply with ONLY the answer text, no preamble."
    )
    # num_predict: textarea needs full sentences; yes/no / short text needs very few tokens
    _is_short = not any(w in question.lower() for w in
                        ("describe", "explain", "tell us", "elaborate", "cover letter",
                         "essay", "summary", "background", "experience with"))
    num_predict = 60 if _is_short else 250

    payload = json.dumps({
        "model":   model,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0.2, "num_predict": num_predict},
    }).encode()

    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"      → [Ollama] retry {attempt}/{max_retries} for: {question[:60]!r}...")
                _time.sleep(retry_delay)
            req = urllib.request.Request(
                OLLAMA_URL, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                resp = json.loads(r.read())
            answer = resp.get("response", "").strip()
            if answer:
                return answer
            # Empty response — retry
        except Exception as _e:
            if attempt == max_retries:
                print(f"      → [Ollama] failed after {max_retries} attempts: {_e}")
    return ""


def _ollama_answer_batch(
    questions: list[dict],   # [{"id": str, "q": str, "options": list[str], "type": str}]
    profile: dict,
    job_title: str = "",
    company: str = "",
    resume_path=None,
) -> dict:
    """
    Send ALL pending form questions to Ollama in ONE HTTP call.
    Returns {id: answer_str}.  Falls back to empty dict on failure.

    Reduces N sequential Ollama calls (each 8-15s) to a single call (~10-15s total).
    Resume text is used as primary context when available.
    """
    import time as _time
    if not questions:
        return {}

    model = __import__("os").environ.get("OLLAMA_MODEL", _OLLAMA_MODEL_LIVE)
    summary  = profile.get("summary", "")
    title    = profile.get("current_title", "")
    yoe      = profile.get("years_experience", "")
    location = profile.get("location", "")
    school   = profile.get("school", "")
    degree   = profile.get("degree", "")
    reloc    = "yes" if profile.get("open_to_relocation") else "no"
    sponsor  = "yes" if profile.get("needs_sponsorship") else "no"
    skills   = profile.get("skills", "")

    # Resume text is the richest context — use it if available
    resume_text = _extract_resume_text(resume_path)
    if resume_text:
        ctx = f"Candidate resume:\n{resume_text}\n"
    elif summary:
        ctx = f"{summary}\n"
    if job_title:
        ctx = f"Applying for: {job_title} at {company}.\n" + ctx

    q_lines = []
    for i, item in enumerate(questions, 1):
        q   = item["q"]
        opts = item.get("options", [])
        if opts:
            opts_str = " | ".join(opts[:12])   # cap to avoid huge prompts
            q_lines.append(f'Q{i}: {q}\n   Options: {opts_str}')
        else:
            q_lines.append(f'Q{i}: {q}')

    prompt = (
        f"{ctx}\n"
        f"Answer EVERY question below for this job application. "
        f"Reply ONLY with a JSON object like: "
        f'{{\"Q1\":\"answer\",\"Q2\":\"answer\",...}}\n'
        f"Rules:\n"
        f"- For yes/no questions: reply YES or NO only\n"
        f"- For select/options: reply with the EXACT option text\n"
        f"- For text fields: one concise sentence, no bullet points\n"
        f"- Never make up facts not in the profile above\n\n"
        + "\n".join(q_lines)
    )

    payload = json.dumps({
        "model":   model,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0.1, "num_predict": 120 + 30 * len(questions)},
    }).encode()

    for attempt in range(1, 3):
        try:
            if attempt > 1:
                _time.sleep(1)
            req = urllib.request.Request(
                OLLAMA_URL, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = json.loads(r.read()).get("response", "").strip()
            # Extract the JSON object from the response
            m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
            if not m:
                continue
            parsed = json.loads(m.group())
            result = {}
            for item in questions:
                key = f"Q{questions.index(item)+1}"
                val = parsed.get(key, "").strip()
                if val:
                    result[item["id"]] = val
            if result:
                return result
        except Exception as _e:
            if attempt == 2:
                print(f"      → [Ollama batch] failed: {_e}")
    return {}


def _ollama_pick_option(field_label: str, options: list[str], desired: str) -> str:
    """
    Ask a local Ollama model which option from `options` best matches `desired`
    for the given `field_label`.  Falls back to simple fuzzy matching if Ollama
    is unavailable or returns an unexpected response.

    Examples:
      field_label = "Location (City)"
      options     = ["Austin, Texas, United States", "Austin, TX", "Austin, MN"]
      desired     = "Austin, Tx"
      → returns   "Austin, Texas, United States"

      field_label = "Will you require visa sponsorship?"
      options     = ["Yes", "No", "Maybe"]
      desired     = "Yes"
      → returns   "Yes"
    """
    def _fuzzy(opts, want):
        wl = want.lower()
        for o in opts:
            if wl == o.lower():
                return o
        for o in opts:
            if wl in o.lower() or o.lower() in wl:
                return o
        return opts[0] if opts else want

    if not options:
        return desired

    # Fast path: exact or obvious match (avoids Ollama round-trip)
    wl = desired.lower()
    for o in options:
        if wl == o.lower():
            return o

    try:
        model = __import__("os").environ.get("OLLAMA_MODEL", _OLLAMA_MODEL_LIVE)
        numbered = "\n".join(f"{i+1}. {o}" for i, o in enumerate(options))
        prompt = (
            f"You are filling a job application form field.\n"
            f"Field: \"{field_label}\"\n"
            f"The value to fill is: \"{desired}\"\n"
            f"Available options:\n{numbered}\n\n"
            f"Which option number best matches \"{desired}\"? "
            f"Reply with ONLY the number. No explanation, no punctuation."
        )
        payload = json.dumps({
            "model":   model,
            "prompt":  prompt,
            "stream":  False,
            "options": {"temperature": 0, "num_predict": 4},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            resp = json.loads(r.read())
        text = resp.get("response", "").strip()
        m = re.search(r"\d+", text)
        if m:
            idx = int(m.group()) - 1
            if 0 <= idx < len(options):
                return options[idx]
    except Exception:
        pass

    return _fuzzy(options, desired)


async def _get_field_label(target, el, tag: str) -> str:
    """
    Resolve the human-readable label for a form element.
    Check order:
      1. label[for=id]        — standard HTML
      2. aria-labelledby      — ARIA / React Select
      3. DOM walk up          — wrapping label/legend
      4. aria-label / placeholder / name  — last resort
    """
    id_ = await el.get_attribute("id") or ""
    if id_:
        try:
            lbl = target.locator(f"label[for='{id_}']").first
            if await lbl.count() > 0:
                txt = (await lbl.inner_text()).strip()
                if txt:
                    return txt
        except Exception:
            pass

    # aria-labelledby — React Select links its search input to the field label this way
    labelledby = await el.get_attribute("aria-labelledby") or ""
    if labelledby:
        for lid in labelledby.split():
            try:
                lbl_el = target.locator(f"[id='{lid}']").first
                if await lbl_el.count() > 0:
                    txt = (await lbl_el.inner_text()).strip()
                    if txt:
                        return txt
            except Exception:
                pass

    # Walk up the DOM tree looking for label / legend
    try:
        text = await el.evaluate(
            "el => {"
            "  let p = el.parentElement;"
            "  while (p && !['FORM','BODY'].includes(p.tagName)) {"
            "    const t = p.querySelector('label,legend,[class*=label]');"
            "    if (t) return t.innerText.trim();"
            "    p = p.parentElement;"
            "  }"
            "  return '';"
            "}"
        )
        if text:
            return text.strip()
    except Exception:
        pass

    return (
        await el.get_attribute("aria-label") or
        await el.get_attribute("placeholder") or
        await el.get_attribute("name") or ""
    ).strip()


async def _collect_form_answers(target, profile: dict, email: str,
                                job_title: str = "", company: str = "",
                                resume_path=None) -> dict:
    """
    Walk every visible input/select/textarea in target (Page or Frame).
    For each field:
      1. Profile answer  → use silently
      2. Saved answer    → confirm and use
      3. Unknown         → ask user in terminal (numbered menu for selects),
                           save answer for next time
    Returns {norm_label: answer}.
    """
    all_saved   = load_custom_answers()
    saved       = all_saved.get(email, {})
    new_answers: dict = {}
    result:      dict = {}

    INPUT_SELS = (
        "input[type='text'], input[type='email'], input[type='tel'], "
        "input[type='url'], input[type='number'], textarea, select"
    )

    try:
        _all_els = await target.locator(INPUT_SELS).all()
    except Exception:
        _all_els = []
    for el in _all_els:
        try:
            if not await el.is_visible(timeout=0):
                continue
            tag = (await el.evaluate("el => el.tagName")).lower()

            # Skip if already filled
            try:
                cur = await el.input_value()
                if cur and cur not in ("", "0", "-1", "none"):
                    continue
            except Exception:
                pass

            label = await _get_field_label(target, el, tag)
            if not label:
                continue
            norm = label.lower().strip(" *:")

            # 1. Profile answer
            val = answer_for(label, profile, email)
            if val:
                print(f"  [profile] '{label}' → {val!r}")
                result[norm] = val
                continue

            # 2. Saved answer
            if norm in saved:
                print(f"  [saved] '{label}' → {saved[norm]!r}")
                result[norm] = saved[norm]
                continue

            # 3. Auto-handle or ask user
            is_required = await el.evaluate("el => el.required")

            ans = ""
            if tag == "select":
                if not is_required:
                    continue  # skip optional selects silently
                # Required select — use Ollama to pick the best option
                try:
                    opts = await el.evaluate(
                        "el => Array.from(el.options)"
                        ".filter(o => o.value)"
                        ".map(o => o.text.trim())"
                    )
                    if opts:
                        # Ask Ollama to pick the most appropriate option given the profile
                        hint = _ollama_generate_answer(label, profile, job_title, company,
                                                       max_retries=2, retry_delay=2.0,
                                                       resume_path=resume_path)
                        hint = hint.split("\n")[0].strip()[:200] if hint else opts[0]
                        ans = _ollama_pick_option(label, opts, hint)
                        print(f"  [ollama] '{label}' (select) → {ans!r}")
                except Exception:
                    pass

            elif tag == "textarea":
                # Long-form question — Ollama generates full answer
                ans = _ollama_generate_answer(label, profile, job_title, company,
                                              resume_path=resume_path)
                if ans:
                    print(f"  [ollama] '{label}' → (generated {len(ans)} chars)")

            elif not is_required:
                # Optional text/url/number field — skip silently
                continue

            else:
                # Required short-text field — Ollama generates answer
                ans = _ollama_generate_answer(label, profile, job_title, company,
                                              resume_path=resume_path)
                if ans:
                    ans = ans.split("\n")[0].strip()[:300]
                    print(f"  [ollama] '{label}' → {ans!r}")

            if ans:
                result[norm] = ans
                # Cache select answers for reuse; regenerate Ollama text each time
                if tag == "select":
                    new_answers[norm] = ans

        except Exception:
            pass

    # ── Grouped country checkboxes (e.g. "which countries will you work in?") ──
    # These lists are often long and scrolled — find any group that contains mostly
    # country-named checkboxes, then scroll through and check United States only.
    try:
        _country_group_handled = await target.evaluate("""
            () => {
                // Find containers that hold 5+ country-named checkbox labels
                const COUNTRIES = new Set([
                    "australia","belgium","brazil","canada","china","france","germany",
                    "india","indonesia","ireland","israel","italy","japan","luxembourg",
                    "malaysia","mexico","new zealand","poland","portugal","romania",
                    "singapore","south korea","spain","sweden","switzerland",
                    "united kingdom","united states","netherlands","denmark","norway",
                    "finland","austria","hong kong","taiwan"
                ]);
                const containers = document.querySelectorAll(
                    'fieldset, [class*="question"], [class*="field"], [class*="group"], div'
                );
                for (const c of containers) {
                    const labels = Array.from(c.querySelectorAll('label')).map(l => l.innerText.trim().toLowerCase());
                    const countryLabels = labels.filter(l => COUNTRIES.has(l));
                    if (countryLabels.length >= 5) {
                        // This container looks like a country-checkbox group
                        // Check all checkboxes whose label is "United States"
                        const cbs = Array.from(c.querySelectorAll('input[type="checkbox"]'));
                        let checked = 0;
                        for (const cb of cbs) {
                            const lbl = c.querySelector('label[for="' + cb.id + '"]') ||
                                        cb.closest('label') ||
                                        cb.parentElement?.querySelector('label');
                            const lblText = (lbl ? lbl.innerText.trim().toLowerCase() : '');
                            if (lblText === 'united states' || lblText === 'united states of america' || lblText === 'us' || lblText === 'usa') {
                                if (!cb.checked) { cb.click(); }
                                checked++;
                            }
                        }
                        if (checked > 0) return true;
                    }
                }
                return false;
            }
        """)
        if _country_group_handled:
            print("  [auto] Country checkbox group detected — checked 'United States' only")
    except Exception:
        pass

    # ── Checkboxes ──────────────────────────────────────────────────────────────
    try:
        _cb_els = await target.locator("input[type='checkbox']").all()
    except Exception:
        _cb_els = []
    for el in _cb_els:
        try:
            if not await el.is_visible(timeout=0):
                continue
            if await el.is_checked():
                continue
            label = await _get_field_label(target, el, "checkbox")
            if not label:
                continue
            norm = label.lower().strip(" *:")
            if norm in result:
                continue
            # Auto-accept privacy / policy / consent / attestation checkboxes
            if any(w in norm for w in ("privacy", "policy", "consent", "agree", "terms", "attestation", "acknowledge")):
                result[norm] = "yes"
                continue
            if norm in saved:
                print(f"  [saved] '{label}' → {saved[norm]!r}  [checkbox]")
                result[norm] = saved[norm]
                continue
            # Auto-handle country checkboxes: check only US-related, skip all others
            _bare = re.sub(r"[\s\*:]+$", "", norm).strip()
            if _bare in _COUNTRY_CHECKBOX_NAMES:
                _is_us = _bare in ("united states", "united states of america", "us", "usa")
                val = "yes" if _is_us else "no"
                print(f"  [auto] '{label}' → {val!r}  [country checkbox]")
                result[norm] = val
                continue

            # Ollama fallback — ask model whether to check this checkbox
            is_req = await el.evaluate("el => el.required || el.getAttribute('aria-required') === 'true'")
            print(f"  [ollama] '{label}' [checkbox] — asking Ollama...")
            _cb_q = (
                f"Job application checkbox: '{label}'\n"
                f"Should the candidate check this box? "
                f"Answer with only YES or NO."
            )
            _cb_ans = _ollama_generate_answer(_cb_q, profile, job_title, company,
                                              max_retries=2, retry_delay=2.0)
            _cb_lower = _cb_ans.lower() if _cb_ans else ""
            if any(w in _cb_lower for w in ("yes", "check", "true", "agree", "accept", "correct", "applicable")):
                val = "yes"
            else:
                val = "no"
            print(f"      → [ollama] checkbox '{label}' → {val!r}")
            result[norm] = val
            if is_req:
                new_answers[norm] = val
        except Exception:
            pass

    # ── Radio groups ─────────────────────────────────────────────────────────────
    try:
        radio_names = await target.evaluate("""
            () => [...new Set(
                Array.from(document.querySelectorAll('input[type="radio"]'))
                    .filter(r => r.offsetParent !== null)
                    .map(r => r.name).filter(Boolean)
            )]
        """)
    except Exception:
        radio_names = []
    for rname in radio_names:
        try:
            already_selected = await target.evaluate(
                f"() => !!document.querySelector('input[type=\"radio\"][name=\"{rname}\"]:checked')"
            )
            if already_selected:
                continue
            group_label = await target.evaluate(f"""
                () => {{
                    const first = document.querySelector('input[type="radio"][name="{rname}"]');
                    if (!first) return '';
                    const fs = first.closest('fieldset');
                    if (fs) {{ const leg = fs.querySelector('legend'); if (leg) return leg.innerText.trim(); }}
                    const p = first.closest('.field,.form-group,[class*="question"],[class*="field"]');
                    if (p) {{ const lbl = p.querySelector('label,.label'); if (lbl) return lbl.innerText.trim(); }}
                    return first.name;
                }}
            """)
            if not group_label:
                continue
            norm = group_label.lower().strip(" *:")
            if norm in result:
                continue
            val = answer_for(group_label, profile, email)
            if val:
                result[norm] = val
                continue
            if norm in saved:
                print(f"  [saved] '{group_label}' → {saved[norm]!r}  [radio]")
                result[norm] = saved[norm]
                continue
            opts = await target.evaluate(f"""
                () => Array.from(document.querySelectorAll('input[type="radio"][name="{rname}"]'))
                    .map(r => {{
                        const lbl = document.querySelector('label[for="' + r.id + '"]');
                        return lbl ? lbl.innerText.trim() : r.value;
                    }})
            """)
            if not opts:
                continue
            req_str = " (required)" if await target.evaluate(
                f"() => !!document.querySelector('input[type=\"radio\"][name=\"{rname}\"]')?.required"
            ) else " (optional)"
            print(f"\n  ? '{group_label}'{req_str} [radio]")
            for i, opt in enumerate(opts, 1):
                print(f"    {i}. {opt}")
            try:
                raw = input(f"  Choose (1-{len(opts)}) or type: ").strip()
                ans = opts[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(opts) else raw
            except EOFError:
                ans = ""
            if ans:
                result[norm] = ans
                new_answers[norm] = ans
        except Exception:
            pass

    # Persist newly collected answers
    if new_answers:
        saved.update(new_answers)
        all_saved[email] = saved
        save_custom_answers(all_saved)
        print(f"\n  [saved] {len(new_answers)} new answer(s) stored for future use.\n")

    return result


async def _apply_collected_answers(target, answers: dict):
    """
    Fill form elements using the {norm_label: answer} dict.

    Text inputs  — click() to focus (triggers React onFocus), then fill().
    Selects      — Playwright select_option() first; if React doesn't sync its
                   internal state, fall back to the native prototype setter trick
                   which forces React to see the change.
    Small delay between each field lets React process one change before the next.
    """
    INPUT_SELS = (
        "input[type='text'], input[type='email'], input[type='tel'], "
        "input[type='url'], input[type='number'], textarea, select"
    )
    try:
        _all_els = await target.locator(INPUT_SELS).all()
    except Exception:
        _all_els = []
    for el in _all_els:
        try:
            if not await el.is_visible(timeout=0):
                continue
            tag = (await el.evaluate("el => el.tagName")).lower()

            label = await _get_field_label(target, el, tag)
            if not label:
                continue
            norm = label.lower().strip(" *:")
            # Skip city/location field — _fill_location_city handles it with char-by-char
            # typing so the autocomplete dropdown opens and Ollama can pick the right option
            if re.search(r"location.*city|city.*location|location\s*\(city\)|^location\s*city", norm):
                continue
            val  = answers.get(norm, "")
            if not val:
                continue

            if tag == "select":
                # ── Auto-accept selects (privacy/policy/consent/attestation/acknowledge) ──
                # These fields accept the first real option — same logic as checkboxes.
                _AUTO_ACCEPT_SEL = r"privacy|policy|consent|agree|terms|attestation|acknowledge"
                if val in ("yes", "y", "true", "1") and re.search(_AUTO_ACCEPT_SEL, norm):
                    _aa_opts = await el.evaluate(
                        "el => Array.from(el.options).map((o,i)=>({i,v:o.value,t:o.text.trim()}))"
                    )
                    _aa_real = [o for o in _aa_opts if o["v"] and o["t"] not in ("", "Select...", "-- Select --", "Please select")]
                    if _aa_real:
                        _aa_pick = _aa_real[0]
                        print(f"      → [{label[:40]}] auto-accept select → [{_aa_pick['i']}] {_aa_pick['t']!r}")
                        try:
                            await el.select_option(index=_aa_pick["i"])
                        except Exception:
                            try:
                                await el.select_option(value=_aa_pick["v"])
                            except Exception:
                                pass
                        await asyncio.sleep(0.4)
                        continue

                # ── React-safe select fill ────────────────────────────────────
                opts = await el.evaluate(
                    "el => Array.from(el.options)"
                    ".filter(o => o.value)"
                    ".map(o => ({v: o.value, t: o.text.trim().toLowerCase()}))"
                )
                val_l  = val.lower()
                chosen = next(
                    (o["v"] for o in opts if val_l == o["t"] or
                     (len(val_l) > 3 and val_l in o["t"]) or
                     (len(o["t"]) > 3 and o["t"] in val_l)),
                    None,
                )
                # Pick best option — exact match first, then substring, then Ollama
                real_opts = await el.evaluate(
                    "el => Array.from(el.options).filter(o=>o.value).map(o=>({v:o.value,t:o.text.trim()}))"
                )
                real_opt_texts = [o["t"] for o in real_opts]
                best_text = val
                best_val  = chosen or val
                if real_opt_texts:
                    val_l = val.lower()
                    # Layer 1: exact case-insensitive match
                    exact = next((o for o in real_opts if o["t"].lower() == val_l), None)
                    # Layer 2: val is a substring of an option (e.g. "not a protected veteran" in "I am not a protected veteran")
                    if not exact:
                        exact = next((o for o in real_opts if val_l in o["t"].lower()), None)
                    # Layer 3: option is a substring of val
                    if not exact:
                        exact = next((o for o in real_opts if o["t"].lower() in val_l), None)
                    if exact:
                        best_text = exact["t"]
                        best_val  = exact["v"]
                        print(f"      → [{label[:40]}] text match: {best_text!r}")
                    else:
                        # Layer 4: Ollama (last resort)
                        best_text = _ollama_pick_option(label, real_opt_texts, val)
                        best_val  = next((o["v"] for o in real_opts if o["t"] == best_text), chosen or val)
                        print(f"      → [{label[:40]}] Ollama picked select: {best_text!r}")

                applied = False
                try:
                    await el.select_option(value=best_val)
                    applied = True
                except Exception:
                    pass
                if not applied:
                    try:
                        await el.select_option(label=best_text)
                        applied = True
                    except Exception:
                        pass
                # React prototype setter — forces React internal state sync
                await el.evaluate("""
                    (el, tv) => {
                        const opts = Array.from(el.options);
                        const tvl = tv.toLowerCase();
                        const tgt = opts.find(o =>
                            o.value === tv ||
                            o.text.trim().toLowerCase() === tvl ||
                            o.text.trim().toLowerCase().includes(tvl) ||
                            tvl.includes(o.text.trim().toLowerCase())
                        );
                        if (!tgt) return;
                        try {
                            const setter = Object.getOwnPropertyDescriptor(
                                HTMLSelectElement.prototype, 'value'
                            ).set;
                            setter.call(el, tgt.value);
                        } catch(e) { el.value = tgt.value; }
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('input',  {bubbles: true}));
                    }
                """, best_val)
                await asyncio.sleep(0.5)

            else:
                # ── Text / textarea fill ──────────────────────────────────────
                # click() → React fires onFocus; fill() → triggers onChange.
                # IMPORTANT: If this is a React Select search input, typing will
                # open a dropdown of options.  We must click the matching option
                # before moving to the next field, otherwise the dropdown closes
                # without committing the selection.
                try:
                    await el.click()
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
                cur = ""
                try:
                    cur = await el.input_value()
                except Exception:
                    pass
                if not cur or cur == "0":
                    try:
                        await el.fill(val)
                        await asyncio.sleep(0.5)  # wait for dropdown to render

                        # ── React Select dropdown handler ─────────────────────
                        # After typing, check if a listbox/option dropdown appeared.
                        # Use Ollama to pick the best matching option so we handle
                        # cases like "Austin, Tx" → "Austin, Texas, United States".
                        opted = False
                        try:
                            all_opts = await target.get_by_role("option").all()
                            visible = []
                            for o in all_opts:
                                if await o.is_visible(timeout=0):
                                    visible.append(((await o.inner_text()).strip(), o))
                            if visible:
                                opt_texts = [t for t, _ in visible]
                                best = _ollama_pick_option(label, opt_texts, val)
                                print(f"      → [{label[:40]}] Ollama picked: {best!r}")
                                for txt, o in visible:
                                    if txt == best:
                                        await o.click()
                                        await asyncio.sleep(0.4)
                                        opted = True
                                        break
                                if not opted and visible:
                                    # Click the first option as last resort
                                    await visible[0][1].click()
                                    await asyncio.sleep(0.4)
                                    opted = True
                        except Exception:
                            pass

                        if not opted:
                            # No dropdown appeared — plain text field, dispatch events
                            try:
                                await el.evaluate("""
                                    el => {
                                        el.dispatchEvent(new Event('input',  {bubbles: true}));
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                    }
                                """)
                            except Exception:
                                pass
                    except Exception:
                        pass
                await asyncio.sleep(0.5)

        except Exception:
            pass

    # ── Apply checkboxes ────────────────────────────────────────────────────────
    try:
        _cb_els = await target.locator("input[type='checkbox']").all()
    except Exception:
        _cb_els = []
    for el in _cb_els:
        try:
            if not await el.is_visible(timeout=0):
                continue
            label = await _get_field_label(target, el, "checkbox")
            norm  = label.lower().strip(" *:") if label else ""
            should_check = any(w in norm for w in ("privacy", "policy", "consent", "agree", "terms", "attestation"))
            if not should_check and norm:
                val = answers.get(norm, "")
                should_check = val.lower() in ("yes", "true", "1", "y")
            if should_check and not await el.is_checked():
                await el.check()
                await asyncio.sleep(0.15)
                # React synthetic event may not have fired — verify and retry via click
                try:
                    if not await el.is_checked():
                        await el.click()
                        await asyncio.sleep(0.15)
                    # Last resort: click associated label to trigger React's onChange
                    if not await el.is_checked():
                        await target.evaluate("""
                            el => {
                                const lbl = el.id
                                    ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]')
                                    : el.closest('label') || el.parentElement?.querySelector('label');
                                if (lbl) { lbl.click(); return; }
                                el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                            }
                        """, el)
                except Exception:
                    pass
        except Exception:
            pass

    # ── Apply radio buttons ─────────────────────────────────────────────────────
    try:
        radio_names = await target.evaluate("""
            () => [...new Set(
                Array.from(document.querySelectorAll('input[type="radio"]'))
                    .filter(r => r.offsetParent !== null)
                    .map(r => r.name).filter(Boolean)
            )]
        """)
    except Exception:
        radio_names = []
    for rname in radio_names:
        try:
            if await target.evaluate(
                f"() => !!document.querySelector('input[type=\"radio\"][name=\"{rname}\"]:checked')"
            ):
                continue
            group_label = await target.evaluate(f"""
                () => {{
                    const first = document.querySelector('input[type="radio"][name="{rname}"]');
                    if (!first) return '';
                    const fs = first.closest('fieldset');
                    if (fs) {{ const leg = fs.querySelector('legend'); if (leg) return leg.innerText.trim(); }}
                    const p = first.closest('.field,.form-group,[class*="question"],[class*="field"]');
                    if (p) {{ const lbl = p.querySelector('label,.label'); if (lbl) return lbl.innerText.trim(); }}
                    return first.name;
                }}
            """)
            norm = group_label.lower().strip(" *:") if group_label else ""
            val  = answers.get(norm, "")
            if not val:
                continue

            # Collect radio option labels for this group
            opts = await target.evaluate(f"""
                () => Array.from(document.querySelectorAll('input[type="radio"][name="{rname}"]'))
                    .map(r => {{
                        const lbl = document.querySelector('label[for="' + r.id + '"]');
                        return {{ id: r.id, text: (lbl ? lbl.innerText : r.value).trim() }};
                    }})
            """)

            # Use Ollama to pick the best matching option from actual options on the page
            opt_texts = [o["text"] for o in opts if o["text"]]
            if not opt_texts:
                continue
            best = _ollama_pick_option(group_label, opt_texts, val)
            print(f"      → [{group_label[:40]}] Ollama picked radio: {best!r}")
            for o in opts:
                if o["text"] != best:
                    continue
                rid = o["id"]
                clicked = False
                # Method 1: Playwright label click (fires React synthetic events)
                try:
                    lbl_el = target.locator(f"label[for='{rid}']").first
                    if await lbl_el.count() > 0 and await lbl_el.is_visible(timeout=0):
                        await lbl_el.click()
                        await asyncio.sleep(0.3)
                        clicked = True
                except Exception:
                    pass
                # Method 2: Playwright click on the radio input itself
                if not clicked:
                    try:
                        radio_el = target.locator(f"input#'{rid}'").first
                        if await radio_el.count() == 0:
                            radio_el = target.locator(f"[id='{rid}']").first
                        if await radio_el.count() > 0:
                            await radio_el.click(force=True)
                            await asyncio.sleep(0.3)
                            clicked = True
                    except Exception:
                        pass
                # Method 3: JS click (dispatch both click + change events)
                if not clicked:
                    try:
                        await target.evaluate(f"""
                            () => {{
                                const r = document.getElementById('{rid}');
                                if (!r) return;
                                r.checked = true;
                                r.click();
                                r.dispatchEvent(new Event('change', {{bubbles:true}}));
                            }}
                        """)
                        await asyncio.sleep(0.3)
                        clicked = True
                    except Exception:
                        pass
                # Verify — if still not checked, try clicking the label via JS
                if clicked:
                    still_unchecked = await target.evaluate(
                        f"() => !document.querySelector('input[type=\"radio\"][name=\"{rname}\"]:checked')"
                    )
                    if still_unchecked:
                        try:
                            await target.evaluate(f"""
                                () => {{
                                    const lbl = document.querySelector('label[for="{rid}"]');
                                    if (lbl) lbl.click();
                                }}
                            """)
                            await asyncio.sleep(0.3)
                        except Exception:
                            pass
                break
        except Exception:
            pass

    # ── React Select auto-accept pass ────────────────────────────────────────────
    # Privacy Policy, Attestation, Acknowledge fields are React Select dropdowns
    # (div-based, NOT native <select>). They render role="combobox" containers
    # with role="option" items. select_option() never touches them.
    # Strategy: find every combobox still showing "Select..." placeholder whose
    # label matches auto-accept patterns → click control → click first option.
    _REACT_AUTO_PATS = re.compile(
        r"privacy|policy|consent|agree|terms|attestation|acknowledge|candidate ai", re.I
    )
    try:
        # JS: collect all visible "Select..." placeholders and their ancestor labels
        react_fields = await target.evaluate("""
            () => {
                const results = [];
                // Find all placeholder divs showing "Select..."
                document.querySelectorAll('[class*="placeholder"]').forEach(ph => {
                    if (!ph.offsetParent) return;
                    if (!/^select/i.test(ph.textContent?.trim() || '')) return;
                    // Walk up max 8 levels to find the control div
                    let ctrl = ph.parentElement;
                    for (let i = 0; i < 8 && ctrl; i++) {
                        if (ctrl.getAttribute('role') === 'combobox' ||
                            (ctrl.className && /select.*control|selectControl/i.test(ctrl.className))) {
                            break;
                        }
                        ctrl = ctrl.parentElement;
                    }
                    if (!ctrl) return;
                    // Walk further up to find the label
                    let label = '';
                    let p = ctrl.parentElement;
                    for (let i = 0; i < 6 && p; i++) {
                        const lbl = p.querySelector('label, legend, [class*="label"]');
                        if (lbl) { label = lbl.innerText.trim(); break; }
                        p = p.parentElement;
                    }
                    if (label) results.push(label);
                });
                return results;
            }
        """)
        for field_label in react_fields:
            if not _REACT_AUTO_PATS.search(field_label):
                continue
            print(f"      → [{field_label[:40]}] React Select placeholder found — auto-accepting")
            try:
                # Find and click the control by label
                # get_by_label resolves the hidden React Select input — clicking its
                # parent control div opens the dropdown
                lbl_el = target.get_by_label(re.compile(re.escape(field_label[:30]), re.I)).first
                if await lbl_el.count() == 0:
                    continue
                # Click the parent control (not the hidden input itself)
                await lbl_el.evaluate("""el => {
                    let p = el.parentElement;
                    for (let i = 0; i < 6 && p; i++) {
                        if (p.getAttribute('role') === 'combobox' ||
                            (p.className && /control/i.test(p.className))) {
                            p.click(); return;
                        }
                        p = p.parentElement;
                    }
                    el.click();  // fallback: click the input itself
                }""")
                await asyncio.sleep(0.8)
                # Click first visible role=option
                opt = target.get_by_role("option").first
                if await opt.count() > 0 and await opt.is_visible(timeout=2000):
                    opt_text = (await opt.inner_text()).strip()
                    await opt.click()
                    await asyncio.sleep(0.4)
                    print(f"      → [{field_label[:40]}] selected: {opt_text!r}")
                else:
                    # No role=option — try ArrowDown+Enter on the input
                    await lbl_el.press("ArrowDown")
                    await asyncio.sleep(0.3)
                    await lbl_el.press("Enter")
                    await asyncio.sleep(0.3)
                    print(f"      → [{field_label[:40]}] ArrowDown+Enter fallback")
            except Exception as e:
                print(f"      → [{field_label[:40]}] React Select error: {e}")
    except Exception as e:
        print(f"      → React Select auto-accept pass error: {e}")

    # ── Hardcoded rescue: veteran + acknowledge selects ──────────────────────────
    # These fields need exact option text or first-real-option logic.
    # Architecture: static rules → exact match → index fallback → React setter.
    _HARDCODED_SELECT_RULES = {
        # label pattern : (target text, fallback_index)
        # None as target text = pick first real option automatically
        r"veteran":                          ("I am not a protected veteran", 1),
        r"acknowledge":                      (None, 1),
        r"disabilit":                        ("I don't wish to answer", 1),
        r"privacy|policy|consent|agree|terms|attestation": (None, 1),
    }
    try:
        all_sel_els = await target.locator("select").all()
        for el in all_sel_els:
            try:
                if not await el.is_visible(timeout=0):
                    continue
                label = await _get_field_label(target, el, "select")
                lnorm = label.lower()
                rule = next(((pat, txt, idx) for pat, (txt, idx) in _HARDCODED_SELECT_RULES.items()
                             if re.search(pat, lnorm)), None)
                if not rule:
                    continue
                pat, target_text, fallback_idx = rule
                opts = await el.evaluate(
                    "el => Array.from(el.options).map((o,i)=>({i,v:o.value,t:o.text.trim()}))"
                )
                real = [o for o in opts if o["v"]]
                if not real:
                    continue
                print(f"      → [{label[:40]}] hardcoded rule ({pat}), options:")
                for o in real:
                    print(f"           [{o['i']}] {o['t']}")

                # Already has a real value? Skip.
                cur = await el.evaluate("el => el.options[el.selectedIndex]?.text?.trim() || ''")
                if cur and cur not in ("", "Select...", "-- Select --", "Please select", "Select"):
                    print(f"      → [{label[:40]}] already set: {cur!r}")
                    continue

                # Pick: exact text match → fallback index
                chosen = None
                if target_text:
                    tl = target_text.lower()
                    chosen = next((o for o in real if tl in o["t"].lower() or o["t"].lower() in tl), None)
                if not chosen:
                    chosen = real[fallback_idx] if fallback_idx < len(real) else real[0]

                print(f"      → [{label[:40]}] selecting [{chosen['i']}]: {chosen['t']!r}")
                applied = False
                try:
                    await el.select_option(index=chosen["i"])
                    applied = True
                except Exception:
                    pass
                if not applied:
                    try:
                        await el.select_option(value=chosen["v"])
                        applied = True
                    except Exception:
                        pass
                if not applied:
                    # React native prototype setter
                    await el.evaluate("""
                        (el, v) => {
                            try {
                                Object.getOwnPropertyDescriptor(
                                    HTMLSelectElement.prototype,'value'
                                ).set.call(el, v);
                            } catch(e) { el.value = v; }
                            el.dispatchEvent(new Event('change',{bubbles:true}));
                            el.dispatchEvent(new Event('input', {bubbles:true}));
                        }
                    """, chosen["v"])
            except Exception:
                pass
    except Exception:
        pass

    # ── Hardcoded rescue: veteran + acknowledge radio groups ──────────────────────
    _HARDCODED_RADIO_RULES = {
        r"veteran":     "I am not a protected veteran",
        r"acknowledge": None,   # None = first option
        r"disabilit":   "I don't wish to answer",
    }
    try:
        all_rnames_rescue = await target.evaluate("""
            () => [...new Set(
                Array.from(document.querySelectorAll('input[type="radio"]'))
                    .filter(r => r.offsetParent !== null)
                    .map(r => r.name).filter(Boolean)
            )]
        """)
        for rname in all_rnames_rescue:
            try:
                is_checked = await target.evaluate(
                    f"() => !!document.querySelector('input[type=\"radio\"][name=\"{rname}\"]:checked')"
                )
                if is_checked:
                    continue
                group_label = await target.evaluate(f"""
                    () => {{
                        const first = document.querySelector('input[type="radio"][name="{rname}"]');
                        if (!first) return '';
                        const fs = first.closest('fieldset');
                        if (fs) {{ const leg = fs.querySelector('legend'); if (leg) return leg.innerText.trim(); }}
                        const p = first.closest('.field,.form-group,[class*="question"],[class*="field"]');
                        if (p) {{ const lbl = p.querySelector('label,.label'); if (lbl) return lbl.innerText.trim(); }}
                        return first.name;
                    }}
                """)
                lnorm = group_label.lower()
                rule_text = next((txt for pat, txt in _HARDCODED_RADIO_RULES.items()
                                  if re.search(pat, lnorm)), "SKIP")
                if rule_text == "SKIP":
                    continue
                opts = await target.evaluate(f"""
                    () => Array.from(document.querySelectorAll('input[type="radio"][name="{rname}"]'))
                        .map(r => {{
                            const lbl = document.querySelector('label[for="' + r.id + '"]');
                            return {{id: r.id, text: (lbl ? lbl.innerText : r.value).trim()}};
                        }})
                """)
                real_opts = [o for o in opts if o["text"]]
                if not real_opts:
                    continue
                print(f"      → [{group_label[:40]}] hardcoded radio rescue, options:")
                for o in real_opts:
                    print(f"           {o['text']}")
                # Pick by text or first
                chosen_opt = None
                if rule_text:
                    rl = rule_text.lower()
                    chosen_opt = next((o for o in real_opts if rl in o["text"].lower()
                                       or o["text"].lower() in rl), None)
                if not chosen_opt:
                    chosen_opt = real_opts[0]
                print(f"      → [{group_label[:40]}] clicking: {chosen_opt['text']!r}")
                rid = chosen_opt["id"]
                try:
                    lbl_el = target.locator(f"label[for='{rid}']").first
                    if await lbl_el.count() > 0 and await lbl_el.is_visible(timeout=0):
                        await lbl_el.click()
                        await asyncio.sleep(0.3)
                    else:
                        raise Exception("label not found")
                except Exception:
                    await target.evaluate(f"""
                        () => {{
                            const r = document.getElementById('{rid}');
                            if (!r) return;
                            r.checked = true;
                            r.click();
                            r.dispatchEvent(new Event('change', {{bubbles:true}}));
                            const lbl = document.querySelector('label[for="{rid}"]');
                            if (lbl) lbl.click();
                        }}
                    """)
                    await asyncio.sleep(0.3)
            except Exception:
                pass
    except Exception:
        pass

    # ── General rescue pass: any <select> still at default — show options, pick by index ──
    try:
        sel_els = await target.locator("select").all()
        for el in sel_els:
            try:
                if not await el.is_visible(timeout=0):
                    continue
                cur = await el.evaluate("el => el.options[el.selectedIndex]?.text?.trim() || ''")
                if cur and cur not in ("", "Select...", "-- Select --", "Please select", "Select"):
                    continue
                label = await _get_field_label(target, el, "select")
                opts = await el.evaluate(
                    "el => Array.from(el.options).map((o,i) => ({i, v:o.value, t:o.text.trim()}))"
                )
                real = [o for o in opts if o["v"] and o["t"] not in ("", "Select...", "-- Select --")]
                if not real:
                    continue
                print(f"      → [{label[:40]}] unfilled select, options:")
                for o in real:
                    print(f"           [{o['i']}] {o['t']}")
                desired = answers.get(label.lower().strip(" *:"), "") or ""
                # Pick by text match first, then Ollama, then index 0
                chosen = None
                if desired:
                    dl = desired.lower()
                    chosen = next((o for o in real if dl in o["t"].lower() or o["t"].lower() in dl), None)
                if not chosen:
                    best_text = _ollama_pick_option(label, [o["t"] for o in real], desired or label)
                    chosen = next((o for o in real if o["t"] == best_text), real[0])
                print(f"      → [{label[:40]}] selecting index {chosen['i']}: {chosen['t']!r}")
                try:
                    await el.select_option(index=chosen["i"])
                except Exception:
                    try:
                        await el.select_option(value=chosen["v"])
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

    # ── Ollama rescue pass: find any radio group where nothing is checked ────────
    try:
        all_rnames = await target.evaluate("""
            () => [...new Set(
                Array.from(document.querySelectorAll('input[type="radio"]'))
                    .filter(r => r.offsetParent !== null)
                    .map(r => r.name).filter(Boolean)
            )]
        """)
        for rname in all_rnames:
            try:
                is_checked = await target.evaluate(
                    f"() => !!document.querySelector('input[type=\"radio\"][name=\"{rname}\"]:checked')"
                )
                if is_checked:
                    continue
                group_label = await target.evaluate(f"""
                    () => {{
                        const first = document.querySelector('input[type="radio"][name="{rname}"]');
                        if (!first) return '';
                        const fs = first.closest('fieldset');
                        if (fs) {{ const leg = fs.querySelector('legend'); if (leg) return leg.innerText.trim(); }}
                        const p = first.closest('.field,.form-group,[class*="question"],[class*="field"]');
                        if (p) {{ const lbl = p.querySelector('label,.label'); if (lbl) return lbl.innerText.trim(); }}
                        return first.name;
                    }}
                """)
                opts = await target.evaluate(f"""
                    () => Array.from(document.querySelectorAll('input[type="radio"][name="{rname}"]'))
                        .map(r => {{
                            const lbl = document.querySelector('label[for="' + r.id + '"]');
                            return {{ id: r.id, text: (lbl ? lbl.innerText : r.value).trim() }};
                        }})
                """)
                opt_texts = [o["text"] for o in opts if o["text"]]
                if not opt_texts:
                    continue
                desired = answers.get(group_label.lower().strip(" *:"), "") or group_label
                best = _ollama_pick_option(group_label, opt_texts, desired)
                print(f"      → [{group_label[:40]}] Ollama rescue radio: {best!r}")
                for o in opts:
                    if o["text"] != best:
                        continue
                    rid = o["id"]
                    # Try label click first, then force click radio, then JS
                    done = False
                    try:
                        lbl_el = target.locator(f"label[for='{rid}']").first
                        if await lbl_el.count() > 0 and await lbl_el.is_visible(timeout=0):
                            await lbl_el.click()
                            await asyncio.sleep(0.3)
                            done = True
                    except Exception:
                        pass
                    if not done:
                        try:
                            await target.evaluate(f"""
                                () => {{
                                    const r = document.getElementById('{rid}');
                                    if (!r) return;
                                    r.checked = true;
                                    r.click();
                                    r.dispatchEvent(new Event('change', {{bubbles:true}}));
                                    const lbl = document.querySelector('label[for="{rid}"]');
                                    if (lbl) lbl.click();
                                }}
                            """)
                            await asyncio.sleep(0.3)
                        except Exception:
                            pass
                    break
            except Exception:
                pass
    except Exception:
        pass


