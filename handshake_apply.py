#!/usr/bin/env python3
"""Handshake job search and application runner."""

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, Page
from company_apply.common import answer_for

from job_eligibility import early_career_rejection_reason

HERE = Path(__file__).parent
PROFILES_JSON = HERE / "profiles.json"
RESUMES_JSON = HERE / "resumes.json"
APPLIED_CSV = HERE / "handshake_applied_jobs.csv"


def safe_email(email: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", email.lower())


def session_dir(email: str) -> Path:
    path = Path.home() / f".handshake-playwright-profile-{safe_email(email)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_for(email: str) -> dict:
    data = json.loads(PROFILES_JSON.read_text()) if PROFILES_JSON.exists() else {}
    profile = dict(data.get(email, {}))
    profile["email"] = email
    return profile


_resume_text_cache: dict[Path, str] = {}


def resume_text(path: Path) -> str:
    if path in _resume_text_cache:
        return _resume_text_cache[path]
    text = ""
    try:
        if path.suffix.lower() == ".docx":
            from docx import Document
            text = " ".join(p.text for p in Document(str(path)).paragraphs)
        elif path.suffix.lower() == ".pdf":
            import pdfplumber
            with pdfplumber.open(str(path)) as pdf:
                text = " ".join(page.extract_text() or "" for page in pdf.pages[:6])
    except Exception:
        pass
    _resume_text_cache[path] = text.lower()
    return _resume_text_cache[path]


def resume_for(email: str, job_title: str = "") -> Path | None:
    data = json.loads(RESUMES_JSON.read_text()) if RESUMES_JSON.exists() else {}
    entry = data.get(email, {})
    default = Path(entry.get("default_resume", "")).expanduser()
    folder = Path(entry.get("resume_folder", "")).expanduser()
    files = sorted(list(folder.glob("*.pdf")) + list(folder.glob("*.docx"))) if folder.is_dir() else []
    if not files:
        return default if default.exists() else None
    if not job_title:
        return default if default.exists() else files[0]
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", job_title.lower()).split() if len(w) > 2]
    best = None
    best_score = -1
    for path in files:
        content = resume_text(path)
        haystack = f"{path.name.lower()} {content}"
        score = sum(2 for word in words if word in content) + sum(1 for word in words if word in path.name.lower())
        if score > best_score:
            best_score, best = score, path
    return best or (default if default.exists() else files[0])


def title_matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    value = title.lower()
    if "on-campus" in value or "on campus" in value:
        return False
    return any(keyword.lower() in value for keyword in keywords)


async def wait_for_login(page: Page, timeout: int = 600) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if page.is_closed():
            raise RuntimeError("Handshake login browser was closed")
        url = page.url.lower()
        body = ""
        try:
            body = (await page.locator("body").inner_text(timeout=1000)).lower()
        except Exception:
            pass
        if "login" not in url and ("job search" in body or "search jobs" in body or "/postings" in url):
            return
        await asyncio.sleep(2)
    raise RuntimeError("Timed out waiting for Handshake login")


async def login(email: str) -> None:
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(session_dir(email)), headless=False, locale="en-US"
        )
        page = await context.new_page()
        await page.goto("https://app.joinhandshake.com/login", wait_until="domcontentloaded", timeout=0)
        print("Handshake login opened. Sign in in the browser; waiting up to 10 minutes...", flush=True)
        await wait_for_login(page)
        (session_dir(email) / ".handshake_session_ready").write_text("ready")
        print("Handshake session saved.", flush=True)
        await context.close()


async def collect_jobs(page: Page, keywords: list[str]) -> list[dict]:
    url = "https://app.joinhandshake.com/job-search"
    await page.goto(url, wait_until="domcontentloaded", timeout=0)
    for _ in range(20):
        await page.wait_for_timeout(1000)
        try:
            body = (await page.locator("body").inner_text()).lower()
            if "finding jobs" not in body and ("results" in body or "/jobs/" in page.url):
                break
        except Exception:
            pass

    try:
        collection = page.get_by_role("button", name=re.compile(r"wright state collections|collections", re.I)).first
        if await collection.count() and await collection.is_visible(timeout=1000):
            await collection.click()
            await page.wait_for_timeout(500)
            clear = page.get_by_text("Clear", exact=True).last
            if await clear.count() and await clear.is_visible(timeout=1000):
                await clear.click(force=True)
                await page.wait_for_timeout(1500)
                print("[Handshake] Cleared saved collection filter", flush=True)
    except Exception as exc:
        print(f"[Handshake] Could not clear saved collection filter: {exc}", flush=True)

    jobs: dict[str, dict] = {}
    for link in await page.locator("a[href]").all():
        try:
            href = await link.get_attribute("href")
            text = " ".join((await link.inner_text()).split())
            if not text:
                for parent_level in range(1, 5):
                    try:
                        parent_xpath = "../" * (parent_level - 1) + ".."
                        parent = link.locator("xpath=" + parent_xpath)
                        candidate = " ".join((await parent.inner_text()).split())
                        if candidate:
                            text = candidate[:240]
                            break
                    except Exception:
                        pass
        except Exception:
            continue
        if not href or not re.search(r"/(postings|jobs|job-search)/\d+", href):
            continue
        if not text:
            text = "Handshake job"
        absolute = href if href.startswith("http") else "https://app.joinhandshake.com" + href
        absolute = re.sub(r"/job-search/(\d+)", r"/jobs/\1", absolute)
        if absolute not in jobs and title_matches(text, keywords):
            jobs[absolute] = {"title": text, "url": absolute}
    if not jobs:
        try:
            await page.screenshot(path="/tmp/handshake_jobs_debug.png", full_page=True)
            body = " ".join((await page.locator("body").inner_text()).split())
            hrefs = await page.locator("a[href]").evaluate_all("els => els.map(e => e.href).filter(Boolean).slice(0, 30)")
            print(f"[Handshake] No job cards. Page: {body[:500]}", flush=True)
            print(f"[Handshake] Sample links: {hrefs}", flush=True)
            print(f"[Handshake] Buttons: {(await page.locator('button').all_inner_texts())[:30]}", flush=True)
        except Exception:
            pass
    return list(jobs.values())


async def apply_job(page: Page, job: dict, resume: Path | None, profile: dict) -> str:
    job_id_match = re.search(r"/(?:jobs|job-search)/(\d+)", job["url"])
    if job_id_match:
        print("  [Step 2] Opening Handshake job detail...", flush=True)
        await page.goto("https://app.joinhandshake.com/job-search", wait_until="domcontentloaded", timeout=30000)
        for _ in range(20):
            await page.wait_for_timeout(750)
            card = page.locator(f"a[href*='job-search/{job_id_match.group(1)}']").first
            if await card.count() and await card.is_visible(timeout=500):
                await card.click(timeout=5000)
                print("  [Step 2] Job detail opened; looking for Apply externally...", flush=True)
                break
        else:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    else:
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)
    apply_control = None
    for locator in (
        page.get_by_role("button").filter(has_text=re.compile(r"apply|application|quick apply", re.I)),
        page.get_by_role("link").filter(has_text=re.compile(r"apply|application|quick apply", re.I)),
        page.locator("[data-testid*='apply' i], [aria-label*='apply' i], a[href*='apply' i]"),
    ):
        for candidate in await locator.all():
            try:
                if await candidate.is_visible(timeout=500):
                    apply_control = candidate
                    break
            except Exception:
                pass
        if apply_control:
            break
    if not apply_control:
        try:
            await page.screenshot(path="/tmp/handshake_job_debug.png", full_page=True)
            controls = await page.locator("button, a, [role=button]").all_inner_texts()
            print(f"  [Handshake] No apply control. Visible controls: {controls[:30]}", flush=True)
        except Exception:
            pass
        return "skipped - no Handshake apply button"
    print("  [Step 2] Apply externally control found; clicking...", flush=True)
    target = page
    clicked = False
    try:
        async with page.expect_popup(timeout=3000) as popup_info:
            await apply_control.click(timeout=5000)
            clicked = True
            print("  [Step 2] Apply externally clicked; waiting for employer site...", flush=True)
        target = await popup_info.value
        await target.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        if not clicked:
            try:
                await apply_control.click(timeout=5000)
                clicked = True
            except Exception:
                return "error: external apply control could not be clicked"
    page = target
    await page.wait_for_timeout(1800)
    # Some employer portals route through Google account selection after the
    # external link. Choose the signed-in profile account when it is shown.
    if "accounts.google.com" in page.url or "choose an account" in (await page.locator("body").inner_text()).lower():
        account = page.get_by_text(profile.get("email", ""), exact=True).first
        if await account.count() and await account.is_visible(timeout=1500):
            await account.click()
            await page.wait_for_timeout(2500)
            print(f"  [External] Selected Google account {profile.get('email', '')}", flush=True)
    if "joinhandshake.com" not in page.url:
        print(f"  [Handshake] External application opened: {page.url}", flush=True)

    # Reuse the tested ATS engines for employer redirects.
    external_url = page.url.lower()
    if "myworkdayjobs.com" in external_url:
        try:
            from company_apply.workday import _fill_workday
            status = await _fill_workday(
                page, profile, profile.get("email", ""), resume,
                "Handshake external employer", new_pages=[]
            )
            return status
        except Exception as exc:
            return f"error: external Workday form {str(exc)[:120]}"
    if "greenhouse.io" in external_url:
        try:
            from company_apply.greenhouse import _fill_greenhouse
            return await _fill_greenhouse(page, profile, profile.get("email", ""), resume, job.get("title", ""), "Handshake external employer")
        except Exception as exc:
            return f"error: external Greenhouse form {str(exc)[:120]}"
    if "lever.co" in external_url:
        try:
            from company_apply.lever import _fill_lever
            return await _fill_lever(page, profile, profile.get("email", ""), resume)
        except Exception as exc:
            return f"error: external Lever form {str(exc)[:120]}"
    if "ashbyhq.com" in external_url:
        try:
            from company_apply.ashby import _fill_ashby
            return await _fill_ashby(page, profile, profile.get("email", ""), resume)
        except Exception as exc:
            return f"error: external Ashby form {str(exc)[:120]}"

    await page.wait_for_timeout(1200)
    if resume:
        file_inputs = page.locator("input[type=file]")
        if await file_inputs.count():
            await file_inputs.first.set_input_files(str(resume))

    name_parts = profile.get("name", "").split()
    _loc_parts = [p.strip() for p in profile.get("location", "").split(",")]
    _city_val  = profile.get("city") or (_loc_parts[0] if _loc_parts else "")
    _state_val = profile.get("state") or (_loc_parts[1] if len(_loc_parts) > 1 else "")
    for field, value in {
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "first name": name_parts[0] if name_parts else "",
        "last name": " ".join(name_parts[1:]),
        "address": profile.get("address_line1", ""),
        "city": _city_val,
        "state": _state_val,
        "zip": profile.get("postal_code", ""),
    }.items():
        if not value:
            continue
        locator = page.get_by_label(re.compile(field, re.I)).first
        if await locator.count():
            try:
                await locator.fill(str(value))
            except Exception:
                pass

    # Fill standard employer fields using the same profile answer mapper as
    # the supported company ATS flows. Leave unknown questions untouched.
    for control in await page.locator("input:not([type=hidden]):not([type=file]), textarea").all():
        try:
            if not await control.is_visible(timeout=300):
                continue
            input_type = (await control.get_attribute("type") or "text").lower()
            if input_type in {"checkbox", "radio", "submit", "button"}:
                continue
            label = await control.get_attribute("aria-label") or await control.get_attribute("name") or await control.get_attribute("id") or ""
            if not label:
                label = " ".join((await control.locator("xpath=..").inner_text()).split())
            value = answer_for(label, profile, profile.get("email", ""))
            if value and not await control.input_value():
                await control.fill(str(value))
        except Exception:
            pass

    for select in await page.locator("select").all():
        try:
            if not await select.is_visible(timeout=300):
                continue
            label = await select.get_attribute("aria-label") or await select.get_attribute("name") or await select.get_attribute("id") or ""
            answer = answer_for(label, profile, profile.get("email", ""))
            if not answer:
                continue
            options = await select.locator("option").all()
            for option in options:
                text = " ".join((await option.inner_text()).split())
                if answer.lower() in text.lower() or text.lower() in answer.lower():
                    await select.select_option(label=text)
                    break
        except Exception:
            pass

    # Accept ordinary application acknowledgements, but never guess at
    # demographic or legal attestations.
    for checkbox in await page.locator("input[type=checkbox]").all():
        try:
            label = " ".join((await checkbox.locator("xpath=..").inner_text()).split()).lower()
            if any(word in label for word in ("terms", "privacy", "acknowledge", "consent")):
                if not await checkbox.is_checked():
                    await checkbox.check()
        except Exception:
            pass

    if "captcha" in (await page.locator("body").inner_text()).lower():
        return "blocked - CAPTCHA requires manual completion"

    submit = page.locator("button[type='submit'], input[type='submit']").last
    if not await submit.count():
        submit = page.get_by_role("button").filter(has_text=re.compile(r"submit|send application|apply|continue|finish", re.I)).last
    if await submit.count() and await submit.is_visible(timeout=1500):
        try:
            await submit.click(timeout=5000)
        except Exception:
            return "external application opened; submit control unavailable"
        await page.wait_for_timeout(1500)
        body = (await page.locator("body").inner_text()).lower()
        if any(word in body for word in ("thank you", "application submitted", "applied successfully")):
            return "applied"
    if "joinhandshake.com" not in page.url:
        return "external application opened; form requires additional fields"
    return "submitted (unconfirmed)"


async def run(args) -> None:
    profile = profile_for(args.profile)
    if args.login:
        await login(args.profile)
        return
    if not (session_dir(args.profile) / ".handshake_session_ready").exists():
        print("No Handshake session found. Opening login first...", flush=True)
        await login(args.profile)
    keywords = [item.strip() for item in args.keywords.split(",") if item.strip()] if args.keywords else [
        "computer science", "information systems", "management information systems", "mis",
        "software engineer", "software developer", "full stack", "web developer",
        "application developer", "java", "spring", "python", "developer", "programmer",
        "backend", "frontend", "cloud", "cloud engineer", "devops", "ai", "machine learning",
        "microservices", "data engineer", "data analyst", "data scientist", "business analyst",
        "systems analyst", "systems engineer", "information technology", "it analyst",
        "cybersecurity", "security analyst", "network engineer", "qa automation", "qa tester",
        "test engineer", "database engineer", "technical support", "technology consultant",
        "implementation analyst", "product analyst", "intern",
    ]
    levels = [item.strip() for item in args.experience_levels.split(",") if item.strip()]
    max_years = max(0, min(4, args.max_required_years))
    print(f"Keywords: {', '.join(keywords)}", flush=True)
    print("Resume: selecting the best matching uploaded resume per job", flush=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(str(session_dir(args.profile)), headless=False, locale="en-US")
        page = await context.new_page()
        jobs = await collect_jobs(page, keywords)
        eligible = []
        for job in jobs:
            reason = early_career_rejection_reason(job["title"], "", max_years, levels)
            if not reason:
                eligible.append(job)
        eligible = eligible[:args.max_jobs] if args.max_jobs else eligible
        print(f"Found {len(jobs)} related jobs; {len(eligible)} eligible.", flush=True)
        for index, job in enumerate(eligible, 1):
            if page.is_closed():
                page = await context.new_page()
                print("  [Browser] Reopened Handshake page for next job", flush=True)
            resume = resume_for(args.profile, job["title"])
            print(f"[{index}/{len(eligible)}] {job['title']}\n  {job['url']}", flush=True)
            print(f"  Resume selected: {resume or 'not configured'}", flush=True)
            try:
                status = await asyncio.wait_for(
                    apply_job(page, job, resume, profile), timeout=180
                )
                print(f"  → {status}", flush=True)
                if status in ("applied", "submitted (unconfirmed)") or status.startswith("external application"):
                    write_application(email=args.profile, job=job, status=status)
            except asyncio.TimeoutError:
                print("  → error: external application timed out before Step 2; continuing", flush=True)
            except Exception as exc:
                print(f"  → error: {str(exc)[:120]}", flush=True)
        await context.close()


def write_application(email: str, job: dict, status: str) -> None:
    exists = APPLIED_CSV.exists()
    with APPLIED_CSV.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["timestamp", "profile_email", "job_title", "job_url", "status"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), email, job["title"], job["url"], status])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--experience-levels", default="intern,new_grad,early_career,entry")
    parser.add_argument("--max-required-years", type=int, default=4)
    parser.add_argument("--max-jobs", type=int, default=0)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
