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

_HERE            = Path(__file__).parent
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


def get_resume(profile: dict, email: str, job_title: str = "",
               company: str = "") -> Optional[Path]:
    """
    Pick the best-matching resume for the job title.
    Scoring uses file content (2×) + filename (1×) against job title keywords.
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

        best = best_path if best_score > 0 else (default_path or all_resumes[0])
        return best
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


# ── Greenhouse form filler ──────────────────────────────────────────────────────

async def _fill_text_inputs(page: Page, profile: dict, email: str):
    """Fill all visible unfilled text/email/tel/textarea inputs by their label."""
    for inp in await page.locator(
        "input[type='text'], input[type='email'], input[type='tel'], textarea"
    ).all():
        try:
            if not await inp.is_visible(timeout=0):
                continue
            if await inp.input_value():
                continue
            inp_id = await inp.get_attribute("id") or ""
            label  = ""
            if inp_id:
                try:
                    label = (await page.locator(f"label[for='{inp_id}']").first.inner_text()).strip()
                except Exception:
                    pass
            if not label:
                label = (
                    await inp.get_attribute("placeholder") or
                    await inp.get_attribute("aria-label") or
                    await inp.get_attribute("name") or ""
                )
            val = answer_for(label, profile, email)
            if val:
                await inp.fill(val)
        except Exception:
            pass


async def _fill_selects(page: Page, profile: dict, email: str):
    """Fill all visible unfilled <select> elements by their label."""
    for sel_el in await page.locator("select").all():
        try:
            if not await sel_el.is_visible(timeout=0):
                continue
            cur = await sel_el.input_value()
            if cur and cur not in ("", "0", "-1", "none", "select"):
                continue
            sel_id = await sel_el.get_attribute("id") or ""
            label  = ""
            if sel_id:
                try:
                    label = (await page.locator(f"label[for='{sel_id}']").first.inner_text()).strip()
                except Exception:
                    pass
            if not label:
                label = await sel_el.get_attribute("aria-label") or ""

            val  = answer_for(label, profile, email)
            opts = await sel_el.evaluate(
                "el => Array.from(el.options)"
                ".filter(o => o.value && !['', '0', '-1'].includes(o.value))"
                ".map(o => ({value: o.value, text: o.text.trim().toLowerCase()}))"
            )
            if not opts:
                continue

            val_lower = val.lower() if val else ""
            chosen    = None
            if val_lower:
                chosen = next(
                    (o["value"] for o in opts
                     if val_lower in o["text"] or o["text"] in val_lower),
                    None,
                )
            if not chosen:
                chosen = next((o["value"] for o in opts if "prefer not" in o["text"]), None)
            if not chosen:
                chosen = opts[0]["value"]
            await sel_el.select_option(value=chosen)
        except Exception:
            pass


async def _upload_resume(page: Page, resume: Optional[Path], label: str = ""):
    """Find the resume file input and upload. Works for hidden inputs too."""
    if not resume or not resume.exists():
        return False
    for sel in [
        "input#resume",
        "input[name='resume']",
        "input[type='file'][accept*='pdf']",
        "input[type='file'][accept*='doc']",
        "input[type='file'][name*='resume' i]",
        "input[type='file'][id*='resume' i]",
        "input[type='file']",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.set_input_files(str(resume))
                print(f"      → Resume: {resume.name}{' [' + label + ']' if label else ''}")
                await asyncio.sleep(2)
                return True
        except Exception:
            pass
    return False


_GH_SUCCESS_PHRASES = [
    "thank you", "application received", "successfully submitted",
    "application complete", "we've received", "we have received your",
    "application submitted", "your application has been",
]


def _extract_code_from_text(text: str) -> str | None:
    """Extract a verification code from email subject+snippet text."""
    lower = text.lower()
    if not any(k in lower for k in [
        "verification", "verify", "confirm", "security code",
        "one-time", "passcode", "your code", "enter the code", "access code",
    ]):
        return None
    # Greenhouse: "application: NakeSwk3" (mixed-case 8-char)
    m = re.search(r'application[:\s]+([A-Za-z0-9]{8})\b', text, re.IGNORECASE)
    if m:
        return m.group(1)
    # Generic "code: XXXXXXXX" (mixed or uppercase)
    m = re.search(r'\bcode[:\s]+([A-Za-z0-9]{8})\b', text, re.IGNORECASE)
    if m:
        return m.group(1)
    # Uppercase-only 8-char fallback
    m = re.search(r'\b([A-Z][A-Z0-9]{7}|[A-Z0-9]{7}[A-Z])\b', text)
    if m:
        return m.group(1)
    # 6-digit numeric (some ATS)
    codes = re.findall(r'\b(\d{6})\b', text)
    if codes:
        return codes[0]
    return None


async def _gmail_history_watcher(email: str, svc, queue: asyncio.Queue,
                                  stop_event: asyncio.Event):
    """Background coroutine: polls Gmail history API every 1s for verification codes."""
    history_id: str | None = None
    try:
        info = await asyncio.to_thread(
            lambda: svc.users().getProfile(userId="me").execute()
        )
        history_id = str(info["historyId"])
        print(f"  [Code Watcher] Live for {email} (historyId={history_id})", flush=True)
    except Exception as e:
        print(f"  [Code Watcher] Could not get historyId: {e}", flush=True)
        return

    while not stop_event.is_set():
        await asyncio.sleep(1)
        try:
            history = await asyncio.to_thread(
                lambda hid=history_id: svc.users().history().list(
                    userId="me",
                    startHistoryId=hid,
                    historyTypes=["messageAdded"],
                    maxResults=20,
                ).execute()
            )
            history_id = history.get("historyId", history_id)

            for record in history.get("history", []):
                for msg_ref in record.get("messagesAdded", []):
                    msg_id = msg_ref["message"]["id"]
                    try:
                        meta = await asyncio.to_thread(
                            lambda mid=msg_id: svc.users().messages().get(
                                userId="me", id=mid, format="metadata",
                                metadataHeaders=["Subject", "From"],
                            ).execute()
                        )
                        snippet = meta.get("snippet", "")
                        headers = {
                            h["name"]: h["value"]
                            for h in meta["payload"]["headers"]
                        }
                        subject = headers.get("Subject", "")
                        code = _extract_code_from_text(f"{subject} {snippet}")
                        if code:
                            print(f"  [Code Watcher] Code detected: {code[:4]}****", flush=True)
                            await queue.put(code)
                    except Exception:
                        pass

        except Exception as exc:
            err = str(exc)
            if "Invalid historyId" in err or "404" in err:
                print("  [Code Watcher] historyId expired — stopping.", flush=True)
                break
            if "rateLimitExceeded" in err or "429" in err:
                await asyncio.sleep(15)


def _start_code_watcher(email: str, svc) -> asyncio.Queue:
    """
    Create (or return existing) code queue for email and ensure watcher task is running.
    Must be called from within an async context (asyncio.create_task needs a running loop).
    """
    existing_stop = _watcher_stops.get(email)
    if email not in _code_queues or (existing_stop and existing_stop.is_set()):
        q    = asyncio.Queue()
        stop = asyncio.Event()
        _code_queues[email]   = q
        _watcher_stops[email] = stop
        asyncio.create_task(_gmail_history_watcher(email, svc, q, stop))
    return _code_queues[email]


async def _handle_verification_code(target, outer_page, email: str = "") -> bool:
    """
    Detect and fill an email verification code screen (Greenhouse or Stripe).
    Returns "entered" if code was filled, "no_code" if unavailable, False if no screen.
    """
    # Check both the iframe (target) and the outer page
    verification_target = None
    for ctx in [outer_page, target]:
        if ctx is None:
            continue
        try:
            txt = await ctx.inner_text("body")
            tl = txt.lower()
            if "verification code" in tl or "security code" in tl or "8-character" in tl:
                verification_target = ctx
                break
        except Exception:
            continue

    if verification_target is None:
        return False

    print("      → [Verification] Code screen detected — waiting for code...", flush=True)

    # Priority 1: live 1-second history watcher (already polling since session start)
    code = None
    if email and email in _code_queues:
        print("      → [Verification] Using live Gmail watcher (up to 90s)...", flush=True)
        try:
            code = await asyncio.wait_for(_code_queues[email].get(), timeout=90.0)
            print(f"      → [Verification] Code received via live watcher: {code}", flush=True)
        except asyncio.TimeoutError:
            print("      → [Verification] Live watcher timed out.", flush=True)

    # Priority 2: direct Gmail API poll as fallback
    if not code and _gmail_sender:
        print("      → [Verification] Falling back to direct Gmail poll...", flush=True)
        for attempt in range(6):
            try:
                code = _gmail_sender.fetch_verification_code(
                    max_age_seconds=180, to_email=email
                )
            except Exception as _exc:
                _msg = str(_exc)
                if "rateLimitExceeded" in _msg or "403" in _msg:
                    print(f"      → [Verification] Gmail rate limit — backing off 15s...", flush=True)
                    await asyncio.sleep(15)
                    continue
            if code:
                break
            print(f"      → [Verification] Waiting for code (attempt {attempt+1}/6)...", flush=True)
            await asyncio.sleep(8)

    if not code:
        print("      → [Verification] Could not retrieve code from Gmail — skipping.", flush=True)
        return "no_code"

    print(f"      → [Verification] Code retrieved: {code}", flush=True)

    # Fill individual single-char boxes (one box per character)
    entered = False
    try:
        boxes = await verification_target.locator("input[maxlength='1'], input[maxLength='1']").all()
        if len(boxes) >= len(code):
            for i, ch in enumerate(code):
                await boxes[i].click()
                await boxes[i].fill(ch)
            entered = True
    except Exception:
        pass

    # Fallback: single input field
    if not entered:
        try:
            inp = verification_target.locator(
                "input[type='text'], input[type='number'], input:not([type])"
            ).first
            if await inp.is_visible(timeout=2000):
                await inp.click()
                await inp.fill(code)
                entered = True
        except Exception:
            pass

    if not entered:
        print("      → [Verification] Could not find code input field.", flush=True)
        return False

    await asyncio.sleep(0.5)

    # Click submit on the verification page
    for btn_name in ["Submit application", "Submit Application", "Submit"]:
        try:
            btn = verification_target.get_by_role("button", name=re.compile(btn_name, re.I)).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                print(f"      → [Verification] Submitted with code {code}.", flush=True)
                await asyncio.sleep(6)
                return "entered"
        except Exception:
            pass

    return "entered"  # code entered even if submit-click failed


async def _gh_submit(target, outer_page: Page = None, email: str = "") -> str:
    """
    Click the final submit button on a Greenhouse form.
    target: the Page or Frame containing the form.
    outer_page: the outer Page (for captcha detection); defaults to target.
    Only matches form-submit button text — NOT 'Apply Now' / 'Apply'.
    """
    captcha_page = outer_page if outer_page is not None else target
    if await has_captcha(captcha_page):
        await pause_for_captcha(captcha_page)

    async def _check_confirmed(t, outer) -> str | None:
        """Return 'applied' if success is detected, 'error:...' for validation errors, None otherwise."""
        # Check page body for success phrases
        try:
            body = (await t.inner_text("body")).lower()
            if any(w in body for w in _GH_SUCCESS_PHRASES):
                return "applied"
        except Exception:
            pass
        # Check outer page too (iframe may redirect outer)
        if outer and outer is not t:
            try:
                outer_body = (await outer.inner_text("body")).lower()
                if any(w in outer_body for w in _GH_SUCCESS_PHRASES):
                    return "applied"
            except Exception:
                pass
        # Check for visible validation errors
        try:
            err_els = await t.locator(
                ".error-message, .field_with_errors, [class*='error'], [class*='invalid'], "
                ".inline-error, [aria-invalid='true']"
            ).all()
            for e in err_els:
                if await e.is_visible(timeout=0):
                    txt = (await e.inner_text()).strip()[:120]
                    if txt:
                        return f"error: form validation — {txt}"
        except Exception:
            pass
        return None

    for btn_text in ["Submit Application", "Submit My Application", "Submit"]:
        try:
            btn = target.get_by_role("button", name=re.compile(f"^{btn_text}$", re.I)).first
            if not await btn.is_visible(timeout=0):
                continue
            pre_url = ""
            try:
                pre_url = target.url
            except Exception:
                pass
            await btn.click()
            await asyncio.sleep(SUBMIT_WAIT)
            # Handle email verification code screen if it appears
            _verify_result = await _handle_verification_code(target, captcha_page, email=email)
            if _verify_result == "no_code":
                return "error: email verification required — code not retrieved"
            # URL change in iframe = navigation to confirmation page
            try:
                post_url = target.url
                if pre_url and post_url != pre_url:
                    return "applied"
            except Exception:
                pass
            result = await _check_confirmed(target, captcha_page)
            if result:
                return result
            return "submitted (unconfirmed)"
        except Exception:
            pass

    # Fallback: input[type=submit] (classic board)
    try:
        sub = target.locator("input[type='submit']").first
        if await sub.is_visible(timeout=0):
            pre_url = ""
            try:
                pre_url = target.url
            except Exception:
                pass
            await sub.click()
            await asyncio.sleep(SUBMIT_WAIT)
            # Handle email verification code screen if it appears
            _verify_result = await _handle_verification_code(target, captcha_page, email=email)
            if _verify_result == "no_code":
                return "error: email verification required — code not retrieved"
            try:
                if pre_url and target.url != pre_url:
                    return "applied"
            except Exception:
                pass
            result = await _check_confirmed(target, captcha_page)
            if result:
                return result
            return "submitted (unconfirmed)"
    except Exception:
        pass

    # ── Broad CSS fallback: scroll page then try button[type=submit] / data attrs ──
    try:
        await target.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)
    except Exception:
        pass
    for sel in (
        "button[type='submit']",
        "[data-testid*='submit' i]",
        "[data-qa*='submit' i]",
    ):
        try:
            sub = target.locator(sel).last
            if await sub.count() > 0 and await sub.is_visible(timeout=0):
                pre_url = ""
                try:
                    pre_url = target.url
                except Exception:
                    pass
                await sub.click()
                await asyncio.sleep(SUBMIT_WAIT)
                _verify_result = await _handle_verification_code(target, captcha_page, email=email)
                if _verify_result == "no_code":
                    return "error: email verification required — code not retrieved"
                try:
                    if pre_url and target.url != pre_url:
                        return "applied"
                except Exception:
                    pass
                result = await _check_confirmed(target, captcha_page)
                if result:
                    return result
                return "submitted (unconfirmed)"
        except Exception:
            pass

    return "error: submit button not found"


async def _get_gh_frame(page: Page):
    """Return the embedded Greenhouse iframe as a Frame, or None."""
    for frame in page.frames:
        if "job-boards.greenhouse.io" in frame.url and "embed" in frame.url:
            return frame
    # Classic Airbnb-style embedded iframe
    try:
        iframe_el = page.locator("iframe#grnhse_iframe")
        if await iframe_el.count() > 0:
            return await iframe_el.content_frame()
    except Exception:
        pass
    # Stripe-style: iframe[class*="careers-apply"] or title="Greenhouse application..."
    try:
        iframe_el = page.locator(
            "iframe[class*='careers-apply'], iframe[title*='Greenhouse']"
        ).first
        if await iframe_el.count() > 0:
            return await iframe_el.content_frame()
    except Exception:
        pass
    return None


async def _grnhse_iframe_expanded(page: Page) -> bool:
    """Return True if the grnhse_iframe exists AND is visually expanded (height > 50px)."""
    try:
        h = await page.evaluate(
            "document.getElementById('grnhse_iframe')?.offsetHeight || 0"
        )
        return int(h) > 50
    except Exception:
        return False


async def _gh_form_present(page: Page) -> bool:
    """Return True if a Greenhouse application form is ready to fill.
    For embedded iframes (Airbnb): only True after the iframe is expanded by clicking Apply.
    """
    # Standard page-level selectors (classic + new direct board)
    form_sels = [
        "input#first_name", "input[name='first_name']",
        "form#application_form", "#application_form",
        "input#email[autocomplete='email']",
        "[data-testid*='apply-form']",
    ]
    for sel in form_sels:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible(timeout=0):
                return True
        except Exception:
            pass
    # Airbnb-style grnhse_iframe: only counts when expanded
    if await _grnhse_iframe_expanded(page):
        return True
    # Stripe-style: careers-apply-page__iframe is visible on load (no click needed)
    try:
        el = page.locator(
            "iframe[class*='careers-apply'], iframe[title*='Greenhouse']"
        ).first
        if await el.count() > 0 and await el.is_visible(timeout=0):
            return True
    except Exception:
        pass
    return False


async def _find_apply_button(page: Page):
    """
    Find the Apply button by visible text content (not aria-label).
    Returns (locator, matched_text) or (None, None).
    Tries text-based locator first, then get_by_role as fallback.
    """
    _APPLY_RE = re.compile(
        r"^(Apply for this Job|Apply for this Position|Apply Now|Apply)$",
        re.IGNORECASE,
    )
    # Primary: match by visible text — works even when aria-label differs
    for selector in ("button", "a", "[role='button']"):
        try:
            els = page.locator(selector).filter(has_text=_APPLY_RE)
            if await els.count() > 0:
                el = els.first
                text = (await el.text_content() or "").strip()
                return el, text
        except Exception:
            pass

    # Fallback: get_by_role (uses accessible name / aria-label)
    for label in ("Apply for this Job", "Apply for this Position", "Apply Now", "Apply"):
        for role in ("button", "link"):
            try:
                el = page.get_by_role(role, name=re.compile(f"^{re.escape(label)}$", re.I)).first
                if await el.count() > 0:
                    return el, label
            except Exception:
                pass
    return None, None


async def _scroll_and_click_apply(page: Page) -> Page:
    """
    Scroll through the full job-description page to find the Apply button,
    retry clicking it every 0.3 s (up to 10 attempts), then return whichever
    page now has the application form (same page or new tab).
    """
    initial_count = len(page.context.pages)

    # ── Pass 1: scroll to find the button ────────────────────────────────────
    found_el = None
    found_label = None
    for pct in [0, 0.25, 0.5, 0.75, 1.0]:
        try:
            await page.evaluate(
                f"window.scrollTo(0, document.body.scrollHeight * {pct})"
            )
        except Exception:
            pass
        await asyncio.sleep(0.4)

        el, label = await _find_apply_button(page)
        if el:
            try:
                await el.scroll_into_view_if_needed()
            except Exception:
                pass
            await asyncio.sleep(0.2)
            found_el, found_label = el, label
            print(f"      → Found '{label}' at scroll {int(pct*100)}%")
            break

    if found_el is None:
        print("      → Apply button not found after full scroll")
        return page

    # ── Pass 2: hammer click every 0.3 s until navigation/form appears ───────
    for attempt in range(1, 11):
        try:
            await found_el.scroll_into_view_if_needed()
            await found_el.click(timeout=0)
            print(f"      → Clicked '{found_label}' (attempt {attempt})")
        except Exception as exc:
            print(f"      → Click attempt {attempt} failed: {exc}")
            await asyncio.sleep(0.3)
            continue

        await asyncio.sleep(0.5)

        # New tab opened?
        if len(page.context.pages) > initial_count:
            new_page = page.context.pages[-1]
            try:
                await new_page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(1.5)
            print("      → Application opened in new tab")
            return new_page

        # Same-page navigation?
        try:
            current_url = await page.evaluate("location.href")
            if current_url != page.url:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await asyncio.sleep(1)
                return page
        except Exception:
            pass

        # Embedded iframe expanded (Airbnb "Apply Now" → tab switch pattern)?
        if await _grnhse_iframe_expanded(page):
            print("      → grnhse_iframe expanded — form ready")
            await asyncio.sleep(1.5)
            return page

        # SPA: form appeared in-place on main page?
        if await _gh_form_present(page):
            await asyncio.sleep(0.5)
            return page

        await asyncio.sleep(0.3)

    # Final: wait for any in-flight navigation
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    await asyncio.sleep(1)
    return page


async def _wait_for_visible_options(target, max_seconds: float = 10.0) -> list:
    """Poll until role=option elements are visible, up to max_seconds. Returns list of (text, locator)."""
    deadline = asyncio.get_event_loop().time() + max_seconds
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.25)
        try:
            all_opts = await target.get_by_role("option").all()
            visible = []
            for o in all_opts:
                if await o.is_visible(timeout=0):
                    txt = (await o.inner_text()).strip()
                    if txt:
                        visible.append((txt, o))
            if visible:
                return visible
        except Exception:
            pass
    return []


async def _pick_and_click_option(target, el, visible: list, city: str, label: str = "Location (City)"):
    """Given visible (text, locator) options, use Ollama to pick best and click it."""
    opt_texts = [t for t, _ in visible]
    best = _ollama_pick_option(label, opt_texts, city)
    print(f"      → [{label}] Ollama picked: {best!r}")
    for txt, o in visible:
        if txt == best:
            await o.click()
            await asyncio.sleep(0.5)
            return True
    # Fallback: click first option
    await visible[0][1].click()
    await asyncio.sleep(0.5)
    return True


async def _fill_location_city(target, city: str, page=None):
    """
    Fill Location (City) with 4 strategies + verbose logging.

    Strategy 1 — React Select combobox (same pattern as Country strategy 2):
        Look for the input inside the React Select control by aria-label /
        placeholder / name, click it, type char-by-char, wait for role=option.

    Strategy 2 — JS: find field container whose label says "Location/City",
        click the control div to open it, then keyboard-type into the focused input.

    Strategy 3 — get_by_label → native text input (Google Places / plain input).

    Strategy 4 — CSS fallback selectors (placeholder / name attrs).
    """
    if not city:
        return

    print(f"      → [Location] starting fill for {city!r}")
    city_bare = city.split(",")[0].strip()   # "Austin" from "Austin, Tx"

    # Keyboard always comes from the outer Page — Frame objects don't have .keyboard
    _kbd = page.keyboard if page is not None else (
        target.keyboard if hasattr(target, "keyboard") else target.page.keyboard
    )

    async def _type_and_arrow_enter(el):
        """
        1. Clear field and type city name char-by-char.
        2. Wait 2 s for the autocomplete API to respond.
        3. Scan visible options and click the one where city_bare is a whole word
           (e.g. "Austin" matches "Austin, Texas" but NOT "Austintown, Ohio").
        4. Fall back to ArrowDown+Enter only if no whole-word match found.
        5. Screenshot if field still empty.
        """
        # Clear via locator — works on Frame and Page
        await el.click(click_count=3)
        await asyncio.sleep(0.2)
        await el.press("Control+a")
        await el.press("Backspace")
        await asyncio.sleep(0.3)

        # Type char-by-char — triggers React onChange per keystroke
        await el.type(city_bare, delay=80)

        print(f"      → [Location] typed {city_bare!r}, waiting 2 s for dropdown...")
        await asyncio.sleep(2.0)

        # ── Pick the right option by whole-word match ─────────────────────────
        # "Austin" → matches "Austin, Texas, US" but NOT "Austintown, Ohio, US"
        city_word_re = re.compile(r'\b' + re.escape(city_bare) + r'\b', re.I)
        clicked = False
        try:
            all_opts = await target.get_by_role("option").all()
            visible = []
            for o in all_opts:
                if await o.is_visible(timeout=0):
                    txt = (await o.inner_text()).strip()
                    if txt:
                        visible.append((txt, o))
            print(f"      → [Location] {len(visible)} option(s) visible")
            for txt, o in visible:
                print(f"           • {txt!r}")

            # Priority 1: whole-word match (Austin ≠ Austintown)
            for txt, o in visible:
                if city_word_re.search(txt):
                    print(f"      → [Location] whole-word match → clicking: {txt!r}")
                    await o.click()
                    await asyncio.sleep(0.5)
                    clicked = True
                    break

            # Priority 2: first option (fallback)
            if not clicked and visible:
                first_txt, first_o = visible[0]
                print(f"      → [Location] fallback → first option: {first_txt!r}")
                await first_o.click()
                await asyncio.sleep(0.5)
                clicked = True
        except Exception as e:
            print(f"      → [Location] option scan error: {e}")

        # Priority 3: ArrowDown+Enter if no options were visible
        if not clicked:
            print(f"      → [Location] no options visible — using ArrowDown+Enter")
            await el.press("ArrowDown")
            await asyncio.sleep(0.3)
            await el.press("Enter")
            await asyncio.sleep(0.5)

        # Verify
        filled = ""
        try:
            filled = await el.input_value()
        except Exception:
            pass
        if filled:
            print(f"      → [Location] field value after selection: {filled!r}")
        else:
            try:
                shot_path = str(_HERE / "debug_location.png")
                await target.screenshot(path=shot_path)
                print(f"      → [Location] field still empty — screenshot: {shot_path}")
            except Exception:
                pass

    # ── Find the field — same selectors as before, but simpler fill ─────────────
    found = False

    # By CSS selectors (id/name/aria-label)
    for sel in (
        "input[id*='location' i][type='text']",
        "input[name*='location' i][type='text']",
        "input[id*='city' i][type='text']",
        "input[name*='city' i][type='text']",
        "[aria-label*='Location' i]",
        "[aria-label*='City' i]",
        "[placeholder*='city' i]",
        "[placeholder*='location' i]",
    ):
        try:
            el = target.locator(sel).first
            if await el.count() == 0 or not await el.is_visible(timeout=0):
                continue
            print(f"      → [Location] found via: {sel}")
            await _type_and_arrow_enter(el)
            found = True
            break
        except Exception as e:
            print(f"      → [Location] {sel} error: {e}")

    # JS fallback: find by label text, click control, then type into focused element
    if not found:
        try:
            clicked = await target.evaluate("""
                () => {
                    const pats = [/location.*city/i, /^location\\s*\\(city\\)/i, /^city$/i, /^location$/i];
                    for (const lbl of document.querySelectorAll('label, legend, [class*="label"]')) {
                        if (!pats.some(p => p.test(lbl.innerText?.trim() || ''))) continue;
                        let p = lbl.parentElement;
                        for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
                            const ctrl = p.querySelector('[class*="select__control"],[role="combobox"],input[type="text"]');
                            if (ctrl) { ctrl.click(); ctrl.focus(); return true; }
                        }
                    }
                    return false;
                }
            """)
            if clicked:
                print(f"      → [Location] JS clicked control, typing into focused element")
                await asyncio.sleep(0.4)
                # Use page-level keyboard — works even when target is a Frame
                await _kbd.type(city_bare, delay=80)
                print(f"      → [Location] typed {city_bare!r}, waiting 2 s...")
                await asyncio.sleep(2.0)
                await _kbd.press("ArrowDown")
                await asyncio.sleep(0.3)
                await _kbd.press("Enter")
                await asyncio.sleep(0.5)
                found = True
        except Exception as e:
            print(f"      → [Location] JS fallback error: {e}")

    # get_by_label fallback
    if not found:
        for pat in (re.compile(r"location\s*\(city\)", re.I), re.compile(r"^location", re.I)):
            try:
                el = target.get_by_label(pat).first
                if await el.count() == 0 or not await el.is_visible(timeout=0):
                    continue
                print(f"      → [Location] get_by_label found")
                await _type_and_arrow_enter(el)
                found = True
                break
            except Exception as e:
                print(f"      → [Location] get_by_label error: {e}")

    if not found:
        print(f"      → [Location] field not found — taking debug screenshot")
        try:
            shot_path = str(_HERE / "debug_location_notfound.png")
            await target.screenshot(path=shot_path)
            print(f"      → screenshot: {shot_path}")
        except Exception:
            pass


async def _ensure_country_filled(target):
    """
    Three-strategy country filler.  Greenhouse's new React board uses a
    React-controlled <select> — setting sel.value directly doesn't notify
    React's internal state, so form submission sees the original empty value.

    Strategy 1 — Playwright get_by_label + select_option():
        Playwright fires both native and React synthetic events.  Works for
        plain HTML selects and most React-controlled selects.

    Strategy 2 — React Select combobox (click → type → pick):
        Some GH forms use a custom React Select dropdown (styled div, not a
        real <select>).  We click to open, type to filter, click the option.

    Strategy 3 — React native value setter via JS:
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set
        bypasses the React wrapper and writes directly to the underlying DOM
        property, then dispatches change/input so React re-syncs its state.
    """
    US = "United States"

    # ── Strategy 1: Playwright locator → select_option (fires React events) ──
    for pat in (re.compile(r"^Country[\s*:]*$", re.I), re.compile(r"Country", re.I)):
        try:
            el = target.get_by_label(pat).first
            if await el.count() > 0:
                tag = (await el.evaluate("e => e.tagName")).lower()
                if tag == "select":
                    cur = (await el.evaluate("e => e.options[e.selectedIndex]?.text||''")).strip()
                    if US.lower() in cur.lower():
                        return          # already correct
                    for us_label in [US, "United States of America", "USA", "US"]:
                        try:
                            await el.select_option(label=us_label)
                            print(f"      → Country set [strategy 1 – get_by_label: {us_label!r}]")
                            return
                        except Exception:
                            pass
        except Exception:
            pass

    # Also try select[name*=country i] / select[id*=country i]
    try:
        el = target.locator("select[name*='country' i], select[id*='country' i]").first
        if await el.count() > 0:
            cur = (await el.evaluate("e => e.options[e.selectedIndex]?.text||''")).strip()
            if US.lower() not in cur.lower():
                for us_label in [US, "United States of America", "USA", "US"]:
                    try:
                        await el.select_option(label=us_label)
                        print(f"      → Country set [strategy 1 – name/id selector: {us_label!r}]")
                        return
                    except Exception:
                        pass
    except Exception:
        pass

    # ── Strategy 2: React Select custom combobox (click → type → pick) ────────
    for combobox_sel in (
        "[aria-label*='Country' i]",
        "[placeholder*='Country' i]",
        "input[name*='country' i]",
        "[id*='country' i][role='combobox']",
    ):
        try:
            el = target.locator(combobox_sel).first
            if await el.count() > 0 and await el.is_visible(timeout=0):
                await el.click()
                await asyncio.sleep(0.6)
                await el.fill(US)
                await asyncio.sleep(1.0)  # wait for dropdown options to render
                opt = target.get_by_role("option", name=re.compile(US, re.I)).first
                if await opt.count() > 0:
                    await opt.click()
                    await asyncio.sleep(0.5)
                    print(f"      → Country set [strategy 2 – React Select combobox]")
                    return
        except Exception:
            pass

    # ── Strategy 3: JS with React-native prototype setter ─────────────────────
    try:
        result = await target.evaluate("""
            () => {
                const selects = Array.from(document.querySelectorAll('select'));
                for (const sel of selects) {
                    if (!sel.offsetParent) continue;
                    // Resolve label (CSS.escape handles bracket IDs safely)
                    let labelText = '';
                    const id = sel.id;
                    if (id) {
                        try {
                            const lbl = document.querySelector('[for="' + CSS.escape(id) + '"]');
                            if (lbl) labelText = lbl.innerText.trim();
                        } catch(e) {}
                    }
                    if (!labelText) {
                        const p = sel.closest('.field,.form-group,[class*="question"],[class*="field"]');
                        if (p) {
                            const lbl = p.querySelector('label,.label,legend');
                            if (lbl) labelText = lbl.innerText.trim();
                        }
                    }
                    if (!labelText) labelText = sel.getAttribute('name') || sel.id || '';
                    if (!/country/i.test(labelText)) continue;
                    const cur = sel.options[sel.selectedIndex];
                    if (cur && /united states/i.test(cur.text)) continue;
                    // Find target option: 'US' value or 'United States'/'United States of America' text
                    let tgt = null;
                    for (const o of sel.options) {
                        if (o.value.toUpperCase() === 'US') { tgt = o; break; }
                    }
                    if (!tgt) for (const o of sel.options) {
                        if (/^united states/i.test(o.text)) { tgt = o; break; }
                    }
                    if (!tgt) for (const o of sel.options) {
                        if (/\busa\b|\bus\b/i.test(o.text)) { tgt = o; break; }
                    }
                    if (!tgt) continue;
                    // React-native prototype setter — forces React state sync
                    try {
                        Object.getOwnPropertyDescriptor(
                            HTMLSelectElement.prototype, 'value'
                        ).set.call(sel, tgt.value);
                    } catch(e) { sel.value = tgt.value; }
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    sel.dispatchEvent(new Event('input',  {bubbles: true}));
                    return tgt.text + ' [value=' + tgt.value + ']';
                }
                return '';
            }
        """)
        if result:
            print(f"      → Country set [strategy 3 – JS native setter]: {result}")
    except Exception:
        pass


async def _fill_greenhouse(page: Page, profile: dict, email: str,
                            resume: Optional[Path],
                            job_title: str = "", company: str = "") -> str:
    """
    Handle all Greenhouse board variants:
      • boards.greenhouse.io/{slug}/jobs/{id}         — classic HTML board
      • job-boards.greenhouse.io/{slug}/jobs/{id}     — new React board (2024+)
      • custom domain with embedded iframe (Airbnb)   — GH form inside grnhse_iframe
    """
    try:
        await page.wait_for_load_state("load", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    # ── Step 1: scroll & click Apply ─────────────────────────────────────────
    # For embedded-iframe sites (Airbnb): the iframe exists on page load but is
    # collapsed (height=0). Clicking Apply Now expands it. Always click if the
    # iframe isn't already expanded, even when _gh_form_present sees the iframe.
    has_collapsed_iframe = (
        await page.locator("iframe#grnhse_iframe").count() > 0
        and not await _grnhse_iframe_expanded(page)
    )
    if has_collapsed_iframe or not await _gh_form_present(page):
        page = await _scroll_and_click_apply(page)
        # Wait for iframe to expand OR standard selectors to appear
        try:
            await page.wait_for_function(
                "(document.getElementById('grnhse_iframe')?.offsetHeight || 0) > 50"
                " || !!document.querySelector("
                "'input#first_name, input[name=\"first_name\"], form#application_form')",
                timeout=20000,
            )
        except Exception:
            pass
        await asyncio.sleep(1.5)

    # ── Step 2: resolve the form target — iframe Frame or the page itself ─────
    # Poll up to 15 s for the iframe to appear and load (Airbnb's Apply Now
    # expands the iframe asynchronously; we may arrive here before it's ready)
    frame = None
    for _attempt in range(30):          # 30 × 0.5 s = 15 s max
        frame = await _get_gh_frame(page)
        if frame and frame.url and "greenhouse" in frame.url:
            break
        await asyncio.sleep(0.5)

    if frame:
        try:
            await frame.wait_for_load_state("domcontentloaded", timeout=25000)
        except Exception:
            pass
        await asyncio.sleep(1)
        target = frame
        target_url = frame.url
        print(f"      → Form in embedded iframe: {target_url.split('?')[0]}")
    else:
        target = page
        target_url = page.url

    # ── Step 3: detect board type ─────────────────────────────────────────────
    is_new_board = (
        "job-boards.greenhouse.io" in target_url
        or await target.locator(
            "[data-testid='apply-page'], [data-testid='application-form'], "
            "[data-testid='job-application']"
        ).count() > 0
    )

    # ── Step 4: upload resume first (before interactive questions) ───────────
    await _upload_resume(target, resume, "greenhouse")

    # ── Step 5: collect answers interactively, fill, submit ───────────────────
    print(f"      → Reading form fields...")
    answers = await _collect_form_answers(target, profile, email, job_title, company, resume)
    await asyncio.sleep(1)
    await _apply_collected_answers(target, answers)
    await asyncio.sleep(3)          # let React settle all field changes

    # Location City needs special treatment (autocomplete field)
    city = profile.get("location", "").split(",")[0].strip()
    await _fill_location_city(target, city, page)
    await asyncio.sleep(1)

    await _ensure_country_filled(target)
    await asyncio.sleep(2)          # let all changes settle before submit

    # ── Step 6: fill any remaining React Select dropdowns still showing "Select..." ─
    await _fill_all_react_selects(target, profile, job_title, company, email)
    await asyncio.sleep(1)

    # ── Step 7: submit — multi-pass rescue on validation errors ──────────────────
    status = await _gh_submit(target, outer_page=page, email=email)
    if status.startswith("error: form validation"):
        print(f"      → Validation error — running React Select rescue pass...")
        await _fill_all_react_selects(target, profile, job_title, company, email)
        await asyncio.sleep(1)
        # Also rescue empty required text/URL inputs (e.g. LinkedIn URL)
        await _rescue_empty_required_inputs(target, profile, email, job_title, company, resume)
        await asyncio.sleep(1)
        status = await _gh_submit(target, outer_page=page, email=email)

    # If still failing and the error mentions a specific field, attempt targeted fill
    if status.startswith("error: form validation"):
        err_field = status[len("error: form validation — "):].lower()
        if "linkedin" in err_field:
            await _rescue_linkedin_field(target, profile)
            await asyncio.sleep(1)
            status = await _gh_submit(target, outer_page=page, email=email)

    # ── Final fallback: DOM-inspect every validation error and ask Ollama ──────
    if status.startswith("error: form validation"):
        print(f"      → [dom-fallback] Running DOM inspection pass for unanswered required fields...")
        filled = await _dom_fallback_fill_required_fields(target, profile, job_title, company, email, resume)
        if filled:
            await asyncio.sleep(1)
            status = await _gh_submit(target, outer_page=page, email=email)

    return status


async def _rescue_empty_required_inputs(target, profile: dict, email: str,
                                        job_title: str = "", company: str = "",
                                        resume_path=None):
    """Fill any visible, empty, required text/url/email inputs using profile data or Ollama."""
    try:
        fields = await target.evaluate("""
            () => {
                const out = [];
                document.querySelectorAll(
                    'input[type="text"],input[type="url"],input[type="email"],textarea'
                ).forEach(el => {
                    if (!el.offsetParent) return;
                    // Check both el.value and any visible text in the field wrapper
                    if (el.value.trim()) return;
                    // Skip hidden/readonly/disabled
                    if (el.disabled || el.readOnly) return;
                    // Skip location/city autocomplete fields — handled separately
                    const nm = (el.name || '').toLowerCase();
                    const id = (el.id || '').toLowerCase();
                    if (/location|city|autocomplete/.test(nm) || /location|city|autocomplete/.test(id)) return;
                    const p = el.closest(
                        '.field,.form-group,[class*="field"],[class*="question"]'
                    );
                    if (!p) return;
                    const lbl = p.querySelector('label,legend,[class*="label"]');
                    // Take only the DIRECT text of the label node, not its descendants
                    // This prevents select option text from bleeding into the label string
                    let rawLabel = '';
                    if (lbl) {
                        // Use only the first text-node child (the visible label text)
                        for (const node of lbl.childNodes) {
                            if (node.nodeType === Node.TEXT_NODE) {
                                rawLabel = node.textContent.trim();
                                if (rawLabel) break;
                            }
                        }
                        if (!rawLabel) rawLabel = lbl.firstElementChild?.innerText?.trim() || lbl.innerText.split('\\n')[0].trim();
                    }
                    const label = rawLabel || el.name || el.id || '';
                    if (!label) return;
                    // Skip if label looks like it already contains select-option text
                    if (label.includes('option') && label.includes('selected')) return;
                    out.push({label, name: el.name || '', id: el.id || '', type: el.type,
                              required: el.required || el.getAttribute('aria-required') === 'true'});
                });
                return out;
            }
        """)
    except Exception:
        return

    # ── Pass 1: resolve what we already know from profile ───────────────────
    resolved: dict[int, str] = {}   # index → answer
    need_ollama: list[dict]  = []   # fields that need Ollama

    for i, fld in enumerate(fields or []):
        raw_label = fld.get("label", "")
        label = raw_label.split("\n")[0].strip().rstrip(" *:")
        if not label:
            continue
        if re.search(r"location.*city|city.*location|^location\s*city|^location$|^city$", label.lower()):
            continue
        fld["_label"] = label
        fld["_idx"]   = i
        val = answer_for(label, profile, email)
        if val:
            resolved[i] = val
        else:
            is_required = fld.get("required", False)
            field_type  = fld.get("type", "text")
            if is_required or field_type == "textarea":
                need_ollama.append(fld)

    # ── Pass 2: ONE batch Ollama call for all unknown fields ─────────────────
    if need_ollama:
        batch_qs = [
            {"id": str(f["_idx"]), "q": f["_label"],
             "options": [], "type": f.get("type", "text")}
            for f in need_ollama
        ]
        print(f"      → [rescue] {len(batch_qs)} field(s) — asking Ollama in one batch...")
        batch_answers = _ollama_answer_batch(batch_qs, profile, job_title, company, resume_path)
        for fld in need_ollama:
            ans = batch_answers.get(str(fld["_idx"]), "")
            if ans:
                field_type = fld.get("type", "text")
                if field_type != "textarea":
                    ans = ans.split("\n")[0].strip()[:300]
                print(f"      → [rescue/ollama] '{fld['_label']}' → {ans[:80]!r}")
                resolved[fld["_idx"]] = ans

    # ── Pass 3: fill all resolved fields ────────────────────────────────────
    for i, fld in enumerate(fields or []):
        val = resolved.get(i, "")
        if not val:
            continue
        for sel in [
            f"input[name='{fld['name']}']" if fld.get("name") else None,
            f"input#{fld['id']}"           if fld.get("id")   else None,
        ]:
            if not sel:
                continue
            try:
                el = target.locator(sel).first
                if await el.count() > 0 and await el.is_visible(timeout=0):
                    await el.fill(val)
                    break
            except Exception:
                pass


async def _dom_fallback_fill_required_fields(
    target, profile: dict, job_title: str, company: str, email: str,
    resume_path=None,
) -> int:
    """
    Final fallback: scan the live DOM for every visible 'This field is required'
    validation error, extract the full question text and available options, ask
    Ollama to pick/generate the best answer, then fill using Playwright.

    Handles: React Select dropdowns, native <select>, text inputs, textareas,
    checkbox groups, and radio groups.

    Returns the number of fields successfully filled.
    """
    import time as _time
    # Frame objects don't have .keyboard — use the parent page's keyboard
    _kbd = target.keyboard if hasattr(target, "keyboard") else target.page.keyboard

    # ── Step 1: find all error containers and their parent question wrappers ──
    error_fields = await target.evaluate("""
        () => {
            const WRAPPER_SELS = [
                '.field', '.form-group', '[class*="field"]',
                '[class*="question"]', '[class*="form-row"]',
                'fieldset', 'li', 'div',
            ];
            const results = [];
            // Find all visible "required" error nodes
            const allEls = Array.from(document.querySelectorAll('*'));
            const errEls = allEls.filter(el => {
                if (el.children.length > 0) return false;
                const t = (el.innerText || '').trim().toLowerCase();
                return t === 'this field is required.' || t === 'this field is required';
            });

            for (const errEl of errEls) {
                // Walk up max 8 levels to find a wrapper that has a label
                let wrapper = errEl.parentElement;
                for (let i = 0; i < 8 && wrapper; i++) {
                    const lbl = wrapper.querySelector(
                        'label, [class*="label"], [class*="question-text"], legend, p'
                    );
                    if (lbl) break;
                    wrapper = wrapper.parentElement;
                }
                if (!wrapper) continue;

                // Extract full question text — take ALL text nodes in the label
                const lblEl = wrapper.querySelector(
                    'label, [class*="label"], [class*="question-text"], legend'
                );
                const question = lblEl ? lblEl.innerText.trim() : '';
                if (!question || question.length < 4) continue;

                // Detect field type inside this wrapper
                const nativeSelect  = wrapper.querySelector('select');
                const reactCtrl     = wrapper.querySelector(
                    '[class*="react-select__control"], [class*="Select__control"], ' +
                    '[class*="SelectControl"], [class*="select__control"]'
                );
                const textInput     = wrapper.querySelector(
                    'input[type="text"], input[type="email"], input[type="url"], ' +
                    'input[type="number"], textarea'
                );
                const checkboxes    = wrapper.querySelectorAll('input[type="checkbox"]');
                const radios        = wrapper.querySelectorAll('input[type="radio"]');

                let fieldType = 'unknown';
                let options   = [];
                let elId      = '';
                let elName    = '';

                if (nativeSelect && !reactCtrl) {
                    fieldType = 'native-select';
                    options   = Array.from(nativeSelect.options)
                        .filter(o => o.value && o.text.trim())
                        .map(o => o.text.trim());
                    elId   = nativeSelect.id || '';
                    elName = nativeSelect.name || '';
                } else if (reactCtrl) {
                    fieldType = 'react-select';
                    // Can't read options without opening — Playwright will do that
                    elId = (reactCtrl.id || reactCtrl.getAttribute('inputId') ||
                            wrapper.querySelector('input')?.id || '');
                } else if (radios.length > 0) {
                    fieldType = 'radio';
                    options   = Array.from(radios).map(r => {
                        const lbl = document.querySelector('label[for="' + r.id + '"]');
                        return (lbl ? lbl.innerText.trim() : r.value);
                    }).filter(Boolean);
                    elName = radios[0].name || '';
                } else if (checkboxes.length > 1) {
                    fieldType = 'checkbox-group';
                    options   = Array.from(checkboxes).map(cb => {
                        const lbl = document.querySelector('label[for="' + cb.id + '"]');
                        return (lbl ? lbl.innerText.trim() : cb.value);
                    }).filter(Boolean);
                } else if (textInput) {
                    fieldType = textInput.tagName === 'TEXTAREA' ? 'textarea' : 'text';
                    elId      = textInput.id || '';
                    elName    = textInput.name || '';
                }

                if (fieldType === 'unknown') continue;

                results.push({
                    question, fieldType, options, elId, elName,
                    wrapperPath: wrapper.className || '',
                });
            }
            return results;
        }
    """)

    if not error_fields:
        return 0

    filled_count = 0

    # ── Pre-pass: open every React Select to discover its options ────────────
    # Do this BEFORE the batch Ollama call so we have all options ready.
    ctrl_sel = (
        "[class*='react-select__control'], [class*='Select__control'], "
        "[class*='SelectControl'], [class*='select__control']"
    )
    for fld in error_fields:
        if fld.get("fieldType") != "react-select":
            continue
        try:
            ctrls = await target.locator(ctrl_sel).all()
            for ctrl in ctrls:
                if not await ctrl.is_visible(timeout=0):
                    continue
                placeholder_text = await ctrl.evaluate(
                    "el => el.querySelector('[class*=\"placeholder\"]')?.innerText?.trim() || ''"
                )
                val_text = await ctrl.evaluate(
                    "el => el.querySelector('[class*=\"single-value\"]')?.innerText?.trim() || ''"
                )
                if val_text:   # already has a value — skip
                    continue
                await ctrl.click()
                await asyncio.sleep(0.6)
                raw_opts = await target.evaluate("""
                    () => {
                        const menu = document.querySelector(
                            '[class*="react-select__menu"]:not([style*="display: none"]), '
                            + '[class*="Select__menu"]:not([style*="display: none"])'
                        );
                        if (!menu) return [];
                        return Array.from(menu.querySelectorAll(
                            '[class*="react-select__option"], [class*="select__option"], [role="option"]'
                        )).map(o => o.innerText.trim()).filter(Boolean);
                    }
                """)
                await _kbd.press("Escape")
                await asyncio.sleep(0.2)
                # Filter out phone dial-code entries
                real_opts = [o for o in raw_opts if not re.search(r'\+\d{1,4}$', o.strip())]
                if real_opts:
                    fld["options"] = real_opts
                    break  # matched this ctrl to this fld (one at a time)
        except Exception:
            pass

    # ── Batch Ollama call for all fields that need an answer ─────────────────
    batch_qs = []
    for i, fld in enumerate(error_fields):
        q    = fld.get("question", "").strip()
        opts = fld.get("options", [])
        if not q:
            continue
        prof_hint = answer_for(q, profile, email)
        if prof_hint:
            fld["_answer"] = prof_hint   # already known — skip Ollama
        else:
            batch_qs.append({"id": str(i), "q": q, "options": opts,
                             "type": fld.get("fieldType", "text")})

    if batch_qs:
        print(f"      → [dom-fallback] {len(batch_qs)} field(s) — batch Ollama call...")
        batch_answers = _ollama_answer_batch(batch_qs, profile, job_title, company, resume_path)
        for item in batch_qs:
            ans = batch_answers.get(item["id"], "")
            if ans:
                idx = int(item["id"])
                error_fields[idx]["_answer"] = ans

    for fld in error_fields:
        question   = fld.get("question", "").strip()
        field_type = fld.get("fieldType", "")
        options    = fld.get("options", [])
        el_id      = fld.get("elId", "")
        el_name    = fld.get("elName", "")

        if not question or field_type == "unknown":
            continue

        print(f"      → [dom-fallback] '{question[:70]}' [{field_type}]")

        try:
            # ── React Select: click to open, read options, pick via Ollama ──
            if field_type == "react-select":
                # Find the control and click it to open the menu
                ctrl_sel = (
                    "[class*='react-select__control'], [class*='Select__control'], "
                    "[class*='SelectControl'], [class*='select__control']"
                )
                # Find the control nearest to our question by iterating visible ones
                ctrls = await target.locator(ctrl_sel).all()
                best_ctrl = None
                for ctrl in ctrls:
                    if await ctrl.is_visible(timeout=0):
                        # Check if this control is currently empty (no value selected)
                        val_text = await ctrl.evaluate(
                            "el => el.querySelector('[class*=\"single-value\"],[class*=\"placeholder\"]')?.innerText?.trim() || ''"
                        )
                        placeholder_text = await ctrl.evaluate(
                            "el => el.querySelector('[class*=\"placeholder\"]')?.innerText?.trim() || ''"
                        )
                        # Empty if it shows a placeholder (not a value)
                        if placeholder_text and val_text == placeholder_text:
                            best_ctrl = ctrl
                            break

                if not best_ctrl:
                    continue

                # Click to open the dropdown
                await best_ctrl.click()
                await asyncio.sleep(0.8)

                # Read options ONLY from the actively open React Select menu,
                # not from phone dial-code dropdowns (iti__country) or hidden menus.
                import re as _re
                opts_text = await target.evaluate("""
                    () => {
                        // Find the currently visible/open React Select menu
                        const menu = document.querySelector(
                            '[class*="react-select__menu"]:not([style*="display: none"]), '
                            + '[class*="Select__menu"]:not([style*="display: none"]), '
                            + '[class*="select__menu"]:not([style*="display: none"])'
                        );
                        if (!menu) return [];
                        const opts = menu.querySelectorAll(
                            '[class*="react-select__option"], [class*="Select__option"], '
                            + '[class*="select__option"], [role="option"]'
                        );
                        return Array.from(opts)
                            .map(o => o.innerText.trim())
                            .filter(t => t.length > 0);
                    }
                """)

                # Filter out phone dial-code entries (e.g. "United States+1", "Bangladesh+880")
                opts_text = [o for o in opts_text
                             if not _re.search(r'\+\d{1,4}$', o.strip())]

                if not opts_text:
                    await _kbd.press("Escape")
                    continue

                # Use pre-computed batch answer as hint; map to exact option text
                hint = fld.get("_answer", "") or opts_text[0]
                picked = _ollama_pick_option(question, opts_text, hint)
                print(f"          Ollama picked: {picked!r}")

                # Click the matching option — short timeout, it should be visible right now
                opt_locator = target.locator(
                    "[class*='react-select__option'], [class*='Select__option'], "
                    "[class*='select__option'], [role='option']"
                ).filter(has_text=picked)
                try:
                    if await opt_locator.count() > 0:
                        await opt_locator.first.click(timeout=3000)
                        await asyncio.sleep(0.4)
                        filled_count += 1
                    else:
                        raise Exception("option not in menu")
                except Exception:
                    # Type the short answer into the search input and press Enter
                    try:
                        input_in_ctrl = best_ctrl.locator("input").first
                        if await input_in_ctrl.count() > 0:
                            await input_in_ctrl.fill(picked[:30])
                            await asyncio.sleep(0.6)
                            await input_in_ctrl.press("Enter")
                            filled_count += 1
                        else:
                            await _kbd.press("Escape")
                    except Exception:
                        await _kbd.press("Escape")

            # ── Native <select> ──────────────────────────────────────────────
            elif field_type == "native-select" and options:
                hint   = fld.get("_answer", "") or options[0]
                picked = _ollama_pick_option(question, options, hint)
                print(f"          picked: {picked!r}")
                sel_locator = (
                    target.locator(f"select#{el_id}") if el_id else
                    target.locator(f"select[name='{el_name}']") if el_name else
                    target.locator("select").first
                )
                if await sel_locator.count() > 0:
                    try:
                        await sel_locator.select_option(label=picked)
                    except Exception:
                        await sel_locator.select_option(index=options.index(picked) + 1
                                                        if picked in options else 0)
                    filled_count += 1

            # ── Radio group ──────────────────────────────────────────────────
            elif field_type == "radio" and options:
                hint   = fld.get("_answer", "") or options[0]
                picked = _ollama_pick_option(question, options, hint)
                print(f"          picked: {picked!r}")
                radio_lbl = target.get_by_label(re.compile(re.escape(picked), re.I))
                if await radio_lbl.count() > 0:
                    await radio_lbl.first.click()
                    filled_count += 1

            # ── Text / Textarea ──────────────────────────────────────────────
            elif field_type in ("text", "textarea"):
                ans = fld.get("_answer", "")
                if not ans:
                    continue
                if field_type == "text":
                    ans = ans.split("\n")[0].strip()[:300]
                print(f"          answer: {ans[:80]!r}")
                el_loc = (
                    target.locator(f"#{el_id}") if el_id else
                    target.locator(f"[name='{el_name}']") if el_name else None
                )
                if el_loc and await el_loc.count() > 0 and await el_loc.is_visible(timeout=0):
                    await el_loc.fill(ans)
                    filled_count += 1

            # ── Checkbox group ───────────────────────────────────────────────
            elif field_type == "checkbox-group" and options:
                ans    = fld.get("_answer", "")
                chosen = {line.strip().lower() for line in (ans or "").splitlines()
                          if line.strip()}
                for opt in options:
                    if opt.lower() in chosen:
                        lbl_loc = target.get_by_label(re.compile(re.escape(opt), re.I))
                        if await lbl_loc.count() > 0:
                            cb = lbl_loc.first
                            if not await cb.is_checked():
                                await cb.click()
                                filled_count += 1

        except Exception as _e:
            print(f"          [dom-fallback] error on '{question[:40]}': {_e}")
            continue

    if filled_count:
        print(f"      → [dom-fallback] filled {filled_count} field(s)")
    return filled_count


async def _rescue_linkedin_field(target, profile: dict):
    """Fill the LinkedIn profile URL field, constructing a URL from name if needed."""
    linkedin_url = profile.get("linkedin_url", "").strip()
    if not linkedin_url:
        name = profile.get("name", "applicant")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        linkedin_url = f"https://www.linkedin.com/in/{slug}"
        print(f"      → [LinkedIn] No URL in profile — using constructed: {linkedin_url}")

    for sel in [
        "input[name*='linkedin' i]", "input[id*='linkedin' i]",
        "input[placeholder*='linkedin' i]", "input[aria-label*='linkedin' i]",
    ]:
        try:
            el = target.locator(sel).first
            if await el.count() > 0 and await el.is_visible(timeout=0):
                await el.clear()
                await el.fill(linkedin_url)
                await el.press("Tab")
                print(f"      → [LinkedIn] Filled via selector {sel!r}")
                return
        except Exception:
            pass

    # Fallback: find by label text
    try:
        el = target.get_by_label(re.compile(r"linkedin", re.I)).first
        if await el.count() > 0:
            await el.clear()
            await el.fill(linkedin_url)
            await el.press("Tab")
            print(f"      → [LinkedIn] Filled via label search")
    except Exception:
        pass


async def _fill_all_react_selects(target, profile: dict,
                                   job_title: str = "", company: str = "",
                                   email: str = ""):
    """
    Find every visible React Select dropdown still showing 'Select...' placeholder
    and fill it using Ollama to pick the best option (or first option as fallback).
    Handles all required custom dropdowns that native select_option() can't reach.
    """
    try:
        # Collect all visible unfilled React Select placeholders + their labels via JS
        react_fields = await target.evaluate("""
            () => {
                const results = [];
                document.querySelectorAll('[class*="placeholder"]').forEach(ph => {
                    if (!ph.offsetParent) return;
                    if (!/^select/i.test((ph.textContent || '').trim())) return;
                    // Walk up to find control div
                    let ctrl = ph.parentElement;
                    for (let i = 0; i < 8 && ctrl; i++) {
                        if (ctrl.getAttribute('role') === 'combobox' ||
                            (ctrl.className && /select.*control|selectControl/i.test(ctrl.className)))
                            break;
                        ctrl = ctrl.parentElement;
                    }
                    if (!ctrl) return;

                    // ── Strategy 1: aria-labelledby on the combobox input ──────
                    // Lever forms use <input role="combobox" aria-labelledby="school--0-label">
                    let label = '';
                    const comboInput = ctrl.querySelector('input[aria-labelledby], input[role="combobox"]');
                    if (comboInput) {
                        const lblId = comboInput.getAttribute('aria-labelledby');
                        if (lblId) {
                            const lblEl = document.getElementById(lblId);
                            if (lblEl) label = lblEl.innerText.trim();
                        }
                        // Also try aria-label directly on the input
                        if (!label) label = comboInput.getAttribute('aria-label') || '';
                    }

                    // ── Strategy 2: walk up the DOM looking for a label element ─
                    if (!label) {
                        let p = ctrl.parentElement;
                        for (let i = 0; i < 8 && p; i++) {
                            // Prefer <label> and <legend> over generic [class*="label"]
                            // to avoid picking up placeholder-like elements
                            const lbl = p.querySelector('label, legend') ||
                                        p.querySelector('[class*="label"]:not([class*="placeholder"])');
                            if (lbl && lbl.innerText.trim()) {
                                const t = lbl.innerText.trim();
                                // Skip if it looks like a placeholder/generic hint
                                if (!/^select$/i.test(t) && t.length > 1) {
                                    label = t;
                                    break;
                                }
                            }
                            p = p.parentElement;
                        }
                    }

                    if (label && !/^select[.]{0,3}$/i.test(label.trim())) {
                        results.push(label);
                    }
                });
                return [...new Set(results)];
            }
        """)
    except Exception:
        return

    # Load hardcoded custom answers so they take priority over Ollama
    _cust_all = load_custom_answers()
    _cust = _cust_all.get(email, {})

    for field_label in react_fields:
        clean_label = field_label.strip(" *:\n")
        try:
            # Get all options by clicking the control to open the dropdown
            lbl_el = target.get_by_label(
                re.compile(re.escape(clean_label[:40]), re.I)
            ).first
            if await lbl_el.count() == 0:
                # Try finding by placeholder text walk
                lbl_el = target.locator(
                    f"[class*='placeholder']:has-text('{clean_label[:20]}')"
                ).first

            # ── Check if we have a definitive answer for this field ──────────
            # custom_answers (hardcoded) > profile > Ollama
            # Type it into the search box to filter hundreds of options down to
            # just the right one — guarantees School/Degree/Country are correct.
            norm_label = clean_label.lower().strip()
            profile_answer = (_cust.get(norm_label)
                              or answer_for(clean_label, profile, email))

            # Open the dropdown.  Strategy: if lbl_el is already the combobox
            # input (role="combobox"), click it directly — this is the most
            # reliable way and ensures we type into the SAME element we opened.
            # Otherwise walk up to the control div and click that.
            el_role = await lbl_el.evaluate(
                "el => (el.getAttribute('role') || el.tagName || '').toLowerCase()"
            )
            if el_role == "combobox":
                # The label resolved to the input itself — click it to open
                await lbl_el.click()
                combo_input = lbl_el          # we'll type into this same element
            else:
                await lbl_el.evaluate("""el => {
                    let p = el.parentElement;
                    for (let i = 0; i < 8 && p; i++) {
                        if (p.getAttribute('role') === 'combobox' ||
                            (p.className && /control/i.test(p.className))) {
                            p.click(); return;
                        }
                        p = p.parentElement;
                    }
                    el.click();
                }""")
                # After clicking, the open combobox is uniquely identified by
                # aria-expanded="true".  This avoids picking the wrong input when
                # multiple dropdowns are on the same page.
                combo_input = target.locator(
                    "input[aria-expanded='true'], "
                    "input[role='combobox'][aria-expanded='true']"
                ).first
            await asyncio.sleep(0.6)

            # ── Type-to-search strategy for known answers ─────────────────────
            # School/degree/country dropdowns have hundreds of options. Typing the
            # known value filters the list so we pick the right one immediately.
            if profile_answer:
                search_term = profile_answer.split("\n")[0].strip()[:60]
                if await combo_input.count() > 0 and await combo_input.is_visible(timeout=600):
                    await combo_input.fill(search_term)
                    await asyncio.sleep(0.8)   # wait for filtered options
                    # Collect the now-filtered options
                    filtered_opts = []
                    for o in await target.get_by_role("option").all():
                        try:
                            if await o.is_visible(timeout=300):
                                filtered_opts.append(((await o.inner_text()).strip(), o))
                        except Exception:
                            pass
                    # Filter out phone dial-codes
                    filtered_opts = [(t, el) for t, el in filtered_opts
                                     if not re.search(r'\+\d{1,4}$', t.strip())]
                    if filtered_opts:
                        if len(filtered_opts) == 1:
                            # Only one match — click directly, no Ollama needed
                            best = filtered_opts[0][0]
                            chosen_el = filtered_opts[0][1]
                        else:
                            best = _ollama_pick_option(
                                clean_label,
                                [t for t, _ in filtered_opts],
                                search_term,
                            )
                            chosen_el = next((el for t, el in filtered_opts if t == best),
                                             filtered_opts[0][1])
                        await chosen_el.click()
                        await asyncio.sleep(0.3)
                        print(f"      → [{clean_label[:45]}] type-search {search_term!r} → {best!r}")
                        continue
                    # No filtered results — clear and fall through to regular open
                    await combo_input.fill("")
                    await asyncio.sleep(0.3)

            # Collect all visible options (full list)
            all_opts = await target.get_by_role("option").all()
            visible_opts = []
            for o in all_opts:
                try:
                    if await o.is_visible(timeout=500):
                        visible_opts.append(((await o.inner_text()).strip(), o))
                except Exception:
                    pass
            # Filter out phone dial-codes
            visible_opts = [(t, el) for t, el in visible_opts
                            if not re.search(r'\+\d{1,4}$', t.strip())]

            def _pick_best(opts_list, label, prof, jt, co):
                """Ask Ollama to pick the best option; never blindly use index 0."""
                opt_texts = [t for t, _ in opts_list]
                # First try answer_for() as a strong hint (school, degree, country, etc.)
                profile_hint = answer_for(label, prof, email)
                hint = profile_hint or _ollama_generate_answer(label, prof, jt, co,
                                                               max_retries=2, retry_delay=2.0)
                hint = hint.split("\n")[0].strip()[:200] if hint else ""
                return _ollama_pick_option(label, opt_texts, hint) if hint else opt_texts[0]

            if not visible_opts:
                # ── Fallback 1: ArrowDown to open ────────────────────────────
                await lbl_el.press("ArrowDown")
                await asyncio.sleep(0.5)
                visible_opts = []
                for o in await target.get_by_role("option").all():
                    try:
                        if await o.is_visible(timeout=300):
                            visible_opts.append(((await o.inner_text()).strip(), o))
                    except Exception:
                        pass
                if visible_opts:
                    best = _pick_best(visible_opts, clean_label, profile, job_title, company)
                    chosen_el  = next((el for t, el in visible_opts if t == best), visible_opts[0][1])
                    await chosen_el.click()
                    print(f"      → [{clean_label[:45]}] FB1 ArrowDown → {best!r}")
                    await asyncio.sleep(0.3)
                    continue

                # ── Fallback 2: JS mousedown on control div ───────────────────
                await lbl_el.evaluate("""el => {
                    let p = el.parentElement;
                    for (let i = 0; i < 10 && p; i++) {
                        if (p.getAttribute('role') === 'combobox' ||
                            (p.className && /control/i.test(p.className))) {
                            p.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                            p.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true}));
                            p.click();
                            return;
                        }
                        p = p.parentElement;
                    }
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                    el.click();
                }""")
                await asyncio.sleep(0.8)
                visible_opts = []
                for o in await target.get_by_role("option").all():
                    try:
                        if await o.is_visible(timeout=300):
                            visible_opts.append(((await o.inner_text()).strip(), o))
                    except Exception:
                        pass
                if visible_opts:
                    best = _pick_best(visible_opts, clean_label, profile, job_title, company)
                    chosen_el  = next((el for t, el in visible_opts if t == best), visible_opts[0][1])
                    await chosen_el.click()
                    print(f"      → [{clean_label[:45]}] FB2 JS mousedown → {best!r}")
                    await asyncio.sleep(0.3)
                    continue

                # ── Fallback 3: click dropdown indicator arrow ────────────────
                await lbl_el.evaluate("""el => {
                    let p = el.parentElement;
                    for (let i = 0; i < 10 && p; i++) {
                        const ind = p.querySelector(
                            '[class*="indicator"],[class*="arrow"],[class*="chevron"],' +
                            'svg,[class*="dropdown"]'
                        );
                        if (ind) { ind.click(); return true; }
                        p = p.parentElement;
                    }
                    return false;
                }""")
                await asyncio.sleep(0.8)
                visible_opts = []
                for o in await target.get_by_role("option").all():
                    try:
                        if await o.is_visible(timeout=300):
                            visible_opts.append(((await o.inner_text()).strip(), o))
                    except Exception:
                        pass
                if visible_opts:
                    best = _pick_best(visible_opts, clean_label, profile, job_title, company)
                    chosen_el  = next((el for t, el in visible_opts if t == best), visible_opts[0][1])
                    await chosen_el.click()
                    print(f"      → [{clean_label[:45]}] FB3 indicator → {best!r}")
                    await asyncio.sleep(0.3)
                    continue

                # ── Fallback 4: JS direct listbox ─────────────────────────────
                fb4_opts = await target.evaluate("""() => {
                    const menu = document.querySelector(
                        '[class*="menu"],[role="listbox"],[class*="dropdown-menu"]'
                    );
                    if (!menu) return [];
                    return Array.from(menu.querySelectorAll('[role="option"],[class*="option"]'))
                        .map(o => o.innerText.trim()).filter(Boolean);
                }""")
                if fb4_opts:
                    best = _ollama_pick_option(clean_label, fb4_opts,
                                               answer_for(clean_label, profile, email) or fb4_opts[0])
                    clicked = await target.evaluate(f"""() => {{
                        const menu = document.querySelector(
                            '[class*="menu"],[role="listbox"],[class*="dropdown-menu"]'
                        );
                        if (!menu) return false;
                        const items = Array.from(menu.querySelectorAll('[role="option"],[class*="option"]'));
                        const target_text = {json.dumps(best)};
                        const match = items.find(o => o.innerText.trim() === target_text) || items[0];
                        if (match) {{ match.click(); return true; }}
                        return false;
                    }}""")
                    print(f"      → [{clean_label[:45]}] FB4 JS direct → {best!r}")
                else:
                    print(f"      → [{clean_label[:45]}] all fallbacks failed — skipping")
                continue

            opt_texts = [t for t, _ in visible_opts]

            # Use answer_for() hint first, then Ollama — never blind first-option
            profile_hint = answer_for(clean_label, profile, email)
            hint = profile_hint or _ollama_generate_answer(clean_label, profile, job_title, company,
                                                           max_retries=2, retry_delay=2.0)
            hint = hint.split("\n")[0].strip()[:200] if hint else opt_texts[0]
            best_text = _ollama_pick_option(clean_label, opt_texts, hint)

            chosen_el  = next((el for txt, el in visible_opts if txt == best_text), visible_opts[0][1])
            chosen_txt = next((txt for txt, el in visible_opts if txt == best_text), visible_opts[0][0])
            await chosen_el.click()
            await asyncio.sleep(0.4)
            print(f"      → [{clean_label[:45]}] React Select → {chosen_txt!r}")

        except Exception as e:
            print(f"      → [{clean_label[:45]}] React Select rescue error: {e}")


# ── Lever form filler ───────────────────────────────────────────────────────────

async def _fill_lever(page: Page, profile: dict, email: str,
                      resume: Optional[Path]) -> str:
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    await asyncio.sleep(1.5)

    # ── Known Lever field names (very stable across all companies) ──
    _LEVER_MAP = {
        "name":           profile.get("name", ""),
        "email":          email,
        "phone":          profile.get("phone", ""),
        "org":            "",   # current company — leave blank
        "urls[LinkedIn]": profile.get("linkedin_url", ""),
        "urls[GitHub]":   profile.get("github_url", ""),
        "urls[Portfolio]":profile.get("website_url", ""),
    }
    for name_attr, val in _LEVER_MAP.items():
        if not val:
            continue
        for sel in [f"input[name='{name_attr}']",
                    f"textarea[name='{name_attr}']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=0):
                    current = await el.input_value()
                    if not current:
                        await el.fill(val)
                    break
            except Exception:
                pass

    # ── Resume upload ──
    await _upload_resume(page, resume, "lever")

    # ── Custom questions (.application-question blocks) ──
    for card in await page.locator(
        ".application-question, [class*='questionWrapper'], [class*='question-block']"
    ).all():
        try:
            if not await card.is_visible(timeout=0):
                continue
            label_el   = card.locator("label, h4, p, span[class*='label']").first
            label_text = ""
            if await label_el.count() > 0:
                label_text = (await label_el.inner_text()).strip()

            # textarea
            ta = card.locator("textarea").first
            if await ta.count() > 0 and await ta.is_visible(timeout=0):
                if not await ta.input_value():
                    val = answer_for(label_text, profile, email)
                    if val:
                        await ta.fill(val)
                continue

            # text / tel input
            inp = card.locator("input[type='text'], input[type='tel']").first
            if await inp.count() > 0 and await inp.is_visible(timeout=0):
                if not await inp.input_value():
                    val = answer_for(label_text, profile, email)
                    if val:
                        await inp.fill(val)
                continue

            # select
            sel_el = card.locator("select").first
            if await sel_el.count() > 0 and await sel_el.is_visible(timeout=0):
                if not await sel_el.input_value():
                    opts = await sel_el.evaluate(
                        "el => Array.from(el.options).filter(o=>o.value)"
                        ".map(o=>({value:o.value,text:o.text.trim().toLowerCase()}))"
                    )
                    if opts:
                        val       = answer_for(label_text, profile, email)
                        val_lower = val.lower() if val else ""
                        chosen    = next(
                            (o["value"] for o in opts
                             if val_lower and (val_lower in o["text"] or o["text"] in val_lower)),
                            opts[0]["value"],
                        )
                        await sel_el.select_option(value=chosen)
                continue
        except Exception:
            pass

    # ── CAPTCHA ──
    if await has_captcha(page):
        await pause_for_captcha(page)

    # ── Submit (never use "Apply" — that's a navigation button on Lever too) ──
    for btn_text in ["Submit Application", "Submit My Application", "Submit"]:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{btn_text}$", re.I)).first
            if await btn.is_visible(timeout=0):
                await btn.click()
                await asyncio.sleep(SUBMIT_WAIT)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in ["thank you", "application received",
                                            "submitted", "application sent",
                                            "we'll be in touch"]):
                    return "applied"
                return "submitted (unconfirmed)"
        except Exception:
            pass

    try:
        sub = page.locator("input[type='submit']").first
        if await sub.is_visible(timeout=0):
            await sub.click()
            await asyncio.sleep(SUBMIT_WAIT)
            return "submitted (unconfirmed)"
    except Exception:
        pass

    # ── Broad CSS fallback ──────────────────────────────────────────────────────
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)
    except Exception:
        pass
    for sel in ("button[type='submit']", "[data-testid*='submit' i]", "[data-qa*='submit' i]"):
        try:
            sub = page.locator(sel).last
            if await sub.count() > 0 and await sub.is_visible(timeout=0):
                await sub.click()
                await asyncio.sleep(SUBMIT_WAIT)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in ["thank you", "application received",
                                            "submitted", "application sent",
                                            "we'll be in touch"]):
                    return "applied"
                return "submitted (unconfirmed)"
        except Exception:
            pass

    return "error: submit button not found"


# ── Ashby form filler ───────────────────────────────────────────────────────────

async def _fill_ashby(page: Page, profile: dict, email: str,
                      resume: Optional[Path]) -> str:
    """Ashby apply pages open with a modal/inline form. Click Apply if needed."""
    await page.wait_for_load_state("networkidle", timeout=25000)
    await asyncio.sleep(2)

    # Ashby job pages have an "Apply" button that opens the form
    for btn_text in ["Apply for this job", "Apply Now", "Apply"]:
        try:
            btn = page.get_by_role("button", name=re.compile(btn_text, re.I)).first
            if await btn.is_visible(timeout=0):
                await btn.click()
                await asyncio.sleep(2)
                break
        except Exception:
            pass

    # ── Resume upload ──
    await _upload_resume(page, resume, "ashby")

    # ── Fill all visible labeled inputs ──
    for inp in await page.locator(
        "input[type='text'], input[type='email'], input[type='tel'], textarea"
    ).all():
        try:
            if not await inp.is_visible(timeout=0):
                continue
            if await inp.input_value():
                continue
            inp_id  = await inp.get_attribute("id") or ""
            label   = ""
            if inp_id:
                try:
                    label = (await page.locator(f"label[for='{inp_id}']").first.inner_text()).strip()
                except Exception:
                    pass
            if not label:
                label = (
                    await inp.get_attribute("placeholder") or
                    await inp.get_attribute("aria-label") or
                    await inp.get_attribute("name") or ""
                )
            val = answer_for(label, profile, email)
            if val:
                await inp.fill(val)
        except Exception:
            pass

    # ── Selects ──
    for sel_el in await page.locator("select").all():
        try:
            if not await sel_el.is_visible(timeout=0):
                continue
            if await sel_el.input_value():
                continue
            label  = await sel_el.get_attribute("aria-label") or ""
            val    = answer_for(label, profile, email)
            opts   = await sel_el.evaluate(
                "el => Array.from(el.options).filter(o=>o.value)"
                ".map(o=>({value:o.value,text:o.text.trim().toLowerCase()}))"
            )
            if not opts:
                continue
            val_lower = val.lower() if val else ""
            chosen    = next(
                (o["value"] for o in opts
                 if val_lower and (val_lower in o["text"] or o["text"] in val_lower)),
                opts[0]["value"],
            )
            await sel_el.select_option(value=chosen)
        except Exception:
            pass

    # ── CAPTCHA ──
    if await has_captcha(page):
        await pause_for_captcha(page)

    # ── Submit (never "Apply" — that opens the form, not submits it) ──
    for btn_text in ["Submit Application", "Submit My Application",
                     "Submit", "Send Application"]:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{btn_text}$", re.I)).first
            if await btn.is_visible(timeout=0):
                await btn.click()
                await asyncio.sleep(SUBMIT_WAIT)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in ["thank you", "application received",
                                            "submitted", "we'll review",
                                            "we will review"]):
                    return "applied"
                return "submitted (unconfirmed)"
        except Exception:
            pass

    # ── Broad CSS fallback ──────────────────────────────────────────────────────
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)
    except Exception:
        pass
    for sel in ("button[type='submit']", "[data-testid*='submit' i]", "[data-qa*='submit' i]"):
        try:
            sub = page.locator(sel).last
            if await sub.count() > 0 and await sub.is_visible(timeout=0):
                await sub.click()
                await asyncio.sleep(SUBMIT_WAIT)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in ["thank you", "application received",
                                            "submitted", "we'll review",
                                            "we will review"]):
                    return "applied"
                return "submitted (unconfirmed)"
        except Exception:
            pass

    return "error: submit button not found"


# ── Workday form filler ─────────────────────────────────────────────────────────

async def _wd_wait_ready(page: Page, timeout: int = 15000) -> None:
    """Wait for Workday SPA navigation — networkidle ideal but not required."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 8000))
    except Exception:
        pass  # Workday never truly goes idle; continue after best-effort wait
    await asyncio.sleep(1.5)


async def _wd_fill_field(page: Page, automation_id: str, value: str) -> bool:
    """
    Fill a Workday input identified by a data-automation-id.
    In Workday the automation-id is usually on a container DIV, so we look
    for the actual <input>/<textarea> inside that container.
    """
    if not value:
        return False
    try:
        # Try direct match first (some Workday portals put the id on the input)
        loc = page.locator(f"[data-automation-id='{automation_id}']").first
        if await loc.count() > 0 and await loc.is_visible(timeout=2000):
            tag = await loc.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("input", "textarea"):
                await loc.triple_click()
                await loc.fill(value)
                return True
            # Container div — find the input inside it
            inp = loc.locator("input, textarea").first
            if await inp.count() > 0 and await inp.is_visible(timeout=1000):
                await inp.triple_click()
                await inp.fill(value)
                return True
        # Fallback: formField- container pattern (Adobe / most Workday portals)
        container = page.locator(f"[data-automation-id='formField-{automation_id}']").first
        if await container.count() > 0:
            inp = container.locator("input, textarea").first
            if await inp.count() > 0 and await inp.is_visible(timeout=1000):
                await inp.triple_click()
                await inp.fill(value)
                return True
    except Exception:
        pass
    return False


async def _wd_dropdown(page: Page, automation_id: str, value: str) -> bool:
    """
    Handle Workday custom dropdowns (not <select>).
    Tries the direct automation-id, then the formField- container pattern.
    Uses force=True throughout — Workday elements often fail is_visible() checks.
    """
    if not value:
        return False
    candidates = [
        page.locator(f"[data-automation-id='{automation_id}']").first,
        page.locator(f"[data-automation-id='formField-{automation_id}'] [data-automation-id='multiselectInputContainer']").first,
        page.locator(f"[data-automation-id='formField-{automation_id}'] button").first,
        page.locator(f"[data-automation-id='formField-{automation_id}'] [role='combobox']").first,
    ]
    for cand in candidates:
        try:
            if await cand.count() == 0:
                continue
            await cand.click(force=True)
            await asyncio.sleep(0.8)
            # Try exact match first, then partial; check multiple option selectors
            for exact in (True, False):
                for opt_sel in ("[data-automation-id='promptOption']", "[role='option']", "li[tabindex]"):
                    opts = await page.locator(opt_sel).all()
                    for opt in opts:
                        try:
                            text = (await opt.inner_text()).strip()
                            match = (text.lower() == value.lower()) if exact else (value.lower() in text.lower())
                            if match:
                                await opt.click(force=True)
                                await asyncio.sleep(0.3)
                                return True
                        except Exception:
                            continue
            await page.keyboard.press("Escape")
            return False
        except Exception:
            pass
    return False


async def _wd_multiselect_first(page: Page, container_aid: str) -> bool:
    """Open a Workday multiselect by its formField- container and pick the first option."""
    try:
        trigger = page.locator(
            f"[data-automation-id='{container_aid}'] [data-automation-id='multiselectInputContainer']"
        ).first
        if await trigger.count() == 0 or not await trigger.is_visible(timeout=2000):
            return False
        await trigger.click()
        await asyncio.sleep(0.8)
        # All visible promptOptions — pick the topmost one (by position, not phone-code)
        opts = await page.locator("[data-automation-id='promptOption']").all()
        for opt in opts:
            try:
                if not await opt.is_visible(timeout=0):
                    continue
                bb = await opt.bounding_box()
                text = (await opt.inner_text()).strip()
                # Skip phone country code options (contain "+")
                if bb and bb['y'] < 600 and "+" not in text:
                    await opt.click()
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


async def _wd_next(page: Page) -> bool:
    """Click the Next button in Workday multi-step form."""
    # pageFooterNextButton = Adobe/most Workday; bottom-navigation-next-btn = older portals
    for aid in ("pageFooterNextButton", "bottom-navigation-next-btn", "next-btn", "saveAndContinueButton"):
        try:
            btn = page.locator(f"[data-automation-id='{aid}']").first
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                await btn.click()
                await _wd_wait_ready(page)
                return True
        except Exception:
            pass
    return False


async def _wd_upload_resume(page: Page, resume: Optional[Path]) -> None:
    """Upload resume via Workday file input (data-automation-id='file-upload-input-ref')."""
    if not resume or not resume.exists():
        return
    try:
        file_input = page.locator("[data-automation-id='file-upload-input-ref']").first
        if await file_input.count() > 0:
            await file_input.set_input_files(str(resume))
            await asyncio.sleep(1.5)
    except Exception:
        pass


async def _wd_fill_input(page: Page, aid: str, val: str) -> bool:
    """
    Fill a Workday text input using multiple strategies.
    Returns True if the fill was verified successful.
    """
    if not val:
        return False

    # Strategy 1: formField container → first input inside
    try:
        inp = page.locator(f"[data-automation-id='formField-{aid}'] input").first
        if await inp.count() > 0 and await inp.is_visible(timeout=2000):
            await inp.click()
            await inp.fill("")
            await inp.type(val, delay=30)
            await asyncio.sleep(0.2)
            actual = await inp.input_value()
            if actual:
                return True
    except Exception:
        pass

    # Strategy 2: id contains the aid string
    try:
        aid_dash = aid.replace("--", "-")
        for sel in (f"input[id*='{aid}']", f"input[id*='{aid_dash}']"):
            inp = page.locator(sel).first
            if await inp.count() > 0 and await inp.is_visible(timeout=1000):
                await inp.click()
                await inp.fill("")
                await inp.type(val, delay=30)
                await asyncio.sleep(0.2)
                if await inp.input_value():
                    return True
    except Exception:
        pass

    return False


async def _wd_my_information(page: Page, profile: dict, email: str, resume: Optional[Path]) -> None:
    """Fill Workday 'My Information' step. Works for both guest and authenticated flows."""
    await _wd_upload_resume(page, resume)

    # Wait for the form to actually be rendered before trying to fill
    try:
        await page.wait_for_selector(
            "[data-automation-id='formField-legalName--firstName'], "
            "[data-automation-id='formField-firstName']",
            timeout=10000
        )
    except Exception:
        pass
    await asyncio.sleep(1)

    name_parts = profile.get("name", "").split(None, 1)
    first = name_parts[0] if name_parts else ""
    last  = name_parts[1] if len(name_parts) > 1 else ""
    loc   = profile.get("location", "")
    city  = loc.split(",")[0].strip() if loc else ""

    field_map = [
        ("legalName--firstName",  first),
        ("legalName--lastName",   last),
        ("firstName",             first),   # some portals use shorter aid
        ("lastName",              last),
        ("addressLine1",          profile.get("address_line1", "")),
        ("city",                  city),
        ("postalCode",            profile.get("postal_code", "")),
        ("emailAddress",          email),
        ("phoneNumber",           profile.get("phone", "")),
    ]
    filled = set()
    for aid, val in field_map:
        if not val or aid in filled:
            continue
        ok = await _wd_fill_input(page, aid, val)
        if ok:
            filled.add(aid)
            # Skip the short-form aid if long-form already worked
            if aid == "legalName--firstName":
                filled.add("firstName")
            if aid == "legalName--lastName":
                filled.add("lastName")

    # "Have you been employed here before?" → No
    try:
        no_radio = page.locator(
            "[data-automation-id='formField-candidateIsPreviousWorker'] input[value='false']"
        ).first
        if await no_radio.count() > 0 and not await no_radio.is_checked():
            await no_radio.click()
    except Exception:
        pass

    # "How Did You Hear About Us?" multiselect
    # Strategy: try multiple ways to open the dropdown, then select first non-phone option
    try:
        source_field = page.locator("[data-automation-id='formField-source']").first
        if await source_field.count() > 0 and await source_field.is_visible(timeout=2000):

            def _dropdown_open():
                return page.locator("[data-automation-id='promptOption'], [role='listbox'] [role='option']")

            opened = False

            # Attempt 1: JavaScript click on the ☰ button inside formField-source
            try:
                await page.evaluate("""
                    const field = document.querySelector("[data-automation-id='formField-source']");
                    if (field) {
                        const btn = field.querySelector("button") || field.querySelector("input") || field;
                        btn.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                        btn.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true}));
                        btn.click();
                    }
                """)
                await asyncio.sleep(1.2)
                if await _dropdown_open().count() > 0:
                    opened = True
            except Exception:
                pass

            # Attempt 2: Playwright force-click on multiselectInputContainer
            if not opened:
                try:
                    trigger = source_field.locator("[data-automation-id='multiselectInputContainer']").first
                    if await trigger.count() > 0:
                        await trigger.click(force=True)
                        await asyncio.sleep(1.2)
                        if await _dropdown_open().count() > 0:
                            opened = True
                except Exception:
                    pass

            # Attempt 3: force-click the button inside the field
            if not opened:
                try:
                    btn = source_field.locator("button").first
                    if await btn.count() > 0:
                        await btn.click(force=True)
                        await asyncio.sleep(1.2)
                        if await _dropdown_open().count() > 0:
                            opened = True
                except Exception:
                    pass

            # Attempt 4: Tab to field + Space to open
            if not opened:
                try:
                    await source_field.press("Space")
                    await asyncio.sleep(1.2)
                    if await _dropdown_open().count() > 0:
                        opened = True
                except Exception:
                    pass

            # Debug snapshot
            try:
                ss2 = Path("/tmp/wd_source_debug.png")
                await page.screenshot(path=str(ss2))
                print(f"          [Source] opened={opened}, screenshot: {ss2}", flush=True)
            except Exception:
                pass

            # Adobe's source dropdown is a TWO-LEVEL hierarchical menu.
            # Level 1: categories ("Job Board", "Social Media", "Adobe Source", ...)
            # Level 2: sub-options within that category
            # Strategy: click first visible category, then click first sub-option.

            # Step 1 — click a category (prefer "Job Board", fall back to any visible li)
            selected_category = False
            for category_text in ("Job Board", "Social Media", "Adobe Source",
                                   "External Organizations", "Through my University"):
                try:
                    cat = page.get_by_text(category_text).first
                    if await cat.count() > 0 and await cat.is_visible(timeout=500):
                        await cat.click()
                        await asyncio.sleep(1)
                        selected_category = True
                        break
                except Exception:
                    pass

            if not selected_category:
                # Fall back: click the first visible li in the page that looks like a menu item
                try:
                    for li in await page.locator("li").all():
                        if await li.is_visible(timeout=0):
                            txt = (await li.inner_text()).strip()
                            if txt and "+" not in txt and len(txt) > 3:
                                await li.click()
                                await asyncio.sleep(1)
                                selected_category = True
                                break
                except Exception:
                    pass

            # Step 2 — pick the first sub-option from whatever sub-menu appeared
            try:
                for opt_sel in (
                    "[data-automation-id='promptOption']",
                    "[role='option']",
                    "li",
                ):
                    opts = await page.locator(opt_sel).all()
                    for opt in opts:
                        try:
                            if not await opt.is_visible(timeout=0):
                                continue
                            text = (await opt.inner_text()).strip()
                            if text and "+" not in text and len(text) > 3:
                                await opt.click()
                                await asyncio.sleep(0.4)
                                raise StopIteration
                        except StopIteration:
                            raise
                        except Exception:
                            continue
            except StopIteration:
                pass
            except Exception:
                pass
    except Exception:
        pass

    # State/region dropdown
    if "," in loc:
        state = loc.split(",")[1].strip()
        await _wd_dropdown(page, "countryRegion", state)

    await _wd_next(page)


async def _wd_my_experience(page: Page, profile: dict, resume: Optional[Path]) -> None:
    """Fill Workday 'My Experience' step — work history + education + resume upload."""
    # Upload resume first (early attempt), then retry at end after field fills
    await _wd_upload_resume(page, resume)
    await asyncio.sleep(0.5)


    title   = profile.get("current_title", "Software Engineer")
    company = profile.get("current_company", "Self-employed")
    school  = profile.get("school", "")
    degree  = profile.get("degree", "")
    grad_yr = profile.get("graduation_year", "2024")
    start_yr = profile.get("work_start_year", "2020")

    # --- Work Experience fields ---
    # Wait for the Work Experience section to render
    try:
        await page.wait_for_selector("[data-automation-id='formField-jobTitle']", timeout=5000)
    except Exception:
        await asyncio.sleep(1.5)

    # Job Title
    await _wd_fill_input(page, "jobTitle", title)
    # Company name — automation-id varies by portal
    for aid in ("company", "employer", "companyName", "organizationName"):
        if await _wd_fill_input(page, aid, company):
            break

    await asyncio.sleep(0.8)  # let React re-render after text inputs

    # "Currently work here" checkbox — force=True because Workday styles the real input hidden
    try:
        chk = page.locator("[data-automation-id='formField-currentlyWorkHere'] input[type='checkbox']").first
        if await chk.count() > 0 and not await chk.is_checked():
            await chk.click(force=True)
            await asyncio.sleep(0.5)
    except Exception:
        pass

    # Workday date pickers split into separate month and year inputs.
    # automation-id = 'dateSectionMonth-input' and 'dateSectionYear-input'
    # DOM order with checkbox checked:   [WE-From-mo, WE-From-yr,  Edu-From-yr, Edu-To-yr]
    # DOM order with checkbox unchecked: [WE-From-mo, WE-To-mo,   WE-From-yr,  WE-To-yr, Edu-From-yr, Edu-To-yr]
    cur_yr = str(datetime.now().year)
    try:
        month_inps = await page.locator("[data-automation-id='dateSectionMonth-input']").all()
        year_inps  = await page.locator("[data-automation-id='dateSectionYear-input']").all()
        print(f"          [Exp] date inputs: {len(month_inps)} month, {len(year_inps)} year", flush=True)

        # Work Experience From (always at index 0)
        if len(month_inps) > 0:
            await month_inps[0].fill("01")
        if len(year_inps) > 0:
            await year_inps[0].fill(start_yr)

        if len(month_inps) > 1:
            # "I currently work here" not effective — To field still visible
            await month_inps[1].fill("01")
            if len(year_inps) > 1:
                await year_inps[1].fill(cur_yr)
            edu_yr_i = 2
        else:
            edu_yr_i = 1

        # Education: From (start) and To (graduation)
        edu_from = str(int(grad_yr) - 2)
        if edu_yr_i < len(year_inps):
            await year_inps[edu_yr_i].fill(edu_from)
        if edu_yr_i + 1 < len(year_inps):
            await year_inps[edu_yr_i + 1].fill(grad_yr)
    except Exception as _e:
        print(f"          [Exp] Date fill error: {_e}", flush=True)

    # --- Education fields ---
    # School name — typeahead searchable field; force-click to avoid is_visible issues
    try:
        for sch_aid in ("schoolName", "school", "university"):
            container = page.locator(f"[data-automation-id='formField-{sch_aid}']").first
            if await container.count() == 0:
                continue
            inp = container.locator("input").first
            if await inp.count() == 0:
                continue
            await inp.click(force=True)
            await asyncio.sleep(0.2)
            await inp.fill(school)
            await asyncio.sleep(1.0)
            # Pick first suggestion if typeahead opens
            opt = page.locator("[data-automation-id='promptOption']").first
            if await opt.count() > 0:
                try:
                    await opt.click(timeout=1500)
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
            break
    except Exception:
        pass

    # Degree — 4 strategies tried in order:
    _deg_filled = False
    _deg_kw = "master" if "master" in degree.lower() else degree.lower().split()[0]

    # One-shot DOM inspection to understand the degree field structure
    try:
        _deg_dbg = await page.evaluate("""
            () => {
                const c = document.querySelector("[data-automation-id='formField-degree']");
                if (!c) return {error: 'container not found'};
                return {
                    selects:  c.querySelectorAll('select').length,
                    inputs:   c.querySelectorAll('input').length,
                    buttons:  c.querySelectorAll('button').length,
                    roles:    [...c.querySelectorAll('[role]')].map(e=>e.getAttribute('role')+':'+e.getAttribute('data-automation-id')),
                    html:     c.innerHTML.substring(0, 400)
                };
            }
        """)
        print(f"          [Deg] DOM: selects={_deg_dbg.get('selects')} inputs={_deg_dbg.get('inputs')} buttons={_deg_dbg.get('buttons')} roles={_deg_dbg.get('roles')}", flush=True)
        print(f"          [Deg] HTML: {_deg_dbg.get('html','')[:200]}", flush=True)
    except Exception as _de:
        print(f"          [Deg] debug error: {_de}", flush=True)

    # Strategy 1: click the button trigger (force=True), then pick matching option
    if not _deg_filled:
        try:
            container = page.locator("[data-automation-id='formField-degree']").first
            btn = container.locator("button").first
            if await btn.count() > 0:
                await btn.click(force=True)
                await asyncio.sleep(0.8)
                for opt_sel in (
                    "[data-automation-id='promptOption']",
                    "[role='option']",
                    "[role='listbox'] li",
                    "li[tabindex]",
                ):
                    opts = await page.locator(opt_sel).all()
                    for opt in opts:
                        try:
                            txt = (await opt.inner_text()).strip().lower()
                            if _deg_kw in txt:
                                await opt.click(force=True)
                                _deg_filled = True
                                print(f"          [Exp] Degree S1 via '{opt_sel}': {txt}", flush=True)
                                break
                        except Exception:
                            pass
                    if _deg_filled:
                        break
                if not _deg_filled:
                    await page.keyboard.press("Escape")
        except Exception as _e:
            print(f"          [Exp] Degree S1 error: {_e}", flush=True)

    # Strategy 2: click the input (force), type keyword, pick any dropdown option
    if not _deg_filled:
        try:
            container = page.locator("[data-automation-id='formField-degree']").first
            inp = container.locator("input").first
            if await inp.count() > 0:
                await inp.click(force=True)
                await asyncio.sleep(0.3)
                await inp.fill("")
                await inp.type(_deg_kw[:6], delay=50)
                await asyncio.sleep(1.2)
                for opt_sel in (
                    "[data-automation-id='promptOption']",
                    "[role='option']",
                    "[role='listbox'] li",
                    "li[tabindex]",
                ):
                    opt = page.locator(opt_sel).first
                    if await opt.count() > 0:
                        await opt.click(force=True)
                        _deg_filled = True
                        print(f"          [Exp] Degree S2 typeahead via '{opt_sel}'", flush=True)
                        break
                if not _deg_filled:
                    await page.keyboard.press("Escape")
        except Exception as _e:
            print(f"          [Exp] Degree S2 error: {_e}", flush=True)

    # Strategy 3: keyboard navigation — Down arrow to first option, Enter
    if not _deg_filled:
        try:
            container = page.locator("[data-automation-id='formField-degree']").first
            btn = container.locator("button").first
            if await btn.count() == 0:
                btn = container.locator("input").first
            if await btn.count() > 0:
                await btn.click(force=True)
                await asyncio.sleep(0.5)
                await page.keyboard.press("ArrowDown")
                await asyncio.sleep(0.3)
                # Navigate to Master option (usually around index 4-6 in degree lists)
                for _ in range(6):
                    focused = await page.evaluate("document.activeElement?.textContent")
                    if focused and _deg_kw in focused.lower():
                        await page.keyboard.press("Enter")
                        _deg_filled = True
                        print(f"          [Exp] Degree S3 keyboard: {focused}", flush=True)
                        break
                    await page.keyboard.press("ArrowDown")
                    await asyncio.sleep(0.15)
                if not _deg_filled:
                    await page.keyboard.press("Escape")
        except Exception as _e:
            print(f"          [Exp] Degree S3 error: {_e}", flush=True)

    # Strategy 4: JS — find any visible list items after clicking container and pick matching
    if not _deg_filled:
        try:
            container = page.locator("[data-automation-id='formField-degree']").first
            await container.click(force=True)
            await asyncio.sleep(0.8)
            result = await page.evaluate(f"""
                () => {{
                    const kw = '{_deg_kw}';
                    const candidates = [...document.querySelectorAll('li, [role="option"]')];
                    const match = candidates.find(el => el.textContent.toLowerCase().includes(kw)
                                                     && el.offsetParent !== null);
                    if (match) {{ match.click(); return match.textContent.trim(); }}
                    return null;
                }}
            """)
            if result:
                _deg_filled = True
                print(f"          [Exp] Degree S4 JS click: {result}", flush=True)
            else:
                await page.keyboard.press("Escape")
        except Exception as _e:
            print(f"          [Exp] Degree S4 error: {_e}", flush=True)

    # LinkedIn / website
    await _wd_fill_field(page, "linkedinUrl",  profile.get("linkedin_url", ""))
    await _wd_fill_field(page, "portfolioUrl", profile.get("website_url", ""))

    # Re-upload resume after all field fills (React re-renders may reset upload state)
    await _wd_upload_resume(page, resume)
    await asyncio.sleep(2.0)  # wait for upload to process

    await _wd_next(page)


async def _wd_questions(page: Page, profile: dict, email: str) -> None:
    """Fill Workday 'Application Questions' step — dropdowns, radios, text inputs."""
    needs_sponsorship = profile.get("needs_sponsorship", False)
    open_to_relocation = profile.get("open_to_relocation", True)

    # Text/email/tel inputs
    for inp in await page.locator("input[type='text'], input[type='email'], input[type='tel']").all():
        try:
            if await inp.input_value():
                continue
            label = (
                await inp.get_attribute("data-automation-id") or
                await inp.get_attribute("aria-label") or
                await inp.get_attribute("placeholder") or ""
            )
            val = answer_for(label, profile, email)
            if val:
                await inp.fill(val)
        except Exception:
            pass

    _PLACEHOLDER_OPTS = {"select one", "-- select --", "please select", "", "select", "-", "—", "select...", "choose..."}

    # Navigation keywords — skip any element whose context is just the nav bar
    _NAV_SKIP = {"search for jobs", "candidate home", "sign in", "settings", "back to job posting"}

    def _classify(parent_text: str) -> str:
        pt = parent_text.lower()
        if any(k in pt for k in ("sponsor", "visa", "h-1b", "h1b", "employment visa")):
            return "yes" if needs_sponsorship else "no"
        if any(k in pt for k in ("worked at adobe", "worked for adobe", "capacity:",
                                  "ever work", "formerly employed", "previous adobe")):
            return "no"
        if "relocat" in pt:
            return "yes" if open_to_relocation else "no"
        return "yes"

    def _is_nav_el(parent_text: str) -> bool:
        pt = parent_text.lower()
        return sum(1 for k in _NAV_SKIP if k in pt) >= 2

    async def _pick_option(want: str) -> bool:
        want_words = (
            ("yes", "true", "i do", "i am", "i will", "i can")
            if want == "yes" else
            ("no", "never", "n/a", "not employed", "i do not", "i don't", "i have not", "i haven't", "never worked")
        )
        # Wait up to 2s for any option to appear
        try:
            await page.wait_for_selector(
                "[data-automation-id='promptOption'], [role='option'], [role='listbox'] li",
                timeout=2000
            )
        except Exception:
            pass

        for opt_sel in (
            "[data-automation-id='promptOption']",
            "[role='option']",
            "[role='listbox'] li",
            "li[tabindex]",
            "[data-automation-id='menuItem']",
        ):
            opts = await page.locator(opt_sel).all()
            best_fallback = None
            for opt in opts:
                try:
                    t = (await opt.inner_text()).strip().lower()
                    if t in _PLACEHOLDER_OPTS:
                        continue
                    if any(w in t for w in want_words):
                        await opt.click(force=True)
                        return True
                    if best_fallback is None and t:
                        best_fallback = opt
                except Exception:
                    pass
            if best_fallback:
                await best_fallback.click(force=True)
                return True

        # Check native <select> opened by the button click
        try:
            for ns in await page.locator("select:visible").all():
                options = await ns.evaluate("el => [...el.options].map(o => ({v: o.value, t: o.text.trim()}))")
                best_fb = None
                for o in options:
                    t = o.get("t", "").lower()
                    if t in _PLACEHOLDER_OPTS:
                        continue
                    if any(w in t for w in want_words):
                        await ns.select_option(value=o.get("v") or o.get("t"))
                        return True
                    if best_fb is None and t:
                        best_fb = o
                if best_fb:
                    await ns.select_option(value=best_fb.get("v") or best_fb.get("t"))
                    return True
        except Exception:
            pass

        await page.keyboard.press("Escape")
        return False

    async def _answer_from_trigger(trigger_el, want: str, label: str) -> bool:
        try:
            try:
                await trigger_el.scroll_into_view_if_needed()
            except Exception:
                pass
            await trigger_el.click(force=True)
            await asyncio.sleep(1.0)  # Give dropdown time to animate open
            picked = await _pick_option(want)
            await asyncio.sleep(0.3)
            print(f"          [Q] '{label[:70]}' → {want} ({'ok' if picked else 'fallback'})", flush=True)
            return picked
        except Exception:
            return False

    # Scroll entire page in sections to trigger lazy rendering
    await asyncio.sleep(0.4)
    try:
        for frac in (0.33, 0.66, 1.0, 0.0):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {frac})")
            await asyncio.sleep(0.35)
    except Exception:
        pass

    # Track answered bounding boxes to avoid double-answering
    answered_boxes: set = set()

    async def _box_key(el) -> str:
        try:
            bb = await el.bounding_box()
            if bb:
                return f"{int(bb['y']//10)*10}"
        except Exception:
            pass
        return ""

    # ── STRATEGY A: "Select One" text elements ────────────────────────────────
    # Finds any DOM element (div, button, span) that shows the text "Select One"
    try:
        sel_one_locs = await page.locator("text='Select One'").all()
        print(f"          [Q] StratA 'Select One' found: {len(sel_one_locs)}", flush=True)
        for el in sel_one_locs:
            try:
                parent_text = await el.evaluate("""
                    el => {
                        let p = el.parentElement;
                        for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
                            const t = p.textContent.trim();
                            if (t.length > 10 && t.length < 800) return t;
                        }
                        return '';
                    }
                """)
                if not parent_text or _is_nav_el(parent_text):
                    continue
                bk = await _box_key(el)
                if bk and bk in answered_boxes:
                    continue
                want = _classify(parent_text)
                trigger = await el.evaluate_handle("""
                    el => el.closest('button, [role="button"]') || el
                """)
                await _answer_from_trigger(trigger.as_element(), want, parent_text[:70])
                if bk:
                    answered_boxes.add(bk)
                await asyncio.sleep(0.2)
            except Exception:
                pass
    except Exception:
        pass

    # ── STRATEGY B: All formField containers — catch non-"Select One" placeholders ─
    # This finds dropdowns whose button shows "", "-", "Select...", or other text
    try:
        form_fields = await page.locator("[data-automation-id^='formField']").all()
        print(f"          [Q] StratB formFields found: {len(form_fields)}", flush=True)
        for ff in form_fields:
            try:
                # Find button/trigger inside this field
                btn = ff.locator("button, [role='button']").first
                if await btn.count() == 0:
                    continue
                btn_text = (await btn.inner_text()).strip().lower()
                # Only process if button looks unanswered
                if btn_text not in _PLACEHOLDER_OPTS and len(btn_text) > 5:
                    continue
                bk = await _box_key(btn)
                if bk and bk in answered_boxes:
                    continue
                parent_text = await ff.inner_text()
                if not parent_text or _is_nav_el(parent_text):
                    continue
                want = _classify(parent_text)
                await _answer_from_trigger(btn, want, parent_text[:70])
                if bk:
                    answered_boxes.add(bk)
                await asyncio.sleep(0.2)
            except Exception:
                pass
    except Exception:
        pass

    # ── STRATEGY C: fieldError markers already present on page ────────────────
    # Workday sometimes pre-marks required fields with error styling before submit
    try:
        error_markers = await page.locator(
            "[data-automation-id='fieldError'], [class*='error'], [aria-invalid='true']"
        ).all()
        print(f"          [Q] StratC fieldError markers: {len(error_markers)}", flush=True)
        for em in error_markers:
            try:
                bk = await _box_key(em)
                if bk and bk in answered_boxes:
                    continue
                # Walk up to find adjacent dropdown trigger
                parent_text = await em.evaluate("""
                    el => {
                        let p = el.parentElement;
                        for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
                            const btn = p.querySelector('button, [role="button"]');
                            if (btn) return { text: p.textContent.trim(), btn: btn };
                        }
                        return null;
                    }
                """)
                if not parent_text:
                    continue
                ptext = parent_text.get("text", "") if isinstance(parent_text, dict) else ""
                if _is_nav_el(ptext):
                    continue
                want = _classify(ptext)
                # Click the button found in the parent
                btn_handle = await em.evaluate_handle("""
                    el => {
                        let p = el.parentElement;
                        for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
                            const btn = p.querySelector('button, [role="button"]');
                            if (btn) return btn;
                        }
                        return null;
                    }
                """)
                if btn_handle:
                    await _answer_from_trigger(btn_handle.as_element(), want, ptext[:70])
                    if bk:
                        answered_boxes.add(bk)
            except Exception:
                pass
    except Exception:
        pass

    # ── STRATEGY D: JS text-node scan + full DOM dump ─────────────────────────
    # Find questions whose label text doesn't surface via Playwright text locators
    # (e.g., text split across nodes, inside shadow DOM, or inside table cells)
    try:
        dom_info = await page.evaluate("""
            () => {
                const cap_nodes = [];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const t = node.textContent.trim();
                    if (t.length < 4 || t.length > 400) continue;
                    const tl = t.toLowerCase();
                    if (tl.includes('ever work') || tl.includes('capacity') ||
                        tl.includes('formerly') || tl.includes('adobe employee') ||
                        tl.includes('previously work')) {
                        const p = node.parentElement || {};
                        cap_nodes.push({
                            text: t.substring(0, 120),
                            tag: p.tagName,
                            aid: p.getAttribute && p.getAttribute('data-automation-id'),
                            role: p.getAttribute && p.getAttribute('role')
                        });
                    }
                }
                // All interactive elements for diagnostic
                const ixs = [...document.querySelectorAll(
                    'button, [role="button"], [role="combobox"], [role="option"], select, input[type="radio"]'
                )].map(el => ({
                    tag: el.tagName,
                    text: (el.textContent || '').trim().substring(0, 60),
                    aid: el.getAttribute('data-automation-id'),
                    role: el.getAttribute('role')
                }));
                return { cap_nodes, ixs };
            }
        """)
        cap_nodes = dom_info.get("cap_nodes", []) if isinstance(dom_info, dict) else []
        ixs = dom_info.get("ixs", []) if isinstance(dom_info, dict) else []
        print(f"          [Q] StratD cap_nodes: {cap_nodes}", flush=True)
        print(f"          [Q] StratD interactives: {ixs}", flush=True)

        # Try to answer any capacity-related nodes we found
        for cn in cap_nodes:
            aid = cn.get("aid") or ""
            tag = cn.get("tag") or ""
            if not aid:
                continue
            # Click the element's nearest button
            try:
                el = page.locator(f"[data-automation-id='{aid}']").first
                if await el.count() == 0:
                    continue
                btn_h = await el.evaluate_handle("""
                    el => {
                        const btn = el.querySelector('button, [role="button"]') ||
                                    el.closest('[data-automation-id^="formField"]')?.querySelector('button, [role="button"]');
                        return btn || null;
                    }
                """)
                if btn_h and btn_h.as_element():
                    await _answer_from_trigger(btn_h.as_element(), "no", cn.get("text", "")[:70])
            except Exception:
                pass
    except Exception:
        pass

    # ── STRATEGY E: Checkbox groups ─────────────────────────────────────────────
    # "Have you ever worked at Adobe?" is a checkbox list — need to tick "I have not worked..."
    try:
        # Diagnostic: dump all checkbox-related elements
        cb_info = await page.evaluate("""
            () => {
                const labels = [...document.querySelectorAll('label')].map(l => ({
                    text: l.textContent.trim().substring(0, 80),
                    for_attr: l.getAttribute('for')
                }));
                const role_cb = [...document.querySelectorAll('[role="checkbox"]')].map(el => ({
                    text: el.textContent.trim().substring(0, 80),
                    aria_checked: el.getAttribute('aria-checked'),
                    aid: el.getAttribute('data-automation-id')
                }));
                const inp_cb = [...document.querySelectorAll('input[type="checkbox"]')].map(el => ({
                    id: el.id, checked: el.checked,
                    aid: el.getAttribute('data-automation-id'),
                    label: document.querySelector('label[for="'+el.id+'"]')?.textContent?.trim()?.substring(0,80)
                }));
                return {labels, role_cb, inp_cb};
            }
        """)
        print(f"          [Q] StratE labels: {cb_info.get('labels', [])}", flush=True)
        print(f"          [Q] StratE role_cb: {cb_info.get('role_cb', [])}", flush=True)
        print(f"          [Q] StratE inp_cb: {cb_info.get('inp_cb', [])}", flush=True)

        _never_keywords = ("have not worked", "not worked for adobe", "never worked",
                           "none of the above", "have not previously", "no, never")

        # Try input[type='checkbox'] with matching label
        for cb_data in (cb_info.get("inp_cb") or []):
            lbl_txt = (cb_data.get("label") or "").lower()
            if any(k in lbl_txt for k in _never_keywords):
                cb_id = cb_data.get("id") or ""
                # Use [id='...'] not #id — IDs starting with digits aren't valid CSS selectors
                cb_el = page.locator(f"[id='{cb_id}']").first if cb_id else None
                if cb_el and await cb_el.count() > 0:
                    if not cb_data.get("checked"):
                        await cb_el.click(force=True)
                    print(f"          [Q] StratE inp_cb '{lbl_txt[:60]}' → checked", flush=True)

        # Try role='checkbox' elements
        for rc in (cb_info.get("role_cb") or []):
            txt = (rc.get("text") or "").lower()
            if any(k in txt for k in _never_keywords):
                el_aid = rc.get("aid") or ""
                rc_el = page.locator(f"[data-automation-id='{el_aid}']").first if el_aid else None
                if not rc_el or await rc_el.count() == 0:
                    # Try by text
                    rc_el = page.locator(f"[role='checkbox']:has-text('{txt[:30]}')").first
                if rc_el and await rc_el.count() > 0:
                    if rc.get("aria_checked") != "true":
                        await rc_el.click(force=True)
                    print(f"          [Q] StratE role_cb '{txt[:60]}' → clicked", flush=True)

        # Try <label> elements
        all_labels = await page.locator("label").all()
        for lbl in all_labels:
            try:
                ltext = (await lbl.inner_text()).strip().lower()
                if any(k in ltext for k in _never_keywords):
                    lbl_for = await lbl.get_attribute("for") or ""
                    if lbl_for:
                        # Use [id='...'] not #id — IDs starting with digits aren't valid CSS selectors
                        cb = page.locator(f"[id='{lbl_for}']").first
                        if await cb.count() > 0 and not await cb.is_checked():
                            await cb.click(force=True)
                        print(f"          [Q] StratE lbl '{ltext[:60]}' → checked", flush=True)
                    else:
                        await lbl.click(force=True)
                        print(f"          [Q] StratE lbl-click '{ltext[:60]}'", flush=True)
            except Exception:
                pass

        # Last resort: find by text content of any clickable element
        never_els = await page.locator("text=/have not worked/i, text=/not worked for Adobe/i, text=/never worked/i").all()
        for nel in never_els:
            try:
                bk = await _box_key(nel)
                if bk and bk in answered_boxes:
                    continue
                parent = await nel.evaluate_handle("el => el.closest('input[type=\"checkbox\"], [role=\"checkbox\"]') || el.parentElement")
                await parent.as_element().click(force=True)
                print(f"          [Q] StratE text-click '(never worked element)' → clicked", flush=True)
                if bk:
                    answered_boxes.add(bk)
            except Exception:
                pass
    except Exception as e:
        _es = str(e)
        if "Target crashed" in _es or "Target page, context or browser has been closed" in _es:
            raise  # browser is dead — propagate to step runner
        print(f"          [Q] StratE error: {e}", flush=True)

    # ── STRATEGY F: Native <select> elements ─────────────────────────────────
    try:
        native_sels = await page.locator("select").all()
        for ns in native_sels:
            try:
                parent_text = await ns.evaluate("el => el.closest('[data-automation-id^=\"formField\"]')?.textContent || el.parentElement?.textContent || ''")
                if _is_nav_el(parent_text):
                    continue
                bk = await _box_key(ns)
                if bk and bk in answered_boxes:
                    continue
                want = _classify(parent_text)
                options = await ns.evaluate("el => [...el.options].map(o => ({v: o.value, t: o.text.trim()}))")
                want_words = (
                    ("yes", "true", "i do", "i am", "i will", "i can")
                    if want == "yes" else
                    ("no", "never", "n/a", "not", "i do not", "i have not")
                )
                chosen = None
                for o in options:
                    t = o.get("t", "").lower()
                    if t in _PLACEHOLDER_OPTS:
                        continue
                    if any(w in t for w in want_words):
                        chosen = o.get("v") or o.get("t")
                        break
                if not chosen:
                    # Fallback: first non-placeholder option
                    for o in options:
                        t = o.get("t", "").lower()
                        if t not in _PLACEHOLDER_OPTS and t:
                            chosen = o.get("v") or o.get("t")
                            break
                if chosen:
                    await ns.select_option(value=chosen)
                    print(f"          [Q] StratF native select '{parent_text[:60]}' → '{chosen[:30]}'", flush=True)
                    if bk:
                        answered_boxes.add(bk)
            except Exception:
                pass
    except Exception:
        pass

    # ── Radio buttons fallback ─────────────────────────────────────────────────
    try:
        radios = await page.locator("input[type='radio']").all()
        for radio in radios:
            try:
                if await radio.is_checked():
                    continue
                rid = await radio.get_attribute("id") or ""
                lbl = page.locator(f"label[for='{rid}']").first if rid else None
                lbl_text = (await lbl.inner_text()).lower() if lbl and await lbl.count() > 0 else ""
                if "yes" in lbl_text:
                    await radio.click(force=True)
            except Exception:
                pass
    except Exception:
        pass

    # Named dropdowns (work auth, etc.)
    for sel_id in ("workAuth", "country", "legalName"):
        val = answer_for(sel_id, profile, email)
        if val:
            await _wd_dropdown(page, sel_id, val)

    # ── Post-save error retry (up to 2 rounds) ─────────────────────────────────
    # Click Save and Continue; if Workday highlights error fields, answer them and retry.
    for _retry in range(3):
        await _wd_next(page)
        await asyncio.sleep(1.0)

        # Check if we're still on Application Questions (Save and Continue button still present)
        save_btn = page.locator(
            "[data-automation-id='pageFooterNextButton'], [data-automation-id='saveAndContinueButton']"
        ).first
        if await save_btn.count() == 0:
            break  # Navigated away — success

        # Still on same step — find inline error fields and answer them
        print(f"          [Q] retry {_retry+1}: still on step — scanning error fields", flush=True)
        found_error = False

        # Error approach 1: [data-automation-id='fieldError'] siblings
        err_els = await page.locator("[data-automation-id='fieldError']").all()
        for ee in err_els:
            try:
                btn_h = await ee.evaluate_handle("""
                    el => {
                        let p = el.parentElement;
                        for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
                            const btn = p.querySelector('button, [role="button"]');
                            if (btn) return btn;
                        }
                        return null;
                    }
                """)
                if btn_h and btn_h.as_element():
                    ptext = await ee.evaluate("el => el.closest('[data-automation-id^=\"formField\"]')?.textContent || el.parentElement?.textContent || ''")
                    want = _classify(ptext or "")
                    await _answer_from_trigger(btn_h.as_element(), want, f"error-field: {(ptext or '')[:60]}")
                    found_error = True
            except Exception:
                pass

        # Error approach 2: buttons showing "Select One" that appeared post-click
        post_sel = await page.locator("text='Select One'").all()
        for el in post_sel:
            try:
                bk = await _box_key(el)
                if bk and bk in answered_boxes:
                    continue
                parent_text = await el.evaluate("""
                    el => {
                        let p = el.parentElement;
                        for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
                            const t = p.textContent.trim();
                            if (t.length > 10 && t.length < 800) return t;
                        }
                        return '';
                    }
                """)
                if not parent_text or _is_nav_el(parent_text):
                    continue
                want = _classify(parent_text)
                trigger = await el.evaluate_handle("el => el.closest('button,[role=\"button\"]') || el")
                await _answer_from_trigger(trigger.as_element(), want, parent_text[:70])
                if bk:
                    answered_boxes.add(bk)
                found_error = True
            except Exception:
                pass

        # Error approach 3: aria-invalid elements — walk up to find the button trigger
        invalid_els = await page.locator("[aria-invalid='true']").all()
        for ie in invalid_els:
            try:
                bk = await _box_key(ie)
                if bk and bk in answered_boxes:
                    continue
                ptext = await ie.evaluate("el => el.closest('[data-automation-id^=\"formField\"]')?.textContent || el.parentElement?.textContent || ''")
                want = _classify(ptext or "")
                # Find the button trigger from the formField container, not click the invalid el directly
                btn_h = await ie.evaluate_handle("""
                    el => {
                        let p = el.parentElement;
                        for (let i = 0; i < 15 && p; i++, p = p.parentElement) {
                            const btn = p.querySelector('button, [role="button"]');
                            if (btn && btn !== el) return btn;
                        }
                        return el.closest('button,[role="button"]') || el;
                    }
                """)
                await _answer_from_trigger(btn_h.as_element(), want, f"aria-invalid: {(ptext or '')[:60]}")
                if bk:
                    answered_boxes.add(bk)
                found_error = True
            except Exception:
                pass

        if not found_error:
            # No more error fields detected — click one more time and exit
            await _wd_next(page)
            break
    else:
        # Retry loop exhausted — try one final Next to advance regardless
        await _wd_next(page)
        await asyncio.sleep(1.5)


async def _wd_voluntary(page: Page, profile: dict = None) -> None:
    """Fill Workday 'Voluntary Disclosures' step (EEO, gender, veteran, disability)."""
    _prof = profile or {}
    _gender_val = (_prof.get("gender") or "Male").strip().lower()       # "male" or "female"
    _race_val   = (_prof.get("race") or "Asian").strip().lower()        # "asian", "white", etc.

    # Keywords for each field type — order matters (most specific first)
    _VETERAN_KWS  = ("not a veteran", "i am not a veteran", "i have not served",
                     "not protected", "no, i am not", "none of the above")
    _MALE_KWS     = ("male",)          # avoid "female" — filtered below
    _FEMALE_KWS   = ("female",)
    _ASIAN_KWS    = ("asian",)
    _WHITE_KWS    = ("white", "caucasian")
    _HISP_KWS     = ("hispanic", "latino")
    _BLACK_KWS    = ("black", "african american")
    _DECLINE_KWS  = ("decline", "prefer not", "i don't wish", "i do not wish",
                     "choose not", "i prefer not", "not wish")

    def _gender_kws():
        return _MALE_KWS if _gender_val == "male" else _FEMALE_KWS

    def _race_kws():
        rv = _race_val
        if "asian" in rv:        return _ASIAN_KWS
        if "white" in rv or "caucasian" in rv: return _WHITE_KWS
        if "hispanic" in rv or "latino" in rv: return _HISP_KWS
        if "black" in rv or "african" in rv:   return _BLACK_KWS
        return _DECLINE_KWS

    _needs_sponsorship = bool(_prof.get("needs_sponsorship", True))

    def _field_kws(field_text: str):
        ft = field_text.lower()
        if any(k in ft for k in ("veteran", "military", "armed")):  return _VETERAN_KWS
        if "gender" in ft:                                           return _gender_kws()
        if any(k in ft for k in ("ethnicity", "race", "racial")):   return _race_kws()
        if any(k in ft for k in ("disability", "disabled")):
            return ("no, i don't", "no disability", "i do not have", "i choose not",
                    "i do not wish", "prefer not", "decline", "i am not")
        # AQ-type questions that may appear here due to step cascading
        if any(k in ft for k in ("background check", "background screen", "background verif")):
            return ("yes", "i agree", "agree", "consent")
        if any(k in ft for k in ("sponsor", "visa", "h-1b", "h1b", "employment visa")):
            return ("yes",) if _needs_sponsorship else ("no", "will not", "do not")
        if any(k in ft for k in ("legal age", "of legal age", "eligible to work", "authorized to work")):
            return ("yes", "i am", "i do")
        if any(k in ft for k in ("documentation", "authorization", "work permit")):
            return ("yes", "i can", "i will", "i am able")
        if any(k in ft for k in ("work location", "work on a daily", "on site", "onsite")):
            return ("yes", "i am", "i will", "i can")
        return _DECLINE_KWS

    async def _pick_for_field(field_text: str) -> bool:
        want_kws = _field_kws(field_text)
        for opt_sel in ("[data-automation-id='promptOption']", "[role='option']", "li[tabindex]"):
            opts = await page.locator(opt_sel).all()
            first_real = None
            for opt in opts:
                try:
                    t = (await opt.inner_text()).strip().lower()
                    if not t:
                        continue
                    # For gender=male: skip "female" option
                    if _gender_val == "male" and "female" in t and "gender" in field_text.lower():
                        continue
                    if any(w in t for w in want_kws):
                        await opt.click(force=True)
                        await asyncio.sleep(0.3)
                        return True
                    if first_real is None:
                        first_real = opt
                except Exception:
                    pass
            # No keyword match — use first real option as last resort
            if first_real:
                await first_real.click(force=True)
                await asyncio.sleep(0.3)
                return True
        await page.keyboard.press("Escape")
        return False

    # Diagnostic: dump all buttons + form fields on this page
    try:
        vd_info = await page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button, [role="button"]')]
                    .map(b => ({text: b.textContent.trim().substring(0,60), aid: b.getAttribute('data-automation-id')}));
                const ffs = [...document.querySelectorAll('[data-automation-id^="formField"]')]
                    .map(ff => ({
                        aid: ff.getAttribute('data-automation-id'),
                        btn: ff.querySelector('button')?.textContent?.trim()?.substring(0,40),
                        text: ff.textContent?.trim()?.substring(0,80)
                    }));
                return {btns, ffs};
            }
        """)
        if isinstance(vd_info, dict):
            print(f"          [VD] buttons: {vd_info.get('btns', [])}", flush=True)
            print(f"          [VD] formFields: {vd_info.get('ffs', [])}", flush=True)
    except Exception:
        pass

    # Strategy 1: explicit dropdown via known formField automation IDs
    _field_map = {
        "veteranStatus":        (_VETERAN_KWS,  None),
        "protectedVeteranStatus": (_VETERAN_KWS, None),
        "gender":               (_gender_kws(), "female" if _gender_val == "male" else None),
        "ethnicity":            (_race_kws(),   None),
        "race":                 (_race_kws(),   None),
        "disabilityStatus":     (("no, i don't", "no disability", "i do not have",
                                   "i choose not", "i do not wish", "prefer not", "decline"), None),
        "hispanicOrLatino":     (_DECLINE_KWS,  None),
    }
    for aid, (kws, skip_kw) in _field_map.items():
        ff = page.locator(f"[data-automation-id='formField-{aid}']").first
        if await ff.count() == 0:
            print(f"          [VD] Strat1 {aid}: formField not found on page — skipping", flush=True)
            continue
        btn = ff.locator("button").first
        if await btn.count() == 0:
            print(f"          [VD] Strat1 {aid}: formField found but no button inside — skipping", flush=True)
            continue
        cur_text = (await btn.inner_text()).strip().lower()
        print(f"          [VD] Strat1 {aid}: current value='{cur_text[:60]}' | want={kws} | skip='{skip_kw}'", flush=True)
        # Already set correctly
        if any(w in cur_text for w in kws) and (skip_kw is None or skip_kw not in cur_text):
            print(f"          [VD] Strat1 {aid}: ✓ already correct — not changing", flush=True)
            continue
        print(f"          [VD] Strat1 {aid}: clicking dropdown button...", flush=True)
        try:
            await btn.scroll_into_view_if_needed()
        except Exception:
            pass
        await btn.click(force=True)

        # After clicking, use wait_for_selector to catch options as soon as they appear
        # (polling every 0.5s misses fast-appearing dropdowns)
        _option_sels = [
            "[data-automation-id='promptOption']",
            "[role='option']",
            "li[tabindex]",
            "[role='listbox'] li",
            "div[role='option']",
            "[data-automation-id='dropdownOption']",
        ]
        _pw_opts = []
        _used_opt_sel = None
        _cnt = 0

        # Strategy A: wait_for_selector (reacts as soon as element appears, up to 4s)
        for _opt_sel in _option_sels:
            try:
                await page.wait_for_selector(_opt_sel, timeout=4000, state="visible")
                _loc = page.locator(_opt_sel)
                _cnt = await _loc.count()
                if _cnt > 0:
                    _pw_opts = await _loc.all()
                    _used_opt_sel = _opt_sel
                    print(f"          [VD] Strat1 {aid}:   wait_for_selector '{_opt_sel}' → {_cnt} elements", flush=True)
                    break
            except Exception:
                pass  # timeout — try next selector

        # Strategy B: if still 0, try clicking the visible button of the field (not force)
        if not _pw_opts:
            print(f"          [VD] Strat1 {aid}:   no options via wait_for_selector — retrying with normal click", flush=True)
            try:
                await btn.click()  # normal click (not force) — different event sequence
                for _opt_sel in _option_sels:
                    try:
                        await page.wait_for_selector(_opt_sel, timeout=3000, state="visible")
                        _loc = page.locator(_opt_sel)
                        _cnt = await _loc.count()
                        if _cnt > 0:
                            _pw_opts = await _loc.all()
                            _used_opt_sel = _opt_sel
                            print(f"          [VD] Strat1 {aid}:   normal click → '{_opt_sel}' {_cnt} opts", flush=True)
                            break
                    except Exception:
                        pass
            except Exception as _nc_e:
                print(f"          [VD] Strat1 {aid}:   normal click error: {_nc_e}", flush=True)

        # Log all selectors for debugging when options are still missing
        if not _pw_opts:
            print(f"          [VD] Strat1 {aid}: ✗ 0 options after all wait attempts — current counts:", flush=True)
            for _opt_sel in _option_sels:
                try:
                    _c = await page.locator(_opt_sel).count()
                    print(f"          [VD] Strat1 {aid}:   '{_opt_sel}' → {_c}", flush=True)
                except Exception:
                    pass
            await page.keyboard.press("Escape")  # close any open dropdown
        else:
            print(f"          [VD] Strat1 {aid}: using selector '{_used_opt_sel}' with {len(_pw_opts)} options", flush=True)

        picked = False
        first_real = None
        for _i, _opt in enumerate(_pw_opts):
            try:
                t = (await _opt.inner_text()).strip().lower()
                print(f"          [VD] Strat1 {aid}:   option[{_i}]='{t[:70]}' | skip_kw='{skip_kw}' | want={kws}", flush=True)
                if not t or t in ("select one", "-- select --"):
                    print(f"          [VD] Strat1 {aid}:   → skipping placeholder", flush=True)
                    continue
                if skip_kw and skip_kw in t:
                    print(f"          [VD] Strat1 {aid}:   → skipping (contains skip_kw '{skip_kw}')", flush=True)
                    continue
                if first_real is None:
                    first_real = _opt
                # Exact-word match to avoid "male" hitting inside "female"
                exact = any(
                    t == w or t.startswith(w + " ") or t.endswith(" " + w) or f" {w} " in f" {t} "
                    for w in kws
                )
                kw_match = any(w in t for w in kws)
                if exact or kw_match:
                    print(f"          [VD] Strat1 {aid}:   → MATCH (exact={exact} kw={kw_match}) clicking '{t[:60]}'", flush=True)
                    await _opt.click(force=True)
                    picked = True
                    break
                else:
                    print(f"          [VD] Strat1 {aid}:   → no keyword match", flush=True)
            except Exception as _oe:
                print(f"          [VD] Strat1 {aid}:   option[{_i}] error: {_oe}", flush=True)

        if not picked and first_real is not None:
            _fr_text = (await first_real.inner_text()).strip()
            print(f"          [VD] Strat1 {aid}: no keyword match — falling back to first real option: '{_fr_text[:60]}'", flush=True)
            try:
                await first_real.click(force=True)
                picked = True
            except Exception as _fe:
                print(f"          [VD] Strat1 {aid}: fallback click error: {_fe}", flush=True)

        if not picked and not _pw_opts:
            # Last resort: keyboard navigation — type the value and press Enter
            print(f"          [VD] Strat1 {aid}: no options found — trying keyboard type '{kws[0]}'", flush=True)
            try:
                await page.keyboard.type(kws[0].title(), delay=50)
                await asyncio.sleep(0.4)
                _kb_opts = await page.locator("[role='option']").all()
                print(f"          [VD] Strat1 {aid}: keyboard → {len(_kb_opts)} [role=option] appeared", flush=True)
                if _kb_opts:
                    _kb_t = (await _kb_opts[0].inner_text()).strip()
                    print(f"          [VD] Strat1 {aid}: clicking keyboard option[0]='{_kb_t[:60]}'", flush=True)
                    await _kb_opts[0].click(force=True)
                    picked = True
                else:
                    await page.keyboard.press("Enter")
                    picked = True
            except Exception as _ke:
                print(f"          [VD] Strat1 {aid}: keyboard fallback error: {_ke}", flush=True)
                await page.keyboard.press("Escape")

        print(f"          [VD] Strat1 {aid}: RESULT → {'✓ picked' if picked else '✗ FAILED — not selected'}", flush=True)
        await asyncio.sleep(0.3)

    # Strategy 2: scan remaining "Select One" buttons, re-query after each answer
    _skip_aids = {"utilityMenuButton", "navigationItem", "pageFooterNextButton",
                  "pageFooterBackButton", "backToJobPosting", "hammyMenuIcon"}
    _answered_parents: set = set()
    for _attempt in range(16):
        try:
            all_btns = await page.locator("button, [role='button']").all()
            found_unanswered = False
            for btn in all_btns:
                try:
                    btn_aid = await btn.get_attribute("data-automation-id") or ""
                    if any(s in btn_aid for s in _skip_aids):
                        continue
                    t = (await btn.inner_text()).strip().lower()
                    if t not in ("select one", "", "-- select --", "please select", "select..."):
                        continue
                    parent_text = await btn.evaluate("el => el.closest('[data-automation-id^=\"formField\"]')?.textContent?.trim() || ''")
                    # Skip if we already answered this field and it reverted (broken field)
                    pk = parent_text[:50]
                    if pk in _answered_parents:
                        continue
                    try:
                        await btn.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await btn.click(force=True)
                    await asyncio.sleep(0.9)
                    picked = await _pick_for_field(parent_text)
                    print(f"          [VD] Strat2 '{parent_text[:60]}' → {'ok' if picked else 'no match'}", flush=True)
                    await asyncio.sleep(0.3)
                    if picked:
                        _answered_parents.add(pk)
                    found_unanswered = True
                    break  # Re-query buttons after each answer to get fresh DOM state
                except Exception:
                    pass
            if not found_unanswered:
                break
        except Exception:
            pass

    # Strategy 2b: checkbox groups (e.g. "Have you ever worked at Adobe" with no dropdown trigger)
    _adobe_never_kws = ("have not worked", "not worked for adobe", "i have not worked",
                        "never worked", "none of the above", "not previously employed",
                        "i have not been employed")
    try:
        all_labels = await page.locator("label").all()
        for lbl in all_labels:
            try:
                ltext = (await lbl.inner_text()).strip().lower()
                if not ltext:
                    continue
                lbl_for = await lbl.get_attribute("for") or ""
                if not lbl_for:
                    continue
                # Check if this label belongs to a checkbox
                cb = page.locator(f"[id='{lbl_for}']").first
                if await cb.count() == 0:
                    continue
                cb_type = await cb.get_attribute("type") or ""
                if cb_type != "checkbox":
                    continue
                # "I have not worked for Adobe in the past" → check it
                if any(k in ltext for k in _adobe_never_kws):
                    if not await cb.is_checked():
                        await cb.click(force=True)
                        await asyncio.sleep(0.3)
                    print(f"          [VD] Strat2b adobe-never '{ltext[:60]}' → checked", flush=True)
            except Exception:
                pass
    except Exception:
        pass

    # Strategy 3: radio buttons — pick by field context
    try:
        radios = await page.locator("input[type='radio']").all()
        for radio in radios:
            try:
                if await radio.is_checked():
                    continue
                rid = await radio.get_attribute("id") or ""
                lbl = page.locator(f"label[for='{rid}']").first if rid else None
                lbl_text = (await lbl.inner_text()).lower() if lbl and await lbl.count() > 0 else ""
                parent_text = await radio.evaluate("el => el.closest('[data-automation-id^=\"formField\"]')?.textContent?.trim() || ''")
                want_kws = _field_kws(parent_text)
                if any(w in lbl_text for w in want_kws):
                    skip_kw_r = "female" if (_gender_val == "male" and "gender" in parent_text.lower()) else None
                    if skip_kw_r and skip_kw_r in lbl_text:
                        continue
                    await radio.click(force=True)
                    print(f"          [VD] Strat3 radio '{lbl_text[:50]}'", flush=True)
            except Exception:
                pass
    except Exception:
        pass

    # Strategy 4: terms/agreement checkboxes (acceptTermsAndAgreements etc.)
    _terms_cb_ids: set = set()
    try:
        for terms_aid in ("acceptTermsAndAgreements", "termsAndConditions", "acceptTerms"):
            ff = page.locator(f"[data-automation-id='formField-{terms_aid}']").first
            if await ff.count() == 0:
                continue
            cb = ff.locator("input[type='checkbox'], [role='checkbox']").first
            if await cb.count() > 0:
                cb_id = await cb.get_attribute("id") or ""
                if cb_id:
                    _terms_cb_ids.add(cb_id)
                try:
                    already = await cb.is_checked()
                except Exception:
                    already = False
                if not already:
                    await cb.click(force=True)
                    await asyncio.sleep(0.3)
                    print(f"          [VD] Strat4 terms checkbox '{terms_aid}' → checked", flush=True)

        # Generic: find any checkbox labeled "confirm" or "agree" — skip ones already handled
        confirm_labels = await page.locator("label").all()
        for lbl in confirm_labels:
            try:
                ltext = (await lbl.inner_text()).strip().lower()
                if not any(k in ltext for k in ("confirm", "agree", "certify", "acknowledge", "attest")):
                    continue
                lbl_for = await lbl.get_attribute("for") or ""
                if not lbl_for or lbl_for in _terms_cb_ids:
                    continue  # Skip already-handled checkboxes
                cb = page.locator(f"[id='{lbl_for}']").first
                if await cb.count() == 0:
                    continue
                try:
                    already = await cb.is_checked()
                except Exception:
                    already = False
                if not already:
                    await cb.click(force=True)
                    await asyncio.sleep(0.3)
                    print(f"          [VD] Strat4 confirm lbl '{ltext[:50]}' → checked", flush=True)
            except Exception:
                pass
    except Exception:
        pass

    await _wd_next(page)


async def _wd_self_identify(page: Page, profile: dict = None) -> None:
    """Fill Workday 'Self Identify' step (disability, veteran — federal contractor forms)."""
    _decline_terms = (
        "i don't wish", "i do not wish", "decline", "prefer not",
        "no, i don't", "i choose not", "i am not", "i have not",
        "not a protected", "not a veteran", "no disability",
    )

    # Diagnostic dump
    try:
        si_info = await page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button, [role="button"]')]
                    .map(b => ({text: b.textContent.trim().substring(0,50), aid: b.getAttribute('data-automation-id')}));
                const inps = [...document.querySelectorAll('input[type="text"], input[type="date"], textarea')]
                    .map(i => ({aid: i.getAttribute('data-automation-id'), ph: i.placeholder, val: i.value}));
                const ffs = [...document.querySelectorAll('[data-automation-id^="formField"]')]
                    .map(ff => ({
                        aid: ff.getAttribute('data-automation-id'),
                        btn: ff.querySelector('button')?.textContent?.trim()?.substring(0,30),
                        inp_type: ff.querySelector('input')?.type,
                        text: ff.textContent?.trim()?.substring(0,60)
                    }));
                return {btns, inps, ffs};
            }
        """)
        if isinstance(si_info, dict):
            print(f"          [SI] btns: {si_info.get('btns', [])}", flush=True)
            print(f"          [SI] inps: {si_info.get('inps', [])}", flush=True)
            print(f"          [SI] ffs: {si_info.get('ffs', [])}", flush=True)
    except Exception:
        pass

    # Fill name field
    try:
        name_val = (profile or {}).get("name", "") if profile else ""
        name_ff = page.locator("[data-automation-id='formField-name']").first
        if await name_ff.count() > 0 and name_val:
            name_inp = name_ff.locator("input").first
            if await name_inp.count() > 0:
                cur = await name_inp.input_value()
                if not cur:
                    await name_inp.fill(name_val)
                    print(f"          [SI] name → '{name_val}'", flush=True)
    except Exception:
        pass

    # Fill date field (dateSignedOn) — multiple strategies
    try:
        from datetime import date as _dt
        today = _dt.today()
        date_str = today.strftime('%m/%d/%Y')
        _date_filled = False

        async def _fill_date_sections(container):
            m_inp = container.locator("[data-automation-id='dateSectionMonth-input']").first
            d_inp = container.locator("[data-automation-id='dateSectionDay-input']").first
            y_inp = container.locator("[data-automation-id='dateSectionYear-input']").first
            m_ok = await m_inp.count() > 0
            d_ok = await d_inp.count() > 0
            y_ok = await y_inp.count() > 0
            if m_ok:
                await m_inp.fill(str(today.month).zfill(2))
            if d_ok:
                await d_inp.fill(str(today.day).zfill(2))
            if y_ok:
                await y_inp.fill(str(today.year))
            return m_ok or d_ok or y_ok

        # Strategy A: dateSectionMonth/Day/Year inside formField-dateSignedOn
        date_ff = page.locator("[data-automation-id='formField-dateSignedOn']").first
        if await date_ff.count() > 0:
            _date_filled = await _fill_date_sections(date_ff)
            if _date_filled:
                print(f"          [SI] date StratA → {date_str}", flush=True)
            else:
                # Strategy B: click first input and keyboard-type
                any_inp = date_ff.locator("input").first
                if await any_inp.count() > 0:
                    await any_inp.click(force=True)
                    await asyncio.sleep(0.2)
                    await page.keyboard.type(date_str)
                    _date_filled = True
                    print(f"          [SI] date StratB (type) → {date_str}", flush=True)

        # Strategy C: dateSectionMonth/Day/Year anywhere on page
        if not _date_filled:
            _date_filled = await _fill_date_sections(page)
            if _date_filled:
                print(f"          [SI] date StratC (page-wide) → {date_str}", flush=True)

        # Strategy D: fill any date-like input with placeholder MM/DD/YYYY
        if not _date_filled:
            date_inputs = await page.locator("input[placeholder*='MM'], input[placeholder*='date' i], input[type='date']").all()
            for di in date_inputs:
                try:
                    await di.fill(date_str)
                    _date_filled = True
                    print(f"          [SI] date StratD (placeholder) → {date_str}", flush=True)
                    break
                except Exception:
                    pass

        if not _date_filled:
            print(f"          [SI] date: no date input found", flush=True)
    except Exception as _de:
        print(f"          [SI] date error: {_de}", flush=True)

    # Handle disability / "check one of the boxes" checkbox groups
    # Scan ALL labels on the page for no-disability / don't-wish options
    _no_dis_kws = (
        "no, i don't", "no, i don’t",  # straight + curly apostrophe
        "no disability", "i do not have", "i choose not",
        "don't wish to answer", "i don’t wish",
        "i do not wish", "i choose not to identify",
        "prefer not", "decline",
        "i don't wish", "i don’t have",
    )
    _yes_dis_kws = ("yes, i have", "yes, i do", "i have a disability")

    async def _check_disability_in(container):
        """Find and check the 'no disability' option inside a container locator.
        Tries 6 strategies, verifying each with is_checked() before moving on."""
        all_lbls = await container.locator("label").all()
        print(f"          [SI] disability container labels: {[await l.inner_text() for l in all_lbls[:8]]}", flush=True)

        for lbl in all_lbls:
            try:
                ltext = (await lbl.inner_text()).strip().lower()
                if not any(k in ltext for k in _no_dis_kws):
                    continue

                lbl_for = await lbl.get_attribute("for") or ""
                inp = page.locator(f"[id='{lbl_for}']").first if lbl_for else None

                async def _verified():
                    if not inp or await inp.count() == 0:
                        return False
                    try:
                        return await inp.is_checked()
                    except Exception:
                        return False

                # S1: Normal label click (fires full pointer-event sequence → React onChange)
                try:
                    await lbl.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
                    await lbl.click()
                    await asyncio.sleep(0.8)
                    if await _verified():
                        print(f"          [SI] disability S1-lbl '{ltext[:55]}' → verified ✓", flush=True)
                        return True
                    print(f"          [SI] disability S1-lbl '{ltext[:55]}' → NOT verified, trying S2", flush=True)
                except Exception as e:
                    print(f"          [SI] disability S1 ex: {e}", flush=True)

                # S2: JS label click
                if lbl_for:
                    result = await page.evaluate("""
                        (id) => {
                            const lbl = document.querySelector('label[for="' + id + '"]');
                            if (lbl) { lbl.click(); return 'lbl'; }
                            const inp = document.getElementById(id);
                            if (inp) { inp.click(); return 'inp'; }
                            return 'not-found';
                        }
                    """, lbl_for)
                    await asyncio.sleep(0.8)
                    if await _verified():
                        print(f"          [SI] disability S2-js '{ltext[:55]}' → {result} verified ✓", flush=True)
                        return True
                    print(f"          [SI] disability S2-js '{ltext[:55]}' → {result} NOT verified", flush=True)

                # S3: Playwright .check()
                if inp and await inp.count() > 0:
                    try:
                        await inp.check(force=True)
                        await asyncio.sleep(0.8)
                        if await _verified():
                            print(f"          [SI] disability S3-check '{ltext[:55]}' → verified ✓", flush=True)
                            return True
                        print(f"          [SI] disability S3-check '{ltext[:55]}' → NOT verified", flush=True)
                    except Exception as e:
                        print(f"          [SI] disability S3 ex: {e}", flush=True)

                # S4: React fiber — call onChange directly on the input's fiber node
                if lbl_for:
                    result = await page.evaluate("""
                        (id) => {
                            const inp = document.getElementById(id);
                            if (!inp) return 'no-inp';
                            const fk = Object.keys(inp).find(k =>
                                k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
                            if (!fk) return 'no-fiber';
                            let node = inp[fk];
                            for (let i = 0; node && i < 30; i++) {
                                if (node.pendingProps && node.pendingProps.onChange) {
                                    try {
                                        node.pendingProps.onChange({
                                            target: inp, currentTarget: inp,
                                            type: 'change', bubbles: true,
                                            preventDefault: () => {}, stopPropagation: () => {},
                                            nativeEvent: new Event('change')
                                        });
                                        return 'fiber-ok';
                                    } catch(e) { return 'fiber-err:' + e.message; }
                                }
                                node = node.return;
                            }
                            return 'no-onChange';
                        }
                    """, lbl_for)
                    await asyncio.sleep(0.8)
                    if await _verified():
                        print(f"          [SI] disability S4-fiber '{ltext[:55]}' → {result} verified ✓", flush=True)
                        return True
                    print(f"          [SI] disability S4-fiber '{ltext[:55]}' → {result} NOT verified", flush=True)

                # S5: Raw mouse coordinates click on the label bounding box
                try:
                    box = await lbl.bounding_box()
                    if box:
                        await page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                        await asyncio.sleep(0.8)
                        if await _verified():
                            print(f"          [SI] disability S5-mouse '{ltext[:55]}' → verified ✓", flush=True)
                            return True
                        print(f"          [SI] disability S5-mouse '{ltext[:55]}' → NOT verified", flush=True)
                except Exception as e:
                    print(f"          [SI] disability S5 ex: {e}", flush=True)

                # S6: Focus input + Space key
                if inp and await inp.count() > 0:
                    try:
                        await inp.focus()
                        await asyncio.sleep(0.2)
                        await page.keyboard.press(" ")
                        await asyncio.sleep(0.8)
                        if await _verified():
                            print(f"          [SI] disability S6-space '{ltext[:55]}' → verified ✓", flush=True)
                            return True
                        print(f"          [SI] disability S6-space '{ltext[:55]}' → NOT verified", flush=True)
                    except Exception as e:
                        print(f"          [SI] disability S6 ex: {e}", flush=True)

                # All strategies exhausted — we clicked it, React may still have registered
                print(f"          [SI] disability all-strategies-tried '{ltext[:55]}' → assuming ok", flush=True)
                return True

            except Exception:
                pass

        return False

    # Scroll to bottom to trigger lazy-loaded disability fields
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.8)
    except Exception:
        pass

    _disability_checked = False
    _checked_input_ids: set = set()

    try:
        # Try known formField IDs — check specific ones first, disabilityForm last
        # NOTE: formField-veteranStatus intentionally excluded — VD step handles it
        for dis_aid in ("formField-disabilityStatus", "formField-disability",
                        "formField-selfIdentify",
                        "formField-disabilityForm"):
            ff = page.locator(f"[data-automation-id='{dis_aid}']").first
            if await ff.count() == 0:
                continue
            # For disabilityForm: skip if it only contains a language selector
            if dis_aid == "formField-disabilityForm":
                inner_text = (await ff.inner_text()).strip().lower()
                if inner_text.startswith("language"):
                    continue
            result = await _check_disability_in(ff)
            if result:
                _disability_checked = True
                # Record which input IDs we just clicked to avoid double-clicking
                all_ff_lbls = await ff.locator("label").all()
                for fl in all_ff_lbls:
                    try:
                        ffor = await fl.get_attribute("for") or ""
                        if ffor:
                            _checked_input_ids.add(ffor)
                    except Exception:
                        pass
                break
    except Exception:
        pass
    await asyncio.sleep(0.5)  # Let Workday re-render after radio click

    # Generic: scan ALL labels on page for "no disability"/"don't wish" checkboxes
    # Only run if formField scan didn't find anything
    if not _disability_checked:
        try:
            all_labels = await page.locator("label").all()
            for lbl in all_labels:
                try:
                    ltext = (await lbl.inner_text()).strip().lower()
                    if not any(k in ltext for k in _no_dis_kws):
                        continue
                    lbl_for = await lbl.get_attribute("for") or ""
                    if not lbl_for or lbl_for in _checked_input_ids:
                        continue
                    cb = page.locator(f"[id='{lbl_for}']").first
                    if await cb.count() == 0:
                        continue
                    try:
                        already = await cb.is_checked()
                    except Exception:
                        already = False
                    if not already:
                        await cb.click(force=True)
                        _disability_checked = True
                        print(f"          [SI] generic label '{ltext[:50]}' → checked", flush=True)
                except Exception:
                    pass
        except Exception:
            pass

    # Known dropdown IDs — skip veteran/gender/ethnicity (VD step already set them)
    # Also skip any formField already answered (not "Select One")
    for aid in ("disability", "protectedVeteran", "disabilityStatus", "selfIdentify"):
        try:
            ff = page.locator(f"[data-automation-id='formField-{aid}']").first
            if await ff.count() > 0:
                btn = ff.locator("button").first
                if await btn.count() > 0:
                    cur_text = (await btn.inner_text()).strip().lower()
                    if cur_text not in ("select one", "", "-- select --", "please select"):
                        continue  # Already answered — don't override
            for term in _decline_terms:
                matched = await _wd_dropdown(page, aid, term)
                if matched:
                    print(f"          [SI] dropdown '{aid}' → '{term}'", flush=True)
                    break
        except Exception:
            pass

    # Scan all unanswered dropdowns (same re-query pattern as _wd_voluntary)
    _skip_aids = {"utilityMenuButton", "navigationItem", "pageFooterNextButton",
                  "pageFooterBackButton", "backToJobPosting", "hammyMenuIcon",
                  "dateIcon", "calendarIcon"}
    for _attempt in range(10):
        try:
            all_btns = await page.locator("button, [role='button']").all()
            found = False
            for btn in all_btns:
                try:
                    btn_aid = await btn.get_attribute("data-automation-id") or ""
                    if any(s in btn_aid for s in _skip_aids):
                        continue
                    t = (await btn.inner_text()).strip().lower()
                    if t not in ("select one", "", "-- select --", "please select"):
                        continue
                    parent_text = await btn.evaluate("el => el.closest('[data-automation-id^=\"formField\"]')?.textContent?.trim() || ''")
                    await btn.click(force=True)
                    await asyncio.sleep(0.9)
                    # Pick decline option
                    picked = False
                    for opt_sel in ("[data-automation-id='promptOption']", "[role='option']", "li[tabindex]"):
                        opts = await page.locator(opt_sel).all()
                        for opt in opts:
                            try:
                                ot = (await opt.inner_text()).strip().lower()
                                if any(w in ot for w in _decline_terms):
                                    await opt.click(force=True)
                                    picked = True
                                    break
                            except Exception:
                                pass
                        if picked:
                            break
                    if not picked:
                        # Fallback: first real option
                        for opt_sel in ("[data-automation-id='promptOption']", "[role='option']"):
                            opts = await page.locator(opt_sel).all()
                            for opt in opts:
                                try:
                                    ot = (await opt.inner_text()).strip()
                                    if ot:
                                        await opt.click(force=True)
                                        picked = True
                                        break
                                except Exception:
                                    pass
                            if picked:
                                break
                        if not picked:
                            await page.keyboard.press("Escape")
                    print(f"          [SI] dropdown scan '{parent_text[:50]}' → {'ok' if picked else 'no-opt'}", flush=True)
                    await asyncio.sleep(0.3)
                    found = True
                    break
                except Exception:
                    pass
            if not found:
                break
        except Exception:
            pass

    # Radio buttons
    try:
        radios = await page.locator("input[type='radio']").all()
        for radio in radios:
            try:
                label_id = await radio.get_attribute("id") or ""
                label = page.locator(f"label[for='{label_id}']").first if label_id else None
                label_text = (await label.inner_text()).lower() if label and await label.count() > 0 else ""
                if any(t in label_text for t in _decline_terms):
                    if not await radio.is_checked():
                        await radio.click(force=True)
            except Exception:
                pass
    except Exception:
        pass

    # Checkboxes: agree/confirm terms
    try:
        all_labels = await page.locator("label").all()
        for lbl in all_labels:
            try:
                ltext = (await lbl.inner_text()).strip().lower()
                if any(k in ltext for k in ("confirm", "agree", "certify", "acknowledge", "attest")):
                    lbl_for = await lbl.get_attribute("for") or ""
                    if lbl_for:
                        cb = page.locator(f"[id='{lbl_for}']").first
                        if await cb.count() > 0 and not await cb.is_checked():
                            await cb.click(force=True)
                            print(f"          [SI] confirm checkbox '{ltext[:50]}' → checked", flush=True)
            except Exception:
                pass
    except Exception:
        pass

    await _wd_next(page)


async def _wd_submit(page: Page) -> str:
    """
    Click the final Submit button on Workday's Review step.
    Prints all visible buttons + URL before clicking so we can debug what's actually on screen.
    Uses URL change as the primary signal that submission happened.
    """
    _confirm_words = ("thank you", "application submitted", "application received",
                      "we'll be in touch", "your application", "successfully submitted",
                      "application complete")

    await _wd_wait_ready(page, timeout=8000)

    # --- Debug: print current URL and all visible buttons on this page ---
    try:
        url_before = page.url
        print(f"          [Submit] Current URL: {url_before}", flush=True)
        heading = await page.locator("h1, h2, [data-automation-id='headingSectionTitle']").first.inner_text()
        print(f"          [Submit] Page heading: {heading.strip()[:80]}", flush=True)
    except Exception:
        url_before = ""
    try:
        all_btns = await page.locator("button, [role='button']").all()
        btn_labels = []
        for b in all_btns:
            try:
                if await b.is_visible(timeout=0):
                    txt = (await b.inner_text()).strip()
                    aid = await b.get_attribute("data-automation-id") or ""
                    if txt:
                        btn_labels.append(f"'{txt}'" + (f" [{aid}]" if aid else ""))
            except Exception:
                pass
        print(f"          [Submit] Visible buttons: {', '.join(btn_labels[:8])}", flush=True)
    except Exception:
        pass

    # Take a screenshot for visual inspection
    try:
        _ss_path = Path("/tmp/wd_submit_debug.png")
        await page.screenshot(path=str(_ss_path))
        print(f"          [Submit] Screenshot saved: {_ss_path}", flush=True)
    except Exception:
        pass

    # --- Attempt submit ---
    _confirm_words = ("thank you", "application submitted", "application received",
                      "we'll be in touch", "your application", "successfully submitted",
                      "application complete")

    all_aids = ("saveAndSubmitButton", "submitButton", "submit-btn",
                "pageFooterNextButton", "bottom-navigation-next-btn")

    for aid in all_aids:
        try:
            btn = page.locator(f"[data-automation-id='{aid}']").first
            if await btn.count() == 0 or not await btn.is_visible(timeout=2000):
                continue
            btn_text = (await btn.inner_text()).strip().lower()
            url_before = page.url
            await btn.click()
            await asyncio.sleep(SUBMIT_WAIT)

            # Primary check: did the URL change? (Workday redirects on successful submit)
            url_after = page.url
            if url_after != url_before:
                print(f"          [Submit] URL changed → {url_after[:80]}", flush=True)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in _confirm_words):
                    return "applied"
                return f"submitted (unconfirmed — url changed after '{btn_text}')"

            # Secondary check: confirmation words in body
            body = (await page.inner_text("body")).lower()
            if any(w in body for w in _confirm_words):
                return "applied"

            # URL didn't change and no confirmation — the click didn't submit
            print(f"          [Submit] Clicked '{btn_text}' but URL unchanged — not submitted", flush=True)
        except Exception:
            pass

    # Broad fallback
    try:
        for sel in ("button[type='submit']", "[aria-label*='submit' i]"):
            btn = page.locator(sel).last
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                url_before = page.url
                await btn.click()
                await asyncio.sleep(SUBMIT_WAIT)
                if page.url != url_before:
                    return "submitted (unconfirmed — url changed)"
    except Exception:
        pass

    # Recovery: if still on error page, check for CC-305 disability radio not selected
    try:
        has_error = await page.locator("button:has-text('Errors Found')").count() > 0
        has_cc305 = (
            await page.locator("text='CC-305'").count() > 0
            or await page.locator("[data-automation-id='formField-dateSignedOn']").count() > 0
        )
        has_radio = await page.locator("input[type='radio']").count() > 0
        if has_error and (has_cc305 or has_radio):
            print(f"          [Submit] Error page — attempting CC-305 disability radio recovery", flush=True)
            _rec_kws = ("i don't wish", "i do not wish", "prefer not", "no disability",
                        "i do not have", "choose not", "decline", "i am not")
            radios = await page.locator("input[type='radio']").all()
            for radio in radios:
                try:
                    rid = await radio.get_attribute("id") or ""
                    lbl = page.locator(f"label[for='{rid}']").first if rid else None
                    lbl_text = (await lbl.inner_text()).strip().lower() if lbl and await lbl.count() > 0 else ""
                    if any(k in lbl_text for k in _rec_kws) or not lbl_text:
                        if not await radio.is_checked():
                            await radio.click(force=True)
                        print(f"          [Submit] CC-305 recovery radio → '{lbl_text[:50]}'", flush=True)
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    pass
            # Retry next button
            for _nxt_aid in ("pageFooterNextButton", "saveAndSubmitButton", "submitButton"):
                _nb = page.locator(f"[data-automation-id='{_nxt_aid}']").first
                if await _nb.count() > 0 and await _nb.is_visible(timeout=2000):
                    url_before = page.url
                    await _nb.click()
                    await asyncio.sleep(SUBMIT_WAIT)
                    if page.url != url_before:
                        body = (await page.inner_text("body")).lower()
                        if any(w in body for w in _confirm_words):
                            return "applied"
                        return "submitted (unconfirmed — recovered from CC-305 error)"
                    break
    except Exception as _re:
        print(f"          [Submit] Recovery error: {_re}", flush=True)

    return "error: submit did not navigate — check /tmp/wd_submit_debug.png"


def _workday_session_file(user_data: Path, tenant: str) -> Path:
    return user_data / f".wd_cookies_{tenant}.json"


def _workday_session_exists(user_data: Path, tenant: str) -> bool:
    return _workday_session_file(user_data, tenant).exists()


async def setup_workday_session(company_rec: dict, email: str) -> None:
    """
    Open the browser so the user can sign in to a company's Workday portal.
    Detects login by watching for the user's email in the page, then saves
    all cookies to a JSON file via storage_state() — reliably loaded on every
    future apply run via ctx.add_cookies().
    """
    import subprocess as _sp, time as _t

    tenant = company_rec.get("tenant", "")
    shard  = company_rec.get("shard", 1)
    portal = company_rec.get("portal", "")
    name   = company_rec.get("name", tenant)
    base   = f"https://{tenant}.wd{shard}.myworkdayjobs.com/en-US/{portal}"

    safe_email = re.sub(r"[^a-z0-9]", "_", email.lower())
    user_data  = Path.home() / f".company-apply-profile-{safe_email}"
    user_data.mkdir(parents=True, exist_ok=True)
    session_file = _workday_session_file(user_data, tenant)

    # Clear stale Chrome locks
    _sp.run(["pkill", "-f", "Google Chrome for Testing"], capture_output=True)
    _t.sleep(0.5)
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (user_data / lock).unlink(missing_ok=True)

    print(f"\n  Opening {name} Workday portal...")
    print(f"  URL: {base}")
    print(f"  Sign in with your Workday / {name} account in the browser.")
    print(f"  Browser auto-closes once your email is detected in the page.\n")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await ctx.new_page()
        await page.goto(base, wait_until="domcontentloaded", timeout=0)

        print("  Waiting for sign-in (up to 10 minutes)...", flush=True)

        # Detect login by checking if the user's email appears in the page body.
        # Adobe Workday shows the email in the nav bar when authenticated.
        signed_in = False
        for _ in range(300):  # 300 × 2s = 10 min
            try:
                body = await page.inner_text("body")
                if email.lower() in body.lower():
                    signed_in = True
                    break
            except Exception:
                pass
            await asyncio.sleep(2)

        if signed_in:
            print(f"  ✓ Sign-in detected! Saving session...", flush=True)
            # Explicitly save cookies to JSON — reliable across process restarts
            await ctx.storage_state(path=str(session_file))
        else:
            print("  ⚠  Email not detected in page within 10 minutes.")
            print("  If you did log in, try running --setup-workday again.")

        await ctx.close()

    if signed_in:
        print(f"\n  ✓ Session saved to {session_file}")
        print(f"  All future apply runs will use this session.")


async def _wd_handle_account_gate(page: Page, email: str) -> None:
    """
    Workday gate page handler.
    Priority order:
      1. Authenticated: "Continue as [Name]" — fastest path (pre-filled form)
      2. Guest: "Apply Manually" / "Apply Now" — fallback if not signed in
    """
    await _wd_wait_ready(page, timeout=10000)

    # 1. Authenticated state: Workday shows "Continue as [Name]" when signed in
    try:
        cont = page.get_by_text(re.compile(r"^Continue as\b", re.I)).first
        if await cont.count() > 0 and await cont.is_visible(timeout=2000):
            label = (await cont.inner_text()).strip()
            await cont.click()
            await _wd_wait_ready(page)
            print(f"          [Workday] Gate: authenticated — clicked '{label}'", flush=True)
            return
    except Exception:
        pass

    # Also try automation-id variants Workday uses for the authenticated continue button
    for aid in ("signinWithExistingAccount", "continueAsUser", "use-existing-account",
                "existingAccount"):
        try:
            btn = page.locator(f"[data-automation-id='{aid}']").first
            if await btn.count() > 0 and await btn.is_visible(timeout=1000):
                await btn.click()
                await _wd_wait_ready(page)
                print(f"          [Workday] Gate: authenticated — clicked aid='{aid}'", flush=True)
                return
        except Exception:
            pass

    # 2. Guest path — data-automation-id selectors (confirmed from DOM inspection)
    for aid in ("applyManually", "startApplication", "apply-btn", "applyButton"):
        try:
            btn = page.locator(f"[data-automation-id='{aid}']").first
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                await btn.click()
                await _wd_wait_ready(page)
                print(f"          [Workday] Gate: clicked data-automation-id='{aid}'", flush=True)
                return
        except Exception:
            pass

    # 3. Guest path — text-based ("Apply Manually", "Apply Now", etc.)
    for label in ("Apply Manually", "Continue as Guest", "Apply for this position", "Apply Now"):
        try:
            loc = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I)).first
            if await loc.count() > 0 and await loc.is_visible(timeout=2000):
                await loc.click()
                await _wd_wait_ready(page)
                print(f"          [Workday] Gate: clicked text='{label}'", flush=True)
                return
        except Exception:
            pass

    # 4. Broad fallback: any "Apply…" button/link that isn't "Sign In"
    try:
        for role in ("button", "link"):
            for elem in await page.get_by_role(role).all():
                try:
                    txt = (await elem.inner_text()).strip()
                    if txt.lower().startswith("apply") and "sign" not in txt.lower() \
                            and await elem.is_visible(timeout=0):
                        await elem.click()
                        await _wd_wait_ready(page)
                        print(f"          [Workday] Gate: clicked role={role} '{txt}'", flush=True)
                        return
                except Exception:
                    pass
    except Exception:
        pass

    print(f"          [Workday] Gate: no gate button found — may already be on form", flush=True)


async def _fill_workday(
    page:      Page,
    profile:   dict,
    email:     str,
    resume:    Optional[Path],
    company:   str,
    new_pages: Optional[list] = None,
) -> str:
    """
    Orchestrate the full Workday multi-step application.
    Steps: My Information → My Experience → Application Questions →
           Voluntary Disclosures → Review & Submit
    new_pages: live list populated by ctx.on("page", ...) — used to detect
    popups that open when 'Apply Manually' is clicked.
    """
    print(f"          [Workday] Waiting for page load...", flush=True)
    await _wd_wait_ready(page, timeout=30000)

    try:
        body_preview = (await page.inner_text("body"))[:300].replace("\n", " ").strip()
        print(f"          [Workday] Page state: {body_preview[:120]}", flush=True)
    except Exception:
        pass

    # Handle sign-in / guest gate
    print(f"          [Workday] Handling account gate...", flush=True)
    pages_before_gate = len(new_pages) if new_pages is not None else 0
    await _wd_handle_account_gate(page, email)
    # Give any popup opened by the gate click a moment to settle
    await asyncio.sleep(1.5)

    # Determine which page to use for form filling:
    # If the gate click opened a new tab, use that; otherwise stay on current page.
    apply_page = page
    if new_pages is not None and len(new_pages) > pages_before_gate:
        popup = new_pages[-1]
        try:
            await popup.title()   # will raise if already closed
            await _wd_wait_ready(popup, timeout=10000)
            apply_page = popup
            print(f"          [Workday] Gate opened popup — switched to new tab", flush=True)
        except Exception:
            pass

    try:
        body_preview2 = (await apply_page.inner_text("body"))[:300].replace("\n", " ").strip()
        print(f"          [Workday] After gate: {body_preview2[:120]}", flush=True)
    except Exception as _e:
        print(f"          [Workday] After gate: page inaccessible ({_e.__class__.__name__})", flush=True)

    # CAPTCHA check before filling
    if await has_captcha(apply_page):
        await pause_for_captcha(apply_page)

    step_fns = {
        "My Information":        lambda: _wd_my_information(apply_page, profile, email, resume),
        "My Experience":         lambda: _wd_my_experience(apply_page, profile, resume),
        "Application Questions": lambda: _wd_questions(apply_page, profile, email),
        "Voluntary Disclosures": lambda: _wd_voluntary(apply_page, profile),
        "Self Identify":         lambda: _wd_self_identify(apply_page, profile),
    }

    async def _detect_step() -> str:
        """Find current Workday step by scanning text nodes for exact step name matches."""
        _known = list(step_fns.keys()) + ["Review"]
        try:
            found = await apply_page.evaluate("""
                (names) => {
                    // Walk all text nodes; return first that exactly matches a step name
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let node;
                    while ((node = walker.nextNode())) {
                        const t = node.textContent.trim();
                        if (names.includes(t)) return t;
                    }
                    // Fallback: aria-current on any element
                    const ac = document.querySelector('[aria-current="step"], [aria-current="true"]');
                    if (ac) return ac.textContent.trim();
                    return '';
                }
            """, _known)
            return (found or "").strip()
        except Exception:
            return ""

    # Sequential dispatch — run each step in order, detect page state after each
    step_order = list(step_fns.keys())
    for step_name in step_order:
        fn = step_fns[step_name]
        try:
            print(f"          [Workday] Step: {step_name}...", flush=True)
            await fn()
            await asyncio.sleep(0.8)
            try:
                pg = (await apply_page.inner_text("body"))[:200].replace("\n", " ").strip()
                print(f"          [Workday]   → after step: {pg[:100]}", flush=True)
            except Exception:
                pass
            # Verify page advanced — if still showing same step name with errors, warn
            current = await _detect_step()
            if current == step_name:
                print(f"          [Workday] ⚠ page still shows '{step_name}' — may have errors", flush=True)
        except Exception as e:
            _es = str(e)
            if "Target crashed" in _es or "Target page, context or browser has been closed" in _es:
                print(f"          [Workday] {step_name} fatal: browser crashed — skipping remaining steps", flush=True)
                raise  # propagate to job loop; finally block will recover the page
            print(f"          [Workday] {step_name} step warning: {e}", flush=True)

    # Handle CC-305 "Voluntary Self-Identification of Disability" form that Workday
    # shows after the SI disability radio — it requires Name + Date before advancing.
    try:
        _cc305 = False
        if await apply_page.locator("text='CC-305'").count() > 0:
            _cc305 = True
        elif await apply_page.locator("[data-automation-id='formField-dateSignedOn']").count() > 0:
            _cc305 = True
        elif await apply_page.locator("input[placeholder*='MM/DD/YYYY'], input[placeholder='MM/DD/YYYY']").count() > 0:
            _cc305 = True
        if _cc305:
            print(f"          [Workday] ══════ CC-305 FORM DETECTED ══════", flush=True)
            print(f"          [Workday] CC-305: starting disability radio + Name + Date fill", flush=True)

            # ── Disability checkbox/radio ─────────────────────────────────
            # Scroll to the disability section by locating text near it, then use
            # Playwright locators (which pierce shadow DOM, unlike querySelectorAll).
            # ── scroll to disability section (separate try so failure doesn't block radio search)
            try:
                # Use separate locators — cannot mix text regex and CSS in one comma-separated locator
                _dis_header = apply_page.locator("text=/disability/i").first
                _dis_header_cnt = await _dis_header.count()
                print(f"          [Workday] CC-305: disability header locator count={_dis_header_cnt}", flush=True)
                if _dis_header_cnt > 0:
                    print(f"          [Workday] CC-305: scrolling to disability header...", flush=True)
                    await _dis_header.scroll_into_view_if_needed()
                else:
                    print(f"          [Workday] CC-305: no header found — scrolling to page bottom", flush=True)
                    await apply_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception as _scroll_e:
                print(f"          [Workday] CC-305: scroll error (non-fatal): {_scroll_e} — scrolling to bottom", flush=True)
                await apply_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.0)

            # ── disability radio selection (own try block so scroll error can't skip this)
            _dis_picked = False
            try:
                _dis_decline_kws = (
                    "i don't wish", "i do not wish", "prefer not", "choose not",
                    "no, i don't", "no disability", "i do not have",
                    "i am not", "does not apply", "decline",
                )
                print(f"          [Workday] CC-305: decline keywords = {_dis_decline_kws}", flush=True)

                # Playwright locators pierce shadow DOM — try each element type in order
                _dis_els: list = []
                _dis_sel_used = None
                for _dis_sel in (
                    "input[type='radio']",
                    "[role='radio']",
                    "input[type='checkbox']",
                    "[role='checkbox']",
                ):
                    _loc = apply_page.locator(_dis_sel)
                    _cnt = await _loc.count()
                    print(f"          [Workday] CC-305:   selector '{_dis_sel}' → {_cnt} elements", flush=True)
                    if _cnt > 0:
                        _dis_els = await _loc.all()
                        _dis_sel_used = _dis_sel
                        break

                print(f"          [Workday] CC-305: using selector '{_dis_sel_used}' — {len(_dis_els)} total elements", flush=True)

                # First pass: look for decline/no-disability keywords via label text
                for _i, _el in enumerate(_dis_els):
                    try:
                        _eid = await _el.get_attribute("id") or ""
                        _is_checked = await _el.is_checked() if _dis_sel_used and "input" in (_dis_sel_used or "") else False
                        _lbl = apply_page.locator(f"label[for='{_eid}']").first if _eid else None
                        _lbl_text = (await _lbl.inner_text() if _lbl and await _lbl.count() > 0 else "").strip().lower()
                        if not _lbl_text:
                            _lbl_text = (
                                await _el.get_attribute("aria-label") or
                                await _el.evaluate("el => el.closest('label')?.textContent || el.parentElement?.textContent || ''")
                            ).strip().lower()
                        _kw_hit = [k for k in _dis_decline_kws if k in _lbl_text]
                        print(f"          [Workday] CC-305:   element[{_i}] id='{_eid}' checked={_is_checked} text='{_lbl_text[:80]}' | kw_hits={_kw_hit}", flush=True)
                        if _kw_hit:
                            print(f"          [Workday] CC-305:   → MATCH on '{_kw_hit[0]}' — clicking...", flush=True)
                            await _el.scroll_into_view_if_needed()
                            await _el.click(force=True)
                            print(f"          [Workday] CC-305: ✓ disability selected → '{_lbl_text[:60]}'", flush=True)
                            _dis_picked = True
                            break
                        else:
                            print(f"          [Workday] CC-305:   → no decline keyword match — skipping", flush=True)
                    except Exception as _ee:
                        print(f"          [Workday] CC-305:   element[{_i}] error: {_ee}", flush=True)

                # Fallback: click last available element
                if not _dis_picked and _dis_els:
                    print(f"          [Workday] CC-305: no keyword match — fallback: clicking last element", flush=True)
                    try:
                        _last_el = _dis_els[-1]
                        await _last_el.scroll_into_view_if_needed()
                        await _last_el.click(force=True)
                        print(f"          [Workday] CC-305: ✓ disability fallback last element clicked", flush=True)
                        _dis_picked = True
                    except Exception as _fe:
                        print(f"          [Workday] CC-305: fallback click error: {_fe}", flush=True)

                if not _dis_els:
                    print(f"          [Workday] CC-305: ✗ FAILED — 0 radio/checkbox elements found on page", flush=True)
                elif not _dis_picked:
                    print(f"          [Workday] CC-305: ✗ FAILED — found {len(_dis_els)} elements but none selected", flush=True)
            except Exception as _de:
                print(f"          [Workday] CC-305 disability exception: {_de}", flush=True)

            if _dis_picked:
                await asyncio.sleep(0.5)
                await apply_page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.3)

            # Name field
            name_val = profile.get("name", "")
            print(f"          [Workday] CC-305: filling Name='{name_val}'...", flush=True)
            if name_val:
                name_inp = apply_page.locator("[data-automation-id='formField-name'] input").first
                _name_cnt = await name_inp.count()
                print(f"          [Workday] CC-305:   [data-automation-id=formField-name] input count={_name_cnt}", flush=True)
                if _name_cnt == 0:
                    print(f"          [Workday] CC-305:   scanning all text inputs for empty one...", flush=True)
                    for _t_inp in await apply_page.locator("input[type='text']").all():
                        try:
                            _val = await _t_inp.input_value()
                            if not _val:
                                name_inp = _t_inp
                                print(f"          [Workday] CC-305:   found empty text input, using it", flush=True)
                                break
                        except Exception:
                            pass
                _cur_name = await name_inp.input_value() if await name_inp.count() > 0 else "N/A"
                print(f"          [Workday] CC-305:   current name field value='{_cur_name}'", flush=True)
                if await name_inp.count() > 0 and not _cur_name:
                    await name_inp.fill(name_val)
                    print(f"          [Workday] CC-305: ✓ Name filled → '{name_val}'", flush=True)
                else:
                    print(f"          [Workday] CC-305:   Name already filled or field not found — skipping", flush=True)

            # Date field
            from datetime import date as _dt_cc
            _today = _dt_cc.today()
            _date_str = _today.strftime('%m/%d/%Y')
            print(f"          [Workday] CC-305: filling Date={_date_str}...", flush=True)
            _date_ff = apply_page.locator("[data-automation-id='formField-dateSignedOn']").first
            _date_ff_cnt = await _date_ff.count()
            print(f"          [Workday] CC-305:   formField-dateSignedOn count={_date_ff_cnt}", flush=True)
            _date_done = False
            if _date_ff_cnt > 0:
                for _seg, _v in (("dateSectionMonth-input", str(_today.month).zfill(2)),
                                  ("dateSectionDay-input",   str(_today.day).zfill(2)),
                                  ("dateSectionYear-input",  str(_today.year))):
                    _seg_inp = _date_ff.locator(f"[data-automation-id='{_seg}']").first
                    _seg_cnt = await _seg_inp.count()
                    print(f"          [Workday] CC-305:   date segment '{_seg}' count={_seg_cnt}", flush=True)
                    if _seg_cnt > 0:
                        await _seg_inp.fill(_v)
                        print(f"          [Workday] CC-305:   → filled '{_seg}'='{_v}'", flush=True)
                        _date_done = True
            if not _date_done:
                print(f"          [Workday] CC-305:   segment fill failed — trying generic date input...", flush=True)
                for _di in await apply_page.locator(
                    "input[placeholder*='MM'], input[placeholder*='date' i], input[type='date']"
                ).all():
                    try:
                        await _di.fill(_date_str)
                        _date_done = True
                        print(f"          [Workday] CC-305: ✓ Date filled via generic input → {_date_str}", flush=True)
                        break
                    except Exception:
                        pass
            if not _date_done:
                print(f"          [Workday] CC-305: ✗ Date fill FAILED — no date input found", flush=True)

            print(f"          [Workday] CC-305: clicking Next to advance past CC-305...", flush=True)
            # Advance past CC-305 to Review
            await _wd_next(apply_page)
            await asyncio.sleep(1.5)
            print(f"          [Workday] ══════ CC-305 HANDLER DONE ══════", flush=True)
    except Exception as _cc_e:
        print(f"          [Workday] CC-305 handler: {_cc_e}", flush=True)

    # Final submit
    print(f"          [Workday] Attempting submit...", flush=True)
    return await _wd_submit(apply_page)


# ── Main apply loop ─────────────────────────────────────────────────────────────

async def apply_to_company(
    company_rec: dict,
    profile:     dict,
    email:       str,
    keywords:    list[str],
    locations:   Optional[list[str]] = None,
    posted_days: Optional[int]       = None,
    experience:  Optional[list[str]] = None,
    work_type:   Optional[list[str]] = None,
    us_only:     bool                = False,
    dry_run:     bool                = False,
    max_jobs:    int                 = 0,
):
    company = company_rec["name"]
    ats     = company_rec["ats"]
    slug    = company_rec.get("slug", "")
    c_url   = company_rec.get("careers_url", "")
    applied_urls = load_applied_urls(email)

    # Show resume folder at startup; actual resume picked per-job by title
    try:
        _rd = json.loads(RESUMES_JSON.read_text()) if RESUMES_JSON.exists() else {}
        _resume_folder = _rd.get(email, {}).get("resume_folder", "")
    except Exception:
        _resume_folder = ""

    def _fmt(lst): return ", ".join(lst) if lst else "any"
    print(f"\n{'─'*62}")
    print(f"  Company    : {company}  ({ats})")
    print(f"  Profile    : {profile.get('name')} <{email}>")
    print(f"  Resume dir : {_resume_folder or 'not configured'}")
    print(f"  Keywords   : {', '.join(keywords) if keywords else 'all roles'}")
    print(f"  Location   : {_fmt(locations)}")
    print(f"  Posted     : {'last ' + str(posted_days) + ' days' if posted_days else 'any time'}")
    print(f"  Experience : {_fmt(experience)}")
    print(f"  Work type  : {_fmt(work_type)}")
    print(f"  US only    : {us_only}")
    print(f"  Dry run    : {dry_run}")
    print(f"{'─'*62}\n")

    safe_email = re.sub(r"[^a-z0-9]", "_", email.lower())
    # Use a separate profile dir so company_apply never conflicts with dice/linkedin automation
    user_data  = Path.home() / f".company-apply-profile-{safe_email}"
    user_data.mkdir(parents=True, exist_ok=True)

    # Init Gmail so verification code fetching and recruiter emails work
    if _gmail_sender:
        _gmail_sender.init_gmail(
            profile_dir=user_data,
            sender_name=profile.get("name", "Applicant"),
            sender_email=email,
        )
        # Pre-authenticate and start live verification-code watcher BEFORE Playwright.
        # Capturing historyId early ensures codes arriving during form-fill are not missed.
        if email and not dry_run:
            try:
                _svc = await asyncio.to_thread(_gmail_sender.get_gmail_service)
                if _svc:
                    _start_code_watcher(email, _svc)
                    print("  [Code Watcher] Live Gmail monitor started.", flush=True)
            except Exception:
                pass

    # ── Pre-fetch for API-based ATS (no browser needed for listing) ──────────────
    # Workday, Greenhouse, Lever, Stripe all expose REST/JSON APIs.
    # Fetch + filter BEFORE launching Chrome so dry-run and no-match cases
    # never open a browser window.
    _API_ATS = {"workday", "greenhouse", "lever", "stripe"}
    pre_fetched_jobs: Optional[list] = None

    if ats in _API_ATS:
        print("  Fetching job listings...", flush=True)
        if ats == "workday":
            pre_fetched_jobs = workday_list_jobs(
                tenant=company_rec.get("tenant", ""),
                shard=company_rec.get("shard", 1),
                portal=company_rec.get("portal", ""),
                search=" ".join(keywords) if keywords else "",
            )
        elif ats == "greenhouse":
            pre_fetched_jobs = greenhouse_list_jobs(slug)
        elif ats == "lever":
            pre_fetched_jobs = lever_list_jobs(slug)
        elif ats == "stripe":
            pre_fetched_jobs = stripe_list_jobs()

        all_jobs = list(pre_fetched_jobs)
        print(f"  Found {len(all_jobs)} open role(s).")
        already_done = sum(1 for j in all_jobs if j["url"] in applied_urls)
        if already_done:
            print(f"  Already applied / exhausted: {already_done} (skipped)")

        def _run_filter(kw, locs, days, exp, wt, us):
            return filter_jobs(all_jobs, kw, applied_urls, locs, days, exp, wt, us)

        jobs = _run_filter(keywords, locations, posted_days, experience, work_type, us_only)
        print(f"  {len(jobs)} eligible this run (match filters + not yet applied).")

        if not jobs and experience:
            print(f"\n  ✗  Experience filter [{', '.join(experience)}] matched 0 roles at {company_rec['name']}.")
            print(f"     Skipping — not falling back to unfiltered results.")
        if not jobs and posted_days:
            print(f"\n  ↩  Date filter (last {posted_days}d) matched 0 roles.")
            print(f"     Retrying without date restriction (keeping keywords/location)...")
            jobs = _run_filter(keywords, locations, None, [], work_type, us_only)
            if jobs:
                print(f"  ✓  {len(jobs)} roles found — date filter dropped.\n")
            else:
                print(f"  Still 0 after dropping date.")
        if not jobs and locations:
            print(f"\n  ↩  Location filter [{', '.join(locations)}] matched 0 roles.")
            print(f"     Retrying without location restriction (keeping keywords)...")
            jobs = _run_filter(keywords, [], None, [], work_type, us_only)
            if jobs:
                print(f"  ✓  {len(jobs)} roles found — location filter dropped.\n")
            else:
                print(f"  Still 0 after dropping location.")
        if not jobs and keywords:
            print(f"\n  ↩  Retrying with keywords only: {', '.join(keywords)}")
            jobs = _run_filter(keywords, [], None, [], [], us_only)
            if jobs:
                print(f"  ✓  {len(jobs)} roles match keywords.\n")
            else:
                print(f"  0 roles match keywords either. This company has no matching open roles.")

        print()
        if max_jobs > 0:
            jobs = jobs[:max_jobs]

        if dry_run:
            for i, job in enumerate(jobs, 1):
                loc_str = job.get("location", "")
                resume  = get_resume(profile, email, job["title"],
                                    company=company_rec.get("name", ""))
                print(f"  [{i}/{len(jobs)}] {job['title']}" + (f"  [{loc_str}]" if loc_str else ""))
                print(f"          {job['url']}")
                print(f"          Resume: {resume.name if resume else 'none — skipping upload'}")
                print(f"          → [DRY RUN] would apply\n")
            if not jobs:
                print("  Nothing to apply to. Done.")
            print(f"\n{'─'*62}")
            print(f"  {company} summary (dry run):")
            print(f"    Would apply to: {len(jobs)}")
            print(f"{'─'*62}\n")
            return

        if not jobs:
            print("  Nothing to apply to. Done.")
            return

    # Workday: remind user to set up session if not done yet
    if ats == "workday":
        tenant = company_rec.get("tenant", "")
        if not _workday_session_exists(user_data, tenant):
            print(f"\n  ⚠  No Workday session found for {company}.")
            print(f"     Applying as guest (slower, more brittle).")
            print(f"     For faster authenticated apply, run once:")
            print(f"       python3 company_apply.py --company {company.lower()} --setup-workday\n")

    # Clear any stale Chrome singleton locks left by crashed previous runs
    import subprocess as _sp
    _sp.run(["pkill", "-f", "Google Chrome for Testing"], capture_output=True)
    import time as _t; _t.sleep(0.5)
    for _lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (user_data / _lock).unlink(missing_ok=True)

    async with async_playwright() as pw:
        ctx: BrowserContext = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        # Inject saved Workday cookies so the authenticated session is restored
        # even if Chrome didn't flush them to the profile on a previous run.
        if ats == "workday":
            _sf = _workday_session_file(user_data, company_rec.get("tenant", ""))
            if _sf.exists():
                try:
                    import json as _json
                    _sdata = _json.loads(_sf.read_text())
                    _cookies = _sdata.get("cookies", [])
                    if _cookies:
                        await ctx.add_cookies(_cookies)
                        print(f"  [Workday] ✓ Loaded {len(_cookies)} session cookies", flush=True)
                except Exception as _ce:
                    print(f"  [Workday] Session cookie load failed: {_ce}", flush=True)

        page: Page = await ctx.new_page()

        # ── Fetch job list (browser-required ATS: Ashby, generic) ────────────────
        if pre_fetched_jobs is None:
            print("  Fetching job listings...", flush=True)
            if ats == "ashby":
                jobs = await ashby_list_jobs(page, slug)
            else:
                jobs = await generic_list_jobs(page, c_url)

        # Filter + fallback only needed for browser-fetched ATS (Ashby / generic)
        if pre_fetched_jobs is None:
            all_jobs = list(jobs)
            print(f"  Found {len(all_jobs)} open role(s).")
            already_done = sum(1 for j in all_jobs if j["url"] in applied_urls)
            if already_done:
                print(f"  Already applied / exhausted: {already_done} (skipped)")

            def _run_filter(kw, locs, days, exp, wt, us):
                return filter_jobs(all_jobs, kw, applied_urls, locs, days, exp, wt, us)

            jobs = _run_filter(keywords, locations, posted_days, experience, work_type, us_only)
            print(f"  {len(jobs)} eligible this run (match filters + not yet applied).")

            if not jobs and experience:
                print(f"\n  ↩  Experience filter [{', '.join(experience)}] matched 0 roles.")
                print(f"     {company_rec['name']} likely does not use level labels in job titles.")
                print(f"     Retrying without experience filter (keeping keywords/location/date)...")
                jobs = _run_filter(keywords, locations, posted_days, [], work_type, us_only)
                if jobs:
                    print(f"  ✓  {len(jobs)} roles found — experience filter dropped.\n")
                else:
                    print(f"  Still 0 after dropping experience.")
            if not jobs and posted_days:
                print(f"\n  ↩  Date filter (last {posted_days}d) matched 0 roles.")
                print(f"     Retrying without date restriction (keeping keywords/location)...")
                jobs = _run_filter(keywords, locations, None, [], work_type, us_only)
                if jobs:
                    print(f"  ✓  {len(jobs)} roles found — date filter dropped.\n")
                else:
                    print(f"  Still 0 after dropping date.")
            if not jobs and locations:
                print(f"\n  ↩  Location filter [{', '.join(locations)}] matched 0 roles.")
                print(f"     Retrying without location restriction (keeping keywords)...")
                jobs = _run_filter(keywords, [], None, [], work_type, us_only)
                if jobs:
                    print(f"  ✓  {len(jobs)} roles found — location filter dropped.\n")
                else:
                    print(f"  Still 0 after dropping location.")
            if not jobs and keywords:
                print(f"\n  ↩  Retrying with keywords only: {', '.join(keywords)}")
                jobs = _run_filter(keywords, [], None, [], [], us_only)
                if jobs:
                    print(f"  ✓  {len(jobs)} roles match keywords.\n")
                else:
                    print(f"  0 roles match keywords either. This company has no matching open roles.")

            print()
            if max_jobs > 0:
                jobs = jobs[:max_jobs]

        if not jobs:
            print("  Nothing to apply to. Done.")
            await ctx.close()
            return

        applied = skipped = errors = 0

        for i, job in enumerate(jobs, 1):
            title   = job["title"]
            job_url = job["url"]
            job_ats = job.get("ats", ats)

            loc_str = job.get("location", "")
            resume  = get_resume(profile, email, title,
                                 company=company_rec.get("name", ""))
            _job_start = time.perf_counter()
            print(f"  [{i}/{len(jobs)}] {title}" + (f"  [{loc_str}]" if loc_str else ""))
            print(f"          {job_url}")
            print(f"          Resume: {resume.name if resume else 'none — skipping upload'}")

            if dry_run:
                print(f"          → [DRY RUN] would apply\n")
                continue

            if job_ats == "generic":
                print("          → skipped (generic ATS — Phase 2)\n")
                log_applied(company, job_ats, title, job_url,
                            "skipped - generic not supported", email, location=loc_str)
                skipped += 1
                continue

            try:
                if job_ats == "workday":
                    # Keep listener active through the whole application so that
                    # popups opened by the gate click ('Apply Manually') are captured.
                    _new_pages: list = []
                    _wd_listener = lambda p: _new_pages.append(p)
                    ctx.on("page", _wd_listener)
                    try:
                        await page.goto(job_url, wait_until="domcontentloaded", timeout=0)
                    except Exception:
                        pass
                    await asyncio.sleep(1)  # brief settle before gate
                    # _fill_workday handles gate + popup detection using new_pages
                    status = await _fill_workday(page, profile, email, resume, company,
                                                 new_pages=_new_pages)
                    ctx.remove_listener("page", _wd_listener)
                elif job_ats in ("greenhouse", "stripe"):
                    await page.goto(job_url, wait_until="load", timeout=0)
                    status = await _fill_greenhouse(page, profile, email, resume, title, company)
                elif job_ats == "lever":
                    await page.goto(job_url, wait_until="load", timeout=0)
                    status = await _fill_lever(page, profile, email, resume)
                elif job_ats == "ashby":
                    await page.goto(job_url, wait_until="load", timeout=0)
                    status = await _fill_ashby(page, profile, email, resume)
                else:
                    await page.goto(job_url, wait_until="load", timeout=0)
                    status = "skipped - unsupported ATS"

                # If unconfirmed, double-check via Gmail
                if status == "submitted (unconfirmed)":
                    if check_gmail_confirmation(company, title):
                        status = "applied (gmail confirmed)"
                        _elapsed = time.perf_counter() - _job_start
                        print(f"          → {status} ✓ confirmation email found  ⏱ {_elapsed:.0f}s\n")
                    else:
                        print(f"          → {status}\n")
                else:
                    _elapsed = time.perf_counter() - _job_start
                    print(f"          → {status}  ⏱ {_elapsed:.0f}s\n")
                log_applied(company, job_ats, title, job_url, status, email, location=loc_str)

                if "applied" in status or "submitted" in status:
                    applied += 1
                elif "skipped" in status:
                    skipped += 1
                else:
                    errors += 1

            except Exception as exc:
                err_msg = str(exc)
                if isinstance(exc, BrokenPipeError):
                    break  # pipe closed — process is stopping
                _elapsed = time.perf_counter() - _job_start
                if "Target crashed" in err_msg:
                    err = "error: browser renderer crashed"
                elif "Target page, context or browser has been closed" in err_msg:
                    err = "error: browser closed unexpectedly"
                else:
                    err = f"error: {err_msg[:80]}"
                print(f"          → {err}  ⏱ {_elapsed:.0f}s\n", flush=True)
                log_applied(company, job_ats, title, job_url, err, email, location=loc_str)
                errors += 1

            finally:
                # Always runs — success OR error. Wrapped in its own try so a
                # bad browser state here never escapes the loop and kills the
                # async-with-playwright block (which would close the browser).
                try:
                    # Close any extra tabs opened during this job
                    for extra in list(ctx.pages[1:]):
                        try:
                            await extra.close()
                        except Exception:
                            pass
                    # Verify the main page is still alive; create fresh one if not
                    _main = ctx.pages[0] if ctx.pages else None
                    if _main is None:
                        page = await ctx.new_page()
                    else:
                        try:
                            await _main.title()  # raises if closed
                            page = _main
                        except Exception:
                            page = await ctx.new_page()
                except Exception:
                    try:
                        page = await ctx.new_page()
                    except Exception:
                        pass

                if i < len(jobs):
                    await asyncio.sleep(BETWEEN_JOBS)

        await ctx.close()

    print(f"\n{'─'*62}")
    print(f"  {company} summary:")
    print(f"    Applied  : {applied}")
    print(f"    Skipped  : {skipped}")
    print(f"    Errors   : {errors}")
    print(f"  Log saved to: {APPLIED_LOG_PATH.name}")
    print(f"{'─'*62}\n")


# ── Profile setup sub-command ───────────────────────────────────────────────────

def setup_profiles():
    all_p = load_all_profiles()
    if not all_p:
        print("No profiles found in profiles.json.")
        return
    for em, p in all_p.items():
        print(f"\n  Profile: {p.get('name', em)} <{em}>")
        ensure_profile_complete(p, em)
    print("All profiles up to date.")


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _pick_profile(all_profiles: dict, arg_email: Optional[str]) -> Optional[str]:
    emails = list(all_profiles.keys())
    if not emails:
        return None
    if arg_email:
        if arg_email in all_profiles:
            return arg_email
        print(f"  Profile '{arg_email}' not found.")
        return None
    if len(emails) == 1:
        return emails[0]
    print("  Select profile:")
    for i, em in enumerate(emails, 1):
        print(f"    {i}. {all_profiles[em].get('name', '?')} <{em}>")
    try:
        idx = int(input("  Choice [1]: ").strip() or "1") - 1
        return emails[max(0, min(idx, len(emails) - 1))]
    except (ValueError, EOFError):
        return emails[0]


def main():
    ap = argparse.ArgumentParser(
        description="Apply to all matching jobs at a company's careers page."
    )
    ap.add_argument("--company",  help="Company name (must exist in company_careers_db.json)")
    ap.add_argument("--profile",  help="Gmail address of the profile to use")
    ap.add_argument("--keywords",
                    help="Comma-separated title keywords, e.g. 'python,engineer,ai'. "
                         "Defaults to profile skills.")
    ap.add_argument("--location",
                    help="Comma-separated location filters, e.g. 'remote,austin,new york'.")
    ap.add_argument("--posted-days", type=int,
                    help="Only include jobs posted within the last N days, e.g. 30.")
    ap.add_argument("--experience",
                    help="Comma-separated experience-level keywords in title, e.g. 'senior,staff,principal'.")
    ap.add_argument("--work-type",
                    help="Comma-separated work-type keywords, e.g. 'internship,contract'.")
    ap.add_argument("--us-only", action="store_true",
                    help="Only apply to jobs located in the United States.")
    ap.add_argument("--all-roles", action="store_true",
                    help="Apply to ALL roles — do not filter by keywords (use with --experience / --location).")
    ap.add_argument("--update-filters", action="store_true",
                    help="Re-prompt for filter values and overwrite saved ones.")
    ap.add_argument("--dry-run",  action="store_true",
                    help="List matching jobs without applying")
    ap.add_argument("--max-jobs", type=int, default=0,
                    help="Stop after applying to this many jobs (0 = no limit)")
    ap.add_argument("--list",     action="store_true",
                    help="List all companies in the DB")
    ap.add_argument("--add",      action="store_true",
                    help="Add a new company to the DB")
    ap.add_argument("--setup",          action="store_true",
                    help="Add phone / LinkedIn to your profiles")
    ap.add_argument("--setup-workday",  action="store_true",
                    help="Sign in to a company's Workday portal once (saves session for future runs)")
    args = ap.parse_args()

    # ── Sub-commands ──────────────────────────────────────────────────────────
    if args.setup:
        setup_profiles()
        return

    if getattr(args, "setup_workday", False):
        # Resolve company + profile first, then open browser for login
        all_profiles = load_all_profiles()
        email = _pick_profile(all_profiles, args.profile)
        if not email:
            sys.exit(1)
        db = load_company_db()
        company_name = args.company
        if not company_name:
            print("  --setup-workday requires --company")
            sys.exit(1)
        company_rec = find_company(company_name, db)
        if not company_rec:
            print(f"  '{company_name}' not found. Run --add first.")
            sys.exit(1)
        if company_rec.get("ats") != "workday":
            print(f"  {company_rec['name']} uses '{company_rec['ats']}' ATS, not Workday.")
            sys.exit(1)
        asyncio.run(setup_workday_session(company_rec, email))
        return

    if args.add:
        add_company_interactive()
        return

    if args.list:
        db = load_company_db()
        if not db:
            print("Company DB is empty. Run: python company_apply.py --add")
            return
        print(f"\n  {'Company':<25} {'ATS':<14} {'Slug':<22} Careers URL")
        print(f"  {'─'*25} {'─'*14} {'─'*22} {'─'*40}")
        for _, rec in sorted(db.items(), key=lambda x: x[1]["name"].lower()):
            print(
                f"  {rec['name']:<25} {rec['ats']:<14} "
                f"{rec.get('slug',''):<22} {rec.get('careers_url','')}"
            )
        print()
        return

    # ── Main apply flow ───────────────────────────────────────────────────────
    all_profiles = load_all_profiles()
    if not all_profiles:
        print("No profiles found. Add entries to profiles.json first.")
        sys.exit(1)

    email = _pick_profile(all_profiles, args.profile)
    if not email:
        sys.exit(1)

    profile = all_profiles[email].copy()
    profile["email"] = email
    print(f"\n  Profile: {profile.get('name')} <{email}>")
    profile = ensure_profile_complete(profile, email)

    # ── Pick company ──────────────────────────────────────────────────────────
    db = load_company_db()
    company_name = args.company
    if not company_name:
        if not db:
            print("Company DB is empty. Run: python company_apply.py --add")
            sys.exit(1)
        print("\n  Available companies:")
        for _, rec in sorted(db.items(), key=lambda x: x[1]["name"].lower()):
            print(f"    • {rec['name']}  ({rec['ats']})")
        try:
            company_name = input("\n  Enter company name: ").strip()
        except EOFError:
            company_name = ""

    company_rec = find_company(company_name, db)
    if not company_rec:
        print(f"\n  '{company_name}' not found in company_careers_db.json.")
        print("  Run: python company_apply.py --add   to add it.")
        sys.exit(1)

    # ── Keywords ──────────────────────────────────────────────────────────────
    if getattr(args, "all_roles", False):
        # Dashboard passed --all-roles: skip keyword filter entirely
        keywords = []
        print("  Keywords   : (all roles — no keyword filter)")
    elif args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    else:
        # Split skills by comma, then also split current_title into individual
        # words so "Gen ai engineer" → ["gen", "ai", "engineer"] instead of
        # one unsearchable string. Keep tokens >= 2 chars to include "ai","ml".
        skill_tokens = [k.strip() for k in profile.get("skills", "").split(",")]
        title_tokens = profile.get("current_title", "").split()
        raw_tokens   = skill_tokens + title_tokens
        keywords     = list(dict.fromkeys(          # preserve order, dedupe
            k.lower() for k in raw_tokens if len(k.strip()) >= 2
        ))
        if keywords:
            shown = ", ".join(keywords[:6]) + ("..." if len(keywords) > 6 else "")
            print(f"  Auto-keywords from profile: {shown}")
            try:
                use_all = input("  Apply to ALL roles instead? [y/N]: ").strip().lower()
            except EOFError:
                use_all = "y"   # when stdin is closed (dashboard mode), default to all roles
            if use_all == "y":
                keywords = []

    # ── Filters ───────────────────────────────────────────────────────────────
    def _ask(prompt, default=""):
        try:
            return input(prompt).strip() or default
        except EOFError:
            return default

    def _parse_list(raw):
        stripped = raw.strip().lower()
        if stripped in ("all", "any", ""):
            return []
        return [x.strip() for x in stripped.split(",") if x.strip()]

    # CLI flags override everything; --update-filters forces re-prompt
    cli_filters = bool(args.location or args.posted_days or args.experience or args.work_type)
    saved_f = {} if cli_filters or args.update_filters else load_saved_filters(email)

    # --us-only: CLI flag takes priority; else load from saved filters
    us_only = args.us_only or saved_f.get("us_only", False)
    need_prompt = args.update_filters or (not cli_filters and not saved_f)

    if need_prompt:
        print(f"\n  {'─'*56}")
        print(f"  Filters  (press Enter to skip / type 'all' to clear)")
        print(f"  {'─'*56}")

    if args.location:
        locations = _parse_list(args.location)
    elif need_prompt:
        locations = _parse_list(_ask("  Location    (e.g. remote, austin, new york): "))
    else:
        locations = saved_f.get("locations", [])

    # Strip the dashboard's us-only sentinel from locations — us_only flag handles it
    locations = [l for l in locations if l != "__us_only__"]

    if args.posted_days:
        posted_days = args.posted_days
    elif need_prompt:
        raw = _ask("  Posted within (days, e.g. 7 / 14 / 30): ")
        try:
            posted_days = int(raw) if raw else None
        except ValueError:
            posted_days = None
    else:
        posted_days = saved_f.get("posted_days")

    if args.experience:
        experience = _parse_list(args.experience)
    elif need_prompt:
        experience = _parse_list(_ask("  Experience  (e.g. senior, staff, principal, junior): "))
    else:
        experience = saved_f.get("experience", [])

    if args.work_type:
        work_type = _parse_list(args.work_type)
    elif need_prompt:
        work_type = _parse_list(_ask("  Work type   (e.g. full-time, internship, contract): "))
    else:
        work_type = saved_f.get("work_type", [])

    if need_prompt:
        print(f"  {'─'*56}")
        # Persist what the user just entered
        save_filters(email, {
            "locations":   locations,
            "posted_days": posted_days,
            "experience":  experience,
            "work_type":   work_type,
            "us_only":     us_only,
        })
        print(f"  Filters saved — next run will reuse them automatically.")
    else:
        # Show what's being used
        def _fmt(lst): return ", ".join(lst) if lst else "any"
        print(f"\n  Using saved filters:")
        print(f"    Location   : {_fmt(locations)}")
        print(f"    Posted     : {'last ' + str(posted_days) + ' days' if posted_days else 'any time'}")
        print(f"    Experience : {_fmt(experience)}")
        print(f"    Work type  : {_fmt(work_type)}")
        print(f"    US only    : {us_only}")
        print(f"  (run with --update-filters to change)\n")

    asyncio.run(
        apply_to_company(
            company_rec, profile, email, keywords,
            locations=locations,
            posted_days=posted_days,
            experience=experience,
            work_type=work_type,
            us_only=us_only,
            dry_run=args.dry_run,
            max_jobs=args.max_jobs,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # Pipe closed (e.g. dashboard stopped) — exit silently
        import os
        os._exit(0)
