import asyncio
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, Frame, BrowserContext

try:
    from apply_logger import log as _log
except ImportError:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from apply_logger import log as _log

# ── Imports from sibling modules ────────────────────────────────────────────────
from .common import (
    answer_for,
    has_captcha,
    pause_for_captcha,
    SUBMIT_WAIT,
)

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
    _log.browser("fill_field", automation_id, value=value)
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
                _log.browser("fill_field", automation_id, result=f"ok via direct {tag}")
                return True
            # Container div — find the input inside it
            inp = loc.locator("input, textarea").first
            if await inp.count() > 0 and await inp.is_visible(timeout=1000):
                await inp.triple_click()
                await inp.fill(value)
                _log.browser("fill_field", automation_id, result="ok via container input/textarea")
                return True
        # Fallback: formField- container pattern (Adobe / most Workday portals)
        container = page.locator(f"[data-automation-id='formField-{automation_id}']").first
        if await container.count() > 0:
            inp = container.locator("input, textarea").first
            if await inp.count() > 0 and await inp.is_visible(timeout=1000):
                await inp.triple_click()
                await inp.fill(value)
                _log.browser("fill_field", automation_id, result="ok via formField- container")
                return True
    except Exception:
        pass
    _log.warn(f"_wd_fill_field: all strategies failed for automation_id={automation_id!r}")
    return False


async def _wd_dropdown(page: Page, automation_id: str, value: str) -> bool:
    """
    Handle Workday custom dropdowns (not <select>).
    Tries the direct automation-id, then the formField- container pattern.
    Uses force=True throughout — Workday elements often fail is_visible() checks.
    """
    _log.browser("dropdown", automation_id, value=value)
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
                    _log.browser("dropdown", automation_id, result=f"options found: {len(opts)} via {opt_sel!r} exact={exact}")
                    for opt in opts:
                        try:
                            text = (await opt.inner_text()).strip()
                            match = (text.lower() == value.lower()) if exact else (value.lower() in text.lower())
                            if match:
                                await opt.click(force=True)
                                await asyncio.sleep(0.3)
                                _log.browser("dropdown", automation_id, result=f"matched option={text!r}")
                                return True
                        except Exception:
                            continue
            await page.keyboard.press("Escape")
            _log.warn(f"_wd_dropdown: no match found for automation_id={automation_id!r} value={value!r}")
            return False
        except Exception:
            pass
    _log.warn(f"_wd_dropdown: all candidates failed for automation_id={automation_id!r}")
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
    url_before = page.url
    _log.browser("click", "Next button", result=f"url_before={url_before[:80]}")
    for aid in ("pageFooterNextButton", "bottom-navigation-next-btn", "next-btn", "saveAndContinueButton"):
        try:
            btn = page.locator(f"[data-automation-id='{aid}']").first
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                await btn.click()
                await _wd_wait_ready(page)
                url_after = page.url
                _log.browser("click", f"Next [{aid}]", result=f"url_after={url_after[:80]}")
                return True
        except Exception:
            pass
    _log.warn("_wd_next: no Next button found")
    return False


async def _wd_upload_resume(page: Page, resume: Optional[Path]) -> None:
    """Upload resume via Workday file input (data-automation-id='file-upload-input-ref')."""
    _log.var("resume_path", str(resume) if resume else None)
    if not resume:
        _log.null("resume", reason="resume path is None — not uploading")
        return
    if not resume.exists():
        _log.null("resume", reason=f"resume file does not exist: {resume}")
        return
    try:
        file_input = page.locator("[data-automation-id='file-upload-input-ref']").first
        if await file_input.count() > 0:
            await file_input.set_input_files(str(resume))
            await asyncio.sleep(1.5)
            _log.ok(f"Resume uploaded: {resume.name}")
        else:
            _log.warn("_wd_upload_resume: file-upload-input-ref not found on page")
    except Exception as e:
        _log.err("_wd_upload_resume failed", exc=e)


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


async def _wd_fill_date(page: Page, aid: str, month: str, year: str) -> bool:
    container = page.locator(f"[data-automation-id='formField-{aid}']").first
    if await container.count() == 0:
        return False
    month_input = container.locator("[data-automation-id='dateSectionMonth-input']").first
    year_input = container.locator("[data-automation-id='dateSectionYear-input']").first
    if await month_input.count() == 0 or await year_input.count() == 0:
        return False
    await month_input.fill(str(month).zfill(2))
    await year_input.fill(str(year))
    await year_input.press("Tab")
    return bool(await month_input.input_value() and await year_input.input_value())


async def _wd_my_information(page: Page, profile: dict, email: str, resume: Optional[Path]) -> None:
    """Fill Workday 'My Information' step. Works for both guest and authenticated flows."""
    _log.step("Workday: My Information")
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
    country = profile.get("country") or "United States"

    _log.var("first_name", first); not first and _log.null("first_name", reason="name not in profile")
    _log.var("last_name", last);   not last  and _log.null("last_name",  reason="name not in profile")
    _log.var("email", email);      not email and _log.null("email", reason="email param empty")
    _log.var("phone", profile.get("phone", "")); not profile.get("phone") and _log.null("phone", reason="phone not in profile")
    _log.var("address", profile.get("address_line1", "")); not profile.get("address_line1") and _log.null("address", reason="address_line1 not in profile")
    _log.var("city", city);       not city    and _log.null("city", reason="no city parsed from location")
    _log.var("state", loc.split(",")[1].strip() if "," in loc else ""); not ("," in loc) and _log.null("state", reason="no state parsed from location")
    _log.var("country", country)

    for country_aid in ("country", "addressCountry"):
        if await _wd_dropdown(page, country_aid, country):
            print(f"          [Info] Country: {country}", flush=True)
            await asyncio.sleep(0.5)
            break

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

    # State/region dropdown — expand common abbreviations to full names for Workday dropdowns
    _US_STATES = {
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
    if "," in loc:
        state = loc.split(",")[1].strip()
        state = _US_STATES.get(state.upper(), state)
        await _wd_dropdown(page, "countryRegion", state)

    await _wd_next(page)


async def _wd_my_experience(page: Page, profile: dict, resume: Optional[Path]) -> None:
    """Fill Workday 'My Experience' step — work history + education + resume upload."""
    _log.step("Workday: My Experience")
    # Upload resume first (early attempt), then retry at end after field fills
    await _wd_upload_resume(page, resume)
    await asyncio.sleep(0.5)


    title   = profile.get("current_title") or "Software Engineer"
    company = profile.get("current_company") or "Deloitte"
    school  = profile.get("school", "")
    degree  = profile.get("degree", "")
    field_of_study = profile.get("field_of_study", profile.get("major", ""))
    grad_yr = profile.get("graduation_year") or "2026"
    start_month = profile.get("work_start_month") or "08"
    start_yr = profile.get("work_start_year") or "2023"
    end_month = profile.get("work_end_month")   # None = not set; skip end date fill if missing
    end_yr    = profile.get("work_end_year")     # None = not set; skip end date fill if missing
    currently_raw = profile.get("currently_employed", False)
    currently_employed = currently_raw is True or str(currently_raw).lower() in ("true", "yes", "1")

    _log.var("title", title)
    _log.var("company", company)
    _log.var("school", school);          not school         and _log.null("school", reason="not in profile")
    _log.var("degree", degree);          not degree         and _log.null("degree", reason="not in profile")
    _log.var("field_of_study", field_of_study); not field_of_study and _log.null("field_of_study", reason="not in profile")
    _log.var("grad_yr", grad_yr)
    _log.var("start_month", start_month)
    _log.var("start_yr", start_yr)
    _log.var("end_month", end_month);    end_month is None  and _log.null("end_month", reason="not in profile")
    _log.var("end_yr", end_yr);          end_yr    is None  and _log.null("end_yr", reason="not in profile")
    _log.var("currently_employed", currently_employed)
    if not currently_employed and (not end_month or not end_yr):
        _log.null("end_date", reason="currently_employed=False but end_month/end_yr not set")

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
        if await chk.count() > 0:
            if currently_employed != await chk.is_checked():
                await chk.click(force=True)
                await asyncio.sleep(0.5)
    except Exception:
        pass

    # Workday date pickers split into separate month and year inputs.
    # automation-id = 'dateSectionMonth-input' and 'dateSectionYear-input'
    # DOM order with checkbox checked:   [WE-From-mo, WE-From-yr,  Edu-From-yr, Edu-To-yr]
    # DOM order with checkbox unchecked: [WE-From-mo, WE-To-mo,   WE-From-yr,  WE-To-yr, Edu-From-yr, Edu-To-yr]
    try:
        await _wd_fill_date(page, "startDate", start_month, start_yr)
        if not currently_employed and end_month and end_yr:
            await _wd_fill_date(page, "endDate", end_month, end_yr)
        first_year = page.locator("[data-automation-id='formField-firstYearAttended'] [data-automation-id='dateSectionYear-input']").first
        last_year = page.locator("[data-automation-id='formField-lastYearAttended'] [data-automation-id='dateSectionYear-input']").first
        try:
            grad_yr_int = int(re.sub(r"[^\d]", "", str(grad_yr))[:4])
            if await first_year.count() > 0:
                await first_year.fill(str(grad_yr_int - 1))
            if await last_year.count() > 0:
                await last_year.fill(str(grad_yr_int))
                await last_year.press("Tab")
        except (ValueError, TypeError):
            pass
        print(f"          [Exp] Work dates: {start_month}/{start_yr} to {end_month}/{end_yr}", flush=True)
    except Exception as _e:
        print(f"          [Exp] Date fill error: {_e}", flush=True)

    # --- Education fields ---
    # School name — typeahead searchable field; force-click to avoid is_visible issues
    try:
        for sch_aid in ("schoolName", "school", "university"):
            container = page.locator(f"[data-automation-id='formField-{sch_aid}']").first
            if await container.count() == 0:
                continue
            inp = container.locator("input:visible").first
            if await inp.count() == 0:
                continue
            await inp.click()
            await inp.fill(school)
            await asyncio.sleep(1.0)
            selected = False
            school_key = school.lower().replace("university", "").strip()
            for opt in await page.locator("[data-automation-id='promptOption']:visible, [role='option']:visible").all():
                try:
                    option_text = (await opt.inner_text()).strip().lower()
                    if school_key in option_text or option_text in school.lower():
                        await opt.click()
                        selected = True
                        break
                except Exception:
                    pass
            if not selected:
                await inp.press("ArrowDown")
                await inp.press("Enter")
            await asyncio.sleep(0.4)
            print(f"          [Exp] School: {school}", flush=True)
            break
    except Exception:
        pass

    # Degree — 4 strategies tried in order:
    _deg_filled = False
    _degree_words = degree.lower().split()
    _deg_kw = "master" if "master" in degree.lower() else (_degree_words[0] if _degree_words else "bachelor")

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
            btn = container.locator("button:visible").first
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
            inp = container.locator("input:visible").first
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
                    for opt in await page.locator(opt_sel).all():
                        try:
                            txt = (await opt.inner_text()).strip().lower()
                            if _deg_kw in txt and await opt.is_visible(timeout=0):
                                await opt.click(force=True)
                                _deg_filled = True
                                print(f"          [Exp] Degree S2 via '{opt_sel}': {txt}", flush=True)
                                break
                        except Exception:
                            pass
                    if _deg_filled:
                        break
                if not _deg_filled:
                    await page.keyboard.press("Escape")
        except Exception as _e:
            print(f"          [Exp] Degree S2 error: {_e}", flush=True)

    # Strategy 3: keyboard navigation — Down arrow to first option, Enter
    if not _deg_filled:
        try:
            container = page.locator("[data-automation-id='formField-degree']").first
            btn = container.locator("button:visible").first
            if await btn.count() == 0:
                btn = container.locator("input:visible").first
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

    for field_aid in ("fieldOfStudy", "field-of-study", "major", "discipline"):
        if await _wd_dropdown(page, field_aid, field_of_study):
            print(f"          [Exp] Field of study: {field_of_study}", flush=True)
            break
        if await _wd_fill_input(page, field_aid, field_of_study):
            print(f"          [Exp] Field of study: {field_of_study}", flush=True)
            break

    # LinkedIn / website
    await _wd_fill_field(page, "linkedinUrl",  profile.get("linkedin_url", ""))
    await _wd_fill_field(page, "portfolioUrl", profile.get("website_url", ""))

    # Re-upload resume after all field fills (React re-renders may reset upload state)
    await _wd_upload_resume(page, resume)
    await asyncio.sleep(2.0)  # wait for upload to process

    await _wd_next(page)


async def _wd_questions(page: Page, profile: dict, email: str) -> None:
    """Fill Workday 'Application Questions' step — dropdowns, radios, text inputs."""
    _log.step("Workday: Application Questions")
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
            _log.var(f"question_input", label, note=f"answer={val!r}")
            if val:
                await inp.fill(val)
                _log.browser("fill", label, result=f"filled={val!r}")
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
            _log.var("question_dropdown", label[:70], note=f"want={want}")
            await trigger_el.click(force=True)
            await asyncio.sleep(1.0)  # Give dropdown time to animate open
            picked = await _pick_option(want)
            await asyncio.sleep(0.3)
            _log.browser("dropdown_answer", label[:70], result=f"{want} ({'ok' if picked else 'fallback'})")
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
    _log.step("Workday: Voluntary Disclosures")
    _prof = profile or {}
    _gender_val = (_prof.get("gender") or "Male").strip().lower()       # "male" or "female"
    _race_val   = (_prof.get("race") or "Asian").strip().lower()        # "asian", "white", etc.
    _log.var("gender", _gender_val)
    _log.var("ethnicity", _race_val)
    _log.var("veteran", "not a veteran (hardcoded)")

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
            continue
        btn = ff.locator("button").first
        if await btn.count() == 0:
            continue
        cur_text = (await btn.inner_text()).strip().lower()
        # Already set correctly
        if any(w in cur_text for w in kws) and (skip_kw is None or skip_kw not in cur_text):
            print(f"          [VD] Strat1 {aid} already set: '{cur_text[:40]}'", flush=True)
            continue
        try:
            await btn.scroll_into_view_if_needed()
        except Exception:
            pass
        await btn.click(force=True)
        await asyncio.sleep(0.9)
        # Pick the right option from dropdown
        picked = False
        for opt_sel in ("[data-automation-id='promptOption']", "[role='option']", "li[tabindex]"):
            opts = await page.locator(opt_sel).all()
            first_real = None
            for opt in opts:
                try:
                    t = (await opt.inner_text()).strip().lower()
                    if not t:
                        continue
                    if skip_kw and skip_kw in t:
                        continue
                    if any(w in t for w in kws):
                        await opt.click(force=True)
                        picked = True
                        break
                    if first_real is None:
                        first_real = opt
                except Exception:
                    pass
            if picked:
                break
            if first_real and not picked:
                await first_real.click(force=True)
                picked = True
                break
        if not picked:
            await page.keyboard.press("Escape")
        print(f"          [VD] Strat1 {aid} → {'ok' if picked else 'escaped'}", flush=True)
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
    _log.step("Workday: Submit")
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
        _log.state(page_url=url_before, visible_buttons=btn_labels[:8])
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
            _log.browser("click", f"submit button [{aid}]", result=f"url_before={url_before[:60]}")
            await btn.click()
            await asyncio.sleep(SUBMIT_WAIT)

            # Primary check: did the URL change? (Workday redirects on successful submit)
            url_after = page.url
            if url_after != url_before:
                print(f"          [Submit] URL changed → {url_after[:80]}", flush=True)
                _log.nav(url_after, status="redirected", title="post-submit page")
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in _confirm_words):
                    _log.ok("Workday submit: application confirmed via body text")
                    return "applied"
                _log.warn(f"Workday submit: URL changed but no confirm text — btn={btn_text!r}")
                return f"submitted (unconfirmed — url changed after '{btn_text}')"

            # Secondary check: confirmation words in body
            body = (await page.inner_text("body")).lower()
            if any(w in body for w in _confirm_words):
                _log.ok("Workday submit: confirmed via body text (URL unchanged)")
                return "applied"

            # URL didn't change and no confirmation — the click didn't submit
            print(f"          [Submit] Clicked '{btn_text}' but URL unchanged — not submitted", flush=True)
            _log.warn(f"Workday submit: clicked {btn_text!r} but URL unchanged — not submitted")
        except Exception:
            pass

    # Broad fallback
    try:
        for sel in ("button[type='submit']", "[aria-label*='submit' i]"):
            btn = page.locator(sel).last
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                url_before = page.url
                _log.browser("click", f"submit fallback [{sel}]", result=f"url_before={url_before[:60]}")
                await btn.click()
                await asyncio.sleep(SUBMIT_WAIT)
                if page.url != url_before:
                    _log.ok("Workday submit: URL changed after fallback click")
                    return "submitted (unconfirmed — url changed)"
    except Exception:
        pass

    _log.err("Workday submit: no button triggered navigation — submit failed")
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
    _log.step(f"Workday: Fill Application — {company}")
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
        """Find the active Workday step from its visible page heading."""
        _known = list(step_fns.keys()) + ["Review"]
        try:
            for heading in await apply_page.locator("h1:visible, h2:visible, h3:visible").all():
                text = (await heading.inner_text()).strip()
                for known in _known:
                    if text == known or text.startswith(f"{known} "):
                        return known
            return ""
        except Exception:
            return ""

    # Sequential dispatch — run each step in order, detect page state after each
    step_order = list(step_fns.keys())
    for step_name in step_order:
        fn = step_fns[step_name]
        try:
            current = await _detect_step()
            if current == "Review":
                _log.info(f"_fill_workday: reached Review — skipping remaining steps")
                break
            if current and current != step_name:
                _log.info(f"_fill_workday: detected step={current!r}, expected={step_name!r} — skipping")
                continue
            _log.info(f"_fill_workday: executing step={step_name!r}")
            print(f"          [Workday] Step: {step_name}...", flush=True)
            await fn()
            await asyncio.sleep(0.8)
            try:
                pg = (await apply_page.inner_text("body"))[:200].replace("\n", " ").strip()
                print(f"          [Workday]   → after step: {pg[:100]}", flush=True)
            except Exception:
                pass
            # Verify page advanced — if still showing same step name, check for hard errors
            current = await _detect_step()
            if current == step_name:
                print(f"          [Workday] ✗ '{step_name}' has validation errors — stopping", flush=True)
                _log.err(f"_fill_workday: step {step_name!r} validation failed — page did not advance")
                return f"error: Workday {step_name} validation failed"
            _log.ok(f"_fill_workday: step {step_name!r} completed → now on {current!r}")
        except Exception as e:
            print(f"          [Workday] {step_name} step warning: {e}", flush=True)
            _log.err(f"_fill_workday: step {step_name!r} raised exception", exc=e)

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
            print(f"          [Workday] CC-305 form detected — filling Name + Date", flush=True)
            # Name field
            name_val = profile.get("name", "")
            if name_val:
                name_inp = apply_page.locator("[data-automation-id='formField-name'] input").first
                if await name_inp.count() == 0:
                    for _t_inp in await apply_page.locator("input[type='text']").all():
                        try:
                            if not await _t_inp.input_value():
                                name_inp = _t_inp
                                break
                        except Exception:
                            pass
                if await name_inp.count() > 0 and not await name_inp.input_value():
                    await name_inp.fill(name_val)
                    print(f"          [Workday] CC-305 Name → '{name_val}'", flush=True)
            # Date field
            from datetime import date as _dt_cc
            _today = _dt_cc.today()
            _date_str = _today.strftime('%m/%d/%Y')
            _date_ff = apply_page.locator("[data-automation-id='formField-dateSignedOn']").first
            _date_done = False
            if await _date_ff.count() > 0:
                for _seg, _v in (("dateSectionMonth-input", str(_today.month).zfill(2)),
                                  ("dateSectionDay-input",   str(_today.day).zfill(2)),
                                  ("dateSectionYear-input",  str(_today.year))):
                    _seg_inp = _date_ff.locator(f"[data-automation-id='{_seg}']").first
                    if await _seg_inp.count() > 0:
                        await _seg_inp.fill(_v)
                        _date_done = True
            if not _date_done:
                for _di in await apply_page.locator(
                    "input[placeholder*='MM'], input[placeholder*='date' i], input[type='date']"
                ).all():
                    try:
                        await _di.fill(_date_str)
                        _date_done = True
                        print(f"          [Workday] CC-305 Date → {_date_str}", flush=True)
                        break
                    except Exception:
                        pass
            # Advance past CC-305 to Review
            await _wd_next(apply_page)
            await asyncio.sleep(1.5)
    except Exception as _cc_e:
        print(f"          [Workday] CC-305 handler: {_cc_e}", flush=True)

    # Final submit
    print(f"          [Workday] Attempting submit...", flush=True)
    return await _wd_submit(apply_page)
