"""
Dice.com Job Application Automation — Hybrid Visual Clicker
============================================================
Three-layer button detection (most robust → fallback chain):

  Layer 1 — Playwright DOM     : CSS / role selectors, pierces shadow DOM
  Layer 2 — OCR (pytesseract)  : reads text from screenshot, no AI needed
  Layer 3 — Ollama Vision LLM  : local vision model sees screen like a human

All three layers are optional and auto-detected at startup.
The script works even if only Layer 1 is available.

Connection modes (DICE_MODE in .env):
  session — saves login; log in once, never again  (default)
  login   — auto-login with DICE_EMAIL / DICE_PASSWORD
  cdp     — attach to Chrome with --remote-debugging-port=9222
"""

import asyncio
import base64
import csv
import io
import json
import os
import random
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.async_api import (
    async_playwright, Page, Browser, BrowserContext,
    TimeoutError as PlaywrightTimeoutError,
)
import gmail_sender
from job_eligibility import early_career_rejection_reason

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────
DICE_MODE   = os.getenv("DICE_MODE", "session").strip().lower()
CDP_URL     = os.getenv("CDP_URL", "http://localhost:9222")
SESSION_DIR = Path(os.getenv("SESSION_DIR", str(Path.home() / ".dice-playwright-profile")))

DICE_EMAIL    = os.getenv("DICE_EMAIL", "")
DICE_PASSWORD = os.getenv("DICE_PASSWORD", "")

# ── Search settings (change in .env) ────────────────────────────────────────
# SEARCH_QUERY : any job title or keyword, e.g. "python developer", "data engineer"
# POSTED_DATE  : ONE (today) | THREE (3 days) | SEVEN (7 days) | THIRTY (30 days) | "" (any time)
# EASY_APPLY   : true (Easy Apply jobs only) | false (all jobs)
SEARCH_QUERY = os.getenv("SEARCH_QUERY", "gen ai").strip()
POSTED_DATE  = os.getenv("POSTED_DATE",  "ONE").strip().upper()
EASY_APPLY   = os.getenv("EASY_APPLY",   "true").strip().lower() == "true"
SENDER_NAME  = os.getenv("SENDER_NAME",  "Applicant").strip()
RESUME_PATH  = os.getenv("RESUME_PATH",  "").strip()

def _build_search_url() -> str:
    from urllib.parse import quote_plus
    url = f"https://www.dice.com/jobs?q={quote_plus(SEARCH_QUERY)}&pageSize=20"
    if EASY_APPLY:
        url += "&filters.easyApply=true"
    if POSTED_DATE:
        url += f"&filters.postedDate={POSTED_DATE}"
    return url

SEARCH_URL = _build_search_url()

CSV_FILE    = Path(__file__).parent / "applied_jobs.csv"  # overridden per profile in run()
CSV_HEADERS = ["timestamp", "job_title", "company", "location", "job_url", "status"]

# URLs we've successfully applied to in this session (loaded from CSV at startup)
APPLIED_URLS: set[str] = set()

OLLAMA_TEXT_MODEL = "gemma2:2b"
OLLAMA_VISION_MODEL: str | None = None   # auto-detected at startup

PROFILES_JSON = Path(__file__).parent / "profiles.json"


def _profile_session_dir(email: str) -> Path:
    normalized_email = email.strip().lower()
    for candidate in sorted(Path.home().iterdir()):
        email_file = candidate / ".profile_email"
        if (candidate.is_dir() and candidate.name.startswith(".dice-")
                and email_file.exists()
                and email_file.read_text().strip().lower() == normalized_email):
            return candidate

    safe_email = re.sub(r"[^a-z0-9]", "_", normalized_email)
    session_dir = Path.home() / f".dice-playwright-profile-{safe_email}"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / ".profile_email").write_text(normalized_email)
    return session_dir


def _is_authenticated_dice_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path.lower().rstrip("/")
    if host != "dice.com" and not host.endswith(".dice.com"):
        return False
    if any(part in path for part in ("/login", "/register", "/signin", "/auth", "/sso/")):
        return False
    return path.startswith((
        "/dashboard",
        "/jobs",
        "/profile",
        "/job-detail",
        "/job-applications",
    ))


async def _wait_for_dice_login(page: Page, timeout_seconds: int = 600) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if page.is_closed():
            raise RuntimeError("Dice login browser was closed before setup completed")
        if _is_authenticated_dice_url(page.url):
            return
        await asyncio.sleep(1)
    raise RuntimeError("Timed out waiting for Dice login")


def _mark_dice_session_ready(session_dir: Path) -> None:
    (session_dir / ".dice_session_ready").write_text(
        datetime.now().isoformat(timespec="seconds")
    )

PROFILE_CONTEXT = (
    "Software engineer with 8+ years experience, specializing in Generative AI, "
    "LLMs, Python, RAG, LangChain, AWS. Currently located in Austin TX. "
    "Open to contract and full-time roles."
)

_PROFILE_YEARS: int = 8   # updated per-profile in load_profile()
_PROFILE_MAX_REQUIRED_YEARS: int = 4
_PROFILE_EXPERIENCE_LEVELS: list[str] = ["intern", "new_grad", "early_career", "entry"]

stats = {"applied": 0, "skipped": 0, "errors": 0}
DEBUG_SHOT_TAKEN   = False


# ── Profile loader ──────────────────────────────────────────────────────────

_REQUIRED_PROFILE_KEYS = ["name", "work_auth", "years_experience", "location", "skills"]

_WORK_AUTH_LABELS = {
    "USC": "US Citizen",
    "GC":  "Green Card / Permanent Resident",
    "H1B": "H1B visa",
    "OPT": "OPT / STEM OPT",
    "TN":  "TN visa",
    "EAD": "EAD card",
    "CPT": "CPT",
    "L1":  "L1 visa",
}


def _prompt_profile_setup(email: str, existing: dict) -> dict:
    """
    Interactively ask the user for profile details once.
    Pre-fills with any values already saved.
    Returns the completed profile dict.
    """
    def ask(prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        val = input(f"  {prompt}{suffix}: ").strip()
        return val if val else default

    print(f"\n{'═'*55}")
    print(f"  Profile Setup — {email}")
    print(f"  Answer once; saved for all future runs.")
    print(f"  (Press Enter to keep the value in brackets)")
    print(f"{'═'*55}\n")

    name          = ask("Full name",                       existing.get("name", ""))
    current_title = ask("Current / target job title",      existing.get("current_title", "Software Engineer"))
    years_str     = ask("Total years of experience",       str(existing.get("years_experience", "")))
    try:
        years_exp = int(years_str)
    except ValueError:
        years_exp = 5

    skills        = ask("Key skills (comma-separated)",    existing.get("skills", ""))
    location      = ask("Current location  (City, State)", existing.get("location", ""))

    print("\n  Work authorization options:")
    for code, label in _WORK_AUTH_LABELS.items():
        print(f"    {code:<5} = {label}")
    work_auth = ask("Your status", existing.get("work_auth", "H1B")).upper()

    default_spons = "n" if work_auth in ("USC", "GC") else "y"
    saved_spons   = "y" if existing.get("needs_sponsorship", True) else "n"
    spons_ans     = ask("Need visa sponsorship? (y/n)", saved_spons or default_spons)
    needs_sponsorship = spons_ans.lower() in ("y", "yes")

    preferred_work   = ask("Preferred work type  (Remote/Hybrid/Onsite/Any)",
                           existing.get("preferred_work", "Remote"))
    reloc_saved      = "y" if existing.get("open_to_relocation", False) else "n"
    reloc_ans        = ask("Open to relocation?  (y/n)", reloc_saved)
    open_to_relocation = reloc_ans.lower() in ("y", "yes")

    available_to_start = ask("Available to start",      existing.get("available_to_start", "2 weeks"))
    expected_salary    = ask("Expected salary (or 'open to discussion')",
                             existing.get("expected_salary", "Open to discussion"))

    # Build a rich summary for Ollama
    auth_desc    = _WORK_AUTH_LABELS.get(work_auth, work_auth)
    spons_desc   = "requires visa sponsorship" if needs_sponsorship else "does not need sponsorship"
    reloc_desc   = "open to relocation" if open_to_relocation else "not open to relocation"
    citizen_gc   = work_auth in ("USC", "GC")

    summary = (
        f"{name} is a {current_title} with {years_exp} years of professional experience. "
        f"Key skills: {skills}. "
        f"Location: {location}. "
        f"Work authorization: {auth_desc} — {spons_desc}. "
        f"{'Is a US Citizen or Green Card holder.' if citizen_gc else 'Is NOT a US Citizen or Green Card holder.'} "
        f"Preferred work arrangement: {preferred_work}. {reloc_desc.capitalize()}. "
        f"Available to start: {available_to_start}. "
        f"Expected salary: {expected_salary}."
    )

    profile = {
        "name":               name,
        "current_title":      current_title,
        "work_auth":          work_auth,
        "needs_sponsorship":  needs_sponsorship,
        "years_experience":   years_exp,
        "location":           location,
        "skills":             skills,
        "preferred_work":     preferred_work,
        "open_to_relocation": open_to_relocation,
        "available_to_start": available_to_start,
        "expected_salary":    expected_salary,
        "summary":            summary,
    }

    # Persist to profiles.json
    all_profiles: dict = {}
    if PROFILES_JSON.exists():
        try:
            all_profiles = json.loads(PROFILES_JSON.read_text())
        except Exception:
            pass
    all_profiles[email.lower().strip()] = profile
    PROFILES_JSON.write_text(json.dumps(all_profiles, indent=2))
    print(f"\n  ✅  Profile saved → {PROFILES_JSON}\n")
    return profile


def load_profile(email: str, force_setup: bool = False):
    """Load per-profile settings; runs interactive setup if any required field is missing."""
    global PROFILE_CONTEXT, _QUICK_ANSWERS, _PROFILE_YEARS
    global _PROFILE_MAX_REQUIRED_YEARS, _PROFILE_EXPERIENCE_LEVELS, SENDER_NAME

    saved: dict = {}
    if PROFILES_JSON.exists():
        try:
            data = json.loads(PROFILES_JSON.read_text())
            saved = data.get(email.lower().strip(), {})
        except Exception as e:
            print(f"  [profiles.json] error: {e}")

    needs_setup = force_setup or not all(k in saved for k in _REQUIRED_PROFILE_KEYS)
    if needs_setup:
        saved = _prompt_profile_setup(email, saved)

    _PROFILE_YEARS  = int(saved.get("years_experience", 8))
    _PROFILE_MAX_REQUIRED_YEARS = int(saved.get("max_required_years", 4))
    _PROFILE_EXPERIENCE_LEVELS = saved.get(
        "experience_levels", ["intern", "new_grad", "early_career", "entry"]
    )
    SENDER_NAME     = saved.get("name", SENDER_NAME)
    PROFILE_CONTEXT = saved.get("summary", PROFILE_CONTEXT)

    work_auth          = str(saved.get("work_auth", "H1B")).upper()
    needs_sponsorship  = bool(saved.get("needs_sponsorship", True))
    location           = str(saved.get("location", "Austin, TX"))
    preferred_work     = str(saved.get("preferred_work", "Remote"))
    open_to_relocation = bool(saved.get("open_to_relocation", False))
    available_to_start = str(saved.get("available_to_start", "2 weeks"))
    expected_salary    = str(saved.get("expected_salary", "Open to discussion"))
    school              = str(saved.get("school", ""))
    degree              = str(saved.get("degree", ""))
    field_of_study      = str(saved.get("field_of_study", saved.get("major", "")))
    graduation_year     = str(saved.get("graduation_year", ""))

    _QUICK_ANSWERS = _build_quick_answers(
        work_auth, needs_sponsorship, _PROFILE_YEARS, location,
        preferred_work, open_to_relocation, available_to_start, expected_salary,
        school, degree, field_of_study, graduation_year,
    )

    auth_str = "needs sponsorship" if needs_sponsorship else "no sponsorship needed"
    print(f"  Profile: {SENDER_NAME} | {work_auth} | {_PROFILE_YEARS} yrs | {auth_str}")


# ── Capability detection ────────────────────────────────────────────────────

def detect_capabilities() -> dict:
    caps = {"playwright": True, "ocr": False, "ollama_vision": False, "ollama_text": False}

    # OCR
    try:
        import pytesseract  # noqa: F401
        pytesseract.get_tesseract_version()
        caps["ocr"] = True
    except Exception:
        pass

    # Ollama
    try:
        import ollama
        models_resp = ollama.list()
        model_names = [m.model for m in models_resp.models]

        # Vision models in preference order
        for candidate in ["llama3.2-vision", "llava", "moondream", "minicpm-v", "bakllava"]:
            match = next((m for m in model_names if candidate in m), None)
            if match:
                caps["ollama_vision"] = match
                global OLLAMA_VISION_MODEL
                OLLAMA_VISION_MODEL = match
                break

        # Text model
        if any(OLLAMA_TEXT_MODEL in m for m in model_names):
            caps["ollama_text"] = True
    except Exception:
        pass

    return caps


def print_capabilities(caps: dict):
    print("── Hybrid clicker layers ──────────────────────────────────")
    print(f"  Layer 1 · Playwright DOM   : ✅ always on")
    print(f"  Layer 2 · OCR (tesseract)  : {'✅ ready' if caps['ocr'] else '⚠️  not installed  →  brew install tesseract && pip install pytesseract Pillow'}")
    print(f"  Layer 3 · Ollama vision    : {'✅ ' + str(caps['ollama_vision']) if caps['ollama_vision'] else '⚠️  no vision model  →  ollama pull moondream'}")
    print("───────────────────────────────────────────────────────────\n")


# ── CSV & tracker ───────────────────────────────────────────────────────────

def ensure_csv():
    if not CSV_FILE.exists():
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()


def load_applied_urls():
    """Populate APPLIED_URLS from this profile's CSV (applied jobs only)."""
    global APPLIED_URLS
    APPLIED_URLS = set()
    if not CSV_FILE.exists():
        return
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "applied":
                    url = row.get("job_url", "").strip()
                    if url:
                        APPLIED_URLS.add(url)
    except Exception:
        pass
    if APPLIED_URLS:
        print(f"  Tracker loaded: {len(APPLIED_URLS)} previously applied job(s) for this profile.\n")


def log_application(title, company, location, url, status):
    if status == "applied":
        APPLIED_URLS.add(url)   # update in-memory tracker immediately
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "job_title": title, "company": company,
            "location": location, "job_url": url, "status": status,
        })


# ── Ollama helpers ──────────────────────────────────────────────────────────

_QUICK_ANSWERS: list[tuple[list[str], str]] = []   # built per-profile in load_profile()


def _build_quick_answers(
    work_auth: str, needs_sponsorship: bool, years_exp: int, location: str,
    preferred_work: str = "Remote", open_to_relocation: bool = False,
    available_to_start: str = "2 weeks", expected_salary: str = "Open to discussion",
    school: str = "", degree: str = "", field_of_study: str = "",
    graduation_year: str = "",
) -> list[tuple[list[str], str]]:
    """Build profile-specific quick-answer lookup table."""
    is_citizen    = work_auth in ("USC", "US_CITIZEN", "CITIZEN")
    is_gc         = work_auth in ("GC", "GREEN_CARD", "PERMANENT_RESIDENT", "PR")
    is_authorized = work_auth in ("USC", "GC", "H1B", "OPT", "TN", "EAD",
                                  "CPT", "L1", "CITIZEN", "GREEN_CARD", "PR")
    citizen_or_gc = is_citizen or is_gc

    return [
        (["authorized to work", "work authorization", "eligible to work",
          "legally authorized", "right to work", "legally permitted"],
         "Yes" if is_authorized else "No"),

        (["us citizen", "united states citizen", "american citizen",
          "citizen or green card", "citizen or gc", "citizen or permanent",
          "green card", "gc holder", "permanent resident", "lawful permanent",
          "citizenship status"],
         "Yes" if citizen_or_gc else "No"),

        (["require sponsorship", "visa sponsorship", "need sponsorship",
          "sponsor your visa", "sponsorship required", "h1b transfer",
          "h1b.*transfer", "transfer.*h1b", "currently on.*h1b", "active h1b"],
         "Yes" if needs_sponsorship else "No"),

        (["years of experience", "how many years", "years experience",
          "total experience"],
         str(years_exp)),

        (["current location", "where are you located", "city", "state",
          "based in", "reside"],
         location),

        (["willing to relocate", "open to relocation", "relocate"],
         "Yes" if open_to_relocation else "No"),

        (["remote", "work from home", "hybrid", "on-site preference",
          "work arrangement", "work setting"],
         preferred_work),

        (["notice period", "how soon", "available to start", "start date",
          "earliest start", "when can you start"],
         available_to_start),

        (["expected salary", "desired salary", "salary expectation",
          "compensation expectation", "salary range"],
         expected_salary),

        (["field of study", "area of study", "major", "discipline",
          "concentration", "program of study"],
         field_of_study),

        (["degree type", "type of degree", "highest degree", "education level"],
         degree),

        (["school", "university", "college", "education institution"],
         school),

        (["graduation year", "expected graduation", "year of graduation"],
         graduation_year),
    ]


def _quick_answer(question: str) -> str | None:
    q = question.lower()
    for keywords, answer in _QUICK_ANSWERS:
        if any(k in q for k in keywords):
            return answer
    return None


def _answer_years_question(question: str, profile_years: int) -> str | None:
    """
    For 'Do you have at least X months/years of experience...' questions,
    compare X against the profile's years and answer Yes/No automatically.
    """
    q = question.lower()
    # Extract month threshold
    m = re.search(r'(\d+)\s*months?', q)
    if m:
        threshold_months = int(m.group(1))
        return "Yes" if (profile_years * 12) >= threshold_months else "No"
    # Extract year threshold
    m = re.search(r'(\d+)\s*years?', q)
    if m:
        threshold_years = int(m.group(1))
        return "Yes" if profile_years >= threshold_years else "No"
    return None


def ollama_answer(question: str) -> str | None:
    """Answer a job application question — quick lookup first, then Ollama."""
    # Years/months threshold questions ("Do you have at least 84 months...")
    years_ans = _answer_years_question(question, _PROFILE_YEARS)
    if years_ans:
        return years_ans
    quick = _quick_answer(question)
    if quick:
        return quick
    try:
        import ollama
        resp = ollama.chat(model=OLLAMA_TEXT_MODEL, messages=[{
            "role": "user",
            "content": (
                f"You are filling out a job application. "
                f"Candidate: {PROFILE_CONTEXT}\n\n"
                f"Answer concisely (1-3 sentences):\n{question}"
            ),
        }])
        return resp.message.content.strip()
    except Exception:
        return None


# ── Browser setup ───────────────────────────────────────────────────────────

async def launch_session(pw, login_only: bool = False):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Launching browser ({SESSION_DIR.name}) ...")
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(SESSION_DIR),
        headless=False,
        slow_mo=20,
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://www.dice.com/dashboard", wait_until="domcontentloaded")
    await asyncio.sleep(2)

    needs_login = not _is_authenticated_dice_url(page.url)
    if needs_login:
        (SESSION_DIR / ".dice_session_ready").unlink(missing_ok=True)
        print("\n>>> Log in to Dice.com in the browser window.")
        print(">>> You have 10 minutes — take your time with Google/SSO login.")
        print(">>> This browser stays open until Dice setup is complete.\n")
        await _wait_for_dice_login(page)
        await asyncio.sleep(2)
        print("Logged in. Session saved.\n")
    else:
        print("Already logged in via saved session.\n")

    _mark_dice_session_ready(SESSION_DIR)

    return None, context, page


async def launch_login(pw):
    if not DICE_EMAIL or not DICE_PASSWORD:
        raise RuntimeError("Set DICE_EMAIL and DICE_PASSWORD in .env for login mode.")
    browser = await pw.chromium.launch(headless=False, slow_mo=20)
    context = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await context.new_page()
    await page.goto("https://www.dice.com/dashboard/login", wait_until="domcontentloaded")
    try:
        await page.click("button:has-text('Accept')", timeout=3000)
    except PlaywrightTimeoutError:
        pass
    await page.fill("input[type='email']", DICE_EMAIL)
    await page.fill("input[type='password']", DICE_PASSWORD)
    await page.click("button[type='submit']")
    await page.wait_for_url(re.compile(r"dice\.com/(jobs|dashboard)"), timeout=20000)
    print("Logged in.\n")
    return browser, context, page


async def connect_cdp(pw):
    print(f"Connecting via CDP at {CDP_URL} ...")
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = next((p for p in context.pages if "dice.com" in p.url), None) or await context.new_page()
    await page.bring_to_front()
    print("Connected.\n")
    return browser, context, page


# ══════════════════════════════════════════════════════════════════════════════
#  HYBRID SMART CLICKER
# ══════════════════════════════════════════════════════════════════════════════

async def _layer1_playwright(page: Page, texts: list[str]) -> bool:
    """
    Layer 1: Playwright DOM selectors.
    Tries role-based (shadow-DOM piercing) then CSS has-text for each text variant.
    Fastest — works when element is a standard or Angular shadow-DOM button/link.
    """
    for text in texts:
        pat = re.compile(re.escape(text), re.I)
        for role in ("button", "link"):
            try:
                el = page.get_by_role(role, name=pat).first
                if await el.is_visible(timeout=500):
                    await el.scroll_into_view_if_needed(timeout=1000)
                    await el.click()
                    return True
            except Exception:
                pass
        for tag in ("button", "a", "dhi-apply-button button",
                    "[data-cy='apply-button-link']", "[data-cy='applyButton']",
                    "[data-testid='apply-button']"):
            try:
                sel = f"{tag}:has-text('{text}')" if not tag.startswith("[") else tag
                el = page.locator(sel).first
                if await el.is_visible(timeout=400):
                    await el.scroll_into_view_if_needed(timeout=1000)
                    await el.click()
                    return True
            except Exception:
                pass
    return False


async def _layer2_ocr(page: Page, texts: list[str]) -> bool:
    """
    Layer 2: OCR via pytesseract.
    Takes a screenshot, reads all text with bounding boxes, finds the target
    phrase (supports multi-word), clicks at its center pixel.
    Works even when the button is inside shadow DOM or an iframe.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return False

    screenshot_bytes = await page.screenshot()
    img = Image.open(io.BytesIO(screenshot_bytes))

    # Scale factor: Playwright screenshot is at CSS pixels; on Retina the
    # image may be 2× larger than the viewport. Detect and correct.
    vp = page.viewport_size or {"width": 1400, "height": 900}
    scale_x = img.width  / vp["width"]
    scale_y = img.height / vp["height"]

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words = [w.strip() for w in data["text"]]
    n = len(words)

    for target in texts:
        target_lower = target.lower()
        target_words = target_lower.split()
        wlen = len(target_words)

        for i in range(n - wlen + 1):
            chunk = " ".join(words[i:i + wlen]).lower()
            if not chunk.strip():
                continue
            # Fuzzy: target inside chunk OR chunk inside target
            if target_lower in chunk or chunk in target_lower:
                conf = int(data["conf"][i])
                if conf < 25:
                    continue
                # Bounding box spanning all words in the phrase
                x1 = data["left"][i]
                y1 = min(data["top"][i:i + wlen])
                x2 = data["left"][i + wlen - 1] + data["width"][i + wlen - 1]
                y2 = max(
                    data["top"][j] + data["height"][j]
                    for j in range(i, i + wlen)
                )
                cx = int(((x1 + x2) / 2) / scale_x)
                cy = int(((y1 + y2) / 2) / scale_y)
                await page.mouse.click(cx, cy)
                return True
    return False


async def _layer3_ollama_vision(page: Page, instruction: str) -> bool:
    """
    Layer 3: Local Ollama vision model.
    Sends a screenshot to the vision model and asks it where to click.
    Works for any UI change — the model understands context visually.
    Requires: ollama pull moondream  (or llama3.2-vision)
    """
    if not OLLAMA_VISION_MODEL:
        return False
    try:
        import ollama
    except ImportError:
        return False

    screenshot_bytes = await page.screenshot()
    b64 = base64.b64encode(screenshot_bytes).decode()
    vp = page.viewport_size or {"width": 1400, "height": 900}

    prompt = (
        f"This is a screenshot of a web page "
        f"({vp['width']}x{vp['height']} pixels, CSS coordinates).\n"
        f"Task: {instruction}\n\n"
        f"Reply with ONLY a JSON object and nothing else:\n"
        f'  {{"x": <number>, "y": <number>}}  if found\n'
        f'  {{"x": null, "y": null}}          if not found\n'
        f"Do not add any explanation."
    )

    try:
        response = ollama.chat(
            model=OLLAMA_VISION_MODEL,
            messages=[{"role": "user", "content": prompt, "images": [b64]}],
        )
        content = response.message.content.strip()
        match = re.search(r'\{[^}]+\}', content)
        if match:
            coords = json.loads(match.group())
            if coords.get("x") is not None:
                await page.mouse.click(int(coords["x"]), int(coords["y"]))
                return True
    except Exception:
        pass
    return False


async def _button_visible(page: Page, texts: list[str], timeout_ms: int = 150) -> bool:
    """Return True if any button/link with one of these texts is currently visible."""
    for text in texts:
        pat = re.compile(re.escape(text), re.I)
        for role in ("button", "link"):
            try:
                if await page.get_by_role(role, name=pat).first.is_visible(timeout=timeout_ms):
                    return True
            except Exception:
                pass
        try:
            if await page.locator(f"button:has-text('{text}')").first.is_visible(timeout=timeout_ms):
                return True
        except Exception:
            pass
    return False


async def _find_active_dialog(page: Page) -> str | None:
    """Return the CSS selector of the currently-open wizard/dialog, or None."""
    for sel in [
        "dhi-apply-wizard", "[role='dialog']", "mat-dialog-container",
        "[class*='apply-wizard']", "[class*='applyWizard']",
        "[class*='apply-modal']", "[class*='wizard-container']",
        "[class*='wizard']",
    ]:
        try:
            if await page.locator(sel).first.is_visible(timeout=300):
                return sel
        except Exception:
            pass
    return None


async def _dialog_visible(page: Page, sel: str) -> bool:
    """Return True if the given dialog selector is still visible."""
    try:
        return await page.locator(sel).first.is_visible(timeout=300)
    except Exception:
        return False


async def smart_click(
    page: Page,
    texts: list[str],
    ollama_instruction: str,
    timeout: float = 7.0,
    label: str = "",
) -> bool:
    """
    Hybrid click: tries all three layers in order, returns True on first success.
    Polls until timeout so each layer gets multiple attempts as the page renders.

      texts              — button texts to search (e.g. ["Easy Apply", "Apply Now"])
      ollama_instruction — natural language for the vision model
      timeout            — total seconds to spend across all layers
      label              — short name for debug output (e.g. "Apply", "Next")
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        # Layer 1 — DOM (fastest, try every loop)
        if await _layer1_playwright(page, texts):
            if label:
                print(f"      → [{label}] clicked via DOM")
            return True

        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break

        # Layer 2 — OCR (medium speed, try every other loop)
        if await _layer2_ocr(page, texts):
            if label:
                print(f"      → [{label}] clicked via OCR")
            return True

        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break

        # Layer 3 — Ollama vision (slow, only if still time left)
        if OLLAMA_VISION_MODEL and remaining > 3.0:
            if await _layer3_ollama_vision(page, ollama_instruction):
                if label:
                    print(f"      → [{label}] clicked via Ollama vision")
                return True

        await asyncio.sleep(0.4)

    return False


async def _click_apply_button_top(page: Page, timeout: float = 5.0) -> bool:
    """
    Clicks Apply / Easy Apply every 0.3 s until the click registers.
    'Registered' means a wizard dialog appeared, a new tab opened, or the
    page shows an applied state. Falls back to returning True if the button
    was clicked at least once within the timeout window.
    """
    direct_sels = [
        "dhi-apply-button button",
        "[data-cy='apply-button-link']",
        "[data-cy='applyButton']",
        "[data-testid='apply-button']",
        "apply-button button",
    ]
    clicked_once = False
    initial_page_count = len(page.context.pages)
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        found = False
        for sel in direct_sels:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=200):
                    await el.scroll_into_view_if_needed(timeout=300)
                    await el.click()
                    clicked_once = True
                    found = True
                    break
            except Exception:
                pass

        if not found:
            for text in ["Easy Apply", "Apply Now", "Apply"]:
                try:
                    el = page.get_by_role(
                        "button", name=re.compile(rf"^{re.escape(text)}$", re.I)
                    ).first
                    if await el.is_visible(timeout=200):
                        await el.scroll_into_view_if_needed(timeout=300)
                        await el.click()
                        clicked_once = True
                        break
                except Exception:
                    pass

        # Stop as soon as the click registers — compare against initial count
        # so this works correctly when called from an already-extra tab
        if clicked_once:
            await asyncio.sleep(0.2)   # brief wait for new tabs / dialogs to appear
            if len(page.context.pages) > initial_page_count:
                return True
            if await _find_active_dialog(page):
                return True
            if await page_shows_applied(page):
                return True
            if "dice.com" not in page.url:
                return True

        await asyncio.sleep(0.1)   # short remainder to keep total ~0.3 s per cycle

    return clicked_once


async def _try_apply_external_tab(new_tab: Page) -> str:
    """
    Attempt to apply from a newly-opened external-employer tab.
    Returns 'applied' if a success signal is detected, 'external - skipped' otherwise.
    Always closes the external tab (and any further tabs it spawned) when done.
    """
    ctx = new_tab.context
    try:
        await new_tab.wait_for_load_state("domcontentloaded", timeout=8000)
        await asyncio.sleep(1.0)

        clicked = await _click_apply_button_top(new_tab, timeout=3.0)
        if not clicked:
            clicked = await smart_click(
                new_tab,
                texts=["Apply", "Apply Now", "Apply for this job", "Submit Application"],
                ollama_instruction="the main Apply or Submit button on this job application page",
                timeout=5.0,
                label="External Apply",
            )

        if clicked:
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if await page_shows_applied(new_tab):
                    return "applied"
                await asyncio.sleep(0.5)
    except Exception as e:
        print(f"      → [External tab] error: {e}")
    finally:
        # Close the external tab AND any further tabs it may have spawned,
        # keeping only the original Dice job-listing page.
        original = ctx.pages[0] if ctx.pages else None
        for p in list(ctx.pages):
            if p is not original:
                try:
                    await p.close()
                except Exception:
                    pass

    return "external - skipped"


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE STATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def wait_for_job_page_ready(page: Page) -> str:
    """
    Wait up to 12 s for Angular to render the job detail page.
    Returns: 'apply' | 'applied' | 'external' | 'timeout'
    """
    if "dice.com" not in page.url:
        return "external"

    apply_sels = [
        "button:has-text('Easy Apply')", "a:has-text('Easy Apply')",
        "button:has-text('Apply Now')", "a:has-text('Apply Now')",
        "[data-cy='apply-button-link']", "[data-cy='applyButton']",
        "[data-testid='apply-button']", "dhi-apply-button",
    ]
    applied_sels = [
        "button:has-text('Applied')",
        "[class*='applied-badge']", "[class*='alreadyApplied']",
        "[data-cy='already-applied']",
        "div:has-text('You have already applied')",
    ]

    deadline = asyncio.get_event_loop().time() + 12.0
    while asyncio.get_event_loop().time() < deadline:
        for sel in applied_sels:
            try:
                if await page.locator(sel).first.is_visible(timeout=150):
                    return "applied"
            except Exception:
                pass
        for sel in apply_sels:
            try:
                if await page.locator(sel).first.is_visible(timeout=150):
                    return "apply"
            except Exception:
                pass
        # Layer 2 fallback: OCR check for "Easy Apply" text on page
        try:
            import pytesseract
            from PIL import Image
            img_bytes = await page.screenshot()
            img = Image.open(io.BytesIO(img_bytes))
            text = pytesseract.image_to_string(img).lower()
            if "easy apply" in text or "apply now" in text:
                return "apply"
            if text.count("applied") >= 2:   # badge + header = already applied
                return "applied"
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return "timeout"


async def page_shows_applied(page: Page) -> bool:
    """Quick check: does the current page show an Applied / success state?"""
    for sel in [
        "button:has-text('Applied')", "[class*='applied-badge']",
        ":text('application is on its way')", ":text('application submitted')",
        ":text('Successfully applied')", ":text('You have applied')",
        ":text('we\\'ve received')", ":text('thank you for applying')",
    ]:
        try:
            if await page.locator(sel).first.is_visible(timeout=250):
                return True
        except Exception:
            pass
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  APPLICATION WIZARD
# ══════════════════════════════════════════════════════════════════════════════

async def wait_for_wizard_close(
    page: Page,
    dialog_sel: str | None,
    timeout: float = 12.0,
) -> bool:
    """
    After clicking Submit, wait for any of these success signals:
      • explicit success text / Applied badge
      • the wizard dialog element disappears
      • Submit button vanishes (and no Next appeared — wizard closed, not just advanced)
      • URL changes to success/applied page
      • OCR detects success keywords
    Returns True on success, False on timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    SUBMIT_TEXTS = ["Submit Application", "Submit", "Send Application", "Finish"]

    while asyncio.get_event_loop().time() < deadline:
        # ── success text ───────────────────────────────────────────────────
        for text in [
            "on its way", "application submitted", "application sent",
            "successfully applied", "you have applied", "thank you for applying",
            "application complete", "application received", "we've received",
            "your application", "has been submitted",
        ]:
            for pseudo in (":text-is", ":text"):
                try:
                    if await page.locator(f"{pseudo}('{text}')").first.is_visible(timeout=150):
                        return True
                except Exception:
                    pass

        # ── Applied badge appeared ─────────────────────────────────────────
        if await page_shows_applied(page):
            return True

        # ── Wizard dialog closed ───────────────────────────────────────────
        if dialog_sel and not await _dialog_visible(page, dialog_sel):
            await asyncio.sleep(0.8)   # let Applied badge render
            return True

        # ── Submit button vanished (and no Next = not just step-change) ────
        if not await _button_visible(page, SUBMIT_TEXTS, timeout_ms=200):
            next_visible = await _button_visible(page, ["Next", "Continue"], timeout_ms=200)
            if not next_visible:
                await asyncio.sleep(1.0)
                return True

        # ── URL changed ────────────────────────────────────────────────────
        if any(s in page.url for s in ["applied", "success", "confirmation", "thank"]):
            return True

        # ── Success CSS class ──────────────────────────────────────────────
        for sel in ["[class*='confirmation']", "[class*='submitted']",
                    "[class*='application-sent']", "[class*='success']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=150):
                    txt = (await el.inner_text(timeout=300)).strip()
                    if len(txt) > 5:
                        return True
            except Exception:
                pass

        # ── OCR scan for success keywords ──────────────────────────────────
        try:
            import pytesseract
            from PIL import Image
            img_bytes = await page.screenshot()
            img = Image.open(io.BytesIO(img_bytes))
            ocr_text = pytesseract.image_to_string(img).lower()
            if any(kw in ocr_text for kw in
                   ["on its way", "application submitted", "successfully applied",
                    "application received", "thank you for applying", "has been submitted"]):
                return True
        except Exception:
            pass

        await asyncio.sleep(0.35)

    return await page_shows_applied(page)


async def _get_field_label(page: Page, inp) -> str:
    """Find the question text for a form field using multiple strategies."""
    input_id = await inp.get_attribute("id") or ""
    # 1. label[for=id]
    if input_id:
        try:
            text = await page.locator(f"label[for='{input_id}']").inner_text(timeout=300)
            if text.strip():
                return text.strip()
        except Exception:
            pass
    # 2. aria-label / placeholder
    for attr in ("aria-label", "placeholder"):
        val = await inp.get_attribute(attr) or ""
        if val.strip():
            return val.strip()
    # 3. Nearest visible text above the field (p, label, h*, div, span)
    try:
        label = await page.evaluate("""
            (el) => {
                const candidates = ['label', 'p', 'h1','h2','h3','h4','h5', 'legend', 'span', 'div'];
                // walk up the DOM; look for a text-bearing sibling or ancestor
                let node = el;
                for (let i = 0; i < 5; i++) {
                    if (!node.parentElement) break;
                    node = node.parentElement;
                    // preceding siblings
                    let sib = node.previousElementSibling;
                    while (sib) {
                        const t = sib.innerText?.trim();
                        if (t && t.length < 300) return t;
                        sib = sib.previousElementSibling;
                    }
                    // first matching child that isn't the input itself
                    for (const tag of candidates) {
                        for (const child of node.querySelectorAll(tag)) {
                            if (!child.contains(el)) {
                                const t = child.innerText?.trim();
                                if (t && t.length < 300) return t;
                            }
                        }
                    }
                }
                return '';
            }
        """, await inp.element_handle())
        if label and label.strip():
            return label.strip()
    except Exception:
        pass
    return ""


async def _fill_visible_fields(page: Page):
    """Fill all visible empty form fields: text, textarea, radio, checkbox, select."""
    try:
        # ── Text inputs & textareas ───────────────────────────────────────────
        for inp in await page.locator("input[type='text'], input[type='number'], textarea").all():
            try:
                if not await inp.is_visible(timeout=200):
                    continue
                if await inp.input_value():
                    continue
                label_text = await _get_field_label(page, inp)
                if label_text:
                    answer = ollama_answer(label_text)
                    if answer:
                        await inp.fill(answer)
                        print(f"      → [Field] filled: {label_text[:60]!r}")
            except Exception:
                pass

        # ── Radio button groups (Dice uses React Aria) ───────────────────────
        # Structure: [role="radiogroup"] > label[data-rac] > span(hidden input) + SVG + text
        # Values are integers (1, 2, 3…), label text is "Yes" / "No" etc.
        # Inputs are visually hidden via clip-path — must click the <label> wrapper.
        try:
            groups = await page.evaluate("""
                () => {
                    const result = [];
                    for (const group of document.querySelectorAll('[role="radiogroup"]')) {
                        // Question from aria-labelledby
                        const labelId = group.getAttribute('aria-labelledby');
                        let question = '';
                        if (labelId) {
                            const el = document.getElementById(labelId);
                            if (el) question = el.innerText.trim().replace(/\\s*\\*\\s*$/, '').trim();
                        }

                        const options = [];
                        for (const lbl of group.querySelectorAll('label[data-rac]')) {
                            const inp = lbl.querySelector('input[type="radio"]');
                            if (!inp) continue;
                            options.push({
                                value:     inp.value,
                                name:      inp.name,
                                labelText: lbl.innerText.trim(),
                            });
                        }
                        if (options.length) result.push({ question, options });
                    }
                    return result;
                }
            """)

            for group in (groups or []):
                question = group.get("question", "")
                options  = group.get("options", [])
                if not options:
                    continue

                answer       = (ollama_answer(question) if question else None) or "yes"
                answer_lower = answer.lower()

                # Match by label text ("Yes" / "No")
                chosen = None
                for opt in options:
                    lbl = opt.get("labelText", "").lower()
                    if lbl in answer_lower or answer_lower in lbl:
                        chosen = opt
                        break

                # Prefer "Yes" option as default
                if not chosen:
                    for opt in options:
                        if "yes" in opt.get("labelText", "").lower():
                            chosen = opt
                            break
                if not chosen:
                    chosen = options[0]

                chosen_name  = chosen.get("name", "")
                chosen_val   = chosen.get("value", "")
                chosen_label = chosen.get("labelText", "")

                clicked = False

                # Strategy 1: click the <label> wrapper via JS
                # React Aria listens for pointer events on the label element
                try:
                    did_click = await page.evaluate("""
                        ([name, value]) => {
                            const inp = document.querySelector(
                                `input[type="radio"][name="${CSS.escape(name)}"][value="${CSS.escape(value)}"]`
                            );
                            if (!inp) return false;
                            const lbl = inp.closest('label');
                            if (lbl) { lbl.click(); return true; }
                            inp.closest('[role="radio"]')?.click();
                            return false;
                        }
                    """, [chosen_name, chosen_val])
                    if did_click:
                        clicked = True
                except Exception:
                    pass

                # Strategy 2: Playwright get_by_role (uses ARIA tree)
                if not clicked:
                    try:
                        el = page.get_by_role("radio", name=re.compile(
                            rf"^{re.escape(chosen_label.strip())}$", re.I
                        ))
                        await el.first.click(force=True)
                        clicked = True
                    except Exception:
                        pass

                # Strategy 3: force-click the hidden input
                if not clicked:
                    try:
                        sel = f"input[type='radio'][name='{chosen_name}'][value='{chosen_val}']"
                        await page.locator(sel).first.click(force=True)
                        clicked = True
                    except Exception:
                        pass

                # Dispatch React synthetic events to ensure form state updates
                if clicked:
                    try:
                        await page.evaluate("""
                            ([name, value]) => {
                                const inp = document.querySelector(
                                    `input[type="radio"][name="${CSS.escape(name)}"][value="${CSS.escape(value)}"]`
                                );
                                if (!inp) return;
                                inp.checked = true;
                                ['click', 'input', 'change'].forEach(type =>
                                    inp.dispatchEvent(new Event(type, { bubbles: true }))
                                );
                            }
                        """, [chosen_name, chosen_val])
                        await asyncio.sleep(0.3)
                        print(f"      → [Radio] '{question[:55]}' → '{chosen_label}'")
                    except Exception:
                        pass

        except Exception as e:
            print(f"      → [Radio] error: {e}")

        # ── Checkboxes ────────────────────────────────────────────────────────
        try:
            for chk in await page.locator("input[type='checkbox']").all():
                try:
                    if not await chk.is_visible(timeout=200):
                        continue
                    if await chk.is_checked():
                        continue
                    label_text = await _get_field_label(page, chk)
                    answer = (ollama_answer(label_text) or "").lower() if label_text else "yes"
                    # Only check if the answer suggests yes/agree/true
                    if any(w in answer for w in ("yes", "true", "agree", "confirm", "authorized", "eligible")):
                        await chk.check()
                        print(f"      → [Checkbox] checked: {label_text[:60]!r}")
                except Exception:
                    pass
        except Exception:
            pass

        # ── Select dropdowns ──────────────────────────────────────────────────
        try:
            for sel_el in await page.locator("select").all():
                try:
                    if not await sel_el.is_visible(timeout=200):
                        continue
                    current = await sel_el.input_value()
                    if current and current not in ("", "0", "placeholder"):
                        continue
                    label_text = await _get_field_label(page, sel_el)
                    answer = (ollama_answer(label_text) or "").lower() if label_text else ""
                    # Get all option values and texts
                    options_info = await sel_el.evaluate("""
                        el => Array.from(el.options)
                             .filter(o => o.value && o.value !== '0')
                             .map(o => ({value: o.value, text: o.text.trim().toLowerCase()}))
                    """)
                    if not options_info:
                        continue
                    # Pick the option whose text best matches the answer
                    chosen_val = None
                    for opt in options_info:
                        if answer and (opt["text"] in answer or answer in opt["text"]):
                            chosen_val = opt["value"]
                            break
                    # Default: first non-empty option
                    if not chosen_val:
                        chosen_val = options_info[0]["value"]
                    await sel_el.select_option(value=chosen_val)
                    print(f"      → [Select] '{label_text[:50]}' → '{chosen_val}'")
                except Exception:
                    pass
        except Exception:
            pass

    except Exception:
        pass


async def _page_has_validation_error(page: Page) -> bool:
    """Detect Dice's 'A Problem was Encountered' validation banner."""
    for sel in [
        "text='A Problem was Encountered'",
        "text='A Problem was Encountered Submitting'",
        "[class*='error-banner']",
        "[class*='alert-danger']",
        ".error-message",
    ]:
        try:
            if await page.locator(sel).first.is_visible(timeout=200):
                return True
        except Exception:
            pass
    return False


async def _find_wizard_container(page: Page) -> str | None:
    """Return the CSS selector of the wizard's scrollable container, or None."""
    info = await page.evaluate("""
        () => {
            const selectors = [
                '[role="dialog"]', 'mat-dialog-container',
                '[class*="apply-wizard"]', '[class*="applyWizard"]',
                '[class*="wizard"]', '[class*="modal-body"]',
                '[class*="dialog-content"]', '[class*="modal-content"]'
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.scrollHeight > el.clientHeight + 10) return sel;
            }
            return null;
        }
    """)
    return info  # string or None


async def _scroll_one_section(page: Page, container_sel: str | None, pos: int):
    """Scroll the wizard container (or window) to `pos` px and fill visible fields."""
    if container_sel:
        await page.evaluate(
            f"() => {{ const el = document.querySelector({json.dumps(container_sel)}); "
            f"if (el) el.scrollTop = {pos}; }}"
        )
    else:
        await page.evaluate(f"window.scrollTo(0, {pos})")
    await asyncio.sleep(0.1)
    await _fill_visible_fields(page)


async def _close_wizard(page: Page):
    await smart_click(
        page,
        texts=["Continue search", "Back to Search", "Close", "Done", "Return to search"],
        ollama_instruction="the Close or Continue search button to dismiss the wizard",
        timeout=3.0,
    )
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def handle_custom_questions(page: Page):
    """Fill free-text fields using local Ollama text model (called after Next clicks)."""
    await _fill_visible_fields(page)


async def _email_recruiter_if_found(page: Page, title: str, company: str, url: str):
    """Scan the job page for recruiter emails and send outreach if found."""
    try:
        text = await page.inner_text("body")

        emails = gmail_sender.extract_recruiter_emails(text)
        for addr in emails:
            sent = gmail_sender.send_recruiter_email(
                to=addr,
                job_title=title,
                company=company,
                job_url=url,
                sender_email=DICE_EMAIL,
            )
            if sent:
                print(f"      → [Gmail] Outreach sent to {addr}")
            else:
                print(f"      → [Gmail] Already emailed {addr} — skipped")
    except Exception as e:
        print(f"      → [Gmail] email scan error: {e}")


async def _resolve_company(page: Page, company: str) -> str:
    """Extract company name from the Dice job detail page."""
    if company.strip():
        return company.strip()
    # Primary: company name lives in the .logo wrapper on Dice job pages
    for sel in [".logo p", ".logo a", "[class*='line-clamp'] "]:
        try:
            text = await page.locator(sel).first.inner_text(timeout=600)
            if text.strip():
                return text.strip()
        except Exception:
            pass
    # Fallback: parse from page title "Job Title - Company - Location | Dice.com"
    try:
        title = await page.title()
        parts = title.split(" - ")
        if len(parts) >= 2:
            candidate = parts[1].strip()
            if candidate and " | " not in candidate:
                return candidate
    except Exception:
        pass
    return company


async def apply_to_job(page: Page, title: str, company: str, location: str, url: str) -> str:
    """
    Full Easy Apply flow — hybrid visual clicker at every step.

    Flow:
      1. Wait for page to render  →  detect state (apply / already-applied / external)
      2. Click Easy Apply         →  3-layer hybrid (DOM → OCR → Vision)
      3. One-click apply check    →  some jobs submit without a wizard
      4. Dynamic wizard loop      →  Next × N  then  Submit
      5. Confirm success          →  5 independent signals
    """
    global DEBUG_SHOT_TAKEN

    # ── 1. Wait for Angular to render ─────────────────────────────────────
    state = await wait_for_job_page_ready(page)

    if state == "applied":
        return "skipped - already applied"
    if state == "external":
        await page.go_back()
        await asyncio.sleep(0.8)
        return "external - skipped"
    if state == "apply":
        # Scan page for recruiter emails while it's loaded
        await _email_recruiter_if_found(page, title, company, url)
    if state == "timeout":
        if not DEBUG_SHOT_TAKEN:
            DEBUG_SHOT_TAKEN = True
            shot = str(Path(__file__).parent / "debug_job_page.png")
            await page.screenshot(path=shot)
            print(f"    [debug] Page timed out → {shot}")
        return "error: apply button not found (timeout)"

    # ── 2. Click Easy Apply ────────────────────────────────────────────────
    # Fast path: Apply button is always in the job header at the top of the page
    clicked = await _click_apply_button_top(page)
    if clicked:
        print("      → [Easy Apply] clicked via direct header selector")

    # Fallback: full smart_click if the direct hit missed
    if not clicked:
        clicked = await smart_click(
        page,
        texts=["Easy Apply", "Apply Now", "Apply"],
        ollama_instruction=(
            "the main Easy Apply or Apply Now button for this job posting "
            "(NOT the filter button, NOT the Applied badge)"
        ),
        timeout=8.0,
        label="Easy Apply",
        )

    if not clicked:
        if not DEBUG_SHOT_TAKEN:
            DEBUG_SHOT_TAKEN = True
            shot = str(Path(__file__).parent / "debug_job_page.png")
            await page.screenshot(path=shot)
            print(f"    [debug] Apply button not found → {shot}")
        return "error: apply button not found"

    # Poll up to 5 s for the page to react — exit as soon as something happens
    _wizard_texts = ["Next", "Continue", "Next Step", "Proceed",
                     "Submit Application", "Submit", "Send Application", "Finish"]
    _poll_end = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < _poll_end:
        if "dice.com" not in page.url:
            break
        if len(page.context.pages) > 1:
            break
        if await page_shows_applied(page):
            break
        if await _find_active_dialog(page) or await _find_wizard_container(page):
            break
        if await _button_visible(page, _wizard_texts):
            break
        await asyncio.sleep(0.3)

    # ── 3. External redirect — same tab ───────────────────────────────────
    if "dice.com" not in page.url:
        await page.go_back()
        await asyncio.sleep(0.8)
        return "external - skipped"

    # ── 3b. External redirect — new tab — attempt to apply there too ──────
    all_pages = page.context.pages
    if len(all_pages) > 1:
        new_tab = next((p for p in all_pages if p != page), None)
        if new_tab:
            print("      → New tab opened — attempting external apply...")
            result = await _try_apply_external_tab(new_tab)
            if result == "applied":
                return "applied"
        return "external - skipped"

    # ── 4. One-click apply (no wizard) ────────────────────────────────────
    if await page_shows_applied(page):
        return "applied"

    # ── 5. Wait for wizard dialog to appear (max 4 s) ────────────────────
    # If no dialog appears, the Apply click did nothing useful — bail early
    # instead of scrolling to the footer looking for buttons that don't exist.
    SUBMIT_TEXTS = ["Submit Application", "Submit", "Send Application", "Finish"]
    NEXT_TEXTS   = ["Next", "Continue", "Next Step", "Proceed"]

    dialog_sel    = None
    container_sel = None
    for _ in range(8):           # poll up to 4 s
        dialog_sel    = await _find_active_dialog(page)
        container_sel = await _find_wizard_container(page)
        if dialog_sel or container_sel:
            break
        # also accept if Next/Submit already visible without a detected dialog
        if await _button_visible(page, NEXT_TEXTS + SUBMIT_TEXTS):
            break
        await asyncio.sleep(0.5)

    if not dialog_sel and not container_sel and not await _button_visible(page, NEXT_TEXTS + SUBMIT_TEXTS):
        return "external - skipped"   # wizard never opened — external/unsupported job

    # ── 6. Dynamic wizard loop ────────────────────────────────────────────
    scroll_pos  = 0
    SCROLL_STEP = 350
    next_clicks = 0

    for step in range(40):
        await asyncio.sleep(0.1)

        # ── Applied / dialog-closed checks ────────────────────────────────
        if await page_shows_applied(page):
            await _close_wizard(page)
            return "applied"
        if dialog_sel and not await _dialog_visible(page, dialog_sel):
            await asyncio.sleep(0.8)
            return "applied"

        # ── Re-detect container if we didn't find one initially ───────────
        if not container_sel:
            container_sel = await _find_wizard_container(page)

        # ── Check buttons at the CURRENT scroll position first ────────────
        next_vis   = await _button_visible(page, NEXT_TEXTS)
        submit_vis = await _button_visible(page, SUBMIT_TEXTS)

        if next_vis:
            if next_clicks >= 8:
                print("    [debug] Next clicked 8 times with no Submit — bailing to avoid loop")
                break
            # Fill required fields BEFORE clicking Next
            await _fill_visible_fields(page)
            clicked = await smart_click(
                page,
                texts=NEXT_TEXTS,
                ollama_instruction="the Next or Continue button to go to the next step of the application wizard",
                timeout=4.0,
                label="Next",
            )
            if clicked:
                next_clicks += 1
                await asyncio.sleep(1.0)
                # If Dice shows a validation error, fill fields and retry once
                if await _page_has_validation_error(page):
                    print("      → [Next] validation error — re-filling fields and retrying")
                    await _fill_visible_fields(page)
                    await smart_click(
                        page,
                        texts=NEXT_TEXTS,
                        ollama_instruction="the Next or Continue button to go to the next step of the application wizard",
                        timeout=4.0,
                        label="Next (retry)",
                    )
                    await asyncio.sleep(1.0)
                scroll_pos = 0
                await _scroll_one_section(page, container_sel, scroll_pos)
            continue

        if submit_vis:
            clicked = await smart_click(
                page,
                texts=SUBMIT_TEXTS,
                ollama_instruction="the Submit Application or Submit button to finalize the job application",
                timeout=4.0,
                label="Submit",
            )
            if clicked:
                success = await wait_for_wizard_close(page, dialog_sel, timeout=12.0)
                if success or await page_shows_applied(page):
                    await _close_wizard(page)
                    return "applied"
                if not await _button_visible(page, SUBMIT_TEXTS, timeout_ms=300):
                    return "applied"
                try:
                    btns = await page.locator("button").all_text_contents()
                    print(f"    [debug] Buttons after Submit: {[b.strip() for b in btns if b.strip()]}")
                except Exception:
                    pass
                return "error: no success confirmation"
            continue

        # ── No buttons visible — scroll one section forward ───────────────
        # Stop if we've scrolled past what a normal wizard could be
        if scroll_pos > 3000:
            break
        scroll_pos += SCROLL_STEP
        await _scroll_one_section(page, container_sel, scroll_pos)

    if await page_shows_applied(page):
        return "applied"

    shot = str(Path(__file__).parent / "debug_wizard_stall.png")
    await page.screenshot(path=shot)
    try:
        btns = await page.locator("button").all_text_contents()
        print(f"    [debug] Wizard stalled → {shot}")
        print(f"    [debug] Visible buttons: {[b.strip() for b in btns if b.strip()]}")
    except Exception:
        pass
    return "error: wizard did not complete"


# ══════════════════════════════════════════════════════════════════════════════
#  JOB SCRAPING
# ══════════════════════════════════════════════════════════════════════════════

async def get_job_cards(page: Page) -> list[dict]:
    """Scrape all job cards from the search results page."""
    print("  Waiting for job list...")
    try:
        await page.wait_for_selector(
            "a[href*='/job-detail/'], dhi-search-card, [data-cy='card-title-link']",
            timeout=20000,
        )
    except PlaywrightTimeoutError:
        await page.screenshot(path=str(Path(__file__).parent / "debug_no_cards.png"))
        print(f"  WARNING: No cards found. URL: {page.url}")
        return []

    await asyncio.sleep(1.0)

    # Scroll to bottom in steps so lazy-loaded cards render
    await page.evaluate("""
        async () => {
            const delay = ms => new Promise(r => setTimeout(r, ms));
            const scrollHeight = () => document.body.scrollHeight;
            let last = 0;
            while (true) {
                window.scrollBy(0, 600);
                await delay(300);
                if (document.body.scrollHeight === last) break;
                last = document.body.scrollHeight;
            }
            window.scrollTo(0, 0);
        }
    """)
    await asyncio.sleep(0.8)

    raw = await page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();

            // Strategy 1: dhi-search-card Angular components
            document.querySelectorAll('dhi-search-card').forEach(card => {
                const a = card.querySelector('a[href*="job-detail"]');
                if (!a) return;
                const href = a.getAttribute('href') || '';
                const url  = href.startsWith('http') ? href : 'https://www.dice.com' + href;
                if (seen.has(url)) return;
                seen.add(url);
                const company = card.querySelector(
                    '[data-cy="search-result-company-name"], [class*="company"]'
                );
                const loc = card.querySelector(
                    '[data-cy="search-result-location"], [class*="location"]'
                );
                const appliedBadge = card.querySelector(
                    '[class*="applied-badge"], [class*="alreadyApplied"], [class*="Applied"]'
                );
                results.push({
                    title:          a.textContent.trim(),
                    company:        company ? company.textContent.trim() : '',
                    location:       loc ? loc.textContent.trim() : '',
                    url,
                    already_applied: !!(appliedBadge),
                });
            });
            if (results.length) return results;

            // Strategy 2: generic job-detail links
            document.querySelectorAll('a[href*="/job-detail/"]').forEach(a => {
                const href = a.getAttribute('href') || '';
                const url  = href.startsWith('http') ? href : 'https://www.dice.com' + href;
                if (seen.has(url) || !a.textContent.trim()) return;
                seen.add(url);
                const card    = a.closest('li, article, [class*="card"], [class*="result"]') || a.parentElement;
                const company = card ? card.querySelector('[class*="company"]') : null;
                const loc     = card ? card.querySelector('[class*="location"]') : null;
                const applied = card ? card.querySelector('[class*="applied"]') : null;
                results.push({
                    title:          a.textContent.trim(),
                    company:        company ? company.textContent.trim() : '',
                    location:       loc ? loc.textContent.trim() : '',
                    url,
                    already_applied: !!(applied && applied.textContent.toLowerCase().includes('applied')),
                });
            });
            return results;
        }
    """)

    cards = [r for r in raw if r.get("title") and r.get("url")]
    print(f"  Found {len(cards)} job(s).")
    return cards


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def process_jobs(page: Page, cards: list[dict]):
    total = len(cards)
    for idx, card in enumerate(cards, 1):
        title    = card["title"]
        company  = card.get("company", "")
        location = card.get("location", "")
        url      = card["url"]
        label    = f"[{idx}/{total}]"

        rejection_reason = early_career_rejection_reason(
            title, max_required_years=_PROFILE_MAX_REQUIRED_YEARS,
            allowed_levels=_PROFILE_EXPERIENCE_LEVELS,
        )
        if rejection_reason:
            print(f"  {label} ⏭️  Skipped: {title[:65]} — {rejection_reason}")
            log_application(title, company, location, url, f"skipped - {rejection_reason}")
            stats["skipped"] += 1
            continue

        # ── Tracker: skip only what THIS profile already applied ──────────
        if url in APPLIED_URLS:
            print(f"  {label} ⏭️  Skipped: {title[:65]} — already applied")
            stats["skipped"] += 1
            continue

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(0.8)
        except Exception:
            print(f"  {label} ❌  Navigation failed: {title[:65]}")
            log_application(title, company, location, url, "error: navigation failed")
            stats["errors"] += 1
            continue

        try:
            description = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            description = ""
        rejection_reason = early_career_rejection_reason(
            title, description, _PROFILE_MAX_REQUIRED_YEARS,
            _PROFILE_EXPERIENCE_LEVELS,
        )
        if rejection_reason:
            print(f"  {label} ⏭️  Skipped: {title[:65]} — {rejection_reason}")
            log_application(title, company, location, url, f"skipped - {rejection_reason}")
            stats["skipped"] += 1
            continue

        # Resolve company from the job detail page (search cards often miss it)
        company = await _resolve_company(page, company)

        # ── Apply with one automatic retry on transient errors ────────────
        status = "error: unknown"
        for attempt in range(2):
            try:
                if attempt == 1:
                    print(f"      ↻  Retrying {title[:55]}…")
                    await page.goto(url, wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
                status = await apply_to_job(page, title, company, location, url)
            except Exception as e:
                status = f"error: {e}"

            if status == "applied" or "skipped" in status or "external" in status:
                break
            if attempt == 0 and status.startswith("error"):
                continue

        log_application(title, company, location, url, status)

        if status == "applied":
            print(f"  {label} ✅  Applied: {title[:65]}")
            stats["applied"] += 1
        elif "skipped" in status or "external" in status:
            print(f"  {label} ⏭️  Skipped: {title[:65]} — {status}")
            stats["skipped"] += 1
        else:
            print(f"  {label} ❌  Error: {title[:65]} — {status}")
            stats["errors"] += 1

        await asyncio.sleep(random.uniform(1.0, 2.0))


def prompt_settings() -> dict:
    """Interactive prompt — asks the user for search settings before each run."""
    from urllib.parse import quote_plus

    print("╔══════════════════════════════════════════════════╗")
    print("║       Dice.com Auto Apply — Setup                ║")
    print("╚══════════════════════════════════════════════════╝\n")

    # ── Profile / account ─────────────────────────────────────────────────
    def _profile_label(p: Path) -> str:
        name_file = p / ".profile_name"
        if name_file.exists():
            return name_file.read_text().strip()
        return p.name

    def _profile_email(p: Path) -> str:
        email_file = p / ".profile_email"
        if email_file.exists():
            return email_file.read_text().strip()
        return ""

    def _save_profile(p: Path, label: str, email: str = ""):
        p.mkdir(parents=True, exist_ok=True)
        (p / ".profile_name").write_text(label)
        if email:
            (p / ".profile_email").write_text(email.strip().lower())

    profiles_dir = Path.home()
    existing = sorted(
        p for p in profiles_dir.iterdir()
        if p.is_dir() and p.name.startswith(".dice-")
    )
    print("── Dice profile ──────────────────────────────────")
    if existing:
        for i, p in enumerate(existing, 1):
            em = _profile_email(p)
            em_display = f"  ({em})" if em else ""
            print(f"  {i}. {_profile_label(p)}{em_display}")
        print(f"  {len(existing)+1}. Create new profile")
        choice = input(f"Select profile [1]: ").strip() or "1"
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(existing):
                session_dir = existing[idx]
            else:
                display_name = input("  Profile display name (e.g. Work Account): ").strip() or "New Account"
                folder_slug  = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
                session_dir  = Path.home() / f".dice-{folder_slug}"
                email_input  = input("  Dice account email: ").strip().lower()
                _save_profile(session_dir, display_name, email_input)
        except ValueError:
            session_dir = existing[0]
    else:
        print("  No saved profiles found — creating your first profile.")
        display_name = input("  Profile display name (e.g. My Dice Account): ").strip() or "My Dice Account"
        folder_slug  = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
        session_dir  = Path.home() / f".dice-{folder_slug}"
        email_input  = input("  Dice account email: ").strip().lower()
        _save_profile(session_dir, display_name, email_input)

    # Load (or ask for) the email tied to this profile
    profile_email = _profile_email(session_dir)
    if not profile_email:
        profile_email = input(f"  Email for '{_profile_label(session_dir)}': ").strip().lower()
        (session_dir / ".profile_email").write_text(profile_email)

    print(f"  → Using: {_profile_label(session_dir)} ({profile_email})\n")

    # ── Job title ─────────────────────────────────────────────────────────
    print("── Job search ────────────────────────────────────")
    query = input(f"  Job title / keywords [{SEARCH_QUERY}]: ").strip() or SEARCH_QUERY
    print()

    # ── Date filter ───────────────────────────────────────────────────────
    print("── Posted date ───────────────────────────────────")
    date_options = [
        ("ONE",    "Today (last 24 hrs)"),
        ("THREE",  "Last 3 days"),
        ("SEVEN",  "Last 7 days"),
        ("THIRTY", "Last 30 days"),
        ("",       "Any time"),
    ]
    default_date_idx = next(
        (i for i, (k, _) in enumerate(date_options) if k == POSTED_DATE), 0
    )
    for i, (_, label) in enumerate(date_options, 1):
        marker = " ◀" if i == default_date_idx + 1 else ""
        print(f"  {i}. {label}{marker}")
    choice = input(f"  Select [{ default_date_idx + 1 }]: ").strip() or str(default_date_idx + 1)
    try:
        posted_date = date_options[int(choice) - 1][0]
    except (ValueError, IndexError):
        posted_date = POSTED_DATE
    print()

    # ── Easy Apply toggle ─────────────────────────────────────────────────
    print("── Easy Apply filter ─────────────────────────────")
    default_ea = "y" if EASY_APPLY else "n"
    ea_input = input(f"  Easy Apply jobs only? [{'Y/n' if EASY_APPLY else 'y/N'}]: ").strip().lower()
    easy_apply = (ea_input in ("y", "yes", "")) if ea_input else EASY_APPLY
    if ea_input == "":
        easy_apply = EASY_APPLY
    print()

    # ── Build URL ─────────────────────────────────────────────────────────
    url = f"https://www.dice.com/jobs?q={quote_plus(query)}&pageSize=20"
    if easy_apply:
        url += "&filters.easyApply=true"
    if posted_date:
        url += f"&filters.postedDate={posted_date}"

    date_label = dict(date_options).get(posted_date, "Any time")
    easy_label = "Easy Apply only" if easy_apply else "All jobs"

    profile_display = _profile_label(session_dir)
    print("╔══════════════════════════════════════════════════╗")
    print(f"║  Query   : {query:<38}║")
    print(f"║  Posted  : {date_label:<38}║")
    print(f"║  Filter  : {easy_label:<38}║")
    print(f"║  Profile : {profile_display:<38}║")
    print("╚══════════════════════════════════════════════════╝")
    confirm = input("\nStart applying? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        print("Cancelled.")
        raise SystemExit(0)
    print()

    return {
        "session_dir":   session_dir,
        "profile_email": profile_email,
        "query":         query,
        "posted_date":   posted_date,
        "easy_apply":    easy_apply,
        "search_url":    url,
        "date_label":    date_label,
        "easy_label":    easy_label,
    }


async def run(profile_email: str = "", query: str | None = None,
              posted_date: str | None = None, easy_apply: bool | None = None,
              experience_levels: list[str] | None = None,
              max_required_years: int | None = None):
    caps = detect_capabilities()
    print_capabilities(caps)

    if profile_email:
        # Non-interactive mode: build settings from CLI args + env defaults
        from urllib.parse import quote_plus
        _q  = query       if query       is not None else SEARCH_QUERY
        _pd = posted_date if posted_date is not None else POSTED_DATE
        _ea = easy_apply  if easy_apply  is not None else EASY_APPLY

        session_dir = _profile_session_dir(profile_email)

        url = f"https://www.dice.com/jobs?q={quote_plus(_q)}&pageSize=20"
        if _ea:
            url += "&filters.easyApply=true"
        if _pd:
            url += f"&filters.postedDate={_pd}"

        date_options = {"ONE":"Today","THREE":"Last 3 days","SEVEN":"Last 7 days",
                        "THIRTY":"Last 30 days","":"Any time"}
        settings = {
            "session_dir":   session_dir,
            "profile_email": profile_email,
            "query":         _q,
            "posted_date":   _pd,
            "easy_apply":    _ea,
            "search_url":    url,
            "date_label":    date_options.get(_pd, "Any time"),
            "easy_label":    "Easy Apply only" if _ea else "All jobs",
        }
        print(f"  [non-interactive] Profile: {profile_email}  Query: {_q}")
    else:
        # Interactive setup prompt
        settings = prompt_settings()

    search_url    = settings["search_url"]
    session_dir   = settings["session_dir"]
    date_label    = settings["date_label"]
    easy_label    = settings["easy_label"]
    query         = settings["query"]
    profile_email = settings["profile_email"]

    # Per-profile CSV and tracker
    global CSV_FILE, DICE_EMAIL, _PROFILE_EXPERIENCE_LEVELS, _PROFILE_MAX_REQUIRED_YEARS
    DICE_EMAIL = profile_email or DICE_EMAIL   # override with profile-specific email
    CSV_FILE = session_dir / "applied_jobs.csv"
    ensure_csv()
    load_applied_urls()
    load_profile(DICE_EMAIL, force_setup="--setup" in sys.argv)
    if experience_levels is not None:
        _PROFILE_EXPERIENCE_LEVELS = experience_levels
    if max_required_years is not None:
        _PROFILE_MAX_REQUIRED_YEARS = max(0, min(4, max_required_years))
    gmail_sender.init_gmail(session_dir, sender_name=SENDER_NAME,
                            sender_email=DICE_EMAIL, resume_path=RESUME_PATH)

    async with async_playwright() as pw:
        browser = None
        context = None

        if DICE_MODE == "session":
            # Override SESSION_DIR with the user's chosen profile
            global SESSION_DIR
            SESSION_DIR = session_dir
            browser, context, page = await launch_session(pw)
        elif DICE_MODE == "cdp":
            try:
                browser, context, page = await connect_cdp(pw)
            except Exception as e:
                print(f"CDP failed: {e}\nSwitch to DICE_MODE=session in .env.")
                return
        else:
            browser, context, page = await launch_login(pw)

        try:
            print(f"Searching: \"{query}\" | {easy_label} | Posted: {date_label}\n")
            page_num = 1

            while True:
                paged_url = search_url if page_num == 1 else f"{search_url}&page={page_num}"
                print(f"\n{'─'*55}")
                print(f"  Page {page_num}")
                print(f"{'─'*55}")
                await page.goto(paged_url, wait_until="domcontentloaded")
                await asyncio.sleep(2)

                cards = await get_job_cards(page)
                if not cards:
                    print("  No jobs found — done.")
                    break

                await process_jobs(page, cards)

                # Check next page
                page_num += 1
                await page.goto(f"{search_url}&page={page_num}", wait_until="domcontentloaded")
                await asyncio.sleep(1.5)
                if await page.locator("a[href*='/job-detail/']").count() == 0:
                    print("\nNo more pages. Done.")
                    break

        except Exception as e:
            print(f"\nFatal error: {e}")
        finally:
            print(f"\n{'='*45}")
            print(f"  ✅ Applied : {stats['applied']}")
            print(f"  ⏭️  Skipped : {stats['skipped']}")
            print(f"  ❌ Errors  : {stats['errors']}")
            print(f"  📧 Emailed : {len(gmail_sender.SENT_EMAILS)}")
            print(f"{'='*45}")
            print(f"Log: {CSV_FILE}")

            # Show any recruiter replies received in inbox
            replies = gmail_sender.check_inbox_replies()
            if replies:
                print(f"\n{'='*45}")
                print(f"  📬 Recruiter replies in your inbox ({len(replies)}):")
                for r in replies:
                    print(f"    From   : {r['from']}")
                    print(f"    Subject: {r['subject']}")
                    print(f"    Date   : {r['date']}")
                    print(f"    Preview: {r['snippet'][:120]}")
                    print()
                print(f"{'='*45}")
            try:
                if DICE_MODE == "session" and context:
                    await context.close()
                elif browser:
                    await browser.close()
            except Exception:
                pass


async def login_only(profile_email: str = ""):
    """
    Login-only mode (python3 main.py --login).
    Opens the browser and keeps it open until Dice authentication completes.
    Lets you log in or verify the saved session at your own pace.
    """
    print("╔══════════════════════════════════════════════════╗")
    print("║       Dice.com — Login & Save Session            ║")
    print("╚══════════════════════════════════════════════════╝\n")

    def _profile_label(p: Path) -> str:
        name_file = p / ".profile_name"
        return name_file.read_text().strip() if name_file.exists() else p.name

    def _save_profile_label(p: Path, label: str):
        p.mkdir(parents=True, exist_ok=True)
        (p / ".profile_name").write_text(label)

    if profile_email:
        session_dir = _profile_session_dir(profile_email)
        print(f"── Profile: {profile_email} ──")
    else:
        profiles_dir = Path.home()
        existing = sorted(p for p in profiles_dir.iterdir()
                          if p.is_dir() and p.name.startswith(".dice-"))

        print("── Select or create a Dice profile ───────────────")
        if existing:
            for i, p in enumerate(existing, 1):
                print(f"  {i}. {_profile_label(p)}")
            print(f"  {len(existing)+1}. Create new profile")
            choice = input("Select [1]: ").strip() or "1"
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(existing):
                    session_dir = existing[idx]
                else:
                    display_name = input("  Profile name: ").strip() or "New Account"
                    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
                    session_dir = Path.home() / f".dice-{slug}"
                    _save_profile_label(session_dir, display_name)
            except ValueError:
                session_dir = existing[0]
        else:
            display_name = input("  Profile name (e.g. My Dice Account): ").strip() or "My Dice Account"
            slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
            session_dir = Path.home() / f".dice-{slug}"
            _save_profile_label(session_dir, display_name)

    print(f"\n  → Profile: {_profile_label(session_dir)}\n")

    global SESSION_DIR
    SESSION_DIR = session_dir

    async with async_playwright() as pw:
        session_dir.mkdir(parents=True, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=False,
            slow_mo=20,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Always navigate to Dice login so user can log in or switch accounts
        await page.goto("https://www.dice.com/dashboard", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        if not _is_authenticated_dice_url(page.url):
            (session_dir / ".dice_session_ready").unlink(missing_ok=True)
            print(">>> Browser is open — log in to Dice.com now.")
            print(">>> It will stay open and continue automatically after login.")
            await _wait_for_dice_login(page)
        else:
            print(">>> You are already logged in to Dice.com.")

        _mark_dice_session_ready(session_dir)
        print(f"\n✅  Session saved for '{_profile_label(session_dir)}'.")
        print("    Run  python3 main.py  to start applying.\n")
        try:
            await context.close()
        except Exception:
            pass


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--login",   action="store_true")
    ap.add_argument("--profile", default="")
    ap.add_argument("--query",   default="")
    ap.add_argument("--date",    default="")
    ap.add_argument("--easy-apply", dest="easy_apply", default=None)
    ap.add_argument("--experience-levels", default="")
    ap.add_argument("--max-required-years", type=int, default=None)
    args, _ = ap.parse_known_args()

    if args.login:
        asyncio.run(login_only(args.profile))
    elif args.profile:
        asyncio.run(run(profile_email=args.profile,
                        query=args.query or None,
                        posted_date=args.date or None,
                        easy_apply=(args.easy_apply.lower() in ("true","yes","1")
                                    if args.easy_apply is not None else None),
                        experience_levels=([level for level in args.experience_levels.split(",") if level]
                                           if args.experience_levels else None),
                        max_required_years=args.max_required_years))
    else:
        asyncio.run(run())
