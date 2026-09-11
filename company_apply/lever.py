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

# ── Lever form filler ───────────────────────────────────────────────────────────

async def _fill_lever(page: Page, profile: dict, email: str,
                      resume: Optional[Path]) -> str:
    _log.fn("_fill_lever", email=email, resume=str(resume) if resume else None,
            page_url=page.url)
    _log.step("Lever: wait for page load")
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    await asyncio.sleep(1.5)

    # ── Known Lever field names (very stable across all companies) ──
    _log.step("Lever: fill standard fields")
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
            _log.skip(f"Lever field {name_attr!r} — empty, skipping")
            continue
        _log.var(f"lever_field[{name_attr}]", val[:60])
        for sel in [f"input[name='{name_attr}']",
                    f"textarea[name='{name_attr}']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=0):
                    current = await el.input_value()
                    if not current:
                        await el.fill(val)
                        _log.browser("fill", sel, value=val[:60], result="ok")
                    else:
                        _log.skip(f"Lever field {name_attr!r} already filled")
                    break
            except Exception as e:
                _log.warn(f"Lever field {name_attr!r} fill error: {e}")

    # ── Resume upload ──
    _log.step("Lever: upload resume")
    await _upload_resume(page, resume, "lever")

    # ── Custom questions (.application-question blocks) ──
    _log.step("Lever: fill custom questions")
    q_cards = await page.locator(
        ".application-question, [class*='questionWrapper'], [class*='question-block']"
    ).all()
    _log.var("lever_question_cards", len(q_cards))
    for card in q_cards:
        try:
            if not await card.is_visible(timeout=0):
                continue
            label_el   = card.locator("label, h4, p, span[class*='label']").first
            label_text = ""
            if await label_el.count() > 0:
                label_text = (await label_el.inner_text()).strip()
            _log.var("lever_question_label", label_text[:80])

            # textarea
            ta = card.locator("textarea").first
            if await ta.count() > 0 and await ta.is_visible(timeout=0):
                if not await ta.input_value():
                    val = answer_for(label_text, profile, email)
                    if val:
                        await ta.fill(val)
                        _log.browser("fill", "textarea", value=val[:60], result="ok")
                    else:
                        _log.skip(f"Lever textarea — no answer for {label_text[:50]!r}")
                continue

            # text / tel input
            inp = card.locator("input[type='text'], input[type='tel']").first
            if await inp.count() > 0 and await inp.is_visible(timeout=0):
                if not await inp.input_value():
                    val = answer_for(label_text, profile, email)
                    if val:
                        await inp.fill(val)
                        _log.browser("fill", "text/tel input", value=val[:60], result="ok")
                    else:
                        _log.skip(f"Lever text input — no answer for {label_text[:50]!r}")
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
                        _log.var(f"lever_select[{label_text[:40]}]", chosen)
                        await sel_el.select_option(value=chosen)
                    else:
                        _log.skip(f"Lever select — no options for {label_text[:50]!r}")
                continue
        except Exception as e:
            _log.warn(f"Lever question card error", exc=e)

    # ── CAPTCHA ──
    if await has_captcha(page):
        _log.warn("CAPTCHA detected on Lever form — pausing for manual solve")
        await pause_for_captcha(page)

    # ── Submit (never use "Apply" — that's a navigation button on Lever too) ──
    _log.step("Lever: submit")
    for btn_text in ["Submit Application", "Submit My Application", "Submit"]:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{btn_text}$", re.I)).first
            if await btn.is_visible(timeout=0):
                _log.browser("click", f"button[{btn_text}]", result="clicking submit")
                await btn.click()
                await asyncio.sleep(SUBMIT_WAIT)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in ["thank you", "application received",
                                            "submitted", "application sent",
                                            "we'll be in touch"]):
                    _log.ok("Lever: application submitted — confirmation found in body")
                    return "applied"
                _log.warn("Lever: submit clicked but no confirmation text found")
                return "submitted (unconfirmed)"
        except Exception as e:
            _log.warn(f"Lever submit btn {btn_text!r} error: {e}")

    try:
        sub = page.locator("input[type='submit']").first
        if await sub.is_visible(timeout=0):
            _log.browser("click", "input[type=submit]", result="fallback submit")
            await sub.click()
            await asyncio.sleep(SUBMIT_WAIT)
            return "submitted (unconfirmed)"
    except Exception as e:
        _log.warn(f"Lever input[submit] fallback error: {e}")

    # ── Broad CSS fallback ──────────────────────────────────────────────────────
    _log.warn("Lever: named submit not found — trying CSS fallbacks")
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
                                            "submitted", "application sent",
                                            "we'll be in touch"]):
                    _log.ok(f"Lever: applied via CSS fallback {sel!r}")
                    return "applied"
                return "submitted (unconfirmed)"
        except Exception as e:
            _log.warn(f"Lever CSS fallback {sel!r} error: {e}")

    _log.err("Lever: submit button not found after all attempts")
    return "error: submit button not found"


