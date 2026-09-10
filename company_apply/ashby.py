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


