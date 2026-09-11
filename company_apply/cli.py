import argparse
import asyncio
import csv
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from job_eligibility import early_career_rejection_reason
from playwright.async_api import async_playwright, Page, Frame, BrowserContext
try:
    import gmail_sender as _gmail_sender
except ImportError:
    _gmail_sender = None

load_dotenv()

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

# ── Imports from sibling modules ────────────────────────────────────────────────
from .common import (
    add_company_interactive,
    ashby_list_jobs,
    APPLIED_LOG_PATH,
    BETWEEN_JOBS,
    check_gmail_confirmation,
    ensure_profile_complete,
    filter_jobs,
    find_company,
    generic_list_jobs,
    get_resume,
    greenhouse_list_jobs,
    lever_list_jobs,
    load_all_profiles,
    load_applied_urls,
    load_company_db,
    load_saved_filters,
    log_applied,
    applicable_companies,
    needtofix_companies,
    RESUMES_JSON,
    save_filters,
    stripe_list_jobs,
    workday_list_jobs,
)
from .greenhouse import _fill_greenhouse, _start_code_watcher
from .workday import _fill_workday, setup_workday_session, _workday_session_exists, _workday_session_file
from .lever import _fill_lever
from .ashby import _fill_ashby
from .microsoft import _fill_microsoft, microsoft_list_jobs

# ── Main apply loop ─────────────────────────────────────────────────────────────

async def _loaded_job_rejection_reason(
    page: Page,
    title: str,
    max_required_years: int,
    allowed_levels: Optional[list[str]] = None,
) -> str | None:
    _log.fn("_loaded_job_rejection_reason", title=title,
            max_required_years=max_required_years, allowed_levels=allowed_levels)
    try:
        description = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        description = ""
        _log.warn("Could not read page body for rejection reason check")
    result = early_career_rejection_reason(
        title, description, max_required_years, allowed_levels
    )
    if result:
        _log.skip(f"Job rejected: {result}  title={title!r}")
    else:
        _log.var("rejection_reason", result, note="job eligible")
    return result

async def apply_to_company(
    company_rec: dict,
    profile:     dict,
    email:       str,
    keywords:    list[str],
    locations:   Optional[list[str]] = None,
    posted_days: Optional[int]       = None,
    experience:  Optional[list[str]] = None,
    work_type:   Optional[list[str]] = None,
    us_only:     bool                = False,
    dry_run:     bool                = False,
    max_jobs:    int                 = 0,
):
    company = company_rec["name"]
    ats     = company_rec["ats"]
    slug    = company_rec.get("slug", "")
    c_url   = company_rec.get("careers_url", "")
    applied_urls = load_applied_urls(email)
    max_required_years = int(profile.get("max_required_years", 4))

    _log.session_start(company=company, profile=email, script="company_apply/cli.py")
    _log.set_context(company=company, profile=email)
    _log.fn("apply_to_company", company=company, ats=ats, email=email,
            keywords=keywords, locations=locations, posted_days=posted_days,
            experience=experience, dry_run=dry_run, max_jobs=max_jobs)
    _log.var("slug", slug)
    _log.var("careers_url", c_url)
    _log.var("max_required_years", max_required_years)
    _log.db("read", "applied_urls", count=len(applied_urls))

    # Show resume folder at startup; actual resume picked per-job by title
    try:
        _rd = json.loads(RESUMES_JSON.read_text()) if RESUMES_JSON.exists() else {}
        _resume_folder = _rd.get(email, {}).get("resume_folder", "")
    except Exception:
        _resume_folder = ""
        _log.warn("Could not read resumes.json for resume_folder")

    def _fmt(lst): return ", ".join(lst) if lst else "any"
    print(f"\n{'─'*62}")
    print(f"  Company    : {company}  ({ats})")
    print(f"  Profile    : {profile.get('name')} <{email}>")
    print(f"  Resume dir : {_resume_folder or 'not configured'}")
    print(f"  Keywords   : {', '.join(keywords) if keywords else 'all roles'}")
    print(f"  Location   : {_fmt(locations)}")
    print(f"  Posted     : {'last ' + str(posted_days) + ' days' if posted_days else 'any time'}")
    print(f"  Experience : {_fmt(experience)}")
    print(f"  Work type  : {_fmt(work_type)}")
    print(f"  US only    : {us_only}")
    print(f"  Dry run    : {dry_run}")
    print(f"{'─'*62}\n")

    safe_email = re.sub(r"[^a-z0-9]", "_", email.lower())
    # Use a separate profile dir so company_apply never conflicts with dice/linkedin automation
    user_data  = Path.home() / f".company-apply-profile-{safe_email}"
    user_data.mkdir(parents=True, exist_ok=True)

    # Init Gmail so verification code fetching and recruiter emails work
    if _gmail_sender:
        _gmail_sender.init_gmail(
            profile_dir=user_data,
            sender_name=profile.get("name", "Applicant"),
            sender_email=email,
        )
        # Pre-authenticate and start live verification-code watcher BEFORE Playwright.
        # Capturing historyId early ensures codes arriving during form-fill are not missed.
        if email and not dry_run:
            try:
                _svc = await asyncio.to_thread(_gmail_sender.get_gmail_service)
                if _svc:
                    _start_code_watcher(email, _svc)
                    print("  [Code Watcher] Live Gmail monitor started.", flush=True)
            except Exception:
                pass

    # ── Pre-fetch for API-based ATS (no browser needed for listing) ──────────────
    # Workday, Greenhouse, Lever, Stripe all expose REST/JSON APIs.
    # Fetch + filter BEFORE launching Chrome so dry-run and no-match cases
    # never open a browser window.
    _API_ATS = {"workday", "greenhouse", "lever", "stripe", "microsoft"}
    pre_fetched_jobs: Optional[list] = None

    _log.step(f"Fetch jobs  ({ats})")
    if ats in _API_ATS:
        _log.info(f"API-based ATS — fetching before browser launch  ats={ats}")
        print("  Fetching job listings...", flush=True)
        if ats == "workday":
            pre_fetched_jobs = workday_list_jobs(
                tenant=company_rec.get("tenant", ""),
                shard=company_rec.get("shard", 1),
                portal=company_rec.get("portal", ""),
                search=" ".join(keywords) if keywords else "",
            )
        elif ats == "greenhouse":
            pre_fetched_jobs = greenhouse_list_jobs(slug)
        elif ats == "lever":
            pre_fetched_jobs = lever_list_jobs(slug)
        elif ats == "stripe":
            pre_fetched_jobs = stripe_list_jobs()
        elif ats == "microsoft":
            pre_fetched_jobs = microsoft_list_jobs(
                keywords=keywords or None,
                num=company_rec.get("num_jobs", 50),
            )

        all_jobs = list(pre_fetched_jobs)
        _log.var("all_jobs_count", len(all_jobs), note="raw from API")
        print(f"  Found {len(all_jobs)} open role(s).")
        already_done = sum(1 for j in all_jobs if j["url"] in applied_urls)
        if already_done:
            _log.var("already_applied_count", already_done)
            print(f"  Already applied / exhausted: {already_done} (skipped)")

        def _run_filter(kw, locs, days, exp, wt, us):
            return filter_jobs(
                all_jobs, kw, applied_urls, locs, days, exp, wt, us,
                max_required_years,
            )

        jobs = _run_filter(keywords, locations, posted_days, experience, work_type, us_only)
        _log.var("eligible_jobs_count", len(jobs), note="after all filters")
        print(f"  {len(jobs)} eligible this run (match filters + not yet applied).")

        if not jobs and experience:
            _log.warn(f"Experience filter matched 0 roles  experience={experience}")
            print(f"\n  ✗  Experience filter [{', '.join(experience)}] matched 0 roles at {company_rec['name']}.")
            print(f"     Skipping — not falling back to unfiltered results.")
        if not jobs and posted_days:
            _log.warn(f"Date filter matched 0 roles, retrying without date  posted_days={posted_days}")
            print(f"\n  ↩  Date filter (last {posted_days}d) matched 0 roles.")
            print(f"     Retrying without date restriction (keeping keywords/location)...")
            jobs = _run_filter(keywords, locations, None, experience, work_type, us_only)
            if jobs:
                _log.ok(f"Found {len(jobs)} roles after dropping date filter")
                print(f"  ✓  {len(jobs)} roles found — date filter dropped.\n")
            else:
                _log.warn("Still 0 after dropping date filter")
                print(f"  Still 0 after dropping date.")
        if not jobs and locations:
            _log.warn(f"Location filter matched 0 roles, retrying without location  locations={locations}")
            print(f"\n  ↩  Location filter [{', '.join(locations)}] matched 0 roles.")
            print(f"     Retrying without location restriction (keeping keywords)...")
            jobs = _run_filter(keywords, [], None, experience, work_type, us_only)
            if jobs:
                _log.ok(f"Found {len(jobs)} roles after dropping location filter")
                print(f"  ✓  {len(jobs)} roles found — location filter dropped.\n")
            else:
                _log.warn("Still 0 after dropping location filter")
                print(f"  Still 0 after dropping location.")
        if not jobs and keywords:
            _log.warn(f"Retrying with keywords only  keywords={keywords}")
            print(f"\n  ↩  Retrying with keywords only: {', '.join(keywords)}")
            jobs = _run_filter(keywords, [], None, experience, [], us_only)
            if jobs:
                _log.ok(f"Found {len(jobs)} roles matching keywords only")
                print(f"  ✓  {len(jobs)} roles match keywords.\n")
            else:
                _log.warn("0 roles match keywords either — company has no matching open roles")
                print(f"  0 roles match keywords either. This company has no matching open roles.")

        print()
        if max_jobs > 0:
            jobs = jobs[:max_jobs]
            _log.var("jobs_after_max_cap", len(jobs), note=f"capped at {max_jobs}")

        if dry_run:
            for i, job in enumerate(jobs, 1):
                loc_str = job.get("location", "")
                resume  = get_resume(profile, email, job["title"])
                print(f"  [{i}/{len(jobs)}] {job['title']}" + (f"  [{loc_str}]" if loc_str else ""))
                print(f"          {job['url']}")
                print(f"          Resume: {resume.name if resume else 'none — skipping upload'}")
                print(f"          → [DRY RUN] would apply\n")
            if not jobs:
                print("  Nothing to apply to. Done.")
            print(f"\n{'─'*62}")
            print(f"  {company} summary (dry run):")
            print(f"    Would apply to: {len(jobs)}")
            print(f"{'─'*62}\n")
            return

        if not jobs:
            print("  Nothing to apply to. Done.")
            return

    # Workday: remind user to set up session if not done yet
    if ats == "workday":
        tenant = company_rec.get("tenant", "")
        _log.var("workday_tenant", tenant)
        if not _workday_session_exists(user_data, tenant):
            _log.warn(f"No Workday session found for {company} — applying as guest")
            print(f"\n  ⚠  No Workday session found for {company}.")
            print(f"     Applying as guest (slower, more brittle).")
            print(f"     For faster authenticated apply, run once:")
            print(f"       python3 company_apply.py --company {company.lower()} --setup-workday\n")
        else:
            _log.ok(f"Workday session exists for {company}")

    # Clear any stale Chrome singleton locks left by crashed previous runs
    import subprocess as _sp
    _sp.run(["pkill", "-f", "Google Chrome for Testing"], capture_output=True)
    import time as _t; _t.sleep(0.5)
    for _lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (user_data / _lock).unlink(missing_ok=True)

    _log.step("Launch Browser")
    _log.var("user_data_dir", str(user_data))

    async with async_playwright() as pw:
        ctx: BrowserContext = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        _log.ok("Browser context launched")

        # Inject saved Workday cookies so the authenticated session is restored
        # even if Chrome didn't flush them to the profile on a previous run.
        if ats == "workday":
            _sf = _workday_session_file(user_data, company_rec.get("tenant", ""))
            _log.var("workday_session_file", str(_sf))
            if _sf.exists():
                try:
                    import json as _json
                    _sdata = _json.loads(_sf.read_text())
                    _cookies = _sdata.get("cookies", [])
                    if _cookies:
                        await ctx.add_cookies(_cookies)
                        _log.ok(f"Loaded {len(_cookies)} Workday session cookies")
                        print(f"  [Workday] ✓ Loaded {len(_cookies)} session cookies", flush=True)
                    else:
                        _log.warn("Workday session file exists but contains no cookies")
                except Exception as _ce:
                    _log.err(f"Workday session cookie load failed", exc=_ce)
                    print(f"  [Workday] Session cookie load failed: {_ce}", flush=True)
            else:
                _log.warn(f"No Workday session file found at {_sf}")

        page: Page = await ctx.new_page()
        _log.ok("New browser page created")

        # ── Fetch job list (browser-required ATS: Ashby, generic) ────────────────
        if pre_fetched_jobs is None:
            _log.step(f"Browser job fetch  ({ats})")
            print("  Fetching job listings...", flush=True)
            if ats == "ashby":
                jobs = await ashby_list_jobs(page, slug)
            else:
                jobs = await generic_list_jobs(page, c_url)

        # Filter + fallback only needed for browser-fetched ATS (Ashby / generic)
        if pre_fetched_jobs is None:
            all_jobs = list(jobs)
            _log.var("all_jobs_count", len(all_jobs), note="browser-fetched")
            print(f"  Found {len(all_jobs)} open role(s).")
            already_done = sum(1 for j in all_jobs if j["url"] in applied_urls)
            if already_done:
                _log.var("already_applied_count", already_done)
                print(f"  Already applied / exhausted: {already_done} (skipped)")

            def _run_filter(kw, locs, days, exp, wt, us):
                return filter_jobs(
                    all_jobs, kw, applied_urls, locs, days, exp, wt, us,
                    max_required_years,
                )

            jobs = _run_filter(keywords, locations, posted_days, experience, work_type, us_only)
            print(f"  {len(jobs)} eligible this run (match filters + not yet applied).")

            if not jobs and experience:
                print(f"\n  ✗  Experience filter [{', '.join(experience)}] matched 0 roles at {company_rec['name']}.")
                print(f"     Skipping — not falling back to unfiltered results.")
            if not jobs and posted_days:
                print(f"\n  ↩  Date filter (last {posted_days}d) matched 0 roles.")
                print(f"     Retrying without date restriction (keeping keywords/location)...")
                jobs = _run_filter(keywords, locations, None, experience, work_type, us_only)
                if jobs:
                    print(f"  ✓  {len(jobs)} roles found — date filter dropped.\n")
                else:
                    print(f"  Still 0 after dropping date.")
            if not jobs and locations:
                print(f"\n  ↩  Location filter [{', '.join(locations)}] matched 0 roles.")
                print(f"     Retrying without location restriction (keeping keywords)...")
                jobs = _run_filter(keywords, [], None, experience, work_type, us_only)
                if jobs:
                    print(f"  ✓  {len(jobs)} roles found — location filter dropped.\n")
                else:
                    print(f"  Still 0 after dropping location.")
            if not jobs and keywords:
                print(f"\n  ↩  Retrying with keywords only: {', '.join(keywords)}")
                jobs = _run_filter(keywords, [], None, experience, [], us_only)
                if jobs:
                    print(f"  ✓  {len(jobs)} roles match keywords.\n")
                else:
                    print(f"  0 roles match keywords either. This company has no matching open roles.")

            print()
            if max_jobs > 0:
                jobs = jobs[:max_jobs]

        if not jobs:
            _log.skip("No eligible jobs — nothing to apply to")
            print("  Nothing to apply to. Done.")
            await ctx.close()
            return

        _log.step(f"Apply loop  ({len(jobs)} jobs)")
        applied = skipped = errors = 0

        for i, job in enumerate(jobs, 1):
            title   = job["title"]
            job_url = job["url"]
            job_ats = job.get("ats", ats)

            loc_str = job.get("location", "")
            resume  = get_resume(profile, email, title)
            _log.set_context(company=company, profile=email, job=title)
            _log.step(f"Job {i}/{len(jobs)}: {title}")
            _log.var("job_url", job_url)
            _log.var("job_ats", job_ats)
            _log.var("job_location", loc_str)
            _log.var("resume", resume.name if resume else None,
                     note="None means no resume upload")
            if resume is None:
                _log.null("resume", reason=f"no resume matched title: {title!r}")
            _job_start = time.perf_counter()
            print(f"  [{i}/{len(jobs)}] {title}" + (f"  [{loc_str}]" if loc_str else ""))
            print(f"          {job_url}")
            print(f"          Resume: {resume.name if resume else 'none — skipping upload'}")

            if dry_run:
                print(f"          → [DRY RUN] would apply\n")
                continue

            if job_ats == "generic":
                print("          → skipped (generic ATS — Phase 2)\n")
                log_applied(company, job_ats, title, job_url,
                            "skipped - generic not supported", email, location=loc_str)
                skipped += 1
                continue

            try:
                if job_ats == "workday":
                    _log.step("Dispatch: Workday")
                    # Keep listener active through the whole application so that
                    # popups opened by the gate click ('Apply Manually') are captured.
                    _new_pages: list = []
                    _wd_listener = lambda p: _new_pages.append(p)
                    ctx.on("page", _wd_listener)
                    try:
                        _log.nav(job_url, status="navigating")
                        await page.goto(job_url, wait_until="domcontentloaded", timeout=0)
                        _log.nav(job_url, status="loaded")
                    except Exception as _ne:
                        _log.warn(f"goto failed (continuing anyway): {_ne}")
                    await asyncio.sleep(1)  # brief settle before gate
                    rejection_reason = await _loaded_job_rejection_reason(
                        page, title, max_required_years, experience
                    )
                    if rejection_reason:
                        status = f"skipped - {rejection_reason}"
                        _log.skip(f"Workday: {rejection_reason}")
                    else:
                        _log.step("Filling Workday form")
                        status = await _fill_workday(page, profile, email, resume, company,
                                                     new_pages=_new_pages)
                    ctx.remove_listener("page", _wd_listener)
                elif job_ats in ("greenhouse", "stripe"):
                    _log.step(f"Dispatch: {job_ats}")
                    _log.nav(job_url, status="navigating")
                    await page.goto(job_url, wait_until="load", timeout=0)
                    _log.nav(job_url, status="loaded")
                    rejection_reason = await _loaded_job_rejection_reason(
                        page, title, max_required_years, experience
                    )
                    status = (f"skipped - {rejection_reason}" if rejection_reason else
                              await _fill_greenhouse(page, profile, email, resume, title, company))
                elif job_ats == "lever":
                    _log.step("Dispatch: Lever")
                    _log.nav(job_url, status="navigating")
                    await page.goto(job_url, wait_until="load", timeout=0)
                    _log.nav(job_url, status="loaded")
                    rejection_reason = await _loaded_job_rejection_reason(
                        page, title, max_required_years, experience
                    )
                    status = (f"skipped - {rejection_reason}" if rejection_reason else
                              await _fill_lever(page, profile, email, resume))
                elif job_ats == "ashby":
                    _log.step("Dispatch: Ashby")
                    _log.nav(job_url, status="navigating")
                    await page.goto(job_url, wait_until="load", timeout=0)
                    _log.nav(job_url, status="loaded")
                    rejection_reason = await _loaded_job_rejection_reason(
                        page, title, max_required_years, experience
                    )
                    status = (f"skipped - {rejection_reason}" if rejection_reason else
                              await _fill_ashby(page, profile, email, resume))
                elif job_ats == "microsoft":
                    _log.step("Dispatch: Microsoft")
                    _log.nav(job_url, status="navigating")
                    await page.goto(job_url, wait_until="domcontentloaded", timeout=0)
                    await asyncio.sleep(2)
                    _log.nav(job_url, status="loaded")
                    rejection_reason = await _loaded_job_rejection_reason(
                        page, title, max_required_years, experience
                    )
                    status = (f"skipped - {rejection_reason}" if rejection_reason else
                              await _fill_microsoft(page, profile, email, resume, company))
                else:
                    _log.warn(f"Unsupported ATS: {job_ats!r}")
                    await page.goto(job_url, wait_until="load", timeout=0)
                    status = "skipped - unsupported ATS"

                # If unconfirmed, double-check via Gmail
                if status == "submitted (unconfirmed)":
                    _log.info("Checking Gmail for confirmation email")
                    if check_gmail_confirmation(company, title):
                        status = "applied (gmail confirmed)"
                        _elapsed = time.perf_counter() - _job_start
                        _log.ok(f"Gmail confirmed  elapsed={_elapsed:.0f}s")
                        print(f"          → {status} ✓ confirmation email found  ⏱ {_elapsed:.0f}s\n")
                    else:
                        _log.warn("Gmail confirmation NOT found — status stays unconfirmed")
                        print(f"          → {status}\n")
                else:
                    _elapsed = time.perf_counter() - _job_start
                    _log.var("final_status", status)
                    _log.var("elapsed_seconds", round(_elapsed, 1))
                    if "applied" in status or "submitted" in status:
                        _log.ok(f"Job complete: {status}  elapsed={_elapsed:.0f}s")
                    elif "skipped" in status:
                        _log.skip(status)
                    else:
                        _log.warn(f"Unexpected status: {status}")
                    print(f"          → {status}  ⏱ {_elapsed:.0f}s\n")
                log_applied(company, job_ats, title, job_url, status, email, location=loc_str)
                _log.db("write", "applied_log", value=status)

                if "applied" in status or "submitted" in status:
                    applied += 1
                elif "skipped" in status:
                    skipped += 1
                else:
                    errors += 1

            except Exception as exc:
                err = str(exc)[:80]
                _elapsed = time.perf_counter() - _job_start
                _log.err(f"Job {i} exception: {err}  elapsed={_elapsed:.0f}s", exc=exc)
                print(f"          → error: {err}  ⏱ {_elapsed:.0f}s\n")
                log_applied(company, job_ats, title, job_url, f"error: {err}", email, location=loc_str)
                errors += 1

            finally:
                # Always runs — success OR error. Wrapped in its own try so a
                # bad browser state here never escapes the loop and kills the
                # async-with-playwright block (which would close the browser).
                try:
                    # Close any extra tabs opened during this job
                    for extra in list(ctx.pages[1:]):
                        try:
                            await extra.close()
                        except Exception:
                            pass
                    # Verify the main page is still alive; create fresh one if not
                    _main = ctx.pages[0] if ctx.pages else None
                    if _main is None:
                        page = await ctx.new_page()
                    else:
                        try:
                            await _main.title()  # raises if closed
                            page = _main
                        except Exception:
                            page = await ctx.new_page()
                except Exception:
                    try:
                        page = await ctx.new_page()
                    except Exception:
                        pass

                if i < len(jobs):
                    await asyncio.sleep(BETWEEN_JOBS)

        await ctx.close()

    _log.session_end(applied=applied, skipped=skipped, errors=errors)
    _log.clear_context()
    print(f"\n{'─'*62}")
    print(f"  {company} summary:")
    print(f"    Applied  : {applied}")
    print(f"    Skipped  : {skipped}")
    print(f"    Errors   : {errors}")
    print(f"  Log saved to: {APPLIED_LOG_PATH.name}")
    print(f"{'─'*62}\n")


# ── Profile setup sub-command ───────────────────────────────────────────────────

def setup_profiles():
    _log.fn("setup_profiles")
    all_p = load_all_profiles()
    _log.var("profiles_count", len(all_p))
    if not all_p:
        _log.warn("No profiles found in profiles.json")
        print("No profiles found in profiles.json.")
        return
    for em, p in all_p.items():
        _log.info(f"Ensuring profile complete for {em}")
        print(f"\n  Profile: {p.get('name', em)} <{em}>")
        ensure_profile_complete(p, em)
    _log.ok("All profiles up to date")
    print("All profiles up to date.")


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _pick_profile(all_profiles: dict, arg_email: Optional[str]) -> Optional[str]:
    _log.fn("_pick_profile", arg_email=arg_email, profiles_count=len(all_profiles))
    emails = list(all_profiles.keys())
    if not emails:
        _log.null("emails", reason="no profiles loaded")
        return None
    if arg_email:
        if arg_email in all_profiles:
            _log.ok(f"Profile matched by email: {arg_email}")
            return arg_email
        _log.warn(f"Profile not found: {arg_email!r}")
        print(f"  Profile '{arg_email}' not found.")
        return None
    if len(emails) == 1:
        _log.ok(f"Single profile auto-selected: {emails[0]}")
        return emails[0]
    print("  Select profile:")
    for i, em in enumerate(emails, 1):
        print(f"    {i}. {all_profiles[em].get('name', '?')} <{em}>")
    try:
        idx = int(input("  Choice [1]: ").strip() or "1") - 1
        chosen = emails[max(0, min(idx, len(emails) - 1))]
        _log.ok(f"User selected profile: {chosen}")
        return chosen
    except (ValueError, EOFError):
        _log.warn("Profile selection failed — defaulting to first")
        return emails[0]


def main():
    ap = argparse.ArgumentParser(
        description="Apply to all matching jobs at a company's careers page."
    )
    ap.add_argument("--company",  help="Company name (must exist in company_careers_db.json)")
    ap.add_argument("--profile",  help="Gmail address of the profile to use")
    ap.add_argument("--keywords",
                    help="Comma-separated title keywords, e.g. 'python,engineer,ai'. "
                         "Defaults to profile skills.")
    ap.add_argument("--location",
                    help="Comma-separated location filters, e.g. 'remote,austin,new york'.")
    ap.add_argument("--posted-days", type=int,
                    help="Only include jobs posted within the last N days, e.g. 30.")
    ap.add_argument("--experience",
                    help="Comma-separated experience-level keywords in title, e.g. 'senior,staff,principal'.")
    ap.add_argument("--max-required-years", type=int, default=None,
                    help="Reject roles requiring more than this many years (maximum 4).")
    ap.add_argument("--work-type",
                    help="Comma-separated work-type keywords, e.g. 'internship,contract'.")
    ap.add_argument("--us-only", action="store_true",
                    help="Only apply to jobs located in the United States.")
    ap.add_argument("--all-roles", action="store_true",
                    help="Apply to ALL roles — do not filter by keywords (use with --experience / --location).")
    ap.add_argument("--update-filters", action="store_true",
                    help="Re-prompt for filter values and overwrite saved ones.")
    ap.add_argument("--dry-run",  action="store_true",
                    help="List matching jobs without applying")
    ap.add_argument("--max-jobs", type=int, default=0,
                    help="Stop after applying to this many jobs (0 = no limit)")
    ap.add_argument("--list",     action="store_true",
                    help="List all companies in the DB")
    ap.add_argument("--add",      action="store_true",
                    help="Add a new company to the DB")
    ap.add_argument("--setup",          action="store_true",
                    help="Add phone / LinkedIn to your profiles")
    ap.add_argument("--setup-workday",  action="store_true",
                    help="Sign in to a company's Workday portal once (saves session for future runs)")
    args = ap.parse_args()

    # ── Sub-commands ──────────────────────────────────────────────────────────
    if args.setup:
        setup_profiles()
        return

    if getattr(args, "setup_workday", False):
        # Resolve company + profile first, then open browser for login
        all_profiles = load_all_profiles()
        email = _pick_profile(all_profiles, args.profile)
        if not email:
            sys.exit(1)
        db = load_company_db()
        company_name = args.company
        if not company_name:
            print("  --setup-workday requires --company")
            sys.exit(1)
        company_rec = find_company(company_name, db)
        if not company_rec:
            print(f"  '{company_name}' not found. Run --add first.")
            sys.exit(1)
        if company_rec.get("ats") != "workday":
            print(f"  {company_rec['name']} uses '{company_rec['ats']}' ATS, not Workday.")
            sys.exit(1)
        asyncio.run(setup_workday_session(company_rec, email))
        return

    if args.add:
        add_company_interactive()
        return

    if args.list:
        db = load_company_db()
        if not db:
            print("Company DB is empty. Run: python company_apply.py --add")
            return
        active  = applicable_companies(db)
        broken  = needtofix_companies(db)
        hdr = f"  {'Company':<25} {'ATS':<14} {'Slug':<22} Careers URL"
        sep = f"  {'─'*25} {'─'*14} {'─'*22} {'─'*40}"
        print(f"\n  Applicable companies ({len(active)}):")
        print(hdr); print(sep)
        for _, rec in sorted(active.items(), key=lambda x: x[1]["name"].lower()):
            print(
                f"  {rec['name']:<25} {rec['ats']:<14} "
                f"{rec.get('slug',''):<22} {rec.get('careers_url','')}"
            )
        if broken:
            print(f"\n  Needs-fix companies ({len(broken)}) — skipped during batch apply:")
            print(hdr); print(sep)
            for _, rec in sorted(broken.items(), key=lambda x: x[1]["name"].lower()):
                note = rec.get("fix_note", "")
                print(
                    f"  {rec['name']:<25} {rec['ats']:<14} "
                    f"{rec.get('slug',''):<22} {rec.get('careers_url','')}"
                )
                if note:
                    print(f"    ↳ {note}")
        print()
        return

    # ── Main apply flow ───────────────────────────────────────────────────────
    _log.step("CLI: main() — apply flow")
    all_profiles = load_all_profiles()
    _log.var("profiles_loaded", len(all_profiles))
    if not all_profiles:
        _log.err("No profiles found in profiles.json")
        print("No profiles found. Add entries to profiles.json first.")
        sys.exit(1)

    email = _pick_profile(all_profiles, args.profile)
    if not email:
        _log.err("No profile selected — aborting")
        sys.exit(1)

    profile = all_profiles[email].copy()
    profile["email"] = email
    _log.var("selected_email", email)
    print(f"\n  Profile: {profile.get('name')} <{email}>")
    profile = ensure_profile_complete(profile, email)
    if args.max_required_years is not None:
        profile["max_required_years"] = max(0, min(4, args.max_required_years))
        _log.var("max_required_years_override", profile["max_required_years"])

    # ── Pick company ──────────────────────────────────────────────────────────
    db = load_company_db()
    company_name = args.company
    if not company_name:
        if not db:
            print("Company DB is empty. Run: python company_apply.py --add")
            sys.exit(1)
        print("\n  Available companies:")
        for _, rec in sorted(db.items(), key=lambda x: x[1]["name"].lower()):
            print(f"    • {rec['name']}  ({rec['ats']})")
        try:
            company_name = input("\n  Enter company name: ").strip()
        except EOFError:
            company_name = ""

    company_rec = find_company(company_name, db)
    if not company_rec:
        _log.err(f"Company not found in DB: {company_name!r}")
        print(f"\n  '{company_name}' not found in company_careers_db.json.")
        print("  Run: python company_apply.py --add   to add it.")
        sys.exit(1)
    _log.ok(f"Company found: {company_rec['name']}  ats={company_rec.get('ats')}")

    # ── Keywords ──────────────────────────────────────────────────────────────
    if getattr(args, "all_roles", False):
        # Dashboard passed --all-roles: skip keyword filter entirely
        keywords = []
        print("  Keywords   : (all roles — no keyword filter)")
    elif args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    else:
        # Split skills by comma, then also split current_title into individual
        # words so "Gen ai engineer" → ["gen", "ai", "engineer"] instead of
        # one unsearchable string. Keep tokens >= 2 chars to include "ai","ml".
        skill_tokens = [k.strip() for k in profile.get("skills", "").split(",")]
        title_tokens = profile.get("current_title", "").split()
        raw_tokens   = skill_tokens + title_tokens
        keywords     = list(dict.fromkeys(          # preserve order, dedupe
            k.lower() for k in raw_tokens if len(k.strip()) >= 2
        ))
        if keywords:
            shown = ", ".join(keywords[:6]) + ("..." if len(keywords) > 6 else "")
            print(f"  Auto-keywords from profile: {shown}")
            try:
                use_all = input("  Apply to ALL roles instead? [y/N]: ").strip().lower()
            except EOFError:
                use_all = "y"   # when stdin is closed (dashboard mode), default to all roles
            if use_all == "y":
                keywords = []

    # ── Filters ───────────────────────────────────────────────────────────────
    def _ask(prompt, default=""):
        try:
            return input(prompt).strip() or default
        except EOFError:
            return default

    def _parse_list(raw):
        stripped = raw.strip().lower()
        if stripped in ("all", "any", ""):
            return []
        return [x.strip() for x in stripped.split(",") if x.strip()]

    # CLI flags override everything; --update-filters forces re-prompt
    cli_filters = bool(args.location or args.posted_days or args.experience or args.work_type)
    saved_f = {} if cli_filters or args.update_filters else load_saved_filters(email)

    # --us-only: CLI flag takes priority; else load from saved filters
    us_only = args.us_only or saved_f.get("us_only", False)
    need_prompt = args.update_filters or (not cli_filters and not saved_f)

    if need_prompt:
        print(f"\n  {'─'*56}")
        print(f"  Filters  (press Enter to skip / type 'all' to clear)")
        print(f"  {'─'*56}")

    if args.location:
        locations = _parse_list(args.location)
    elif need_prompt:
        locations = _parse_list(_ask("  Location    (e.g. remote, austin, new york): "))
    else:
        locations = saved_f.get("locations", [])

    # Strip the dashboard's us-only sentinel from locations — us_only flag handles it
    locations = [l for l in locations if l != "__us_only__"]

    if args.posted_days:
        posted_days = args.posted_days
    elif need_prompt:
        raw = _ask("  Posted within (days, e.g. 7 / 14 / 30): ")
        try:
            posted_days = int(raw) if raw else None
        except ValueError:
            posted_days = None
    else:
        posted_days = saved_f.get("posted_days")

    if args.experience:
        experience = _parse_list(args.experience)
    elif need_prompt:
        experience = _parse_list(_ask("  Experience  (e.g. senior, staff, principal, junior): "))
    else:
        experience = saved_f.get("experience", [])

    if args.work_type:
        work_type = _parse_list(args.work_type)
    elif need_prompt:
        work_type = _parse_list(_ask("  Work type   (e.g. full-time, internship, contract): "))
    else:
        work_type = saved_f.get("work_type", [])

    if need_prompt:
        print(f"  {'─'*56}")
        # Persist what the user just entered
        save_filters(email, {
            "locations":   locations,
            "posted_days": posted_days,
            "experience":  experience,
            "work_type":   work_type,
            "us_only":     us_only,
        })
        print(f"  Filters saved — next run will reuse them automatically.")
    else:
        # Show what's being used
        def _fmt(lst): return ", ".join(lst) if lst else "any"
        print(f"\n  Using saved filters:")
        print(f"    Location   : {_fmt(locations)}")
        print(f"    Posted     : {'last ' + str(posted_days) + ' days' if posted_days else 'any time'}")
        print(f"    Experience : {_fmt(experience)}")
        print(f"    Work type  : {_fmt(work_type)}")
        print(f"    US only    : {us_only}")
        print(f"  (run with --update-filters to change)\n")

    asyncio.run(
        apply_to_company(
            company_rec, profile, email, keywords,
            locations=locations,
            posted_days=posted_days,
            experience=experience,
            work_type=work_type,
            us_only=us_only,
            dry_run=args.dry_run,
            max_jobs=args.max_jobs,
        )
    )


if __name__ == "__main__":
    main()
