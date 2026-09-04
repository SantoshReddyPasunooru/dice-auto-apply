"""
LinkedIn Recruiter Outreach
===========================
Searches LinkedIn posts for job opportunities (OPT / W2 / C2C / etc.),
extracts recruiter email addresses, and sends cold outreach emails via Gmail.

Commands:
  python linkedin_outreach.py            — run with saved config
  python linkedin_outreach.py --setup    — (re)configure search settings
  python linkedin_outreach.py --login    — open browser to log in to LinkedIn once
"""

import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext

import gmail_sender
from recruiter_db import recruiter_db

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────

_HERE          = Path(__file__).parent
LI_CONFIG_FILE = _HERE / "linkedin_config.json"
PROFILES_JSON  = _HERE / "profiles.json"
RESUMES_JSON   = _HERE / "resumes.json"

OLLAMA_MODEL   = "gemma2:2b"


def _profile_session_dir(sender_email: str) -> Path:
    """Each profile gets its own dir for gmail_token, sent_emails, and li_state."""
    safe = re.sub(r"[^a-z0-9]", "_", sender_email.lower())
    d = Path.home() / f".dice-playwright-profile-{safe}"
    d.mkdir(parents=True, exist_ok=True)
    return d

# ── Config setup ──────────────────────────────────────────────────────────────

_JOB_TYPE_OPTIONS = ["OPT", "W2", "C2C", "1099", "Corp-to-Corp", "Contract", "Full-time"]
_DATE_OPTIONS     = {
    "1": ("past-week",  "Past week"),
    "2": ("past-month", "Past month"),
    "3": ("",           "Any time"),
}
_EXPERIENCE_OPTIONS = {
    "1": ("junior",  "Junior / Entry Level  (0-2 yrs)"),
    "2": ("mid",     "Mid Level             (2-5 yrs)"),
    "3": ("senior",  "Senior                (5+ yrs)"),
    "4": ("lead",    "Lead / Principal / Staff"),
    "5": ("any",     "Any experience level  (no filter)"),
}
# Keywords used to (a) add to the LinkedIn search query and (b) filter post text
_EXPERIENCE_SEARCH_TERMS: dict[str, list[str]] = {
    "junior":  ["junior", "entry level", "entry-level", "new grad"],
    "mid":     ["mid level", "mid-level", "associate"],
    "senior":  ["senior", "sr."],
    "lead":    ["lead", "principal", "staff engineer"],
}
_EXPERIENCE_POST_KEYWORDS: dict[str, list[str]] = {
    "junior":  ["junior", "entry level", "entry-level", "new grad",
                "0-2 year", "1-2 year", "1-3 year", "fresher"],
    "mid":     ["mid level", "mid-level", "associate", "2-4 year",
                "2-5 year", "3-5 year", "3+ year"],
    "senior":  ["senior", "sr.", " sr ", "5+ year", "6+ year",
                "7+ year", "5-8 year", "5 year", "experienced"],
    "lead":    ["lead", "principal", "staff engineer", "tech lead",
                "architect", "10+ year", "8+ year"],
}


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val if val else default


def prompt_li_config(preset_email: str | None = None) -> dict:
    """Interactive terminal config — saved per-profile in linkedin_config.json."""

    all_cfg = _load_all_configs()

    # Load available sender profiles
    profiles: dict = {}
    if PROFILES_JSON.exists():
        try:
            profiles = json.loads(PROFILES_JSON.read_text())
        except Exception:
            pass

    print(f"\n{'═'*60}")
    print("  LinkedIn Outreach — Profile Setup")
    print("  Answers saved to linkedin_config.json for future runs.")
    print(f"{'═'*60}\n")

    # ── Sender profile ────────────────────────────────────────────────────────
    if preset_email:
        sender_email = preset_email
        print(f"  Configuring profile: {sender_email}")
    elif profiles:
        print("  Available sender profiles:")
        emails = list(profiles.keys())
        for i, e in enumerate(emails, 1):
            name = profiles[e].get("name", "")
            already = " ✓ configured" if e in all_cfg else ""
            print(f"    {i}. {e}  ({name}){already}")
        choice = _ask("Select sender profile (number)", "1")
        try:
            sender_email = emails[int(choice) - 1]
        except Exception:
            sender_email = emails[0]
    else:
        sender_email = _ask("Sender email (Gmail)", "")

    existing = all_cfg.get(sender_email, {})

    # ── Search keywords ───────────────────────────────────────────────────────
    print("\n  Search keywords are combined into a LinkedIn post search.")
    print("  Example: python gen ai llm aws")
    kw_default = " ".join(existing.get("search_keywords", ["python", "gen ai"]))
    kw_raw = _ask("Search keywords (space-separated)", kw_default)
    search_keywords = [k.strip() for k in kw_raw.split() if k.strip()]

    # ── Job type filters ──────────────────────────────────────────────────────
    print("\n  Job type filters (adds to search query so only matching posts appear).")
    print("  Options:", ", ".join(_JOB_TYPE_OPTIONS))
    jt_default = " ".join(existing.get("job_types", ["OPT", "W2", "C2C"]))
    jt_raw = _ask("Job types to include (space-separated)", jt_default)
    job_types = [j.strip().upper() for j in jt_raw.split() if j.strip()]

    # ── Target roles ──────────────────────────────────────────────────────────
    roles_default = ", ".join(existing.get("target_roles", ["Software Engineer", "Gen AI Engineer"]))
    roles_raw = _ask("Target roles (comma-separated)", roles_default)
    target_roles = [r.strip() for r in roles_raw.split(",") if r.strip()]

    # ── Date filter ───────────────────────────────────────────────────────────
    print("\n  Date filter:")
    for k, (_, label) in _DATE_OPTIONS.items():
        print(f"    {k}. {label}")
    existing_date = existing.get("date_filter", "past-week")
    default_date_key = next(
        (k for k, (v, _) in _DATE_OPTIONS.items() if v == existing_date), "1"
    )
    date_choice = _ask("Select date filter (number)", default_date_key)
    date_filter = _DATE_OPTIONS.get(date_choice, _DATE_OPTIONS["1"])[0]

    # ── Experience level filter ───────────────────────────────────────────────
    print("\n  Experience level filter:")
    for k, (_, label) in _EXPERIENCE_OPTIONS.items():
        print(f"    {k}. {label}")
    print("  Enter one or more numbers separated by spaces  (e.g. '2 3' = Mid + Senior)")
    existing_levels = existing.get("experience_levels", ["any"])
    default_exp_keys = " ".join(
        k for k, (v, _) in _EXPERIENCE_OPTIONS.items() if v in existing_levels
    ) or "5"
    exp_raw    = _ask("Experience levels", default_exp_keys)
    exp_chosen = [_EXPERIENCE_OPTIONS[k.strip()][0]
                  for k in exp_raw.split() if k.strip() in _EXPERIENCE_OPTIONS]
    # "any" overrides everything else; default to ["any"] if nothing valid chosen
    if not exp_chosen or "any" in exp_chosen:
        experience_levels = ["any"]
    else:
        experience_levels = exp_chosen
    print(f"  → Selected: {', '.join(experience_levels)}")

    # ── Volume limits ─────────────────────────────────────────────────────────
    max_posts  = int(_ask("Max posts to scan per run",    str(existing.get("max_posts", 60))))
    max_emails = int(_ask("Max emails to send per run",   str(existing.get("max_emails", 25))))
    delay_min  = float(_ask("Min delay between posts (sec)", str(existing.get("delay_min", 6))))
    delay_max  = float(_ask("Max delay between posts (sec)", str(existing.get("delay_max", 14))))

    config = {
        "sender_email":    sender_email,
        "search_keywords": search_keywords,
        "job_types":       job_types,
        "target_roles":    target_roles,
        "date_filter":     date_filter,
        "experience_levels": experience_levels,
        "max_posts":       max_posts,
        "max_emails":      max_emails,
        "delay_min":       delay_min,
        "delay_max":       delay_max,
        "updated_at":      datetime.now().isoformat(timespec="seconds"),
    }

    _save_config(sender_email, config)
    print(f"\n  ✓ Config saved for {sender_email}\n")
    return config


def _load_all_configs() -> dict:
    """Return {email: config_dict} from linkedin_config.json. Migrates old flat format."""
    if not LI_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(LI_CONFIG_FILE.read_text())
        # Migrate old single-profile flat format (had 'sender_email' at top level)
        if "sender_email" in data:
            email = data["sender_email"]
            migrated = {email: data}
            LI_CONFIG_FILE.write_text(json.dumps(migrated, indent=2))
            print(f"  [config] Migrated old config → keyed by {email}")
            return migrated
        return data
    except Exception:
        return {}


def _save_config(email: str, config: dict):
    all_cfg = _load_all_configs()
    all_cfg[email] = config
    LI_CONFIG_FILE.write_text(json.dumps(all_cfg, indent=2))


def load_li_config(sender_email: str | None = None) -> dict:
    """Return config for one profile. If email is None, picks the only/first one."""
    all_cfg = _load_all_configs()
    if not all_cfg:
        print("  No profiles configured — running setup first.\n")
        return prompt_li_config()
    if sender_email:
        if sender_email not in all_cfg:
            print(f"  Profile '{sender_email}' not found — running setup.\n")
            return prompt_li_config(sender_email)
        return all_cfg[sender_email]
    if len(all_cfg) == 1:
        return next(iter(all_cfg.values()))
    # Multiple profiles — ask the user to pick
    emails = list(all_cfg.keys())
    print("\n  Multiple LinkedIn profiles configured:")
    for i, e in enumerate(emails, 1):
        print(f"    {i}. {e}")
    choice = input("  Select profile (number): ").strip()
    try:
        return all_cfg[emails[int(choice) - 1]]
    except Exception:
        return all_cfg[emails[0]]


# ── LinkedIn browser session ──────────────────────────────────────────────────

async def launch_li_session(pw, session_dir: Path) -> tuple[BrowserContext, Page]:
    """
    Launch Playwright browser using the session saved by --login.
    No manual login needed after the first time.
    """
    state_file = session_dir / "li_state.json"
    if not state_file.exists():
        # Derive email from dir name for a helpful message
        dir_stem = session_dir.name.replace(".dice-playwright-profile-", "").replace("_gmail_com", "@gmail.com").replace("_", ".")
        print(f"  ✗ No LinkedIn session found for {dir_stem}")
        print(f"  Run:  python linkedin_outreach.py --login --profile {dir_stem}\n")
        raise RuntimeError(f"No LinkedIn session for {dir_stem}")

    # launch_persistent_context lets us set user_data_dir per profile
    # so two parallel Chrome instances don't fight over the same profile dir.
    chrome_profile_dir = session_dir / "chrome_profile"
    chrome_profile_dir.mkdir(parents=True, exist_ok=True)

    context = await pw.chromium.launch_persistent_context(
        str(chrome_profile_dir),
        headless=False,
        channel="chrome",
        slow_mo=60,
        args=["--disable-blink-features=AutomationControlled", "--disable-infobars"],
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 800},
    )
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )

    # Inject saved LinkedIn cookies (storage_state isn't supported by launch_persistent_context)
    try:
        state   = json.loads(state_file.read_text())
        cookies = state.get("cookies", [])
        if cookies:
            await context.add_cookies(cookies)
    except Exception as e:
        print(f"  [warn] Could not load session cookies: {e}")

    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    await asyncio.sleep(2)

    if any(k in page.url for k in ("authwall", "login", "signup", "uas/login")):
        print("  ✗ LinkedIn session expired — run:  python linkedin_outreach.py --login --profile <email>")
        await context.close()
        raise RuntimeError("LinkedIn session expired")

    print("  ✓ LinkedIn session ready\n")
    return context, page


# ── LinkedIn search navigation ────────────────────────────────────────────────

def _build_li_search_url(keywords: list[str], job_types: list[str],
                         date_filter: str,
                         experience_levels: list[str] | None = None) -> str:
    from urllib.parse import quote_plus

    # Merge job types + keywords — deduplicate, preserve order
    all_terms = list(dict.fromkeys(job_types + keywords))

    # Append the first search term for each chosen experience level
    # (just one term per level keeps the query readable; post-filter does the heavy lifting)
    if experience_levels and "any" not in experience_levels:
        for level in experience_levels:
            terms = _EXPERIENCE_SEARCH_TERMS.get(level, [])
            if terms:
                # Add only the first/most distinctive term if not already in query
                term = terms[0]
                if not any(term.lower() in t.lower() for t in all_terms):
                    all_terms.append(term)

    query = " ".join(all_terms)
    url   = (
        "https://www.linkedin.com/search/results/content/"
        f"?keywords={quote_plus(query)}&sortBy=date_posted"
    )
    if date_filter:
        url += f"&datePosted={quote_plus(date_filter)}"
    return url


async def navigate_to_search(page: Page, config: dict):
    url = _build_li_search_url(
        config["search_keywords"],
        config["job_types"],
        config.get("date_filter", "past-week"),
        config.get("experience_levels", ["any"]),
    )
    print(f"  → Searching: {url}\n")
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(6)  # give LinkedIn time to render results

    # LinkedIn sometimes defaults to "All" tab — click "Posts" if visible
    for tab_text in ["Posts", "Content"]:
        try:
            tab = page.get_by_role("tab", name=tab_text).first
            if await tab.is_visible(timeout=2000):
                await tab.click()
                await asyncio.sleep(4)
                print(f"  → Clicked '{tab_text}' tab")
                break
        except Exception:
            pass

    # Wait for at least one result container to appear
    for sel in ["[data-urn]", "[class*='search-result']", "[class*='entity-result']", "main li"]:
        try:
            await page.wait_for_selector(sel, timeout=5000)
            print(f"  → Results loaded (matched: {sel})")
            break
        except Exception:
            pass
    else:
        print("  [warn] Could not confirm results loaded — proceeding anyway")


# ── Post collection ───────────────────────────────────────────────────────────

async def _expand_see_more(post_el) -> None:
    """Click 'see more' to get the full post text."""
    try:
        btn = post_el.locator(
            "button[aria-label*='see more' i], "
            "button[class*='see-more-less-toggle'], "
            "span.see-more, button:has-text('...more')"
        ).first
        if await btn.is_visible(timeout=400):
            await btn.click()
            await asyncio.sleep(0.4)
    except Exception:
        pass


async def collect_visible_posts(page: Page, seen_urns: set) -> list[dict]:
    """
    Extract post data from currently visible search result items.
    Uses multiple JS strategies to handle LinkedIn DOM changes.
    """
    # Click "see more" on all visible posts before extracting text
    try:
        await page.evaluate("""
            () => {
                const btns = document.querySelectorAll(
                    'button[aria-label*="see more" i], button[class*="see-more"], ' +
                    'span[class*="see-more"], button:has-text("more")'
                );
                for (const b of btns) { try { b.click(); } catch(e) {} }
            }
        """)
        await asyncio.sleep(0.5)
    except Exception:
        pass

    raw_posts = await page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();

            function addPost(text, urn, author, url) {
                text = (text || '').trim();
                if (text.length < 80) return;
                const key = text.substring(0, 80);
                if (seen.has(key)) return;
                seen.add(key);
                results.push({ urn: urn || key, text, author: author || '', url: url || '' });
            }

            function getAuthor(el) {
                const a = el.querySelector(
                    '[class*="actor__name"] span[aria-hidden="true"],' +
                    '[class*="actor__name"],' +
                    '[class*="update-components-actor__name"] span,' +
                    '[class*="app-aware-link"][href*="/in/"]'
                );
                return a ? a.innerText.trim().split('\\n')[0] : '';
            }

            function getUrl(el) {
                const a = el.querySelector('a[href*="/feed/update/"],a[href*="/posts/"]');
                return a ? a.href.split('?')[0] : '';
            }

            // Strategy 1: data-urn (feed items and search results both use this)
            for (const el of document.querySelectorAll('[data-urn]')) {
                const urn = el.getAttribute('data-urn');
                addPost(el.innerText, urn, getAuthor(el), getUrl(el));
            }

            // Strategy 2: entity-result containers (search results page)
            if (results.length === 0) {
                const selectors = [
                    '[class*="search-result"]',
                    '[class*="entity-result"]',
                    '[class*="reusable-search"]',
                    '[class*="occludable-update"]',
                    '[class*="feed-shared-update"]',
                    'article',
                ];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        const urn = el.getAttribute('data-urn') || el.getAttribute('data-id') || '';
                        addPost(el.innerText, urn, getAuthor(el), getUrl(el));
                    }
                    if (results.length > 0) break;
                }
            }

            // Strategy 3: scan the whole page for significant text blocks
            if (results.length === 0) {
                const root = document.querySelector('main,[role="main"]') || document.body;
                // Collect all elements, sorted richest-first, skip wrappers
                const blocks = [];
                for (const el of root.querySelectorAll('div,li,section,article')) {
                    const text = (el.innerText || '').trim();
                    if (text.length < 100 || text.length > 8000) continue;
                    blocks.push({ el, text });
                }
                // Add only "leaf-ish" blocks: no large-text child that covers most of its text
                for (const b of blocks) {
                    const childTexts = [...b.el.querySelectorAll('div,li')].map(
                        c => (c.innerText || '').length
                    );
                    const maxChild = childTexts.length ? Math.max(...childTexts) : 0;
                    if (maxChild < b.text.length * 0.75) {
                        addPost(b.text, '', getAuthor(b.el), getUrl(b.el));
                    }
                }
            }

            return results;
        }
    """)

    if not raw_posts:
        try:
            shot = _HERE / "debug_linkedin_search.png"
            await page.screenshot(path=str(shot), full_page=False)
            print(f"  [debug] No posts found — screenshot: {shot.name}")
            print(f"  [debug] URL: {page.url}")
            # Dump a DOM snippet to help diagnose
            snippet = await page.evaluate("""
                () => {
                    const main = document.querySelector('main,[role="main"]') || document.body;
                    return main.innerHTML.substring(0, 2000);
                }
            """)
            print(f"  [debug] DOM snippet: {snippet[:500]}")
        except Exception:
            pass
        return []

    posts = []
    for p in raw_posts:
        urn = str(p.get("urn", ""))
        if urn in seen_urns:
            continue
        seen_urns.add(urn)
        text = p.get("text", "").strip()
        if len(text) < 80:
            continue
        posts.append({
            "urn":    urn,
            "text":   text,
            "author": p.get("author", ""),
            "url":    p.get("url", ""),
        })

    print(f"  [posts] Found {len(posts)} new posts on page")
    return posts


async def scroll_for_more(page: Page) -> bool:
    """Scroll to trigger LinkedIn infinite scroll. Returns False only if truly at end."""
    try:
        prev_height = await page.evaluate("document.body.scrollHeight")

        # Scroll every scrollable container (LinkedIn puts results in an inner div)
        await page.evaluate("""
            () => {
                // Scroll the window and every candidate container
                window.scrollTo(0, document.body.scrollHeight);
                window.scrollBy(0, 5000);
                const sels = [
                    '.search-results-container',
                    '[class*="scaffold-finite-scroll"]',
                    '[class*="search-results__list"]',
                    'main',
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el) { el.scrollTop = el.scrollHeight; }
                }
            }
        """)
        # Give LinkedIn time to fetch and render the next batch
        await asyncio.sleep(random.uniform(5.0, 7.0))
        new_height = await page.evaluate("document.body.scrollHeight")

        # Keyboard End key as a secondary trigger
        if new_height <= prev_height:
            await page.keyboard.press("End")
            await asyncio.sleep(3)
            new_height = await page.evaluate("document.body.scrollHeight")

        # "Show more results" button (LinkedIn shows this at intervals)
        for btn_text in ["Show more results", "See more results", "Load more"]:
            try:
                btn = page.locator(f"button:has-text('{btn_text}')").first
                if await btn.is_visible(timeout=600):
                    await btn.click()
                    await asyncio.sleep(4)
                    return True
            except Exception:
                pass

        return new_height > prev_height
    except Exception:
        return False


# ── Post analysis ─────────────────────────────────────────────────────────────

_GENERIC_NAMES = {
    "hiring", "manager", "team", "hr", "recruiter", "talent", "acquisition",
    "staffing", "noreply", "no-reply", "hello", "info", "jobs", "careers",
    "support", "admin", "contact", "dear", "there",
}


def _extract_first_name(raw: str) -> str:
    """
    Extract and return the recruiter's first name from a full name string,
    a 'Full Name <email>' header, or a LinkedIn author field.
    Returns "" if no clean first name can be found.
    """
    # Strip email address part
    name = re.sub(r"<[^>]+>", "", raw).strip().strip('"').strip("'")
    # Take only the first token
    first = name.split()[0] if name else ""
    # Discard generic / role-based words and non-alpha strings
    if not first or first.lower() in _GENERIC_NAMES or not re.match(r"^[A-Za-z\-']{2,}$", first):
        return ""
    return first.capitalize()


def _extract_name_from_post(text: str) -> str:
    """
    Try to pull the recruiter's first name from common LinkedIn post patterns.
    e.g. "Hi, I'm Sarah from Acme…" or "— John | Recruiter at…"
    """
    patterns = [
        r"(?:I'm|I am|this is|hi[,!]?\s+i'm)\s+([A-Z][a-z]{1,20})",
        r"^([A-Z][a-z]{1,20})\s+\|",
        r"\|\s*([A-Z][a-z]{1,20})\s*\|",
        r"—\s*([A-Z][a-z]{1,20})\b",
        r"regards[,\s]+([A-Z][a-z]{1,20})\b",
        r"thanks[,\s]+([A-Z][a-z]{1,20})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            name = m.group(1).capitalize()
            if name.lower() not in _GENERIC_NAMES:
                return name
    return ""


_JOB_KEYWORDS = [
    "hiring", "we're hiring", "we are hiring", "looking for", "now hiring",
    "open role", "open position", "job opportunity", "job opening",
    "seeking a", "seeking an", "join our team", "send resume", "send cv",
    "dm me", "dm your resume", "apply now", "email your resume",
    "position available", "actively hiring", "immediately hiring",
    "w2", "opt", "c2c", "corp-to-corp", "1099",
]

_SKIP_KEYWORDS = [
    "congratulations", "congrats", "happy to share", "excited to announce",
    "i got a new job", "i joined", "i started", "i'm thrilled",
    "promoted to", "new chapter", "new role at",
]


def is_job_post(text: str) -> bool:
    """
    Fast keyword check: True if this looks like a recruiter job posting.
    Skips self-promotion / celebration posts.
    """
    lower = text.lower()
    if any(kw in lower for kw in _SKIP_KEYWORDS):
        return False
    return any(kw in lower for kw in _JOB_KEYWORDS)


def is_experience_match(text: str, experience_levels: list[str]) -> bool:
    """
    Returns True if the post text matches at least one of the configured
    experience levels, or if the filter is set to 'any' / not configured.
    Posts with NO experience mention at all are always allowed through.
    """
    if not experience_levels or "any" in experience_levels:
        return True

    lower = text.lower()

    # Collect every known experience keyword across ALL levels
    all_exp_kws = [kw for kws in _EXPERIENCE_POST_KEYWORDS.values() for kw in kws]

    # If the post doesn't mention any experience level at all, let it through
    if not any(kw in lower for kw in all_exp_kws):
        return True

    # Post DOES mention experience — check if it matches a requested level
    return any(
        any(kw in lower for kw in _EXPERIENCE_POST_KEYWORDS.get(level, []))
        for level in experience_levels
    )


def extract_email(text: str) -> str | None:
    """
    Extract a recruiter email from post text.
    Handles direct emails and common obfuscation patterns.
    Returns None if no email found (caller skips the post).
    """
    _SKIP = [
        "noreply", "no-reply", "donotreply", "support@", "info@", "hello@",
        "contact@", "careers@dice", "linkedin.com", "example.com", "sentry.io",
        "privacy@", "legal@", "abuse@",
    ]

    # 1. Direct email
    m = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    if m:
        email = m.group().lower().rstrip(".")
        if not any(s in email for s in _SKIP):
            return email

    # 2. Obfuscated: "john at company dot com"
    m = re.search(
        r"([a-zA-Z0-9._%+\-]+)\s+(?:at|@)\s+([a-zA-Z0-9.\-]+)\s+(?:dot|\.)\s+([a-zA-Z]{2,})",
        text, re.I,
    )
    if m:
        email = f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower()
        if not any(s in email for s in _SKIP):
            return email

    # 3. Bracket obfuscation: "john[@]company.com" or "john[at]company.com"
    m = re.search(
        r"([a-zA-Z0-9._%+\-]+)\s*[\[\(](?:at|@)[\]\)]\s*([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
        text, re.I,
    )
    if m:
        email = f"{m.group(1)}@{m.group(2)}".lower()
        if not any(s in email for s in _SKIP):
            return email

    # 4. Ask Ollama to de-obfuscate if none found above
    try:
        import ollama
        snippet = text[:600]
        resp = ollama.chat(model=OLLAMA_MODEL, messages=[{
            "role": "user",
            "content": (
                f"Does this LinkedIn post contain an email address (direct or obfuscated)?\n\n"
                f"POST:\n{snippet}\n\n"
                f"If yes, reply with ONLY the email address (e.g. john@company.com).\n"
                f"If no email found, reply: NONE"
            ),
        }])
        result = resp.message.content.strip()
        if result.upper() != "NONE" and "@" in result:
            m2 = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", result)
            if m2:
                email = m2.group().lower()
                if not any(s in email for s in _SKIP):
                    return email
    except Exception:
        pass

    return None


def _extract_job_details_sync(text: str) -> dict:
    try:
        import ollama
        resp = ollama.chat(model=OLLAMA_MODEL, messages=[{
            "role": "user",
            "content": (
                f"Extract job details from this LinkedIn post. Reply with ONLY valid JSON, no markdown.\n\n"
                f"POST:\n{text[:500]}\n\n"
                f'Reply: {{"title":"job title","company":"company or empty","location":"city/remote/empty","type":"OPT/W2/C2C/contract/fulltime"}}'
            ),
        }])
        raw = resp.message.content.strip()
        raw = re.sub(r"^```[a-z]*\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"```$", "", raw, flags=re.MULTILINE).strip()
        data  = json.loads(raw)
        title = data.get("title", "Software Engineer")
        if isinstance(title, list):
            title = title[0] if title else "Software Engineer"
        return {
            "title":    str(title) or "Software Engineer",
            "company":  str(data.get("company", "") or ""),
            "location": str(data.get("location", "") or ""),
            "type":     str(data.get("type", "") or ""),
        }
    except Exception:
        lower = text.lower()
        title = "Software Engineer"
        for role in ["engineer", "developer", "data scientist", "analyst",
                     "architect", "manager", "consultant", "specialist"]:
            if role in lower:
                title = role.title()
                break
        return {"title": title, "company": "", "location": "", "type": ""}


async def extract_job_details(text: str) -> dict:
    """Run Ollama synchronously in a thread so it doesn't block the event loop."""
    return await asyncio.to_thread(_extract_job_details_sync, text)


# ── Email composition ─────────────────────────────────────────────────────────

def _compose_cold_email_sync(
    recruiter_name: str,
    job_details: dict,
    post_snippet: str,
    profile: dict,
) -> tuple[str, str]:
    """
    Use Ollama to write a cold outreach email.
    Returns (subject, body).
    recruiter_name should be the raw author string from LinkedIn; we resolve
    the first name here so the greeting is always personal, never generic.
    """
    title     = job_details.get("title", "the role")
    company   = job_details.get("company", "") or "your company"
    name      = profile.get("name", "Applicant")
    work_auth = profile.get("work_auth", "OPT")
    years     = profile.get("years_experience", 3)
    skills    = profile.get("skills", "")[:120]
    location  = profile.get("location", "")
    available = profile.get("available_to_start", "immediately")

    # Resolve the recruiter's first name — try author field first, then post body
    first_name = _extract_first_name(recruiter_name or "")
    if not first_name:
        first_name = _extract_name_from_post(post_snippet)
    # greeting: "Hi Sarah," if found, otherwise just "Hi," — never "Hiring Manager"
    greeting = f"Hi {first_name}," if first_name else "Hi,"

    subject = f"Interested in {title} — {work_auth} candidate, {years} yrs exp"

    try:
        import ollama
        recruiter_line = (
            f"Recruiter first name: {first_name}" if first_name
            else "Recruiter name: unknown — do NOT invent a name or use generic titles"
        )
        prompt = (
            f"Write a short cold outreach email from a job seeker to a recruiter "
            f"who posted a job on LinkedIn.\n\n"
            f"{recruiter_line}\n"
            f"Role posted: {title} at {company}\n"
            f"Their post (snippet): {post_snippet[:250]}\n\n"
            f"Candidate:\n"
            f"  Name: {name}\n"
            f"  Title: {profile.get('current_title', 'Software Engineer')}\n"
            f"  Experience: {years} years\n"
            f"  Skills: {skills}\n"
            f"  Location: {location}\n"
            f"  Work auth: {work_auth}\n"
            f"  Available: {available}\n\n"
            f"Rules:\n"
            f"- Start with exactly: {greeting}\n"
            f"- 3-5 sentences max\n"
            f"- Reference their LinkedIn post naturally in the first sentence\n"
            f"- Mention work authorization and availability\n"
            f"- Professional, warm, confident tone\n"
            f"- End with: Best regards,\\n{name}\n"
            f"- Do NOT add a subject line inside the body\n"
            f"- Do NOT use placeholders like [Your Name] or [Company]\n"
            f"- Do NOT use generic names like 'Hiring Manager', 'Team', 'Recruiter'\n"
            f"- Write only the email body, nothing else\n"
        )
        resp = ollama.chat(model=OLLAMA_MODEL, messages=[{"role": "user", "content": prompt}])
        body = resp.message.content.strip()
        body = re.sub(r"(?i)^subject\s*:.*\n?", "", body).strip()
        # Ensure greeting is correct even if model ignored the instruction
        if not body.startswith("Hi"):
            body = f"{greeting}\n\n{body}"
    except Exception:
        body = (
            f"{greeting}\n\n"
            f"I came across your LinkedIn post about the {title} opportunity at {company} "
            f"and wanted to reach out directly.\n\n"
            f"I have {years} years of experience in {skills[:80]}, "
            f"currently based in {location}. "
            f"I'm on {work_auth} and available to start {available}.\n\n"
            f"I'd love to connect and discuss if there's a fit. "
            f"Please find my resume attached.\n\n"
            f"Best regards,\n{name}"
        )

    return subject, body


async def compose_cold_email(recruiter_name, job_details, post_snippet, profile) -> tuple[str, str]:
    """Run Ollama in a thread so it doesn't block the event loop."""
    return await asyncio.to_thread(
        _compose_cold_email_sync, recruiter_name, job_details, post_snippet, profile
    )


# ── Main outreach loop ────────────────────────────────────────────────────────

async def run_outreach(config: dict, tag: str = ""):
    """
    tag: short label prepended to every print line (e.g. "yagnesh", "santosh").
    Used when multiple profiles run in parallel so output is distinguishable.
    """
    pfx = f"[{tag}] " if tag else "  "
    p = lambda msg: print(f"{pfx}{msg}", flush=True)

    profile_data: dict = {}
    if PROFILES_JSON.exists():
        try:
            all_profiles = json.loads(PROFILES_JSON.read_text())
            profile_data = all_profiles.get(config["sender_email"], {})
        except Exception:
            pass

    sender_email = config["sender_email"]
    sender_name  = profile_data.get("name", config.get("sender_name", "Applicant"))
    session_dir  = _profile_session_dir(sender_email)

    # Init Gmail for the sender
    resume_path = ""
    if RESUMES_JSON.exists():
        try:
            rd = json.loads(RESUMES_JSON.read_text())
            resume_path = rd.get(sender_email, {}).get("default_resume", "")
        except Exception:
            pass

    # Each profile gets its own isolated GmailSender — no shared globals
    gs = gmail_sender.GmailSender()
    gs.init(
        profile_dir=session_dir,
        sender_name=sender_name,
        sender_email=sender_email,
        resume_path=resume_path,
    )

    exp_display = ", ".join(config.get("experience_levels", ["any"]))
    p(f"Sender    : {sender_name} <{sender_email}>")
    p(f"Keywords  : {' '.join(config['search_keywords'])}")
    p(f"Types     : {' '.join(config['job_types'])}")
    p(f"Experience: {exp_display}")
    p(f"Limit     : {config['max_posts']} posts / {config['max_emails']} emails")

    async with async_playwright() as pw:
        context, page = await launch_li_session(pw, session_dir)

        await navigate_to_search(page, config)

        seen_urns: set   = set()
        posts_seen: int  = 0
        emails_sent: int = 0

        empty_rounds     = 0
        scroll_no_change = 0
        while posts_seen < config["max_posts"] and emails_sent < config["max_emails"]:
            posts = await collect_visible_posts(page, seen_urns)

            if not posts:
                empty_rounds += 1
                if empty_rounds >= 8:
                    p("No new posts after 8 scroll attempts — stopping.")
                    break
                p(f"No new posts visible — scrolling... ({empty_rounds}/8)")
                grew = await scroll_for_more(page)
                scroll_no_change = 0 if grew else scroll_no_change + 1
                if scroll_no_change >= 4:
                    p("Page height unchanged after 4 scrolls — end of results.")
                    break
                continue
            empty_rounds     = 0
            scroll_no_change = 0

            for post in posts:
                if posts_seen >= config["max_posts"] or emails_sent >= config["max_emails"]:
                    break

                text = post["text"]
                posts_seen += 1

                if not is_job_post(text):
                    continue

                if not is_experience_match(text, config.get("experience_levels", ["any"])):
                    p(f"[skip] Experience level mismatch — post {posts_seen}")
                    continue

                email = extract_email(text)
                if not email:
                    continue

                if email in gs.sent_emails:
                    p(f"[skip] Already emailed {email}")
                    # Still update last_seen in the DB for this recruiter
                    recruiter_db.upsert(email=email, source="linkedin", status="contacted")
                    continue

                details   = await extract_job_details(text)
                recruiter = post.get("author", "")

                company_tag = f" @ {details['company']}" if details['company'] else ""
                p(f"[post {posts_seen}] {details['title']}{company_tag} — {email}")

                subject, body = await compose_cold_email(
                    recruiter_name=recruiter,
                    job_details=details,
                    post_snippet=text[:300],
                    profile=profile_data,
                )

                resume = gs.pick_resume(details["title"])

                sent = gs.send_cold_email(
                    to=email,
                    subject=subject,
                    body=body,
                    resume=resume,
                    source=post.get("url", "linkedin"),
                )

                if sent:
                    emails_sent += 1
                    p(f"✓ Email sent → {email}  (resume: {resume.name if resume else 'none'})")
                    recruiter_db.upsert(
                        email=email,
                        name=recruiter,
                        company=details.get("company", ""),
                        title=details.get("title", ""),
                        source="linkedin",
                        status="contacted",
                    )
                else:
                    p(f"✗ Failed to send → {email}")

                delay = random.uniform(config.get("delay_min", 6), config.get("delay_max", 14))
                await asyncio.sleep(delay)

            has_more = await scroll_for_more(page)
            if not has_more and posts_seen >= config["max_posts"]:
                break

        p(f"Done.  Posts scanned: {posts_seen}  |  Emails sent: {emails_sent}")
        await context.close()


# ── Login-only mode ───────────────────────────────────────────────────────────

async def login_only(sender_email: str):
    """
    Open a browser window so the user can log in to LinkedIn manually.
    Saves the full session state into the profile's session directory.
    """
    session_dir = _profile_session_dir(sender_email)
    state_file  = session_dir / "li_state.json"

    print(f"\n  Opening browser for profile: {sender_email}")
    print("  Log in to LinkedIn (email/password or Google — both work).")
    print("  Once you see your LinkedIn feed, come back here and press Enter.\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
            ],
            ignore_default_args=["--enable-automation"],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login")

        input("  → Press Enter once you can see your LinkedIn feed: ")

        await context.storage_state(path=str(state_file))
        await browser.close()

    print(f"\n  ✓ Session saved → {state_file}")
    print(f"  ✓ Run:  python linkedin_outreach.py --profile {sender_email}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def _get_profile_arg() -> str | None:
    """Return value of --profile <email> if provided, else None."""
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


if __name__ == "__main__":
    profile_arg = _get_profile_arg()

    if "--setup" in sys.argv:
        # python linkedin_outreach.py --setup [--profile email]
        prompt_li_config(preset_email=profile_arg)

    elif "--login" in sys.argv:
        # python linkedin_outreach.py --login --profile email@gmail.com
        if not profile_arg:
            # Pick from profiles.json if available
            _profiles: dict = {}
            if PROFILES_JSON.exists():
                try:
                    _profiles = json.loads(PROFILES_JSON.read_text())
                except Exception:
                    pass
            if _profiles:
                _emails = list(_profiles.keys())
                print("\n  Which profile to log in to LinkedIn with?")
                for i, e in enumerate(_emails, 1):
                    print(f"    {i}. {e}  ({_profiles[e].get('name','')})")
                _choice = input("  Select (number): ").strip()
                try:
                    profile_arg = _emails[int(_choice) - 1]
                except Exception:
                    profile_arg = _emails[0]
            else:
                profile_arg = input("  Enter your Gmail address: ").strip()
        asyncio.run(login_only(profile_arg))

    elif "--parallel" in sys.argv:
        # python linkedin_outreach.py --parallel  → all profiles simultaneously
        all_cfg = _load_all_configs()
        if not all_cfg:
            print("  No profiles configured — run:  python linkedin_outreach.py --setup")
            sys.exit(1)
        print(f"  Running {len(all_cfg)} profile(s) IN PARALLEL...\n")

        import traceback as _tb

        async def _run_one(email: str, cfg: dict):
            tag = email.split("@")[0][:10]
            try:
                await run_outreach(cfg, tag=tag)
            except Exception as exc:
                print(f"\n{'═'*60}")
                print(f"  [ERROR] Profile {email} crashed:")
                _tb.print_exc()
                print(f"{'═'*60}\n")

        async def _run_all_parallel():
            await asyncio.gather(*[
                _run_one(email, cfg) for email, cfg in all_cfg.items()
            ])

        asyncio.run(_run_all_parallel())

    elif "--all" in sys.argv:
        # python linkedin_outreach.py --all  → run every configured profile sequentially
        all_cfg = _load_all_configs()
        if not all_cfg:
            print("  No profiles configured — run:  python linkedin_outreach.py --setup")
            sys.exit(1)
        print(f"  Running {len(all_cfg)} profile(s) sequentially...\n")
        for email, cfg in all_cfg.items():
            print(f"\n{'═'*60}")
            print(f"  Profile: {email}")
            print(f"{'═'*60}")
            asyncio.run(run_outreach(cfg))

    elif "--setup-all" in sys.argv:
        # python linkedin_outreach.py --setup-all
        # ── Full wizard: show status for every profile, configure each one ────
        _profiles: dict = {}
        if PROFILES_JSON.exists():
            try:
                _profiles = json.loads(PROFILES_JSON.read_text())
            except Exception:
                pass

        if not _profiles:
            print("  No profiles.json found — add profiles first.")
            sys.exit(1)

        all_cfg = _load_all_configs()

        W = 62
        print(f"\n{'═'*W}")
        print("  LinkedIn Outreach — Setup Wizard")
        print(f"{'═'*W}\n")
        print(f"  Found {len(_profiles)} profile(s) in profiles.json:\n")

        # ── Status table ──────────────────────────────────────────────────────
        for i, (email, pdata) in enumerate(_profiles.items(), 1):
            sd     = _profile_session_dir(email)
            li_ok  = (sd / "li_state.json").exists()
            gm_ok  = (sd / "gmail_token.json").exists()
            kw_ok  = email in all_cfg
            name   = pdata.get("name", "")
            cfg    = all_cfg.get(email, {})
            kw_str = " ".join(cfg.get("search_keywords", [])) if kw_ok else "—"
            jt_str = " ".join(cfg.get("job_types", []))       if kw_ok else "—"

            print(f"  {i}. {email}  ({name})")
            print(f"     LinkedIn session : {'✓ ready' if li_ok else '✗ missing — run --login'}")
            print(f"     Gmail token      : {'✓ ready' if gm_ok else '✗ missing — will prompt on first send'}")
            print(f"     Keywords config  : {'✓  ' + kw_str if kw_ok else '✗ not configured'}")
            print(f"     Job types        : {jt_str}")
            print()

        # ── Per-profile keyword config ────────────────────────────────────────
        print(f"{'─'*W}")
        ans = input("  Configure / update keywords for each profile? [Y/n]: ").strip().lower()
        if ans not in ("n", "no"):
            for email in _profiles:
                print(f"\n{'─'*W}")
                print(f"  Configuring: {email}")
                print(f"{'─'*W}")
                prompt_li_config(preset_email=email)

        # ── Missing LinkedIn sessions ─────────────────────────────────────────
        missing_li = [e for e in _profiles if not (_profile_session_dir(e) / "li_state.json").exists()]
        if missing_li:
            print(f"\n{'─'*W}")
            print("  The following profiles still need a LinkedIn login:\n")
            for e in missing_li:
                print(f"    python linkedin_outreach.py --login --profile {e}")
            print()

        # ── Final status ──────────────────────────────────────────────────────
        all_cfg = _load_all_configs()
        ready = [e for e in _profiles
                 if (_profile_session_dir(e) / "li_state.json").exists()
                 and e in all_cfg]
        print(f"{'═'*W}")
        print(f"  Setup complete.  {len(ready)}/{len(_profiles)} profile(s) ready to run.\n")
        if len(ready) == len(_profiles):
            print("  Start outreach:")
            print("    python linkedin_outreach.py --parallel   ← both at once")
            print("    python linkedin_outreach.py --all        ← one after another")
        print(f"{'═'*W}\n")

    elif "--list" in sys.argv:
        # python linkedin_outreach.py --list  → show configured profiles
        all_cfg = _load_all_configs()
        _profiles: dict = {}
        if PROFILES_JSON.exists():
            try:
                _profiles = json.loads(PROFILES_JSON.read_text())
            except Exception:
                pass
        source = _profiles or {e: {} for e in all_cfg}
        if not source:
            print("  No profiles configured yet.")
        else:
            print(f"\n  LinkedIn Outreach — Profile Status\n")
            for email in source:
                sd    = _profile_session_dir(email)
                li_ok = "✓" if (sd / "li_state.json").exists()   else "✗"
                gm_ok = "✓" if (sd / "gmail_token.json").exists() else "✗"
                kw_ok = email in all_cfg
                cfg   = all_cfg.get(email, {})
                kw  = " ".join(cfg.get("search_keywords", [])) if kw_ok else "not configured"
                exp = ", ".join(cfg.get("experience_levels", ["any"])) if kw_ok else "—"
                print(f"  {email}")
                print(f"    LinkedIn: {li_ok}  Gmail: {gm_ok}  Keywords: {kw}  Experience: {exp}")

    else:
        # python linkedin_outreach.py [--profile email]
        config = load_li_config(profile_arg)
        asyncio.run(run_outreach(config))
