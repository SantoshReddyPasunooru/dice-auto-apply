#!/usr/bin/env python3
"""LinkedIn Easy Apply and external application runner."""

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import Page, async_playwright

from company_apply.common import answer_for
from job_eligibility import early_career_rejection_reason

HERE = Path(__file__).parent
PROFILES = HERE / "profiles.json"
RESUMES = HERE / "resumes"
APPLIED = HERE / "linkedin_applied_jobs.csv"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

KEYWORDS = (
    "computer science", "information systems", "management information systems", "mis",
    "software engineer", "software developer", "full stack", "web developer",
    "application developer", "java", "python", "backend", "frontend", "cloud",
    "devops", "data engineer", "data analyst", "data scientist", "business analyst",
    "systems analyst", "systems engineer", "information technology", "it analyst",
    "cybersecurity", "qa automation", "test engineer", "database engineer", "intern",
)


def safe_email(email: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", email.lower())


def session_dir(email: str) -> Path:
    path = Path.home() / f".linkedin-apply-profile-{safe_email(email)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_for(email: str) -> dict:
    data = json.loads(PROFILES.read_text()) if PROFILES.exists() else {}
    profile = dict(data.get(email, {}))
    profile["email"] = email
    return profile


def resume_for(email: str, title: str) -> Path | None:
    folder = RESUMES / safe_email(email)
    files = [p for p in folder.glob("*") if p.suffix.lower() in {".pdf", ".docx"}]
    if not files:
        return None
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    def score(path: Path) -> int:
        name = path.name.lower()
        return sum(2 for word in words if word in name)
    return max(files, key=score)


def write_status(email: str, job: dict, status: str) -> None:
    exists = APPLIED.exists()
    with APPLIED.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["timestamp", "profile_email", "job_title", "job_url", "status"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), email, job["title"], job["url"], status])


async def collect_jobs(page: Page, keywords: list[str]) -> list[dict]:
    query = " ".join(keywords)
    url = f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(query)}&f_TPR=r604800&sortBy=DD"
    print(f"Searching LinkedIn jobs: {query}", flush=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    for _ in range(5):
        await page.mouse.wheel(0, 2200)
        await page.wait_for_timeout(1000)
    jobs = {}
    for link in await page.locator("a[href*='/jobs/view/']").all():
        try:
            href = await link.get_attribute("href")
            title = " ".join((await link.inner_text()).split())
            if not href or not title:
                continue
            absolute = href.split("?")[0]
            if absolute.startswith("/"):
                absolute = "https://www.linkedin.com" + absolute
            if not any(term in title.lower() for term in keywords):
                continue
            if "on-campus" in title.lower() or "on campus" in title.lower():
                continue
            if absolute not in jobs:
                jobs[absolute] = {"title": title, "url": absolute}
        except Exception:
            pass
    return list(jobs.values())


async def fill_external(page: Page, profile: dict, resume: Path | None, title: str) -> str:
    url = page.url.lower()
    if "myworkdayjobs.com" in url:
        from company_apply.workday import _fill_workday
        return await _fill_workday(page, profile, profile["email"], resume, "LinkedIn external", new_pages=[])
    if "greenhouse.io" in url:
        from company_apply.greenhouse import _fill_greenhouse
        return await _fill_greenhouse(page, profile, profile["email"], resume, title, "LinkedIn")
    if "lever.co" in url:
        from company_apply.lever import _fill_lever
        return await _fill_lever(page, profile, profile["email"], resume)
    if "ashbyhq.com" in url:
        from company_apply.ashby import _fill_ashby
        return await _fill_ashby(page, profile, profile["email"], resume)
    if resume and await page.locator("input[type=file]").count():
        await page.locator("input[type=file]").first.set_input_files(str(resume))
    name = profile.get("name", "").split()
    values = {
        "email": profile["email"], "phone": profile.get("phone", ""),
        "first name": name[0] if name else "", "last name": " ".join(name[1:]),
        "city": profile.get("city", ""), "state": profile.get("state", ""),
        "zip": profile.get("postal_code", ""),
    }
    for label, value in values.items():
        if not value:
            continue
        field = page.get_by_label(re.compile(label, re.I)).first
        if await field.count():
            try:
                await field.fill(str(value))
            except Exception:
                pass
    for control in await page.locator("input:not([type=hidden]):not([type=file]), textarea").all():
        try:
            if not await control.is_visible(timeout=200) or await control.input_value():
                continue
            label = await control.get_attribute("aria-label") or await control.get_attribute("name") or ""
            value = answer_for(label, profile, profile["email"])
            if value:
                await control.fill(value)
        except Exception:
            pass
    submit = page.locator("button[type=submit], input[type=submit]").last
    if not await submit.count():
        submit = page.get_by_role("button").filter(has_text=re.compile(r"submit|send application|apply|finish", re.I)).last
    if await submit.count() and await submit.is_visible(timeout=1500):
        await submit.click(timeout=5000)
        await page.wait_for_timeout(1500)
        body = (await page.locator("body").inner_text()).lower()
        if any(word in body for word in ("thank you", "application submitted", "applied successfully")):
            return "applied"
    return "external application opened; submit control unavailable"


async def apply_job(context, page: Page, job: dict, profile: dict, resume: Path | None) -> str:
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3500)
    body = (await page.locator("body").inner_text()).lower()
    if "you applied" in body or "application submitted" in body:
        return "skipped - already applied"
    button = page.get_by_role("button").filter(has_text=re.compile(r"easy apply|apply now|apply", re.I)).last
    if not await button.count():
        button = page.locator("a[href*='apply'], button:has-text('Apply')").last
    if not await button.count():
        return "skipped - apply button not found"
    before = set(context.pages)
    try:
        await button.click(timeout=7000)
    except Exception as exc:
        return f"error: apply click failed ({str(exc)[:80]})"
    await page.wait_for_timeout(2500)
    external = [candidate for candidate in context.pages if candidate not in before]
    target = external[-1] if external else page
    if target != page and "linkedin.com" not in target.url:
        await target.wait_for_load_state("domcontentloaded", timeout=15000)
        return await fill_external(target, profile, resume, job["title"])
    for _ in range(12):
        if resume and await target.locator("input[type=file]").count():
            try:
                await target.locator("input[type=file]").first.set_input_files(str(resume))
            except Exception:
                pass
        for control in await target.locator("input:not([type=hidden]):not([type=file]), textarea").all():
            try:
                if not await control.is_visible(timeout=200) or await control.input_value():
                    continue
                label = await control.get_attribute("aria-label") or await control.get_attribute("name") or ""
                value = answer_for(label, profile, profile["email"])
                if value:
                    await control.fill(value)
            except Exception:
                pass
        submit = target.get_by_role("button").filter(has_text=re.compile(r"submit application|submit|send application|finish", re.I)).last
        if await submit.count() and await submit.is_visible(timeout=500):
            await submit.click(timeout=5000)
            await target.wait_for_timeout(1500)
            return "applied" if "thank" in (await target.locator("body").inner_text()).lower() else "submitted (unconfirmed)"
        next_button = target.get_by_role("button").filter(has_text=re.compile(r"next|continue|review", re.I)).last
        if not await next_button.count() or not await next_button.is_visible(timeout=500):
            return "application opened; additional fields require review"
        await next_button.click(timeout=5000)
        await target.wait_for_timeout(800)
    return "application timed out"


async def login(email: str) -> None:
    async with async_playwright() as playwright:
        options = {"headless": False}
        if CHROME.exists():
            options["executable_path"] = str(CHROME)
        context = await playwright.chromium.launch_persistent_context(str(session_dir(email)), **options)
        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
        print("LinkedIn login opened. Sign in in the browser; waiting up to 10 minutes...", flush=True)
        for _ in range(600):
            if "login" not in page.url.lower():
                (session_dir(email) / ".ready").write_text("ready")
                print("LinkedIn session saved.", flush=True)
                break
            await asyncio.sleep(1)
        await context.close()


async def run(args) -> None:
    if args.login:
        await login(args.profile)
        return
    if not (session_dir(args.profile) / ".ready").exists():
        print("No LinkedIn session found. Opening login first...", flush=True)
        await login(args.profile)
    profile = profile_for(args.profile)
    keywords = [item.strip().lower() for item in args.keywords.split(",") if item.strip()] if args.keywords else list(KEYWORDS)
    levels = [item.strip() for item in args.experience_levels.split(",") if item.strip()]
    async with async_playwright() as playwright:
        options = {"headless": False}
        if CHROME.exists():
            options["executable_path"] = str(CHROME)
        context = await playwright.chromium.launch_persistent_context(str(session_dir(args.profile)), **options)
        page = await context.new_page()
        jobs = await collect_jobs(page, keywords)
        eligible = [job for job in jobs if not early_career_rejection_reason(job["title"], "", min(4, args.max_required_years), levels)]
        print(f"Found {len(jobs)} related jobs; {len(eligible)} eligible.", flush=True)
        for index, job in enumerate(eligible[:args.max_jobs] if args.max_jobs else eligible, 1):
            resume = resume_for(args.profile, job["title"])
            print(f"[{index}/{len(eligible)}] {job['title']}\n  {job['url']}\n  Resume: {resume or 'none'}", flush=True)
            try:
                status = await asyncio.wait_for(apply_job(context, page, job, profile, resume), timeout=180)
            except Exception as exc:
                status = f"error: {str(exc)[:120]}"
            print(f"  → {status}", flush=True)
            if status == "applied":
                write_status(args.profile, job, status)
                print("  ✓ Confirmed submission; continuing to the next job", flush=True)
            else:
                if status.startswith(("submitted", "external application", "application opened")):
                    write_status(args.profile, job, status)
                print("  ■ Stopping: application was not confirmed complete; review this job before continuing", flush=True)
                break
        await context.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--experience-levels", default="intern,junior")
    parser.add_argument("--max-required-years", type=int, default=4)
    parser.add_argument("--max-jobs", type=int, default=0)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
