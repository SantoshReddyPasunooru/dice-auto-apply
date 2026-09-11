import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, Frame, BrowserContext

# ── Imports from sibling modules ────────────────────────────────────────────────
from .common import (
    answer_for,
    has_captcha,
    pause_for_captcha,
    SUBMIT_WAIT,
)
from .greenhouse import _upload_resume

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

# ── Ashby form filler ───────────────────────────────────────────────────────────

async def _fill_ashby(page: Page, profile: dict, email: str,
                      resume: Optional[Path]) -> str:
    """Ashby apply pages open with a modal/inline form. Click Apply if needed."""
    _log.fn("_fill_ashby", email=email, resume=str(resume) if resume else None,
            page_url=page.url)
    _log.step("Ashby: wait for page load")
    await page.wait_for_load_state("networkidle", timeout=25000)
    await asyncio.sleep(2)

    # Ashby job pages have an "Apply" button that opens the form
    _log.step("Ashby: click Apply button to open form")
    for btn_text in ["Apply for this job", "Apply Now", "Apply"]:
        try:
            btn = page.get_by_role("button", name=re.compile(btn_text, re.I)).first
            if await btn.is_visible(timeout=0):
                _log.browser("click", f"button[{btn_text}]", result="opening apply form")
                await btn.click()
                await asyncio.sleep(2)
                break
        except Exception as e:
            _log.warn(f"Ashby open-form button {btn_text!r} not found: {e}")

    # ── Resume upload ──
    _log.step("Ashby: upload resume")
    await _upload_resume(page, resume, "ashby")

    # ── Fill all visible labeled inputs ──
    _log.step("Ashby: fill text inputs")
    all_inputs = await page.locator(
        "input[type='text'], input[type='email'], input[type='tel'], textarea"
    ).all()
    _log.var("ashby_inputs_found", len(all_inputs))
    for inp in all_inputs:
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
                _log.browser("fill", f"input[label={label[:50]!r}]", value=val[:60], result="filling")
                await inp.fill(val)
                _log.browser("fill", f"input[label={label[:50]!r}]", value=val[:60], result="ok")
            else:
                _log.skip(f"Ashby input — no answer for {label[:50]!r}")
        except Exception as e:
            _log.warn(f"Ashby input fill error", exc=e)

    # ── Selects ──
    _log.step("Ashby: fill selects")
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
                _log.skip(f"Ashby select — no options  label={label[:50]!r}")
                continue
            val_lower = val.lower() if val else ""
            chosen    = next(
                (o["value"] for o in opts
                 if val_lower and (val_lower in o["text"] or o["text"] in val_lower)),
                opts[0]["value"],
            )
            _log.var(f"ashby_select[{label[:40]}]", chosen)
            await sel_el.select_option(value=chosen)
        except Exception as e:
            _log.warn(f"Ashby select fill error", exc=e)

    # ── CAPTCHA ──
    if await has_captcha(page):
        _log.warn("CAPTCHA detected on Ashby form — pausing for manual solve")
        await pause_for_captcha(page)

    # ── Submit (never "Apply" — that opens the form, not submits it) ──
    _log.step("Ashby: submit")
    for btn_text in ["Submit Application", "Submit My Application",
                     "Submit", "Send Application"]:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{btn_text}$", re.I)).first
            if await btn.is_visible(timeout=0):
                _log.browser("click", f"button[{btn_text}]", result="clicking submit")
                await btn.click()
                await asyncio.sleep(SUBMIT_WAIT)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in ["thank you", "application received",
                                            "submitted", "we'll review",
                                            "we will review"]):
                    _log.ok("Ashby: application submitted — confirmation found in body")
                    return "applied"
                _log.warn("Ashby: submit clicked but no confirmation text found")
                return "submitted (unconfirmed)"
        except Exception as e:
            _log.warn(f"Ashby submit btn {btn_text!r} error: {e}")

    # ── Broad CSS fallback ──────────────────────────────────────────────────────
    _log.warn("Ashby: named submit not found — trying CSS fallbacks")
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)
    except Exception:
        pass
    for sel in ("button[type='submit']", "[data-testid*='submit' i]", "[data-qa*='submit' i]"):
        try:
            sub = page.locator(sel).last
            if await sub.count() > 0 and await sub.is_visible(timeout=0):
                _log.browser("click", sel, result="CSS fallback submit")
                await sub.click()
                await asyncio.sleep(SUBMIT_WAIT)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in ["thank you", "application received",
                                            "submitted", "we'll review",
                                            "we will review"]):
                    _log.ok(f"Ashby: applied via CSS fallback {sel!r}")
                    return "applied"
                return "submitted (unconfirmed)"
        except Exception as e:
            _log.warn(f"Ashby CSS fallback {sel!r} error: {e}")

    _log.err("Ashby: submit button not found after all attempts")
    return "error: submit button not found"


