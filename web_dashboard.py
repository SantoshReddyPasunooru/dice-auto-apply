#!/usr/bin/env python3
"""
Web Dashboard for the job automation system.

Usage:
  python web_dashboard.py --profile santosh
  python web_dashboard.py --profile santoshpasunoorureddy@gmail.com
  python web_dashboard.py --profile santosh --port 5050
"""

import argparse
import csv
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request
from werkzeug.utils import secure_filename

try:
    from apply_logger import log as _log
except ImportError:
    class _NullLog:
        def __getattr__(self, _): return lambda *a, **k: None
    _log = _NullLog()

# ── Paths ──────────────────────────────────────────────────────────────────────
_HERE           = Path(__file__).parent
PROFILES_JSON   = _HERE / "profiles.json"
RESUMES_JSON    = _HERE / "resumes.json"
APPLIED_CSV     = _HERE / "applied_jobs.csv"
EXT_CSV         = _HERE / "external_applied_jobs.csv"
HANDSHAKE_CSV   = _HERE / "handshake_applied_jobs.csv"
LINKEDIN_APPLIED_CSV = _HERE / "linkedin_applied_jobs.csv"
RECRUITERS_CSV  = _HERE / "recruiters.csv"
COMPANIES_JSON  = _HERE / "applicable_companies.json"
COMPANY_DB_JSON = _HERE / "company_careers_db.json"
LI_CONFIG_JSON  = _HERE / "linkedin_config.json"

app = Flask(__name__)

# ── Global state ───────────────────────────────────────────────────────────────
PROFILE_EMAIL:  str  = ""
PROFILE_NAME:   str  = ""
SETUP_REQUIRED: bool = False

FEATURES = ["gmail_monitor", "linkedin_outreach", "linkedin_apply", "dice_apply", "handshake_apply", "companies_apply"]

PROCESSES:       dict[str, subprocess.Popen] = {}
LOG_BUFFERS:     dict[str, deque]            = {f: deque(maxlen=500) for f in FEATURES}
SSE_SUBSCRIBERS: dict[str, list]             = {f: []               for f in FEATURES}
_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe(email: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", email.lower())

def _profile_dir(email: str) -> Path:
    return Path.home() / f".dice-playwright-profile-{_safe(email)}"

def _dice_session_dir(email: str) -> Path:
    """Find the actual Playwright session directory used by main.py for this email."""
    home = Path.home()
    for p in sorted(home.iterdir()):
        if not p.is_dir() or not p.name.startswith(".dice-"):
            continue
        ep = p / ".profile_email"
        if ep.exists() and ep.read_text().strip().lower() == email.lower():
            return p
    # Fallback: email-keyed dir, then bare default
    keyed = _profile_dir(email)
    if keyed.exists():
        return keyed
    default = home / ".dice-playwright-profile"
    return default if default.exists() else keyed

def _resolve_profile(arg: str) -> tuple[str, str]:
    global SETUP_REQUIRED
    _log.fn("_resolve_profile", arg=arg)
    if not PROFILES_JSON.exists():
        _log.null("PROFILES_JSON", reason="file does not exist — setup required")
        SETUP_REQUIRED = True
        email = arg if "@" in arg else f"{arg}@gmail.com"
        return email, arg
    profiles = json.loads(PROFILES_JSON.read_text())
    _log.var("profiles_count", len(profiles))
    if arg in profiles:
        name = profiles[arg].get("name", arg)
        _log.ok(f"Profile resolved by email: {arg} → {name}")
        return arg, name
    for email, data in profiles.items():
        if data.get("name", "").lower().startswith(arg.lower()):
            name = data.get("name", email)
            _log.ok(f"Profile resolved by name prefix: {arg!r} → {email}  name={name}")
            return email, name
    _log.warn(f"Profile not found for arg={arg!r} — setup required")
    SETUP_REQUIRED = True
    email = arg if "@" in arg else f"{arg}@gmail.com"
    return email, arg

def _push_log(feature: str, line: str):
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "line": line}
    with _lock:
        LOG_BUFFERS[feature].append(entry)
        for q in SSE_SUBSCRIBERS[feature]:
            try:
                q.put_nowait(entry)
            except Exception:
                pass

def _read_output(feature: str, proc: subprocess.Popen):
    _log.fn("_read_output", feature=feature, pid=proc.pid)
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            _push_log(feature, line)
            if any(kw in line.lower() for kw in ("error", "exception", "traceback", "✗")):
                _log.warn(f"[{feature}] subprocess error line: {line[:120]}")
    except Exception as exc:
        _log.err(f"_read_output stream broken for {feature}", exc=exc)
    finally:
        with _lock:
            if PROCESSES.get(feature) is proc:
                PROCESSES.pop(feature, None)
        _log.info(f"[{feature}] process ended  pid={proc.pid}")
        _push_log(feature, "__PROCESS_ENDED__")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if SETUP_REQUIRED:
        return redirect("/setup")
    return render_template("dashboard.html", profile_email=PROFILE_EMAIL, profile_name=PROFILE_NAME)


@app.route("/api/profile", methods=["GET"])
def api_get_profile():
    profiles = json.loads(PROFILES_JSON.read_text()) if PROFILES_JSON.exists() else {}
    resumes  = json.loads(RESUMES_JSON.read_text())  if RESUMES_JSON.exists()  else {}
    p = {**profiles.get(PROFILE_EMAIL, {}), **resumes.get(PROFILE_EMAIL, {})}
    p["email"] = PROFILE_EMAIL
    return jsonify(p)


@app.route("/api/profile", methods=["POST"])
def api_save_profile():
    data = request.json or {}
    resume_keys = {"resume_folder", "default_resume"}

    profiles = json.loads(PROFILES_JSON.read_text()) if PROFILES_JSON.exists() else {}
    resumes  = json.loads(RESUMES_JSON.read_text())  if RESUMES_JSON.exists()  else {}

    profiles.setdefault(PROFILE_EMAIL, {}).update(
        {k: v for k, v in data.items() if k not in resume_keys and k != "email"}
    )
    resumes.setdefault(PROFILE_EMAIL, {}).update(
        {k: v for k, v in data.items() if k in resume_keys}
    )

    PROFILES_JSON.write_text(json.dumps(profiles, indent=2))
    RESUMES_JSON.write_text(json.dumps(resumes, indent=2))
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    # Gmail
    token = _profile_dir(PROFILE_EMAIL) / "gmail_token.json"
    gmail = "connected" if token.exists() else "disconnected"

    # Dice session is marked ready only after an authenticated Dice page is reached.
    dice_dir = _dice_session_dir(PROFILE_EMAIL)
    dice_connected = (dice_dir / ".dice_session_ready").exists()
    dice = "connected" if dice_connected else "disconnected"

    # Ollama
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:11434", timeout=1)
        ollama = "running"
    except Exception:
        ollama = "offline"

    # Process states
    procs = {}
    with _lock:
        for f in FEATURES:
            p = PROCESSES.get(f)
            procs[f] = (p is not None and p.poll() is None)

    return jsonify({"gmail": gmail, "dice": dice, "ollama": ollama, "processes": procs})


@app.route("/api/recruiters")
def api_recruiters():
    """Return recruiter contacts. ?src=linkedin|gmail filters by source."""
    src_filter = request.args.get("src", "")   # "linkedin" | "gmail" | "" (all)
    rows = []
    if RECRUITERS_CSV.exists():
        try:
            with open(RECRUITERS_CSV) as f:
                for row in csv.DictReader(f):
                    src    = row.get("source", "")
                    status = row.get("last_status", "")
                    if status in ("junk",):
                        continue
                    sources     = [s.strip() for s in src.split(",")]
                    is_linkedin = "linkedin" in sources
                    is_inbox    = "gmail_inbox" in sources
                    if src_filter == "linkedin":
                        if not is_linkedin:
                            continue
                    elif src_filter == "gmail":
                        if not (is_inbox and status in ("contacted", "replied", "interview", "rtr", "offer")):
                            continue
                    else:
                        # default: linkedin + engaged gmail_inbox
                        if not is_linkedin and not (is_inbox and status in ("contacted", "replied", "interview", "rtr", "offer")):
                            continue
                    last_seen = row.get("last_seen", "")
                    rows.append({
                        "ts":       last_seen,
                        "date":     last_seen[:10],
                        "time":     last_seen[11:16] if len(last_seen) > 10 else "",
                        "name":     row.get("name", ""),
                        "email":    row.get("email", ""),
                        "company":  row.get("company", ""),
                        "title":    row.get("title", ""),
                        "location": row.get("location", ""),
                        "source":   src,
                        "status":   status,
                        "times":    row.get("times_contacted", "1"),
                    })
        except Exception:
            pass
    rows.sort(key=lambda r: r["ts"], reverse=True)
    limit = request.args.get("limit", 500, type=int)
    return jsonify(rows[:limit])


@app.route("/api/company-jobs")
def api_company_jobs():
    """Return company applications for the current profile, most-recent first."""
    rows = []
    if EXT_CSV.exists():
        try:
            with open(EXT_CSV) as f:
                for row in csv.DictReader(f):
                    if not any(row.values()):
                        continue
                    if row.get("profile_email", PROFILE_EMAIL) != PROFILE_EMAIL:
                        continue
                    status = row.get("status", "")
                    if "skipped" in status:
                        continue
                    ts = row.get("timestamp", "")
                    rows.append({
                        "date":    ts[:10] if ts else "",
                        "time":    ts[11:16] if len(ts) > 10 else "",
                        "company": row.get("company", ""),
                        "ats":     row.get("ats", ""),
                        "title":   row.get("job_title", ""),
                        "location":row.get("location", ""),
                        "url":     row.get("job_url", ""),
                        "status":  status,
                    })
        except Exception:
            pass
    rows.reverse()
    limit = request.args.get("limit", 500, type=int)
    return jsonify(rows[:limit])


@app.route("/api/jobs")
def api_jobs():
    """Return applied jobs for the current profile, most-recent first."""
    per_csv = _dice_session_dir(PROFILE_EMAIL) / "applied_jobs.csv"
    csv_path = per_csv if per_csv.exists() else APPLIED_CSV
    rows = []
    if csv_path.exists():
        try:
            with open(csv_path) as f:
                for row in csv.DictReader(f):
                    s = row.get("status", "")
                    if "skipped" in s:
                        continue
                    ts = row.get("timestamp", "")
                    rows.append({
                        "ts":      ts,
                        "date":    ts[:10] if ts else "",
                        "time":    ts[11:16] if len(ts) > 10 else "",
                        "title":   row.get("job_title", ""),
                        "company": row.get("company", ""),
                        "url":     row.get("job_url", ""),
                        "status":  s,
                    })
        except Exception:
            pass
    rows.reverse()
    limit = request.args.get("limit", 200, type=int)
    return jsonify(rows[:limit])


@app.route("/api/handshake-jobs")
def api_handshake_jobs():
    rows = []
    if HANDSHAKE_CSV.exists():
        try:
            with HANDSHAKE_CSV.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
        except Exception:
            rows = []
    limit = request.args.get("limit", 200, type=int)
    return jsonify(list(reversed(rows[-limit:])))


@app.route("/api/linkedin-applied-jobs")
def api_linkedin_applied_jobs():
    rows = []
    if LINKEDIN_APPLIED_CSV.exists():
        try:
            with LINKEDIN_APPLIED_CSV.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
        except Exception:
            rows = []
    limit = request.args.get("limit", 200, type=int)
    return jsonify(list(reversed(rows[-limit:])))


@app.route("/api/stats")
def api_stats():
    stats = dict(dice_applied=0, dice_errors=0,
                 company_applied=0, company_errors=0,
                 recruiters_total=0, recruiters_replied=0, interviews=0)

    # Dice — per-profile CSV first, fallback to global
    per_csv = _dice_session_dir(PROFILE_EMAIL) / "applied_jobs.csv"
    for csv_path in ([per_csv] if per_csv.exists() else [APPLIED_CSV]):
        if not csv_path.exists():
            continue
        try:
            with open(csv_path) as f:
                for row in csv.DictReader(f):
                    s = row.get("status", "")
                    if s == "applied":
                        stats["dice_applied"] += 1
                    elif s.startswith("error"):
                        stats["dice_errors"] += 1
        except Exception:
            pass

    # Companies
    if EXT_CSV.exists():
        try:
            with open(EXT_CSV) as f:
                for row in csv.DictReader(f):
                    if row.get("profile_email", PROFILE_EMAIL) != PROFILE_EMAIL:
                        continue
                    s = row.get("status", "")
                    if s == "applied":
                        stats["company_applied"] += 1
                    elif s.startswith("error"):
                        stats["company_errors"] += 1
        except Exception:
            pass

    # Recruiters
    if RECRUITERS_CSV.exists():
        try:
            with open(RECRUITERS_CSV) as f:
                for row in csv.DictReader(f):
                    stats["recruiters_total"] += 1
                    ls = row.get("last_status", "")
                    if ls in ("replied", "interview"):
                        stats["recruiters_replied"] += 1
                    if ls == "interview":
                        stats["interviews"] += 1
        except Exception:
            pass

    return jsonify(stats)


@app.route("/api/activity")
def api_activity():
    lines = []
    with _lock:
        for feature in FEATURES:
            for entry in list(LOG_BUFFERS[feature])[-15:]:
                if entry["line"] and not entry["line"].startswith("__"):
                    lines.append({**entry, "feature": feature})
    lines.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify(lines[:50])


@app.route("/api/errors")
def api_errors():
    errors = []
    with _lock:
        for feature in FEATURES:
            for entry in list(LOG_BUFFERS[feature]):
                l = entry["line"].lower()
                if any(w in l for w in ["error", "✗", "traceback", "exception", "failed", "✘"]):
                    errors.append({**entry, "feature": feature})
    errors.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify(errors[:20])


@app.route("/api/logs/<feature>")
def api_logs(feature):
    if feature not in LOG_BUFFERS:
        return jsonify([])
    with _lock:
        return jsonify(list(LOG_BUFFERS[feature]))


@app.route("/api/stream/<feature>")
def api_stream(feature):
    if feature not in LOG_BUFFERS:
        return Response("", status=404)

    q = queue.Queue()
    with _lock:
        existing = list(LOG_BUFFERS[feature])
        SSE_SUBSCRIBERS[feature].append(q)
    for entry in existing:
        q.put(entry)

    def generate():
        try:
            while True:
                try:
                    entry = q.get(timeout=25)
                    yield f"data: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _lock:
                try:
                    SSE_SUBSCRIBERS[feature].remove(q)
                except ValueError:
                    pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/api/run/<feature>", methods=["POST"])
def api_run(feature):
    body = request.json or {}
    _log.fn("api_run", feature=feature, body_keys=list(body.keys()))
    if feature == "linkedin_outreach":
        all_config = json.loads(LI_CONFIG_JSON.read_text()) if LI_CONFIG_JSON.exists() else {}
        config = all_config.setdefault(PROFILE_EMAIL, {})
        if body.get("keywords"):
            config["search_keywords"] = body["keywords"].split()
        for key in ("job_types", "target_roles", "experience_levels", "locations"):
            if body.get(key):
                config[key] = body[key]
        if body.get("date") is not None:
            config["date_filter"] = body["date"]
        config.setdefault("sender_email", PROFILE_EMAIL)
        config["updated_at"] = datetime.now().isoformat(timespec="seconds")
        LI_CONFIG_JSON.write_text(json.dumps(all_config, indent=2))
    if feature in {"dice_apply", "companies_apply"} and body.get("experience_levels"):
        profiles = json.loads(PROFILES_JSON.read_text()) if PROFILES_JSON.exists() else {}
        profile = profiles.setdefault(PROFILE_EMAIL, {})
        profile["experience_levels"] = body["experience_levels"]
        if body.get("max_required_years") is not None:
            profile["max_required_years"] = max(
                0, min(4, int(body["max_required_years"]))
            )
        PROFILES_JSON.write_text(json.dumps(profiles, indent=2))
    with _lock:
        p = PROCESSES.get(feature)
        if p and p.poll() is None:
            _log.warn(f"api_run: {feature} already running  pid={p.pid}")
            return jsonify({"ok": False, "error": "already running"})

    if feature == "gmail_monitor":
        cmd = [sys.executable, "gmail_monitor.py", "--profile", PROFILE_EMAIL]
    elif feature == "linkedin_outreach":
        runner = "linkedin_apply.py" if body.get("auto_apply") else "linkedin_outreach.py"
        cmd = [sys.executable, runner, "--profile", PROFILE_EMAIL]
        if body.get("login"):
            cmd += ["--login"]
            runner = "linkedin_apply.py"
            cmd = [sys.executable, runner, "--profile", PROFILE_EMAIL, "--login"]
            body = {}
        if body.get("keywords"):
            cmd += ["--keywords", body["keywords"]] if body.get("auto_apply") else ["--keywords"] + body["keywords"].split()
        if body.get("date") and not body.get("auto_apply"):
            cmd += ["--date", body["date"]]
        if body.get("job_types") and not body.get("auto_apply"):
            job_types = body["job_types"] if isinstance(body["job_types"], list) else body["job_types"].split()
            cmd += ["--job-types"] + job_types
        if body.get("target_roles") and not body.get("auto_apply"):
            cmd += ["--target-roles"] + body["target_roles"]
        if body.get("experience_levels"):
            levels = body["experience_levels"]
            cmd += ["--experience-levels", ",".join(levels) if isinstance(levels, list) else str(levels)]
        if body.get("locations") and not body.get("auto_apply"):
            cmd += ["--locations"] + body["locations"]
    elif feature == "linkedin_apply":
        cmd = [sys.executable, "linkedin_apply.py", "--profile", PROFILE_EMAIL]
        if body.get("login"):
            cmd += ["--login"]
        if body.get("keywords"):
            cmd += ["--keywords", body["keywords"]]
        if body.get("experience_levels"):
            cmd += ["--experience-levels", ",".join(body["experience_levels"])]
        if body.get("max_required_years") is not None:
            cmd += ["--max-required-years", str(body["max_required_years"])]
    elif feature == "dice_apply":
        if body.get("login"):
            cmd = [sys.executable, "main.py", "--login", "--profile", PROFILE_EMAIL]
        else:
            cmd = [sys.executable, "main.py", "--profile", PROFILE_EMAIL]
            if body.get("query"):
                cmd += ["--query", body["query"]]
            if body.get("date"):
                cmd += ["--date", body["date"]]
            if body.get("easy_apply") is not None:
                cmd += ["--easy-apply", str(body["easy_apply"]).lower()]
            if body.get("experience_levels"):
                cmd += ["--experience-levels", ",".join(body["experience_levels"])]
            if body.get("max_required_years") is not None:
                cmd += ["--max-required-years", str(body["max_required_years"])]
    elif feature == "handshake_apply":
        cmd = [sys.executable, "handshake_apply.py", "--profile", PROFILE_EMAIL]
        if body.get("login"):
            cmd += ["--login"]
        if body.get("keywords"):
            cmd += ["--keywords", body["keywords"]]
        if body.get("experience_levels"):
            cmd += ["--experience-levels", ",".join(body["experience_levels"])]
        if body.get("max_required_years") is not None:
            cmd += ["--max-required-years", str(body["max_required_years"])]
    elif feature == "companies_apply":
        companies = body.get("companies", [])
        _log.var("companies_selected", companies)
        if not companies:
            _log.warn("api_run companies_apply: no companies selected")
            return jsonify({"ok": False, "error": "select at least one company"})

        if body.get("login"):
            if len(companies) != 1:
                return jsonify({"ok": False, "error": "select one Workday company to connect"})
            database = json.loads(COMPANY_DB_JSON.read_text()) if COMPANY_DB_JSON.exists() else {}
            record = next((item for item in database.values()
                           if isinstance(item, dict) and item.get("name") == companies[0]), {})
            if record.get("ats") != "workday":
                return jsonify({"ok": False, "error": "this company does not require a Workday login"})
            cmd = [sys.executable, "company_apply.py", "--company", companies[0],
                   "--profile", PROFILE_EMAIL, "--setup-workday"]
        else:

            # Build shared filter args
            filter_args: list[str] = ["--profile", PROFILE_EMAIL]
            keywords   = body.get("keywords", "").strip()
            location   = body.get("location", "").strip()
            date       = body.get("date", "").strip()
            experience_levels = body.get("experience_levels", [])
            experience = ",".join(experience_levels) if experience_levels else body.get("experience", "").strip()
            max_required_years = body.get("max_required_years")
            if keywords:
                filter_args += ["--keywords", keywords]
            else:
                filter_args += ["--all-roles"]
            if location:
                filter_args += ["--location", location]
            if date:
                filter_args += ["--posted-days", date]
            if experience:
                filter_args += ["--experience", experience]
            if max_required_years is not None:
                filter_args += ["--max-required-years", str(max_required_years)]

            # For a single company use a plain list; for multiple, chain via bash -c
            if len(companies) == 1:
                cmd = [sys.executable, "company_apply.py",
                       "--company", companies[0]] + filter_args
            else:
                parts = []
                for c in companies:
                    args = [sys.executable, "company_apply.py", "--company", c] + filter_args
                    parts.append(" ".join(shlex.quote(a) for a in args))
                cmd = ["bash", "-c", " && ".join(parts)]
    else:
        return jsonify({"ok": False, "error": "unknown feature"})

    _log.var("cmd", cmd)
    try:
        proc_env = os.environ.copy()
        proc_env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            cmd, cwd=str(_HERE),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=proc_env
        )
        with _lock:
            PROCESSES[feature] = proc
            LOG_BUFFERS[feature].clear()   # fresh log for each new run
        threading.Thread(target=_read_output, args=(feature, proc), daemon=True).start()
        _log.ok(f"Launched {feature}  pid={proc.pid}  cmd={' '.join(cmd[:4])}...")
        _push_log(feature, f"▶ Started PID {proc.pid}  [{' '.join(cmd)}]")
        return jsonify({"ok": True, "pid": proc.pid})
    except Exception as e:
        _log.err(f"Failed to launch {feature}", exc=e)
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/stop/<feature>", methods=["POST"])
def api_stop(feature):
    _log.fn("api_stop", feature=feature)
    with _lock:
        proc = PROCESSES.get(feature)
    if not proc or proc.poll() is not None:
        _log.warn(f"api_stop: {feature} not running")
        return jsonify({"ok": False, "error": "not running"})
    _log.info(f"Terminating {feature}  pid={proc.pid}")
    proc.terminate()
    _push_log(feature, "■ Process terminated by user")
    return jsonify({"ok": True})


@app.route("/api/linkedin-config")
def api_linkedin_config():
    if not LI_CONFIG_JSON.exists():
        return jsonify({})
    all_cfg = json.loads(LI_CONFIG_JSON.read_text())
    # Support both old flat format and new per-email keyed format
    cfg = all_cfg.get(PROFILE_EMAIL, all_cfg if isinstance(all_cfg, dict) and "search_keywords" in all_cfg else {})
    return jsonify({
        "keywords":  " ".join(cfg.get("search_keywords", ["gen ai"])),
        "job_types": cfg.get("job_types", ["OPT", "W2"]),
        "target_roles": ", ".join(cfg.get("target_roles", [])),
        "experience_levels": cfg.get("experience_levels", ["junior"]),
        "locations": cfg.get("locations", []),
        "date":      cfg.get("date_filter", "past-week"),
    })


def _load_applicable_names() -> list[str]:
    """Return sorted list of applicable company names from company_careers_db.json.
    Falls back to applicable_companies.json if the DB is missing."""
    _log.fn("_load_applicable_names")
    if COMPANY_DB_JSON.exists():
        try:
            raw = json.loads(COMPANY_DB_JSON.read_text())
            _log.var("db_entries_total", len(raw))
            names = sorted(
                v["name"] for k, v in raw.items()
                if isinstance(v, dict) and v.get("status", "active") == "active" and "name" in v
            )
            _log.var("applicable_names_from_db", len(names))
            if names:
                _log.ok(f"Loaded {len(names)} applicable companies from DB")
                return names
            _log.warn("DB exists but no active companies found")
        except Exception as exc:
            _log.err("Failed to load company_careers_db.json", exc=exc)
    if COMPANIES_JSON.exists():
        _log.warn("Falling back to applicable_companies.json")
        names = json.loads(COMPANIES_JSON.read_text())
        _log.var("fallback_names_count", len(names))
        return names
    _log.null("applicable_names", reason="neither DB nor fallback JSON found")
    return []


@app.route("/api/companies")
def api_companies():
    return jsonify(_load_applicable_names())


@app.route("/api/company-groups")
def api_company_groups():
    available = _load_applicable_names()
    database = json.loads(COMPANY_DB_JSON.read_text()) if COMPANY_DB_JSON.exists() else {}
    supported = [item.get("name") for item in database.values()
                 if isinstance(item, dict) and item.get("name") in available
                 and item.get("ats") != "generic"]
    groups = {
        "FAANG": ["Meta", "Amazon", "Apple", "Netflix", "Google"],
        "MANGOS": ["Meta", "Anthropic", "NVIDIA", "Google", "OpenAI", "SpaceX"],
        "All Configured": available,
    }
    return jsonify({name: {"members": companies,
                           "available": [company for company in companies if company in available],
                           "auto_apply": [company for company in companies if company in supported]}
                    for name, companies in groups.items()})


@app.route("/api/resumes")
def api_resumes():
    data = json.loads(RESUMES_JSON.read_text()) if RESUMES_JSON.exists() else {}
    entry = data.get(PROFILE_EMAIL, {})
    folder = Path(entry.get("resume_folder", "")).expanduser()
    files = (sorted(p.name for p in folder.iterdir()
                    if p.suffix.lower() in {".pdf", ".docx"})
             if folder.is_dir() else [])
    return jsonify({"files": files, "default": Path(entry.get("default_resume", "")).name})


@app.route("/api/resumes/upload", methods=["POST"])
def api_resumes_upload():
    uploads = request.files.getlist("resumes")
    folder = _HERE / "resumes" / _safe(PROFILE_EMAIL)
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for upload in uploads:
        filename = secure_filename(upload.filename or "")
        if Path(filename).suffix.lower() not in {".pdf", ".docx"}:
            continue
        upload.save(folder / filename)
        saved.append(filename)
    if not saved:
        return jsonify({"ok": False, "error": "Select PDF or DOCX resumes"})
    data = json.loads(RESUMES_JSON.read_text()) if RESUMES_JSON.exists() else {}
    entry = data.setdefault(PROFILE_EMAIL, {})
    entry["resume_folder"] = str(folder)
    if request.form.get("set_default") == "true" or not entry.get("default_resume"):
        entry["default_resume"] = str(folder / saved[0])
    RESUMES_JSON.write_text(json.dumps(data, indent=2))
    return jsonify({"ok": True, "files": saved})


@app.route("/api/resumes/default", methods=["POST"])
def api_resume_default():
    filename = secure_filename((request.json or {}).get("filename", ""))
    data = json.loads(RESUMES_JSON.read_text()) if RESUMES_JSON.exists() else {}
    entry = data.setdefault(PROFILE_EMAIL, {})
    path = Path(entry.get("resume_folder", "")).expanduser() / filename
    if not path.is_file():
        return jsonify({"ok": False, "error": "Resume not found"})
    entry["default_resume"] = str(path)
    RESUMES_JSON.write_text(json.dumps(data, indent=2))
    return jsonify({"ok": True})


@app.route("/api/profiles")
def api_profiles():
    if not PROFILES_JSON.exists():
        return jsonify([])
    profiles = json.loads(PROFILES_JSON.read_text())
    return jsonify([{"email": e, "name": d.get("name", e)} for e, d in profiles.items()])


@app.route("/setup")
def setup_page():
    return render_template("setup.html", profile_email=PROFILE_EMAIL)


@app.route("/api/setup/status")
def api_setup_status():
    profiles = json.loads(PROFILES_JSON.read_text()) if PROFILES_JSON.exists() else {}
    resumes  = json.loads(RESUMES_JSON.read_text())  if RESUMES_JSON.exists()  else {}
    p = profiles.get(PROFILE_EMAIL, {})
    r = resumes.get(PROFILE_EMAIL, {})
    resume_folder = r.get("resume_folder", "")
    has_resume = False
    if resume_folder and Path(resume_folder).exists():
        has_resume = bool(list(Path(resume_folder).glob("*.docx")) + list(Path(resume_folder).glob("*.pdf")))
    return jsonify({
        "profile": bool(p.get("name") and p.get("phone")),
        "resume":  has_resume,
        "gmail":   (_HERE / "gmail_credentials.json").exists(),
        "email":   PROFILE_EMAIL,
    })


@app.route("/api/setup/profile", methods=["POST"])
def api_setup_profile():
    global PROFILE_NAME
    data = request.json or {}
    profiles = json.loads(PROFILES_JSON.read_text()) if PROFILES_JSON.exists() else {}
    resumes  = json.loads(RESUMES_JSON.read_text())  if RESUMES_JSON.exists()  else {}
    profile = profiles.get(PROFILE_EMAIL, {})
    profile.update({
        "name":               data.get("name", ""),
        "phone":              data.get("phone", ""),
        "location":           data.get("location", ""),
        "work_auth":          data.get("work_auth", ""),
        "years_experience":   data.get("years_experience", 0),
        "needs_sponsorship":  data.get("needs_sponsorship", False),
        "open_to_relocation": data.get("open_to_relocation", False),
        "skills":             data.get("skills", ""),
        "linkedin_url":       data.get("linkedin_url", ""),
        "github_url":         data.get("github_url", ""),
        "portfolio_url":      data.get("portfolio_url", ""),
        "summary":            data.get("summary", ""),
    })
    profiles[PROFILE_EMAIL] = profile
    resumes.setdefault(PROFILE_EMAIL, {})
    PROFILES_JSON.write_text(json.dumps(profiles, indent=2))
    RESUMES_JSON.write_text(json.dumps(resumes, indent=2))
    PROFILE_NAME = data.get("name", PROFILE_EMAIL)
    return jsonify({"ok": True})


@app.route("/api/setup/resume", methods=["POST"])
def api_setup_resume():
    f = request.files.get("resume")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file uploaded"})
    resume_dir = _HERE / "resumes" / re.sub(r"[^a-z0-9]", "_", PROFILE_EMAIL.lower())
    resume_dir.mkdir(parents=True, exist_ok=True)
    dest = resume_dir / f.filename
    f.save(str(dest))
    resumes = json.loads(RESUMES_JSON.read_text()) if RESUMES_JSON.exists() else {}
    resumes.setdefault(PROFILE_EMAIL, {})
    resumes[PROFILE_EMAIL]["resume_folder"]  = str(resume_dir)
    resumes[PROFILE_EMAIL]["default_resume"] = f.filename
    RESUMES_JSON.write_text(json.dumps(resumes, indent=2))
    return jsonify({"ok": True, "filename": f.filename})


@app.route("/api/setup/gmail-creds", methods=["POST"])
def api_setup_gmail_creds():
    f = request.files.get("creds")
    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"})
    try:
        raw = f.read()
        parsed = json.loads(raw)
        if "installed" not in parsed and "web" not in parsed:
            return jsonify({"ok": False, "error": "Not a valid Gmail OAuth credentials file"})
        (_HERE / "gmail_credentials.json").write_bytes(raw)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/setup/complete", methods=["POST"])
def api_setup_complete():
    global SETUP_REQUIRED
    SETUP_REQUIRED = False
    return jsonify({"ok": True})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Job automation web dashboard")
    ap.add_argument("--profile", required=True, help="Profile name or email")
    ap.add_argument("--port",    type=int, default=5050, help="Port (default 5050)")
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = ap.parse_args()

    PROFILE_EMAIL, PROFILE_NAME = _resolve_profile(args.profile)
    _log.session_start(script="web_dashboard.py", profile=PROFILE_EMAIL)
    _log.var("port", args.port)
    _log.var("profile_name", PROFILE_NAME)
    _log.var("setup_required", SETUP_REQUIRED)
    print(f"\n  Job Dashboard")
    print(f"  Profile : {PROFILE_NAME} <{PROFILE_EMAIL}>")
    print(f"  URL     : http://localhost:{args.port}")
    print(f"  Stop    : Ctrl+C\n")

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    app.run(host="0.0.0.0", port=args.port, debug=False,
            threaded=True, use_reloader=False)
