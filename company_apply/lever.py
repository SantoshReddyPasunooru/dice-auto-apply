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


