"""
Gmail Monitor — automated inbox watcher and recruiter reply bot
===============================================================
Monitors multiple Gmail inboxes in parallel, classifies recruiter
replies with Ollama, auto-replies within 60-120 s, archives junk,
and applies Gmail labels. Live Rich terminal dashboard.

Commands:
  python gmail_monitor.py                        — monitor all profiles
  python gmail_monitor.py --profile email@gmail  — monitor one profile
"""

import asyncio
import base64
import json
import random
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import gmail_sender

load_dotenv()

_HERE         = Path(__file__).parent
PROFILES_JSON = _HERE / "profiles.json"
RESUMES_JSON  = _HERE / "resumes.json"
OLLAMA_MODEL  = "gemma2:2b"

POLL_MIN = 60    # seconds between inbox polls
POLL_MAX = 120
REPLY_DELAY_MIN = 15   # human-like pause before sending reply
REPLY_DELAY_MAX = 45


# ── Per-profile stats ─────────────────────────────────────────────────────────

@dataclass
class ProfileStats:
    email:      str
    name:       str  = ""
    status:     str  = "starting..."
    last_check: str  = ""
    new:        int  = 0
    replied:    int  = 0
    archived:   int  = 0
    interviews: int  = 0
    errors:     int  = 0


# ── Shared state (all profiles write here, dashboard reads) ───────────────────

_stats:    dict[str, ProfileStats] = {}
_activity: deque                   = deque(maxlen=40)
_lock      = asyncio.Lock()


def _log(tag: str, icon: str, action: str, detail: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    _activity.appendleft(f"[dim]{ts}[/dim]  [{tag}]  {icon} {action}  [dim]{detail}[/dim]")


# ── Gmail API helpers ─────────────────────────────────────────────────────────

def _list_unread(svc) -> list[dict]:
    result = svc.users().messages().list(
        userId="me", q="is:unread in:inbox", maxResults=25
    ).execute()
    return result.get("messages", [])


def _get_full_message(svc, msg_id: str) -> tuple[str, str, str, str, str]:
    """Returns (from_raw, from_email, subject, body_text, thread_id)."""
    msg = svc.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    headers   = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    from_raw  = headers.get("From", "")
    subject   = headers.get("Subject", "")
    thread_id = msg.get("threadId", "")

    m = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_raw)
    from_email = m.group().lower() if m else from_raw.lower()

    body = ""
    def _extract(payload):
        nonlocal body
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                body += base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            _extract(part)

    _extract(msg["payload"])
    if not body:
        body = msg.get("snippet", "")

    return from_raw, from_email, subject, body[:1500], thread_id


def _mark_read(svc, msg_id: str):
    svc.users().messages().modify(
        userId="me", id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def _archive(svc, msg_id: str):
    svc.users().messages().modify(
        userId="me", id=msg_id,
        body={"removeLabelIds": ["INBOX"]}
    ).execute()


_label_cache: dict[str, str] = {}

def _apply_label(svc, msg_id: str, name: str):
    if name not in _label_cache:
        existing = svc.users().labels().list(userId="me").execute().get("labels", [])
        match = next((l for l in existing if l["name"].lower() == name.lower()), None)
        if match:
            _label_cache[name] = match["id"]
        else:
            new = svc.users().labels().create(
                userId="me",
                body={"name": name,
                      "labelListVisibility":   "labelShow",
                      "messageListVisibility": "show"}
            ).execute()
            _label_cache[name] = new["id"]
    svc.users().messages().modify(
        userId="me", id=msg_id,
        body={"addLabelIds": [_label_cache[name]]}
    ).execute()


def _send_thread_reply(svc, sender_email: str, to_raw: str,
                       subject: str, body: str, thread_id: str,
                       resume: Optional[Path] = None) -> bool:
    try:
        msg = gmail_sender.GmailSender._compose_cold_msg(
            to_raw, sender_email, f"Re: {subject}", body, resume
        )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id}
        ).execute()
        return True
    except Exception as e:
        return False


# ── Ollama — email classifier ─────────────────────────────────────────────────

def _classify_sync(from_raw: str, subject: str, body: str) -> dict:
    """
    Returns dict with keys: category, reason, wants_resume, is_interview.
    category: INTERVIEW | INFO_REQUEST | REPLY_NEEDED | JUNK
    """
    # Fast keyword shortcut before hitting Ollama
    combined = (subject + " " + body).lower()
    if any(w in combined for w in ["auto-reply", "out of office", "no-reply",
                                    "noreply", "job alert", "unsubscribe",
                                    "do not reply", "donotreply", "automatic reply"]):
        return {"category": "JUNK", "reason": "auto-reply detected",
                "wants_resume": False, "is_interview": False}

    try:
        import ollama
        prompt = (
            "Classify this recruiter email reply. Reply with ONLY valid JSON.\n\n"
            f"From: {from_raw}\nSubject: {subject}\nBody:\n{body[:600]}\n\n"
            "Categories:\n"
            "- INTERVIEW : wants to schedule interview / call / meeting\n"
            "- INFO_REQUEST : asking for resume, availability, work auth, rate\n"
            "- REPLY_NEEDED : interested, asking questions, positive response\n"
            "- JUNK : auto-reply, out-of-office, notification, unsubscribe\n\n"
            'Reply: {"category":"REPLY_NEEDED","reason":"short reason",'
            '"wants_resume":false,"is_interview":false}'
        )
        resp = ollama.chat(model=OLLAMA_MODEL, messages=[{"role": "user", "content": prompt}])
        raw  = resp.message.content.strip()
        raw  = re.sub(r"^```[a-z]*\s*", "", raw, flags=re.MULTILINE)
        raw  = re.sub(r"```$",           "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        return {
            "category":     str(data.get("category", "REPLY_NEEDED")),
            "reason":       str(data.get("reason", "")),
            "wants_resume": bool(data.get("wants_resume", False)),
            "is_interview": bool(data.get("is_interview", False)),
        }
    except Exception:
        # Keyword fallback
        if any(w in combined for w in ["interview", "schedule", "call", "meet",
                                        "zoom", "teams", "google meet", "calendly"]):
            return {"category": "INTERVIEW", "reason": "interview keywords",
                    "wants_resume": False, "is_interview": True}
        if any(w in combined for w in ["resume", "cv", "availability",
                                        "work auth", "visa", "rate", "hourly"]):
            return {"category": "INFO_REQUEST", "reason": "info request keywords",
                    "wants_resume": True, "is_interview": False}
        return {"category": "REPLY_NEEDED", "reason": "recruiter reply",
                "wants_resume": False, "is_interview": False}


# ── Ollama — reply generator ──────────────────────────────────────────────────

def _generate_reply_sync(recruiter_body: str, subject: str,
                         profile: dict, clf: dict) -> str:
    category  = clf.get("category", "REPLY_NEEDED")
    name      = profile.get("name", "Applicant")
    work_auth = profile.get("work_auth", "OPT")
    skills    = (profile.get("skills", "") or "")[:100]
    location  = profile.get("location", "")
    available = profile.get("available_to_start", "immediately")
    years     = profile.get("years_experience", 3)
    title     = profile.get("current_title", "Software Engineer")

    if category == "INTERVIEW":
        ctx = "The recruiter wants to schedule an interview or introductory call."
    elif category == "INFO_REQUEST":
        ctx = "The recruiter is asking for more details: resume, availability, work auth, or rate."
    else:
        ctx = "The recruiter is following up or expressing interest."

    try:
        import ollama
        prompt = (
            f"Write a short professional reply to this recruiter email.\n\n"
            f"Situation: {ctx}\n"
            f"Recruiter's email:\n{recruiter_body[:500]}\n\n"
            f"Candidate:\n"
            f"  Name: {name} | Title: {title}\n"
            f"  Experience: {years} yrs | Skills: {skills}\n"
            f"  Location: {location} | Work auth: {work_auth}\n"
            f"  Available: {available}\n\n"
            f"Rules:\n"
            f"- 3-5 sentences max, no filler phrases\n"
            f"- INTERVIEW: confirm strong interest, ask for available time slots\n"
            f"- INFO_REQUEST: provide the requested info clearly\n"
            f"- Always mention work auth status ({work_auth}) if relevant\n"
            f"- Close with: Best regards,\\n{name}\n"
            f"- Output the email body ONLY — no subject line\n"
        )
        resp = ollama.chat(model=OLLAMA_MODEL,
                           messages=[{"role": "user", "content": prompt}])
        body = resp.message.content.strip()
        body = re.sub(r"(?i)^subject\s*:.*\n?", "", body).strip()
        return body
    except Exception:
        if category == "INTERVIEW":
            return (
                f"Hi,\n\nThank you for reaching out! I'm very excited about this opportunity "
                f"and would love to connect.\n\n"
                f"I'm available for a call this week — please share a few time slots and "
                f"I'll confirm immediately. I'm on {work_auth} and can start {available}.\n\n"
                f"Looking forward to speaking with you.\n\nBest regards,\n{name}"
            )
        return (
            f"Hi,\n\nThank you for your response — I'm very interested in this role.\n\n"
            f"I have {years} years of experience in {skills[:60]}, currently based in "
            f"{location}. I'm on {work_auth} and available to start {available}. "
            f"Happy to share any additional details you need.\n\n"
            f"Best regards,\n{name}"
        )


# ── Core monitor loop (one per profile) ──────────────────────────────────────

async def _monitor_profile(email: str, profile_data: dict,
                           gs: gmail_sender.GmailSender):
    tag   = email.split("@")[0][:10]
    stats = _stats[email]
    stats.name   = profile_data.get("name", email.split("@")[0])

    # On first poll: snapshot existing unread IDs so we don't mass-reply on start
    processed_ids: set[str] = set()
    first_run = True

    while True:
        try:
            stats.status     = "checking..."
            stats.last_check = datetime.now().strftime("%H:%M:%S")

            svc = await asyncio.to_thread(gs.get_service)
            if not svc:
                stats.status = "auth error"
                _log(tag, "✗", "Gmail auth failed")
                await asyncio.sleep(60)
                continue

            messages = await asyncio.to_thread(_list_unread, svc)

            if first_run:
                # Snapshot without processing — avoid replying to old mail
                for ref in messages:
                    processed_ids.add(ref["id"])
                first_run = False
                stats.status = "idle"
                _log(tag, "✓", "Monitor started",
                     f"{len(processed_ids)} existing unread skipped")
                delay = random.uniform(POLL_MIN, POLL_MAX)
                stats.status = f"next check in {int(delay)}s"
                await asyncio.sleep(delay)
                continue

            new_count = 0
            for ref in messages:
                msg_id = ref["id"]
                if msg_id in processed_ids:
                    continue
                processed_ids.add(msg_id)
                new_count += 1

                # Fetch full message in thread
                from_raw, from_email, subject, body, thread_id = \
                    await asyncio.to_thread(_get_full_message, svc, msg_id)

                # Classify
                clf = await asyncio.to_thread(_classify_sync, from_raw, subject, body)
                category = clf["category"]

                async with _lock:
                    if category == "JUNK":
                        await asyncio.to_thread(_archive,      svc, msg_id)
                        await asyncio.to_thread(_mark_read,    svc, msg_id)
                        await asyncio.to_thread(_apply_label,  svc, msg_id, "outreach/junk")
                        stats.archived += 1
                        _log(tag, "🗑", "Archived", f"{from_email} — {subject[:40]}")

                    else:
                        # Generate reply
                        reply_body = await asyncio.to_thread(
                            _generate_reply_sync, body, subject, profile_data, clf
                        )
                        wants_resume = clf.get("wants_resume", False)
                        resume = gs.pick_resume("software engineer") if wants_resume else None

                        # Human-like delay before sending
                        await asyncio.sleep(random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX))

                        ok = await asyncio.to_thread(
                            _send_thread_reply, svc, email,
                            from_raw, subject, reply_body, thread_id, resume
                        )

                        if ok:
                            await asyncio.to_thread(_mark_read, svc, msg_id)
                            if category == "INTERVIEW" or clf.get("is_interview"):
                                await asyncio.to_thread(_apply_label, svc, msg_id, "outreach/interview")
                                stats.interviews += 1
                                stats.replied    += 1
                                _log(tag, "🎯", "Interview reply sent", from_email)
                            else:
                                await asyncio.to_thread(_apply_label, svc, msg_id, "outreach/active")
                                stats.replied += 1
                                _log(tag, "✉", "Replied", f"{from_email} — {subject[:35]}")
                        else:
                            stats.errors += 1
                            _log(tag, "✗", "Reply failed", from_email)

            if new_count:
                stats.new += new_count

        except Exception as e:
            stats.errors += 1
            stats.status  = f"error"
            _log(tag, "✗", "Error", str(e)[:60])

        delay = random.uniform(POLL_MIN, POLL_MAX)
        stats.status = f"next check in {int(delay)}s"
        await asyncio.sleep(delay)


# ── Rich dashboard ────────────────────────────────────────────────────────────

def _render_dashboard() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )
    layout["body"].split_row(
        Layout(name="stats", ratio=2),
        Layout(name="log",   ratio=3),
    )

    # Header
    now = datetime.now().strftime("%H:%M:%S")
    layout["header"].update(Panel(
        Text(f"Gmail Monitor Dashboard  ·  {now}", justify="center", style="bold cyan"),
        style="cyan", padding=(0, 1),
    ))

    # Stats table
    tbl = Table(box=box.SIMPLE_HEAVY, expand=True,
                show_header=True, header_style="bold white on dark_blue")
    tbl.add_column("Profile",    style="cyan",         no_wrap=True)
    tbl.add_column("Status",     style="yellow",       no_wrap=True)
    tbl.add_column("New",        justify="right", style="green bold")
    tbl.add_column("Replied",    justify="right", style="blue bold")
    tbl.add_column("Archived",   justify="right", style="dim")
    tbl.add_column("Interviews", justify="right", style="magenta bold")
    tbl.add_column("Errors",     justify="right", style="red")
    tbl.add_column("Checked",    style="dim",     no_wrap=True)

    for s in _stats.values():
        name_cell = f"{s.name}\n[dim]{s.email[:26]}[/dim]" if s.name else s.email[:30]
        tbl.add_row(
            name_cell,
            s.status,
            str(s.new)        if s.new        else "[dim]0[/dim]",
            str(s.replied)    if s.replied    else "[dim]0[/dim]",
            str(s.archived)   if s.archived   else "[dim]0[/dim]",
            f"[magenta bold]{s.interviews}[/]" if s.interviews else "[dim]0[/dim]",
            f"[red]{s.errors}[/]"              if s.errors     else "[dim]0[/dim]",
            s.last_check,
        )

    layout["stats"].update(Panel(tbl, title="[bold]Profiles[/]", border_style="blue"))

    # Activity log
    log_text = "\n".join(list(_activity)[:20]) or "[dim]Waiting for activity...[/dim]"
    layout["log"].update(Panel(
        log_text, title="[bold]Activity[/]", border_style="dim",
        padding=(0, 1),
    ))

    return layout


# ── Session dir helper ────────────────────────────────────────────────────────

def _profile_session_dir(email: str) -> Path:
    safe = re.sub(r"[^a-z0-9]", "_", email.lower())
    d = Path.home() / f".dice-playwright-profile-{safe}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Entry point ───────────────────────────────────────────────────────────────

async def _run_monitor(target_emails: list[str]):
    all_profiles: dict = {}
    if PROFILES_JSON.exists():
        try:
            all_profiles = json.loads(PROFILES_JSON.read_text())
        except Exception:
            pass

    senders: dict[str, gmail_sender.GmailSender] = {}
    for email in target_emails:
        sd = _profile_session_dir(email)
        gs = gmail_sender.GmailSender()
        resume_path = ""
        if RESUMES_JSON.exists():
            try:
                rd = json.loads(RESUMES_JSON.read_text())
                resume_path = rd.get(email, {}).get("default_resume", "")
            except Exception:
                pass
        gs.init(
            profile_dir=sd,
            sender_name=all_profiles.get(email, {}).get("name", "Applicant"),
            sender_email=email,
            resume_path=resume_path,
        )
        senders[email] = gs
        _stats[email]  = ProfileStats(email=email)

    console = Console()

    async def _dashboard_loop():
        with Live(_render_dashboard(), console=console,
                  refresh_per_second=1, screen=True) as live:
            while True:
                live.update(_render_dashboard())
                await asyncio.sleep(1)

    monitor_coros = [
        _monitor_profile(email, all_profiles.get(email, {}), senders[email])
        for email in target_emails
    ]
    await asyncio.gather(*monitor_coros, _dashboard_loop())


if __name__ == "__main__":
    target: list[str] = []

    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        if idx + 1 < len(sys.argv):
            target = [sys.argv[idx + 1]]
    else:
        if PROFILES_JSON.exists():
            try:
                target = list(json.loads(PROFILES_JSON.read_text()).keys())
            except Exception:
                pass

    if not target:
        print("No profiles found. Add entries to profiles.json first.")
        sys.exit(1)

    print(f"Starting Gmail monitor for {len(target)} profile(s): {', '.join(target)}")
    print("Press Ctrl+C to stop.\n")
    try:
        asyncio.run(_run_monitor(target))
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
