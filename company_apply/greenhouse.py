import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, Frame, BrowserContext

try:
    from apply_logger import log as _log
except ImportError:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).parent.parent))
    from apply_logger import log as _log

# ── Imports from sibling modules ────────────────────────────────────────────────
from .common import (
    answer_for,
    has_captcha,
    pause_for_captcha,
    load_custom_answers,
    _apply_collected_answers,
    _collect_form_answers,
    _ollama_answer_batch,
    _ollama_generate_answer,
    _ollama_pick_option,
    SUBMIT_WAIT,
    _code_queues,
    _watcher_stops,
)

# ── Greenhouse form filler ──────────────────────────────────────────────────────

async def _fill_text_inputs(page: Page, profile: dict, email: str):
    """Fill all visible unfilled text/email/tel/textarea inputs by their label."""
    _log.fn("_fill_text_inputs", email=email)
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
                _log.var(f"text_input[{label[:50]}]", val[:80], note="filling field")
                await inp.fill(val)
            else:
                _log.skip(f"text_input[{label[:50]}] — no answer found in profile")
        except Exception as e:
            _log.warn(f"_fill_text_inputs: error on field", exc=e)
            pass


async def _fill_selects(page: Page, profile: dict, email: str):
    """Fill all visible unfilled <select> elements by their label."""
    _log.fn("_fill_selects", email=email)
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
                _log.skip(f"select[{label[:50]}] — no options available")
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
            _log.var(f"select[{label[:50]}]", chosen, note=f"profile value={val!r}")
            await sel_el.select_option(value=chosen)
        except Exception as e:
            _log.warn(f"_fill_selects: error on select field", exc=e)
            pass


async def _upload_resume(page: Page, resume: Optional[Path], label: str = ""):
    """Find the resume file input and upload. Works for hidden inputs too."""
    _log.fn("_upload_resume", resume=str(resume) if resume else None, label=label)
    if not resume or not resume.exists():
        _log.null("resume", reason="resume path is None or file does not exist")
        return False
    _log.var("resume_file", str(resume), note="uploading resume")
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
                _log.ok(f"Resume uploaded via selector {sel!r}: {resume.name}")
                await asyncio.sleep(2)
                _log.ret("_upload_resume", True)
                return True
        except Exception as e:
            _log.warn(f"Resume upload attempt failed for selector {sel!r}", exc=e)
            pass
    _log.warn("Resume upload: no matching file input found", exc=None)
    _log.ret("_upload_resume", False)
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
    _log.fn("_gh_submit", email=email)
    captcha_page = outer_page if outer_page is not None else target
    if await has_captcha(captcha_page):
        _log.warn("CAPTCHA detected — pausing for manual solve", exc=None)
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
            _log.browser("find", f"button[role=button name={btn_text!r}]", result="found submit button")
            pre_url = ""
            try:
                pre_url = target.url
            except Exception:
                pass
            _log.browser("click", btn_text, result="clicking submit button")
            await btn.click()
            await asyncio.sleep(SUBMIT_WAIT)
            # Handle email verification code screen if it appears
            _verify_result = await _handle_verification_code(target, captcha_page, email=email)
            if _verify_result == "no_code":
                _log.err("Email verification required but code not retrieved", exc=None)
                return "error: email verification required — code not retrieved"
            # URL change in iframe = navigation to confirmation page
            try:
                post_url = target.url
                if pre_url and post_url != pre_url:
                    _log.nav(post_url, status="url_changed", title="post-submit URL change → applied")
                    _log.ok("URL changed after submit — applied")
                    _log.ret("_gh_submit", "applied")
                    return "applied"
            except Exception:
                pass
            result = await _check_confirmed(target, captcha_page)
            if result:
                _log.var("submit_confirmed_result", result, note="_check_confirmed returned result")
                if result == "applied":
                    _log.ok(f"Submission confirmed: {result}")
                else:
                    _log.warn(f"Submission result: {result}", exc=None)
                _log.ret("_gh_submit", result)
                return result
            _log.warn("Submit clicked but confirmation not detected — unconfirmed", exc=None)
            _log.ret("_gh_submit", "submitted (unconfirmed)")
            return "submitted (unconfirmed)"
        except Exception as e:
            _log.warn(f"Submit attempt for button {btn_text!r} failed", exc=e)
            pass

    # Fallback: input[type=submit] (classic board)
    try:
        sub = target.locator("input[type='submit']").first
        if await sub.is_visible(timeout=0):
            _log.browser("find", "input[type='submit']", result="found classic submit input")
            pre_url = ""
            try:
                pre_url = target.url
            except Exception:
                pass
            _log.browser("click", "input[type='submit']", result="clicking classic submit input")
            await sub.click()
            await asyncio.sleep(SUBMIT_WAIT)
            # Handle email verification code screen if it appears
            _verify_result = await _handle_verification_code(target, captcha_page, email=email)
            if _verify_result == "no_code":
                _log.err("Email verification required but code not retrieved (classic board)", exc=None)
                return "error: email verification required — code not retrieved"
            try:
                if pre_url and target.url != pre_url:
                    _log.ok("URL changed after classic submit — applied")
                    _log.ret("_gh_submit", "applied")
                    return "applied"
            except Exception:
                pass
            result = await _check_confirmed(target, captcha_page)
            if result:
                _log.var("classic_submit_result", result)
                _log.ret("_gh_submit", result)
                return result
            _log.ret("_gh_submit", "submitted (unconfirmed)")
            return "submitted (unconfirmed)"
    except Exception as e:
        _log.warn("Classic input[type=submit] fallback failed", exc=e)
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
                _log.browser("find", sel, result="found submit via CSS fallback")
                pre_url = ""
                try:
                    pre_url = target.url
                except Exception:
                    pass
                _log.browser("click", sel, result="clicking CSS fallback submit")
                await sub.click()
                await asyncio.sleep(SUBMIT_WAIT)
                _verify_result = await _handle_verification_code(target, captcha_page, email=email)
                if _verify_result == "no_code":
                    _log.err("Email verification required but code not retrieved (CSS fallback)", exc=None)
                    return "error: email verification required — code not retrieved"
                try:
                    if pre_url and target.url != pre_url:
                        _log.ok(f"URL changed after CSS fallback submit via {sel!r}")
                        _log.ret("_gh_submit", "applied")
                        return "applied"
                except Exception:
                    pass
                result = await _check_confirmed(target, captcha_page)
                if result:
                    _log.var("css_fallback_result", result)
                    _log.ret("_gh_submit", result)
                    return result
                _log.ret("_gh_submit", "submitted (unconfirmed)")
                return "submitted (unconfirmed)"
        except Exception as e:
            _log.warn(f"CSS fallback submit failed for selector {sel!r}", exc=e)
            pass

    _log.err("Submit button not found after all strategies", exc=None)
    _log.ret("_gh_submit", "error: submit button not found")
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
    _log.step(f"Greenhouse: Fill Application — {company} / {job_title}")
    _log.fn("_fill_greenhouse", company=company, job_title=job_title, email=email, resume=str(resume) if resume else None)
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
    _log.var("has_collapsed_iframe", has_collapsed_iframe, note="grnhse_iframe present but collapsed")
    if has_collapsed_iframe or not await _gh_form_present(page):
        _log.browser("scroll+click", "Apply button", result="searching for apply button")
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
        _log.var("target_url", target_url.split("?")[0], note="form in embedded iframe")
        _log.nav(target_url.split("?")[0], status="iframe", title="Greenhouse embedded iframe")
    else:
        target = page
        target_url = page.url
        _log.var("target_url", target_url, note="form on main page (no iframe)")

    # ── Step 3: detect board type ─────────────────────────────────────────────
    is_new_board = (
        "job-boards.greenhouse.io" in target_url
        or await target.locator(
            "[data-testid='apply-page'], [data-testid='application-form'], "
            "[data-testid='job-application']"
        ).count() > 0
    )
    _log.var("is_new_board", is_new_board, note="True=React board (2024+), False=classic HTML board")

    # ── Step 4: upload resume first (before interactive questions) ───────────
    if resume:
        _log.var("resume_path", str(resume), note="attempting upload")
        _log.browser("upload", str(resume), result="calling _upload_resume")
    else:
        _log.null("resume", reason="no resume path provided")
    _resume_ok = await _upload_resume(target, resume, "greenhouse")
    if _resume_ok:
        _log.ok(f"Resume uploaded: {resume.name if resume else 'unknown'}")
    else:
        _log.warn("Resume upload failed or skipped", exc=None)

    # ── Step 5: collect answers interactively, fill, submit ───────────────────
    print(f"      → Reading form fields...")
    _log.info("Collecting form answers via _collect_form_answers")
    answers = await _collect_form_answers(target, profile, email, job_title, company, resume)
    _log.var("answers_count", len(answers) if answers else 0, note="number of form field answers collected")
    if answers:
        for _q, _a in (answers.items() if isinstance(answers, dict) else enumerate(answers)):
            _log.var(f"answer[{str(_q)[:40]}]", str(_a)[:120])
    await asyncio.sleep(1)
    _log.info("Applying collected answers via _apply_collected_answers")
    await _apply_collected_answers(target, answers)
    await asyncio.sleep(3)          # let React settle all field changes

    # Location City needs special treatment (autocomplete field)
    city = profile.get("location", "").split(",")[0].strip()
    if city:
        _log.var("city", city, note="filling location/city autocomplete field")
    else:
        _log.null("city", reason="location not set in profile")
    await _fill_location_city(target, city, page)
    await asyncio.sleep(1)

    _log.info("Ensuring country field is set to United States")
    await _ensure_country_filled(target)
    await asyncio.sleep(2)          # let all changes settle before submit

    # ── Step 6: fill any remaining React Select dropdowns still showing "Select..." ─
    _log.info("Filling remaining React Select dropdowns via _fill_all_react_selects")
    await _fill_all_react_selects(target, profile, job_title, company, email)
    await asyncio.sleep(1)

    # ── Step 7: submit — multi-pass rescue on validation errors ──────────────────
    _log.browser("click", "Submit Application button", result="attempting _gh_submit pass 1")
    status = await _gh_submit(target, outer_page=page, email=email)
    _log.var("status_pass1", status, note="result after first submit attempt")
    if status.startswith("error: form validation"):
        print(f"      → Validation error — running React Select rescue pass...")
        _log.warn(f"Validation error on pass 1: {status} — running React Select rescue pass")
        await _fill_all_react_selects(target, profile, job_title, company, email)
        await asyncio.sleep(1)
        # Also rescue empty required text/URL inputs (e.g. LinkedIn URL)
        _log.info("Rescuing empty required text/URL inputs via _rescue_empty_required_inputs")
        await _rescue_empty_required_inputs(target, profile, email, job_title, company, resume)
        await asyncio.sleep(1)
        _log.browser("click", "Submit Application button", result="attempting _gh_submit pass 2")
        status = await _gh_submit(target, outer_page=page, email=email)
        _log.var("status_pass2", status, note="result after second submit attempt")

    # If still failing and the error mentions a specific field, attempt targeted fill
    if status.startswith("error: form validation"):
        err_field = status[len("error: form validation — "):].lower()
        _log.warn(f"Validation error on pass 2 — field hint: {err_field!r}")
        if "linkedin" in err_field:
            _log.info("Attempting LinkedIn field rescue via _rescue_linkedin_field")
            await _rescue_linkedin_field(target, profile)
            await asyncio.sleep(1)
            _log.browser("click", "Submit Application button", result="attempting _gh_submit pass 3 (linkedin rescue)")
            status = await _gh_submit(target, outer_page=page, email=email)
            _log.var("status_pass3", status, note="result after linkedin rescue submit")

    # ── Final fallback: DOM-inspect every validation error and ask Ollama ──────
    if status.startswith("error: form validation"):
        print(f"      → [dom-fallback] Running DOM inspection pass for unanswered required fields...")
        _log.warn(f"Still validation errors — running DOM fallback inspection pass")
        filled = await _dom_fallback_fill_required_fields(target, profile, job_title, company, email, resume)
        _log.var("dom_fallback_filled_count", filled, note="number of fields filled by dom fallback")
        if filled:
            await asyncio.sleep(1)
            _log.browser("click", "Submit Application button", result="attempting _gh_submit pass 4 (dom fallback)")
            status = await _gh_submit(target, outer_page=page, email=email)
            _log.var("status_pass4", status, note="result after dom fallback submit")

    if status == "applied":
        _log.ok(f"Application submitted successfully: {company} / {job_title}")
    elif status.startswith("error"):
        _log.err(f"Application failed: {status}", exc=None)
    else:
        _log.warn(f"Application status uncertain: {status}", exc=None)
    _log.ret("_fill_greenhouse", status)
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


