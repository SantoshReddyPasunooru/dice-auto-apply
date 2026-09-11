"""
microsoft.py — Microsoft custom ATS form filler
================================================
Microsoft careers site: apply.careers.microsoft.com
NOT Workday — it's Microsoft's own ATS.

Job listing API:
  GET /api/pcsx/search?domain=microsoft.com&q={keywords}&num={n}&sort_by=timestamp&hl=en

Job apply URL:
  https://apply.careers.microsoft.com/careers/apply?pid={position_id}

Form sections (all one-page, no pagination):
  1. Application location(s)  — checkbox
  2. Resume                   — file upload
  3. Contact Information      — name, email, phone, address
  4. Work Authorization       — Yes/No comboboxes
  5. Self-identification       — voluntary ethnicity/gender/disability
  6. Candidate questions       — govt/NDA/prior MS employment
  7. Job specific questions    — role qualification radios
  8. Acknowledgment           — three checkboxes + Submit
"""

import asyncio
import re
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from .common import answer_for, SUBMIT_WAIT

try:
    from apply_logger import log as _log
except ImportError:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from apply_logger import log as _log
    except ImportError:
        class _NullLog:
            def __getattr__(self, _): return lambda *a, **k: None
        _log = _NullLog()


# ── Job listing (REST API — no browser needed) ─────────────────────────────────

def microsoft_list_jobs(keywords: list = None, num: int = 50) -> list:
    """
    Fetch open Microsoft jobs via the /api/pcsx/search REST endpoint.
    Returns a list of job dicts compatible with the apply pipeline.
    """
    import requests
    import time
    _log.fn("microsoft_list_jobs", keywords=keywords, num=num)

    q = " ".join(keywords) if keywords else ""
    base = "https://apply.careers.microsoft.com/api/pcsx/search"
    params = f"domain=microsoft.com&num={num}&hl=en&sort_by=timestamp"
    if q:
        params += f"&q={urllib.parse.quote(q)}"
    url = f"{base}?{params}"

    _log.api("GET", url)
    r = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0.0.0 Safari/537.36",
            })
            _log.api("GET", url, status=r.status_code,
                     snippet=r.text[:120].replace("\n", " "))
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                _log.warn(f"microsoft_list_jobs: 429 rate-limited — waiting {wait}s "
                          f"(attempt {attempt+1}/3)")
                time.sleep(wait)
                continue
            break
        except Exception as exc:
            _log.err("microsoft_list_jobs request failed", exc=exc)
            return []

    if r is None or r.status_code != 200:
        _log.err(f"microsoft_list_jobs: HTTP {r.status_code if r else 'no response'}")
        return []

    try:
        data = r.json()
        positions = data.get("data", {}).get("positions", [])
        _log.var("raw_positions_count", len(positions))

        jobs = []
        for p in positions:
            pid = p.get("id", "")
            title = p.get("name", "")
            locations = p.get("locations", [])
            jobs.append({
                "title":        title,
                "url":          f"https://apply.careers.microsoft.com/careers/apply?pid={pid}",
                "location":     ", ".join(locations),
                "ats":          "microsoft",
                "pid":          pid,
                "displayJobId": p.get("displayJobId", ""),
                "postedTs":     p.get("postedTs", 0),
                "department":   p.get("department", ""),
            })

        _log.ok(f"microsoft_list_jobs: found {len(jobs)} jobs")
        return jobs

    except Exception as exc:
        _log.err("microsoft_list_jobs failed", exc=exc)
        return []


# ── Helper: fill a plain text input by element ID ─────────────────────────────

async def _ms_fill(page: Page, field_id: str, value: str) -> bool:
    """Triple-click + fill a text/tel input by ID."""
    _log.fn("_ms_fill", field_id=field_id, value=value[:60] if value else None)
    if not value:
        _log.skip(f"_ms_fill: empty value for {field_id!r}")
        return False
    try:
        el = page.locator(f"#{field_id}").first
        if not await el.count():
            _log.null(field_id, reason="element not found on page")
            return False
        await el.triple_click()
        await el.fill(value)
        _log.browser("fill", f"#{field_id}", value=value[:60], result="ok")
        return True
    except Exception as exc:
        _log.err(f"_ms_fill({field_id})", exc=exc)
        return False


# ── Helper: interact with Microsoft's custom combobox dropdowns ────────────────

async def _ms_select(page: Page, field_id: str, value: str) -> bool:
    """
    Open a Microsoft custom combobox (data-test-id wrapper) and
    pick the option whose text matches `value`.
    """
    _log.fn("_ms_select", field_id=field_id, value=value)
    try:
        # The combobox input sits inside a [data-test-id="{field_id}"] wrapper
        wrapper = page.locator(f'[data-test-id="{field_id}"]').first
        if not await wrapper.count():
            _log.null(field_id, reason="combobox wrapper not found")
            return False

        combo = wrapper.locator("input[role=combobox]").first
        if not await combo.count():
            _log.warn(f"_ms_select: no combobox input inside {field_id!r}")
            return False

        _log.browser("click", f"[data-test-id={field_id!r}] input[role=combobox]",
                     result="opening dropdown")
        await combo.click()
        await asyncio.sleep(0.6)

        # Options appear in [role=option] elements
        options = page.locator("[role=option]")
        count = await options.count()
        _log.var("dropdown_options_count", count)

        if count == 0:
            _log.warn(f"_ms_select: no [role=option] found after opening {field_id!r}")
            await page.keyboard.press("Escape")
            return False

        # Find best match
        val_lower = value.lower().strip()
        chosen = None
        for i in range(count):
            opt = options.nth(i)
            text = (await opt.inner_text()).strip()
            _log.var(f"option[{i}]", text[:60])
            if val_lower in text.lower() or text.lower() in val_lower:
                chosen = opt
                chosen_text = text
                break

        if chosen is None:
            # Fall back to first option
            chosen = options.first
            chosen_text = (await chosen.inner_text()).strip()
            _log.warn(f"_ms_select: no match for {value!r} in {field_id!r} — using first: {chosen_text!r}")

        await chosen.click()
        _log.browser("click", f"option[{chosen_text[:50]}]",
                     value=value, result="ok")
        await asyncio.sleep(0.3)
        return True

    except Exception as exc:
        _log.err(f"_ms_select({field_id}, {value!r})", exc=exc)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False


# ── Helper: check a checkbox by ID if not already checked ─────────────────────

async def _ms_check(page: Page, locator_str: str, label: str = "") -> bool:
    """Check a checkbox/radio that is not yet checked."""
    _log.fn("_ms_check", locator=locator_str, label=label[:60] if label else "")
    try:
        el = page.locator(locator_str).first
        if not await el.count():
            _log.null(locator_str, reason="checkbox not found")
            return False
        if await el.is_checked():
            _log.skip(f"_ms_check: already checked — {label or locator_str}")
            return True
        await el.check()
        _log.browser("check", locator_str, result="ok")
        return True
    except Exception as exc:
        _log.err(f"_ms_check({locator_str})", exc=exc)
        return False


# ── Main form filler ───────────────────────────────────────────────────────────

async def _fill_microsoft(page: Page, profile: dict, email: str,
                           resume: Optional[Path], company: str) -> str:
    """
    Fill and submit a Microsoft careers application form.
    The browser must already be signed-in to apply.careers.microsoft.com
    (session is persisted in the Playwright user-data directory).
    """
    _log.fn("_fill_microsoft", email=email, company=company,
            page_url=page.url, resume=str(resume) if resume else None)

    _MS_LOGIN_DOMAINS = ("login.microsoftonline.com", "login.live.com",
                         "account.microsoft.com", "login.microsoft.com")
    _FORM_SELECTOR = '#Contact_Information_firstname, [data-test-id^="Contact_Information"]'
    apply_url = page.url  # save in case page closes and we need to retry

    async def _get_body(pg) -> str:
        try:
            return await pg.inner_text("body")
        except Exception:
            return ""

    def _is_login_url(url: str) -> bool:
        return any(d in url for d in _MS_LOGIN_DOMAINS)

    async def _wait_for_form(pg) -> bool:
        """Return True if form appeared, False on timeout."""
        try:
            await pg.wait_for_selector(_FORM_SELECTOR, timeout=35000)
            return True
        except Exception:
            return False

    # ── Guard: race between form load and page close ───────────────────────────
    _log.step("Microsoft: check for sign-in wall / wait for form")

    # Arm a close-event flag BEFORE anything async; also detect if page already closed
    _page_closed = asyncio.Event()
    if page.is_closed():
        _page_closed.set()
    else:
        page.on("close", lambda _: _page_closed.set())
        # Re-check immediately in case close fired between is_closed() and on()
        if page.is_closed():
            _page_closed.set()

    # Give the page a short head-start to settle
    await asyncio.sleep(1)

    # Race: form appears  vs  page closes
    form_task  = asyncio.create_task(_wait_for_form(page))
    close_task = asyncio.create_task(_page_closed.wait())
    done, pending = await asyncio.wait(
        {form_task, close_task}, return_when=asyncio.FIRST_COMPLETED, timeout=37
    )
    for t in pending:
        t.cancel()

    try:
        form_loaded = form_task in done and (not form_task.cancelled()) and bool(form_task.result())
    except Exception:
        form_loaded = False

    if form_loaded:
        _log.ok("Microsoft: form loaded (session active)")
    else:
        # Page closed OR timed out — sign-in is needed
        reason = "page closed" if _page_closed.is_set() else "form not found"
        _log.warn(f"Microsoft: sign-in required ({reason})")
        _log.var("apply_url", apply_url)

        print("\n  ⚠  Microsoft sign-in required.")
        print("  The automation browser opened but could not load the application form.")
        print("  A new browser tab will open at the Microsoft careers site.")
        print("  Please sign in there (Microsoft / LinkedIn / Google),")
        print("  then press Enter here once the page is fully signed in...",
              end="", flush=True)

        # Open a new page for sign-in (original page may be closed)
        try:
            context = page.context
        except Exception:
            _log.err("Microsoft: browser context also closed — cannot recover")
            return "error: sign-in required (context closed)"

        signin_page = await context.new_page()
        await signin_page.goto(
            "https://apply.careers.microsoft.com/careers",
            wait_until="domcontentloaded", timeout=30000,
        )
        _log.nav(signin_page.url, status="sign-in page opened")

        try:
            await asyncio.to_thread(input, "")
        except EOFError:
            _log.warn("Microsoft: stdin closed (non-interactive run) — "
                      "cannot wait for manual sign-in. "
                      "Run from a terminal to complete sign-in.")
            await signin_page.close()
            return "error: sign-in required (run from terminal to sign in)"
        print()
        _log.ok("Microsoft: user pressed Enter — checking sign-in status")

        # Wait for it to settle after sign-in
        try:
            await signin_page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        if _is_login_url(signin_page.url):
            _log.err("Microsoft: still on login URL after user prompt")
            await signin_page.close()
            return "error: sign-in required"

        # Navigate to the actual apply URL on the signed-in page
        await signin_page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
        page = signin_page   # reassign — rest of function uses `page`

        # Now wait for form
        _log.step("Microsoft: wait for application form to load (post sign-in)")
        _log.nav(page.url, status="waiting for form")
        if not await _wait_for_form(page):
            body = await _get_body(page)
            _log.err("Microsoft: form not found after sign-in")
            _log.state(page_url=page.url, body_snippet=body[:200])
            return "error: form not loaded after sign-in"
        _log.ok("Microsoft: form loaded after sign-in")

    await asyncio.sleep(1)

    # ── Extract profile fields ─────────────────────────────────────────────────
    full_name  = profile.get("name", "")
    name_parts = full_name.split()
    first_name = name_parts[0] if name_parts else ""
    last_name  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    _log.var("first_name", first_name)
    _log.var("last_name", last_name)

    location = profile.get("location", "")
    city, state_abbr = "", ""
    if "," in location:
        parts = [p.strip() for p in location.split(",")]
        city       = parts[0]
        state_abbr = parts[1] if len(parts) > 1 else ""
    _log.var("city", city)
    _log.var("state_abbr", state_abbr)

    needs_sponsorship = profile.get("needs_sponsorship", False)
    _log.var("needs_sponsorship", needs_sponsorship)

    # ── Step 1: Application location checkbox ──────────────────────────────────
    _log.step("Microsoft: Step 1 — Application location")
    try:
        pid_match = re.search(r"pid=(\d+)", page.url)
        if pid_match:
            pid = pid_match.group(1)
            loc_wrapper = page.locator(
                f'[data-test-id="Application_location_s__position_location_{pid}"]'
            ).first
            if await loc_wrapper.count():
                loc_cb = loc_wrapper.locator("input[type=checkbox]").first
                if await loc_cb.count() and not await loc_cb.is_checked():
                    await loc_cb.check()
                    _log.browser("check", f"location checkbox pid={pid}", result="ok")
                else:
                    _log.skip("Location checkbox already checked or not found")
            else:
                # Only 1 location available — might auto-select
                _log.info("No multi-location wrapper found — likely auto-selected")
        else:
            _log.warn("Could not parse pid from URL — skipping location checkbox")
    except Exception as exc:
        _log.warn(f"Location checkbox error: {exc}")

    # ── Step 2: Resume upload ──────────────────────────────────────────────────
    _log.step("Microsoft: Step 2 — Resume upload")
    if resume and resume.is_file():
        try:
            file_input = page.locator("#Resume_resume, input[type=file]").first
            if await file_input.count():
                await file_input.set_input_files(str(resume))
                _log.ok(f"Resume uploaded: {resume.name}")
                await asyncio.sleep(2.5)  # wait for upload to process
            else:
                _log.warn("Resume file input not found")
        except Exception as exc:
            _log.err("Resume upload failed", exc=exc)
    else:
        _log.null("resume", reason="no resume file provided or file missing")

    # ── Step 3: Contact Information ────────────────────────────────────────────
    _log.step("Microsoft: Step 3 — Contact Information")

    await _ms_fill(page, "Contact_Information_firstname", first_name)
    await _ms_fill(page, "Contact_Information_lastname", last_name)

    # Same legal name checkbox
    await _ms_check(page,
                    "#Contact_Information_q_cust_sameName",
                    "Is your legal name the same as your preferred name?")

    # Email (often pre-filled from SSO account)
    try:
        email_el = page.locator("#Contact_Information_email").first
        if await email_el.count():
            existing_email = await email_el.input_value()
            if existing_email:
                _log.skip(f"Email already filled from SSO: {existing_email[:40]}")
            else:
                await _ms_fill(page, "Contact_Information_email", email)
    except Exception as exc:
        _log.warn(f"Email field check: {exc}")

    # Phone country code
    await _ms_select(page, "Contact_Information_q_phone-country-code-picker", "+1")

    # Phone number
    phone = re.sub(r"\D", "", profile.get("phone", ""))
    await _ms_fill(page, "Contact_Information_q_phone", phone)

    # Address
    addr = profile.get("address_line1", "") or profile.get("location", "")
    await _ms_fill(page, "Contact_Information_q_address", addr)

    # Country → triggers state dropdown to populate
    await _ms_select(page, "Contact_Information_q_country", "United States")
    await asyncio.sleep(0.8)  # wait for state dropdown to load

    # State
    _US_STATE_NAMES = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
        "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
        "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
        "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
        "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
        "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
        "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
        "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
        "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
        "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
        "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
        "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
        "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    }
    state_full = _US_STATE_NAMES.get(state_abbr.upper(), state_abbr) if state_abbr else "Texas"
    _log.var("state_full", state_full)
    await _ms_select(page, "Contact_Information_q_state", state_full)
    await asyncio.sleep(0.5)

    # City
    await _ms_fill(page, "Contact_Information_q_city", city or "Austin")

    # Zip
    await _ms_fill(page, "Contact_Information_q_zip", profile.get("postal_code", ""))

    # ── Step 4: Work Authorization ─────────────────────────────────────────────
    _log.step("Microsoft: Step 4 — Work Authorization")

    await _ms_select(
        page,
        "Work_Authorization___United_States_q_cust_workLegalAuth",
        "Yes",
    )
    await asyncio.sleep(0.3)

    await _ms_select(
        page,
        "Work_Authorization___United_States_q_cust_empEligibility",
        "Yes" if needs_sponsorship else "No",
    )

    # ── Step 5: Self-identification (voluntary) ────────────────────────────────
    _log.step("Microsoft: Step 5 — Self-identification")

    _RACE_MAP = {
        "asian":    "Asian",
        "black":    "African American or Black",
        "hispanic": "Hispanic or Latino",
        "white":    "White",
        "native":   "American Indian or Alaska Native",
        "pacific":  "Native Hawaiian or Other Pacific Islander",
        "multi":    "Multi-racial",
    }
    profile_race = profile.get("race", "").lower()
    race_value = next(
        (v for k, v in _RACE_MAP.items() if k in profile_race),
        "I do not wish to answer",
    )
    _log.var("race_value", race_value)
    await _ms_select(
        page,
        "Self_identification___US_Puerto_Rico_q_cust_ethnicityUS",
        race_value,
    )

    profile_gender = profile.get("gender", "").lower()
    gender_value = (
        "Male"   if "male" in profile_gender and "fe" not in profile_gender else
        "Female" if "female" in profile_gender else
        "I do not wish to answer"
    )
    _log.var("gender_value", gender_value)
    await _ms_select(
        page,
        "Self_identification___US_Puerto_Rico_q_cust_gender",
        gender_value,
    )

    # Armed forces status & veteran — not a veteran
    await _ms_select(
        page,
        "Self_identification___US_Puerto_Rico_q_cust_armedForcesUS",
        "No",
    )
    await _ms_select(
        page,
        "Self_identification___US_Puerto_Rico_q_cust_veteranStatusUS",
        "I am not a protected veteran",
    )

    # CC-305 disability form — fill name + date + select "no disability"
    await _ms_fill(
        page,
        "Self_identification___US_Puerto_Rico_instr4",
        full_name,
    )
    try:
        today_str = date.today().strftime("%m/%d/%Y")
        date_el = page.locator("#Self_identification___US_Puerto_Rico_instr4date").first
        if await date_el.count():
            await date_el.fill(today_str)
            _log.browser("fill", "#...instr4date", value=today_str, result="ok")
    except Exception as exc:
        _log.warn(f"Disability date fill: {exc}")

    # "I do not have a disability" radio
    try:
        disability_radios = page.locator('[id*="q_cust_disabilityUS"]')
        n = await disability_radios.count()
        _log.var("disability_radio_count", n)
        for i in range(n):
            opt = disability_radios.nth(i)
            opt_id = await opt.get_attribute("id") or ""
            lbl = page.locator(f'label[for="{opt_id}"]').first
            lbl_text = (await lbl.inner_text()).strip() if await lbl.count() else ""
            _log.var(f"disability_radio[{i}]", lbl_text[:80])
            if any(kw in lbl_text.lower() for kw in
                   ["do not have", "no disability", "i don't have", "don't wish"]):
                await opt.check()
                _log.browser("check", f"disability: {lbl_text[:50]}", result="ok")
                break
    except Exception as exc:
        _log.warn(f"Disability radio selection: {exc}")

    # ── Step 6: Candidate questions ────────────────────────────────────────────
    _log.step("Microsoft: Step 6 — Candidate questions")

    _CANDIDATE_QS = [
        ("Candidate_questions_q_cust_employedbyGovt",         "No"),
        ("Candidate_questions_q_cust_signedNonDisclosure",     "No"),
        ("Candidate_questions_q_cust_prevMSEmployee",          "No"),
        ("Candidate_questions_q_cust_prevMSSubsdiaryEmployee", "No"),
    ]
    for field_id, answer in _CANDIDATE_QS:
        _log.var(f"candidate_q[{field_id.split('_')[-1]}]", answer)
        await _ms_select(page, field_id, answer)
        await asyncio.sleep(0.2)

    # ── Step 7: Job specific questions ────────────────────────────────────────
    _log.step("Microsoft: Step 7 — Job specific questions")
    try:
        # Job qualification radio buttons have ID pattern:
        # Job_specific_questions_{displayJobId}_{n}-{value}
        # value 1.0 = Yes, 0.0 = No → we always answer Yes (qualified)
        yes_radios = page.locator('[id$="-1.0"]')
        yes_count = await yes_radios.count()
        _log.var("job_specific_yes_radios", yes_count)
        for i in range(yes_count):
            radio = yes_radios.nth(i)
            r_id = await radio.get_attribute("id") or ""
            if "Job_specific_questions" in r_id:
                await radio.check()
                _log.browser("check", f"job Q Yes radio: {r_id}", result="ok")
    except Exception as exc:
        _log.warn(f"Job specific questions error: {exc}")

    # ── Step 8: Acknowledgment ────────────────────────────────────────────────
    _log.step("Microsoft: Step 8 — Acknowledgment")

    _ACK_PREFIXES = [
        "Acknowledgment_q_cust_disclaimer-",
        "Acknowledgment_q_cust_disclaimer2-",
        "Acknowledgment_q_cust_Candidatecodeofconduct-",
    ]
    for prefix in _ACK_PREFIXES:
        try:
            ack_el = page.locator(f'[id^="{prefix}"]').first
            if await ack_el.count():
                await _ms_check(page, f'[id^="{prefix}"]', label=prefix)
            else:
                _log.warn(f"Ack checkbox not found: {prefix}")
        except Exception as exc:
            _log.warn(f"Ack checkbox {prefix}: {exc}")

    # ── Step 9: Submit ────────────────────────────────────────────────────────
    _log.step("Microsoft: Step 9 — Submit application")
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.5)

    for btn_text in ["Submit application", "Submit Application", "Submit"]:
        try:
            btn = page.get_by_role("button", name=re.compile(btn_text, re.I)).first
            if await btn.count() and await btn.is_visible():
                _log.browser("click", f"button[{btn_text!r}]", result="submitting...")
                await btn.click()
                await asyncio.sleep(SUBMIT_WAIT)
                body = (await page.inner_text("body")).lower()
                _log.var("post_submit_body_snippet", body[:200])
                if any(w in body for w in [
                    "thank you", "application submitted", "successfully submitted",
                    "received your application", "application received",
                ]):
                    _log.ok("Microsoft: application submitted — confirmation text found")
                    return "applied"
                _log.warn("Microsoft: submit clicked, no confirmation text — unconfirmed")
                return "submitted (unconfirmed)"
        except Exception as exc:
            _log.warn(f"Submit btn {btn_text!r}: {exc}")

    _log.err("Microsoft: submit button not found after all attempts")
    return "error: submit button not found"
