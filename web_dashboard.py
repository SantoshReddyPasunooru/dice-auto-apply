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
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

# ── Paths ──────────────────────────────────────────────────────────────────────
_HERE           = Path(__file__).parent
PROFILES_JSON   = _HERE / "profiles.json"
RESUMES_JSON    = _HERE / "resumes.json"
APPLIED_CSV     = _HERE / "applied_jobs.csv"
EXT_CSV         = _HERE / "external_applied_jobs.csv"
RECRUITERS_CSV  = _HERE / "recruiters.csv"
COMPANIES_JSON  = _HERE / "applicable_companies.json"
LI_CONFIG_JSON  = _HERE / "linkedin_config.json"

app = Flask(__name__)

# ── Global state ───────────────────────────────────────────────────────────────
PROFILE_EMAIL: str = ""
PROFILE_NAME:  str = ""

FEATURES = ["gmail_monitor", "linkedin_outreach", "dice_apply", "companies_apply"]

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
    if not PROFILES_JSON.exists():
        sys.exit("profiles.json not found")
    profiles = json.loads(PROFILES_JSON.read_text())
    if arg in profiles:
        return arg, profiles[arg].get("name", arg)
    for email, data in profiles.items():
        if data.get("name", "").lower().startswith(arg.lower()):
            return email, data.get("name", email)
    sys.exit(f"Profile '{arg}' not found in profiles.json")

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
    try:
        for raw in proc.stdout:
            _push_log(feature, raw.rstrip("\n"))
    except Exception:
        pass
    finally:
        with _lock:
            if PROCESSES.get(feature) is proc:
                PROCESSES.pop(feature, None)
        _push_log(feature, "__PROCESS_ENDED__")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html",
                           profile_email=PROFILE_EMAIL,
                           profile_name=PROFILE_NAME)


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

    # Dice session (shared playwright profile dir)
    dice_dir = Path.home() / ".dice-playwright-profile"
    dice_connected = dice_dir.exists() and any(
        f.suffix in (".json", ".sqlite") for f in dice_dir.rglob("*") if f.is_file()
    )
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
                    rows.append({
                        "date":     row.get("last_seen", "")[:10],
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
    rows.sort(key=lambda r: r["date"], reverse=True)
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
    with _lock:
        p = PROCESSES.get(feature)
        if p and p.poll() is None:
            return jsonify({"ok": False, "error": "already running"})

    if feature == "gmail_monitor":
        cmd = [sys.executable, "gmail_monitor.py", "--profile", PROFILE_EMAIL]
    elif feature == "linkedin_outreach":
        cmd = [sys.executable, "linkedin_outreach.py", "--profile", PROFILE_EMAIL]
        if body.get("keywords"):
            cmd += ["--keywords"] + body["keywords"].split()
        if body.get("date"):
            cmd += ["--date", body["date"]]
        if body.get("job_types"):
            cmd += ["--job-types"] + body["job_types"].split()
    elif feature == "dice_apply":
        cmd = [sys.executable, "main.py", "--profile", PROFILE_EMAIL]
        if body.get("query"):
            cmd += ["--query", body["query"]]
        if body.get("date"):
            cmd += ["--date", body["date"]]
        if body.get("easy_apply") is not None:
            cmd += ["--easy-apply", str(body["easy_apply"]).lower()]
    elif feature == "companies_apply":
        company = body.get("company", "")
        if not company:
            return jsonify({"ok": False, "error": "select a company first"})
        cmd = [sys.executable, "company_apply.py",
               "--company", company,
               "--profile", PROFILE_EMAIL,
               "--all-roles"]
    else:
        return jsonify({"ok": False, "error": "unknown feature"})

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(_HERE),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=os.environ.copy()
        )
        with _lock:
            PROCESSES[feature] = proc
            LOG_BUFFERS[feature].clear()   # fresh log for each new run
        threading.Thread(target=_read_output, args=(feature, proc), daemon=True).start()
        _push_log(feature, f"▶ Started PID {proc.pid}  [{' '.join(cmd)}]")
        return jsonify({"ok": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/stop/<feature>", methods=["POST"])
def api_stop(feature):
    with _lock:
        proc = PROCESSES.get(feature)
    if not proc or proc.poll() is not None:
        return jsonify({"ok": False, "error": "not running"})
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
        "job_types": " ".join(cfg.get("job_types", ["OPT", "W2"])),
        "date":      cfg.get("date_filter", "past-week"),
    })


@app.route("/api/companies")
def api_companies():
    if COMPANIES_JSON.exists():
        return jsonify(json.loads(COMPANIES_JSON.read_text()))
    return jsonify([])


@app.route("/api/profiles")
def api_profiles():
    if not PROFILES_JSON.exists():
        return jsonify([])
    profiles = json.loads(PROFILES_JSON.read_text())
    return jsonify([{"email": e, "name": d.get("name", e)} for e, d in profiles.items()])


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Job automation web dashboard")
    ap.add_argument("--profile", required=True, help="Profile name or email")
    ap.add_argument("--port",    type=int, default=5050, help="Port (default 5050)")
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = ap.parse_args()

    PROFILE_EMAIL, PROFILE_NAME = _resolve_profile(args.profile)
    print(f"\n  Job Dashboard")
    print(f"  Profile : {PROFILE_NAME} <{PROFILE_EMAIL}>")
    print(f"  URL     : http://localhost:{args.port}")
    print(f"  Stop    : Ctrl+C\n")

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    app.run(host="0.0.0.0", port=args.port, debug=False,
            threaded=True, use_reloader=False)
