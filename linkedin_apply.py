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
from apply_logger import log as _log

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
    _log.fn("profile_for", email=email)
    _log.var("profiles_path", str(PROFILES), note="path being checked")
    data = json.loads(PROFILES.read_text()) if PROFILES.exists() else {}
    profile = dict(data.get(email, {}))
    profile["email"] = email
    if profile and len(profile) > 1:
        _log.var("profile_keys", list(profile.keys()), note="profile loaded successfully")
    else:
        _log.null("profile", reason=f"no profile entry found for {email}")
    _log.ret("profile_for", f"{len(profile)} keys")
    return profile


def resume_for(email: str, title: str) -> Path | None:
    _log.fn("resume_for", email=email, title=title)
    folder = RESUMES / safe_email(email)
    _log.var("resume_folder", str(folder), note="searching for resumes")
    files = [p for p in folder.glob("*") if p.suffix.lower() in {".pdf", ".docx"}]
    _log.var("resume_candidates", [f.name for f in files], note=f"{len(files)} resume file(s) found")
    if not files:
        _log.null("resume", reason=f"no pdf/docx files in {folder}")
        _log.ret("resume_for", None)
        return None
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    def score(path: Path) -> int:
        name = path.name.lower()
        return sum(2 for word in words if word in name)
    selected = max(files, key=score)
    _log.var("resume_selected", selected.name, note=f"best match for title={title!r}")
    _log.ret("resume_for", selected.name)
    return selected


def write_status(email: str, job: dict, status: str) -> None:
    _log.db("write", "linkedin_applied_jobs.csv", value=f"{job.get('title')}|{status}")
    exists = APPLIED.exists()
    with APPLIED.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["timestamp", "profile_email", "job_title", "job_url", "status"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), email, job["title"], job["url"], status])


async def collect_jobs(page: Page, keywords: list[str]) -> list[dict]:
    _log.fn("collect_jobs", keywords=keywords)
    _log.var("keyword_count", len(keywords), note=f"keywords: {', '.join(keywords[:10])}")
    query = " ".join(keywords)
    url = f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(query)}&f_TPR=r604800&sortBy=DD"
    print(f"Searching LinkedIn jobs: {query}", flush=True)
    _log.nav(url, status="loading", title="LinkedIn job search")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    for _ in range(5):
        await page.mouse.wheel(0, 2200)
        await page.wait_for_timeout(1000)
    jobs = {}
    _skipped_keyword = 0
    _skipped_campus = 0
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
                _log.skip(f"job filtered (keyword mismatch): {title[:80]!r}")
                _skipped_keyword += 1
                continue
            if "on-campus" in title.lower() or "on campus" in title.lower():
                _log.skip(f"job filtered (on-campus): {title[:80]!r}")
                _skipped_campus += 1
                continue
            if absolute not in jobs:
                _log.var(f"job_found", title[:80], note=absolute)
                jobs[absolute] = {"title": title, "url": absolute}
        except Exception:
            pass
    _log.var("jobs_collected", len(jobs), note=f"skipped keyword={_skipped_keyword} campus={_skipped_campus}")
    _log.ret("collect_jobs", f"{len(jobs)} jobs")
    return list(jobs.values())


async def fill_external(page: Page, profile: dict, resume: Path | None, title: str) -> str:
    _log.fn("fill_external", title=title, url=page.url)
    url = page.url.lower()
    _log.nav(page.url, status="external", title=f"external ATS page for {title!r}")
    if "myworkdayjobs.com" in url:
        _log.var("ats_branch", "Workday", note=url)
        from company_apply.workday import _fill_workday
        return await _fill_workday(page, profile, profile["email"], resume, "LinkedIn external", new_pages=[])
    if "greenhouse.io" in url:
        _log.var("ats_branch", "Greenhouse", note=url)
        from company_apply.greenhouse import _fill_greenhouse
        return await _fill_greenhouse(page, profile, profile["email"], resume, title, "LinkedIn")
    if "lever.co" in url:
        _log.var("ats_branch", "Lever", note=url)
        from company_apply.lever import _fill_lever
        return await _fill_lever(page, profile, profile["email"], resume)
    if "ashbyhq.com" in url:
        _log.var("ats_branch", "Ashby", note=url)
        from company_apply.ashby import _fill_ashby
        return await _fill_ashby(page, profile, profile["email"], resume)
    _log.var("ats_branch", "generic/unknown", note=url)
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
    _log.fn("apply_job", title=job.get("title"), url=job.get("url"))
    _log.var("job_title", job.get("title"), note="beginning application")
    _log.var("job_url", job.get("url"))
    _log.var("resume", str(resume) if resume else None, note="resume being used")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    _log.nav(job["url"], status="loaded", title=job.get("title", ""))
    await page.wait_for_timeout(3500)
    body = (await page.locator("body").inner_text()).lower()
    if "you applied" in body or "application submitted" in body:
        _log.skip(f"already applied to {job.get('title')!r}")
        _log.ret("apply_job", "skipped - already applied")
        return "skipped - already applied"
    button = page.get_by_role("button").filter(has_text=re.compile(r"easy apply|apply now|apply", re.I)).last
    if not await button.count():
        button = page.locator("a[href*='apply'], button:has-text('Apply')").last
    if not await button.count():
        _log.warn(f"No apply button found for {job.get('title')!r}", exc=None)
        _log.ret("apply_job", "skipped - apply button not found")
        return "skipped - apply button not found"
    _log.browser("find", "Easy Apply / Apply button", result="found")
    before = set(context.pages)
    try:
        await button.click(timeout=7000)
        _log.browser("click", "Apply button", result="clicked")
    except Exception as exc:
        _log.err(f"Apply button click failed for {job.get('title')!r}", exc=exc)
        return f"error: apply click failed ({str(exc)[:80]})"
    await page.wait_for_timeout(2500)
    external = [candidate for candidate in context.pages if candidate not in before]
    target = external[-1] if external else page
    if target != page and "linkedin.com" not in target.url:
        _log.var("ats_detected", "external", note=f"new tab opened: {target.url}")
        _log.nav(target.url, status="external_redirect", title="external ATS")
        await target.wait_for_load_state("domcontentloaded", timeout=15000)
        result = await fill_external(target, profile, resume, job["title"])
        _log.ret("apply_job", result)
        return result
    _log.var("ats_detected", "LinkedIn Easy Apply", note="no new tab — using LinkedIn native flow")
    for _step in range(12):
        _log.var("easy_apply_step", _step + 1, note="Easy Apply wizard step")
        if resume and await target.locator("input[type=file]").count():
            try:
                await target.locator("input[type=file]").first.set_input_files(str(resume))
                _log.browser("upload", str(resume), result="resume uploaded in Easy Apply")
            except Exception as _ue:
                _log.warn("Easy Apply resume upload failed", exc=_ue)
                pass
        for control in await target.locator("input:not([type=hidden]):not([type=file]), textarea").all():
            try:
                if not await control.is_visible(timeout=200) or await control.input_value():
                    continue
                label = await control.get_attribute("aria-label") or await control.get_attribute("name") or ""
                value = answer_for(label, profile, profile["email"])
                if value:
                    _log.var(f"easy_apply_field[{label[:50]}]", value[:80])
                    await control.fill(value)
            except Exception:
                pass
        submit = target.get_by_role("button").filter(has_text=re.compile(r"submit application|submit|send application|finish", re.I)).last
        if await submit.count() and await submit.is_visible(timeout=500):
            _log.browser("click", "Submit Application (Easy Apply)", result="submitting")
            await submit.click(timeout=5000)
            await target.wait_for_timeout(1500)
            body_after = (await target.locator("body").inner_text()).lower()
            result = "applied" if "thank" in body_after else "submitted (unconfirmed)"
            _log.var("easy_apply_submit_result", result)
            if result == "applied":
                _log.ok(f"Easy Apply submitted: {job.get('title')!r}")
            else:
                _log.warn(f"Easy Apply submitted but not confirmed: {job.get('title')!r}", exc=None)
            _log.ret("apply_job", result)
            return result
        next_button = target.get_by_role("button").filter(has_text=re.compile(r"next|continue|review", re.I)).last
        if not await next_button.count() or not await next_button.is_visible(timeout=500):
            _log.warn("No next/submit button found — additional review required", exc=None)
            _log.ret("apply_job", "application opened; additional fields require review")
            return "application opened; additional fields require review"
        _log.browser("click", "Next/Continue button", result=f"advancing to step {_step + 2}")
        await next_button.click(timeout=5000)
        await target.wait_for_timeout(800)
    _log.warn("Easy Apply timed out after 12 steps", exc=None)
    _log.ret("apply_job", "application timed out")
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
    _log.step("LinkedIn Apply: Session Start")
    _log.fn("run", profile=args.profile, login=args.login)
    if args.login:
        await login(args.profile)
        return
    if not (session_dir(args.profile) / ".ready").exists():
        print("No LinkedIn session found. Opening login first...", flush=True)
        await login(args.profile)
    profile = profile_for(args.profile)
    keywords = [item.strip().lower() for item in args.keywords.split(",") if item.strip()] if args.keywords else list(KEYWORDS)
    _log.var("keywords", keywords, note=f"{len(keywords)} keyword(s)")
    levels = [item.strip() for item in args.experience_levels.split(",") if item.strip()]
    _log.var("experience_levels", levels)
    _total_applied = 0
    _total_skipped = 0
    _total_errors = 0
    async with async_playwright() as playwright:
        options = {"headless": False}
        if CHROME.exists():
            options["executable_path"] = str(CHROME)
        context = await playwright.chromium.launch_persistent_context(str(session_dir(args.profile)), **options)
        page = await context.new_page()
        jobs = await collect_jobs(page, keywords)
        eligible = [job for job in jobs if not early_career_rejection_reason(job["title"], "", min(4, args.max_required_years), levels)]
        _log.var("total_jobs_found", len(jobs))
        _log.var("total_eligible", len(eligible))
        print(f"Found {len(jobs)} related jobs; {len(eligible)} eligible.", flush=True)
        for index, job in enumerate(eligible[:args.max_jobs] if args.max_jobs else eligible, 1):
            resume = resume_for(args.profile, job["title"])
            print(f"[{index}/{len(eligible)}] {job['title']}\n  {job['url']}\n  Resume: {resume or 'none'}", flush=True)
            _log.var(f"job[{index}]", job["title"], note=job["url"])
            try:
                status = await asyncio.wait_for(apply_job(context, page, job, profile, resume), timeout=180)
            except Exception as exc:
                status = f"error: {str(exc)[:120]}"
                _log.err(f"apply_job raised exception for {job['title']!r}", exc=exc)
                _total_errors += 1
            print(f"  → {status}", flush=True)
            if status == "applied":
                _total_applied += 1
                _log.ok(f"[{index}/{len(eligible)}] Applied: {job['title']!r} | tally: applied={_total_applied} skipped={_total_skipped} errors={_total_errors}")
                write_status(args.profile, job, status)
                print("  ✓ Confirmed submission; continuing to the next job", flush=True)
            else:
                if status.startswith(("submitted", "external application", "application opened")):
                    _total_applied += 1
                    write_status(args.profile, job, status)
                elif status.startswith("skipped"):
                    _total_skipped += 1
                else:
                    _total_errors += 1
                _log.warn(f"[{index}/{len(eligible)}] Non-confirmed: {job['title']!r} → {status} | tally: applied={_total_applied} skipped={_total_skipped} errors={_total_errors}", exc=None)
                print("  ■ Stopping: application was not confirmed complete; review this job before continuing", flush=True)
                break
        _log.var("final_tally", f"applied={_total_applied} skipped={_total_skipped} errors={_total_errors}", note="LinkedIn Apply session complete")
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
