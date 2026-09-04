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
import csv
import json
import random
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import gmail_sender
from recruiter_db import recruiter_db

load_dotenv()

_HERE         = Path(__file__).parent
PROFILES_JSON = _HERE / "profiles.json"
RESUMES_JSON  = _HERE / "resumes.json"
APPLIED_CSV   = _HERE / "applied_jobs.csv"
OLLAMA_MODEL  = "gemma2:2b"

POLL_MIN = 10    # seconds between inbox polls
POLL_MAX = 10
REPLY_DELAY_MIN = 15   # human-like pause before sending reply
REPLY_DELAY_MAX = 45


# ── Per-profile stats ─────────────────────────────────────────────────────────

@dataclass
class ProfileStats:
    email:             str
    name:              str = ""
    status:            str = "starting..."
    last_check:        str = ""
    next_check:        str = ""
    # ── Outbound ──────────────────────────────────────────────
    outreach_sent:     int = 0   # LinkedIn cold emails sent (from sent_emails.csv)
    dice_applied:      int = 0   # Dice.com jobs applied (from applied_jobs.csv)
    replies_sent:      int = 0   # Auto-replies sent by monitor
    # ── Inbound ───────────────────────────────────────────────
    replies_received:  int = 0   # Recruiter replies received (non-junk)
    rtrs_received:     int = 0   # Right-to-Represent requests received
    rtrs_replied:      int = 0   # RTRs we replied to
    interviews:        int = 0   # Interview requests received
    offers:            int = 0   # Job offers received
    archived:          int = 0   # Junk/auto-replies archived
    errors:            int = 0
    # ── Last event ────────────────────────────────────────────
    last_from:         str = ""
    last_subject:      str = ""
    last_action:       str = ""
    last_action_time:  str = ""
    uptime_start:      str = ""


# ── Shared state ──────────────────────────────────────────────────────────────

_stats:     dict[str, ProfileStats] = {}
_activity:  deque                   = deque(maxlen=100)
_lock       = asyncio.Lock()
_start_time = datetime.now()


def _log(tag: str, icon: str, action: str, detail: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    _activity.appendleft(
        f"[dim]{ts}[/dim]  [bold cyan]{tag:<12}[/]  {icon} [white]{action}[/]"
        + (f"  [dim]{detail[:50]}[/dim]" if detail else "")
    )


# ── Startup CSV loaders ───────────────────────────────────────────────────────

def _load_outreach_count(profile_dir: Path) -> int:
    csv_path = profile_dir / "sent_emails.csv"
    if not csv_path.exists():
        return 0
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            return max(0, sum(1 for _ in csv.DictReader(f)))
    except Exception:
        return 0


def _load_dice_count(email: str) -> int:
    if not APPLIED_CSV.exists():
        return 0
    try:
        with open(APPLIED_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        # applied_jobs.csv doesn't store which email applied,
        # so return total count (shared across all profiles is fine for display)
        return len(rows)
    except Exception:
        return 0


# ── Gmail API helpers ─────────────────────────────────────────────────────────

_CATEGORY_LABELS = [
    "INBOX",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
]

_TAB_LABEL_NAMES = {
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_SOCIAL":     "social",
    "CATEGORY_UPDATES":    "updates",
    "CATEGORY_FORUMS":     "forums",
    "INBOX":               "inbox",
}


def _list_unread(svc) -> list[dict]:
    """Collect unread messages from inbox + all Gmail tabs, last 7 days only."""
    query = (
        "is:unread newer_than:7d "
        "(in:inbox OR category:promotions OR category:social OR category:updates)"
    )
    result = svc.users().messages().list(
        userId="me", q=query, maxResults=50
    ).execute()
    return result.get("messages", [])


def _get_tab(label_ids: list[str]) -> str:
    """Return the human-readable tab name for a message's label list."""
    for lid in label_ids:
        if lid in _TAB_LABEL_NAMES and lid != "INBOX":
            return _TAB_LABEL_NAMES[lid]
    return "inbox"


def _get_sender_email(svc, msg_id: str) -> tuple[str, str]:
    """Cheap metadata-only fetch — returns (from_raw, from_email). No body download."""
    msg = svc.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From"],
    ).execute()
    headers   = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    from_raw  = headers.get("From", "")
    m         = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_raw)
    from_email = m.group().lower() if m else from_raw.lower()
    return from_raw, from_email


def _get_full_message(svc, msg_id: str) -> tuple[str, str, str, str, str, str]:
    """Returns (from_raw, from_email, subject, body_text, thread_id, tab)."""
    msg = svc.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    headers   = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    from_raw  = headers.get("From", "")
    subject   = headers.get("Subject", "")
    thread_id = msg.get("threadId", "")
    tab       = _get_tab(msg.get("labelIds", []))

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

    return from_raw, from_email, subject, body[:1500], thread_id, tab


def _mark_read(svc, msg_id: str):
    svc.users().messages().modify(
        userId="me", id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def _archive(svc, msg_id: str):
    """Remove from inbox/all tabs — works for inbox, promotions, social, updates."""
    svc.users().messages().modify(
        userId="me", id=msg_id,
        body={"removeLabelIds": _CATEGORY_LABELS}
    ).execute()


_GENERIC_NAMES = {
    "hiring", "manager", "team", "hr", "recruiter", "talent", "acquisition",
    "staffing", "noreply", "no-reply", "hello", "info", "jobs", "careers",
    "support", "admin", "contact", "dear", "there",
}


def _extract_first_name(from_raw: str) -> str:
    """
    Pull first name from 'Full Name <email@domain.com>' or plain name string.
    Returns "" if no clean first name can be determined.
    """
    name = re.sub(r"<[^>]+>", "", from_raw).strip().strip('"').strip("'")
    first = name.split()[0] if name else ""
    if not first or first.lower() in _GENERIC_NAMES or not re.match(r"^[A-Za-z\-']{2,}$", first):
        return ""
    return first.capitalize()


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
    except Exception:
        return False


# ── Ollama — email classifier ─────────────────────────────────────────────────

# Matched against the LOCAL part of the email (before @) — no @ suffix here
_JUNK_LOCAL_PATTERNS = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notification", "notifications", "alert", "alerts", "jobalerts",
    "newsletter", "mailer", "mailer-daemon", "postmaster",
    "automated", "automailer", "bounce", "daemon",
    "promo", "promotions", "marketing", "advertis",
    "survey", "digest", "unsubscribe", "optout",
    "applyonline", "apply-online", "jobconfirm", "jobapply",
    "info", "hello", "support", "contact", "team",
    "updates", "news", "offers", "deals", "events",
    "careers", "jobs", "hiring", "recruit",
]

_JUNK_SENDER_DOMAINS = {
    # Job boards / ATS — never a recruiter personal reply
    "dice.com", "indeedemail.com", "indeed.com", "glassdoor.com",
    "ziprecruiter.com", "monster.com", "careerbuilder.com",
    "lever.co", "greenhouse.io", "workday.com", "icims.com",
    "myworkdayjobs.com", "successfactors.com", "taleo.net",
    "jobright.ai", "leoforce.com", "careers.leoforce.com",
    # Training / courses / events (not recruiter replies)
    "interviewkickstart.com", "udemy.com", "coursera.org",
    "pluralsight.com", "linkedin-email.com",
    # Social / tech platforms
    "linkedin.com", "accounts.google.com", "mail.google.com",
    "facebookmail.com", "twitter.com", "instagram.com",
    # Retail / entertainment / services
    "cinemark.com", "fandango.com", "instacart.com",
    "doordash.com", "ubereats.com", "amazon.com", "amazonses.com",
    "ebay.com", "ticketmaster.com", "eventbrite.com",
    "netflix.com", "hulu.com", "spotify.com",
    "bankofamerica.com", "chase.com", "wellsfargo.com",
}


def _is_junk_sender(from_email: str) -> bool:
    """Return True if the sender address looks automated / non-recruiter."""
    el     = from_email.lower()
    local  = el.split("@")[0] if "@" in el else el
    domain = el.split("@")[1] if "@" in el else ""

    # Exact domain match
    if domain in _JUNK_SENDER_DOMAINS:
        return True
    # Subdomain match (e.g. emails.cinemark.com, survey.instacart.com)
    for jd in _JUNK_SENDER_DOMAINS:
        if domain.endswith("." + jd):
            return True
    # Local-part pattern match (patterns have NO @ — checked against local only)
    if any(p in local for p in _JUNK_LOCAL_PATTERNS):
        return True
    return False


def _classify_sync(from_raw: str, subject: str, body: str) -> dict:
    """
    Returns dict with keys: category, reason, wants_resume, is_interview, is_rtr, is_offer.
    category: INTERVIEW | RTR | INFO_REQUEST | REPLY_NEEDED | JUNK
    """
    m = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_raw)
    from_email = m.group().lower() if m else from_raw.lower()

    # Sender-address-based junk detection (fastest, no body scan needed)
    if _is_junk_sender(from_email):
        return {"category": "JUNK", "reason": f"junk sender: {from_email}",
                "wants_resume": False, "is_interview": False,
                "is_rtr": False, "is_offer": False}

    combined = (subject + " " + body).lower()

    # Body keyword junk shortcuts
    if any(w in combined for w in ["auto-reply", "out of office", "no-reply",
                                    "noreply", "job alert", "unsubscribe",
                                    "do not reply", "donotreply", "automatic reply",
                                    "this is an automated", "you are receiving this",
                                    "manage your preferences", "email preferences",
                                    "click here to unsubscribe", "opt out"]):
        return {"category": "JUNK", "reason": "auto-reply/marketing detected",
                "wants_resume": False, "is_interview": False,
                "is_rtr": False, "is_offer": False}

    try:
        import ollama
        prompt = (
            "Classify this recruiter email reply. Reply with ONLY valid JSON.\n\n"
            f"From: {from_raw}\nSubject: {subject}\nBody:\n{body[:600]}\n\n"
            "Categories:\n"
            "- INTERVIEW  : wants to schedule interview / call / meeting\n"
            "- RTR        : Right-to-Represent — asking permission to submit candidate to client\n"
            "- INFO_REQUEST : asking for resume, availability, work auth, rate, visa\n"
            "- REPLY_NEEDED : interested, asking questions, positive response\n"
            "- JUNK       : auto-reply, out-of-office, notification, unsubscribe\n\n"
            'Reply: {"category":"REPLY_NEEDED","reason":"short reason",'
            '"wants_resume":false,"is_interview":false,"is_rtr":false,"is_offer":false}'
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
            "is_rtr":       bool(data.get("is_rtr", False)),
            "is_offer":     bool(data.get("is_offer", False)),
        }
    except Exception:
        # Keyword fallback
        if any(w in combined for w in ["right to represent", "rtr", "represent you",
                                        "authorization to submit", "submit your profile",
                                        "submit your resume to our client"]):
            return {"category": "RTR", "reason": "RTR keywords",
                    "wants_resume": False, "is_interview": False,
                    "is_rtr": True, "is_offer": False}
        if any(w in combined for w in ["offer", "congratulations", "we'd like to extend",
                                        "compensation", "salary", "start date"]):
            return {"category": "REPLY_NEEDED", "reason": "possible offer",
                    "wants_resume": False, "is_interview": False,
                    "is_rtr": False, "is_offer": True}
        if any(w in combined for w in ["interview", "schedule", "call", "meet",
                                        "zoom", "teams", "google meet", "calendly"]):
            return {"category": "INTERVIEW", "reason": "interview keywords",
                    "wants_resume": False, "is_interview": True,
                    "is_rtr": False, "is_offer": False}
        if any(w in combined for w in ["resume", "cv", "availability",
                                        "work auth", "visa", "rate", "hourly"]):
            return {"category": "INFO_REQUEST", "reason": "info request keywords",
                    "wants_resume": True, "is_interview": False,
                    "is_rtr": False, "is_offer": False}
        return {"category": "REPLY_NEEDED", "reason": "recruiter reply",
                "wants_resume": False, "is_interview": False,
                "is_rtr": False, "is_offer": False}


# ── Ollama — reply generator ──────────────────────────────────────────────────

def _generate_reply_sync(recruiter_body: str, subject: str,
                         profile: dict, clf: dict,
                         recruiter_first_name: str = "") -> str:
    category  = clf.get("category", "REPLY_NEEDED")
    name      = profile.get("name", "Applicant")
    work_auth = profile.get("work_auth", "OPT")
    skills    = (profile.get("skills", "") or "")[:100]
    location  = profile.get("location", "")
    available = profile.get("available_to_start", "immediately")
    years     = profile.get("years_experience", 3)
    title     = profile.get("current_title", "Software Engineer")

    # Personal greeting: "Hi Sarah," if name known, otherwise just "Hi,"
    greeting = f"Hi {recruiter_first_name}," if recruiter_first_name else "Hi,"

    if category == "INTERVIEW":
        ctx = "The recruiter wants to schedule an interview or introductory call."
    elif category == "RTR":
        ctx = "The recruiter is asking for Right-to-Represent authorization to submit my profile to their client."
    elif category == "INFO_REQUEST":
        ctx = "The recruiter is asking for more details: resume, availability, work auth, or rate."
    else:
        ctx = "The recruiter is following up or expressing interest."

    try:
        import ollama
        recruiter_line = (
            f"Recruiter first name: {recruiter_first_name}" if recruiter_first_name
            else "Recruiter name: unknown — do NOT invent a name or use generic titles"
        )
        prompt = (
            f"Write a short professional reply to this recruiter email.\n\n"
            f"Situation: {ctx}\n"
            f"{recruiter_line}\n"
            f"Recruiter's email:\n{recruiter_body[:500]}\n\n"
            f"Candidate:\n"
            f"  Name: {name} | Title: {title}\n"
            f"  Experience: {years} yrs | Skills: {skills}\n"
            f"  Location: {location} | Work auth: {work_auth}\n"
            f"  Available: {available}\n\n"
            f"Rules:\n"
            f"- Start with exactly: {greeting}\n"
            f"- 3-5 sentences max, no filler phrases\n"
            f"- INTERVIEW: confirm strong interest, ask for available time slots\n"
            f"- RTR: confirm authorization, provide full name and work auth status\n"
            f"- INFO_REQUEST: provide the requested info clearly\n"
            f"- Always mention work auth status ({work_auth}) if relevant\n"
            f"- Do NOT use generic salutations like 'Hiring Manager', 'Team', 'Recruiter'\n"
            f"- Close with: Best regards,\\n{name}\n"
            f"- Output the email body ONLY — no subject line\n"
        )
        resp = ollama.chat(model=OLLAMA_MODEL,
                           messages=[{"role": "user", "content": prompt}])
        body = resp.message.content.strip()
        body = re.sub(r"(?i)^subject\s*:.*\n?", "", body).strip()
        # Ensure the greeting is correct even if model ignored the instruction
        if not body.startswith("Hi"):
            body = f"{greeting}\n\n{body}"
        return body
    except Exception:
        if category == "INTERVIEW":
            return (
                f"{greeting}\n\nThank you for reaching out! I'm very excited about this opportunity "
                f"and would love to connect.\n\n"
                f"I'm available for a call this week — please share a few time slots and "
                f"I'll confirm immediately. I'm on {work_auth} and can start {available}.\n\n"
                f"Looking forward to speaking with you.\n\nBest regards,\n{name}"
            )
        if category == "RTR":
            return (
                f"{greeting}\n\nI authorize you to represent me for this position. "
                f"My full name is {name}, and I'm on {work_auth} authorization. "
                f"I'm available to start {available}.\n\n"
                f"Please proceed with submitting my profile. Looking forward to your update.\n\n"
                f"Best regards,\n{name}"
            )
        return (
            f"{greeting}\n\nThank you for your response — I'm very interested in this role.\n\n"
            f"I have {years} years of experience in {skills[:60]}, currently based in "
            f"{location}. I'm on {work_auth} and available to start {available}. "
            f"Happy to share any additional details you need.\n\n"
            f"Best regards,\n{name}"
        )


# ── Core monitor loop (one per profile) ──────────────────────────────────────

async def _monitor_profile(email: str, profile_data: dict,
                           gs: gmail_sender.GmailSender):
    tag   = email.split("@")[0][:12]
    stats = _stats[email]
    stats.name         = profile_data.get("name", email.split("@")[0])
    stats.uptime_start = datetime.now().strftime("%H:%M:%S")

    processed_ids: set[str] = set()

    while True:
        try:
            stats.status     = "checking..."
            stats.last_check = datetime.now().strftime("%H:%M:%S")

            svc = await asyncio.to_thread(gs.get_service)
            if not svc:
                stats.errors += 1
                _log(tag, "✗", "Gmail auth failed", email)
                for i in range(60, 0, -1):
                    stats.status = f"auth error — retry in {i}s"
                    await asyncio.sleep(1)
                continue

            messages = await asyncio.to_thread(_list_unread, svc)
            new_msgs = [r for r in messages if r["id"] not in processed_ids]
            _log(tag, "🔍", "Polled inbox+tabs",
                 f"{len(messages)} unread (7d)  |  {len(new_msgs)} new")

            for ref in new_msgs:
                msg_id = ref["id"]
                processed_ids.add(msg_id)

                # Cheap sender-only check first — skip junk with zero extra API calls
                from_raw_quick, from_email_quick = \
                    await asyncio.to_thread(_get_sender_email, svc, msg_id)
                if _is_junk_sender(from_email_quick):
                    continue   # silently ignore — no archive, no log, no wasted time

                # Legitimate sender — fetch full message and process
                from_raw, from_email, subject, body, thread_id, tab = \
                    await asyncio.to_thread(_get_full_message, svc, msg_id)

                clf      = await asyncio.to_thread(_classify_sync, from_raw, subject, body)
                category = clf["category"]

                tab_label = f"[{tab}]" if tab != "inbox" else ""

                async with _lock:
                    now_str = datetime.now().strftime("%H:%M:%S")
                    stats.last_from    = from_email[:30]
                    stats.last_subject = subject[:40]
                    stats.last_action_time = now_str

                    # Always record the recruiter in the DB regardless of category
                    await asyncio.to_thread(
                        recruiter_db.upsert,
                        from_email,
                        from_raw,                  # full "Name <email>" as name
                        "",                        # company — auto-filled from domain in DB
                        subject,                   # use subject as title hint
                        f"gmail_{tab}",            # source: gmail_inbox / gmail_promotions / etc.
                        category.lower(),          # status: junk / interview / rtr / reply_needed
                    )

                    sender_short = email.split("@")[0]   # e.g. yagneshreddypasunooru

                    if category == "JUNK":
                        await asyncio.to_thread(_archive,     svc, msg_id)
                        await asyncio.to_thread(_mark_read,   svc, msg_id)
                        await asyncio.to_thread(_apply_label, svc, msg_id, "outreach/junk")
                        stats.archived    += 1
                        stats.last_action  = "Archived (junk)"
                        _log(tag, "🗑",
                             f"Archived {tab_label}",
                             f"{from_email} → {sender_short} [{subject[:30]}]")

                    else:
                        stats.replies_received += 1

                        if clf.get("is_rtr") or category == "RTR":
                            stats.rtrs_received += 1

                        if clf.get("is_offer"):
                            stats.offers += 1

                        _log(tag, "📨",
                             f"Received [{category}] {tab_label}",
                             f"{from_email} → {sender_short} | {subject[:35]}")

                        recruiter_first = _extract_first_name(from_raw)
                        reply_body = await asyncio.to_thread(
                            _generate_reply_sync, body, subject, profile_data, clf,
                            recruiter_first
                        )
                        wants_resume = clf.get("wants_resume", False)
                        resume = gs.pick_resume("software engineer") if wants_resume else None

                        await asyncio.sleep(random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX))

                        ok = await asyncio.to_thread(
                            _send_thread_reply, svc, email,
                            from_raw, subject, reply_body, thread_id, resume
                        )

                        if ok:
                            await asyncio.to_thread(_mark_read, svc, msg_id)
                            stats.replies_sent += 1

                            if category == "INTERVIEW" or clf.get("is_interview"):
                                await asyncio.to_thread(
                                    _apply_label, svc, msg_id, "outreach/interview"
                                )
                                stats.interviews  += 1
                                stats.last_action  = "Interview reply sent"
                                _log(tag, "🎯",
                                     f"Interview reply sent {tab_label}",
                                     f"{sender_short} → {from_email}")
                                await asyncio.to_thread(
                                    recruiter_db.upsert, from_email, from_raw, "", subject,
                                    f"gmail_{tab}", "interview",
                                )

                            elif category == "RTR" or clf.get("is_rtr"):
                                await asyncio.to_thread(
                                    _apply_label, svc, msg_id, "outreach/rtr"
                                )
                                stats.rtrs_replied += 1
                                stats.last_action   = "RTR authorized"
                                _log(tag, "📋",
                                     f"RTR authorized {tab_label}",
                                     f"{sender_short} → {from_email}")
                                await asyncio.to_thread(
                                    recruiter_db.upsert, from_email, from_raw, "", subject,
                                    f"gmail_{tab}", "rtr",
                                )

                            else:
                                await asyncio.to_thread(
                                    _apply_label, svc, msg_id, "outreach/active"
                                )
                                stats.last_action = "Replied"
                                _log(tag, "✉",
                                     f"Reply sent {tab_label}",
                                     f"{sender_short} → {from_email} | {subject[:30]}")
                                await asyncio.to_thread(
                                    recruiter_db.upsert, from_email, from_raw, "", subject,
                                    f"gmail_{tab}", "replied",
                                )
                        else:
                            stats.errors      += 1
                            stats.last_action  = "Reply failed"
                            _log(tag, "✗", "Reply failed", from_email)

        except Exception as e:
            stats.errors     += 1
            stats.status      = "error"
            stats.last_action = f"Error: {str(e)[:40]}"
            _log(tag, "✗", "Error", str(e)[:60])

        delay   = random.uniform(POLL_MIN, POLL_MAX)
        nxt     = (datetime.now() + timedelta(seconds=delay)).strftime("%H:%M:%S")
        stats.next_check = nxt
        stats.last_check = datetime.now().strftime("%H:%M:%S")
        remaining = int(delay)
        while remaining > 0:
            stats.status = f"next poll in {remaining}s"
            await asyncio.sleep(1)
            remaining -= 1


# ── Rich dashboard ────────────────────────────────────────────────────────────

_METRIC_ROWS = [
    ("Outreach Sent",    "outreach_sent",    "cyan"),
    ("Dice Applied",     "dice_applied",     "cyan"),
    ("Replies Received", "replies_received", "green"),
    ("Auto-Replies Sent","replies_sent",     "blue"),
    ("RTRs Received",    "rtrs_received",    "yellow"),
    ("RTRs Replied",     "rtrs_replied",     "yellow"),
    ("Interviews",       "interviews",       "magenta"),
    ("Offers",           "offers",           "bright_green"),
    ("Archived (Junk)",  "archived",         "dim"),
    ("Errors",           "errors",           "red"),
]


def _fmt(val: int, color: str) -> str:
    if val == 0:
        return "[dim]—[/dim]"
    return f"[{color} bold]{val}[/]"


def _uptime_str() -> str:
    delta = datetime.now() - _start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _render_dashboard() -> Layout:
    profiles = list(_stats.values())
    now_str  = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

    layout = Layout()
    layout.split_column(
        Layout(name="header",  size=3),
        Layout(name="metrics", size=len(_METRIC_ROWS) + 5),
        Layout(name="status",  size=len(profiles) + 6),
        Layout(name="log"),
    )

    # ── Header ────────────────────────────────────────────────
    layout["header"].update(Panel(
        Text(
            f"  Gmail Recruiter Monitor   |   Uptime: {_uptime_str()}   |   {now_str}"
            f"   |   {len(profiles)} profile{'s' if len(profiles) != 1 else ''}"
            f"   |   {recruiter_db.count()} recruiters in DB",
            justify="center", style="bold bright_cyan"
        ),
        style="bright_cyan", padding=(0, 1),
    ))

    # ── Metrics grid ──────────────────────────────────────────
    # Columns: Metric | Total | profile1 | profile2 | ...
    mtbl = Table(
        box=box.SIMPLE_HEAVY, expand=True,
        show_header=True, header_style="bold white on dark_blue",
        padding=(0, 1),
    )
    mtbl.add_column("Metric", style="bold white", min_width=20)
    mtbl.add_column("Total",  justify="center", style="bold", min_width=8)
    for p in profiles:
        short = (p.name or p.email.split("@")[0])[:14]
        mtbl.add_column(short, justify="center", min_width=9)

    for label, attr, color in _METRIC_ROWS:
        total = sum(getattr(p, attr, 0) for p in profiles)
        row   = [label, _fmt(total, color)]
        for p in profiles:
            row.append(_fmt(getattr(p, attr, 0), color))
        mtbl.add_row(*row)

    layout["metrics"].update(Panel(
        mtbl,
        title="[bold white]  Metrics[/]",
        border_style="blue",
    ))

    # ── Per-profile status strip ──────────────────────────────
    stbl = Table(
        box=box.SIMPLE, expand=True,
        show_header=True, header_style="bold white on grey23",
        padding=(0, 1),
    )
    stbl.add_column("Profile",     style="cyan",   no_wrap=True, min_width=14)
    stbl.add_column("Status",      style="yellow", no_wrap=True, min_width=12)
    stbl.add_column("Last From",   style="white",  no_wrap=True, min_width=22)
    stbl.add_column("Last Action", style="white",  no_wrap=True, min_width=22)
    stbl.add_column("Last Check",  style="dim",    no_wrap=True, min_width=8)
    stbl.add_column("Next Check",  style="dim",    no_wrap=True, min_width=8)

    for p in profiles:
        name_cell = p.name or p.email.split("@")[0]
        stbl.add_row(
            name_cell,
            p.status,
            p.last_from    or "[dim]—[/dim]",
            p.last_action  or "[dim]—[/dim]",
            p.last_check   or "[dim]—[/dim]",
            p.next_check   or "[dim]—[/dim]",
        )

    layout["status"].update(Panel(
        stbl,
        title="[bold white]  Profile Status[/]",
        border_style="dark_cyan",
    ))

    # ── Activity log ──────────────────────────────────────────
    visible = list(_activity)[:30]
    log_txt = "\n".join(visible) if visible else "[dim]Waiting for activity...[/dim]"
    layout["log"].update(Panel(
        log_txt,
        title="[bold white]  Activity Log[/]",
        border_style="dim",
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

        ps = ProfileStats(email=email)
        ps.outreach_sent = _load_outreach_count(sd)
        ps.dice_applied  = _load_dice_count(email)
        _stats[email]    = ps

    # ── Pre-authenticate BEFORE dashboard steals the terminal ────────────────
    # run_local_server() opens a browser for OAuth — must happen in plain terminal,
    # not inside Rich Live(screen=True) which suppresses stdout.
    print("\nAuthenticating Gmail accounts (browser windows may open)...")
    auth_ok: dict[str, bool] = {}
    for email in target_emails:
        svc = senders[email].get_service()   # synchronous — triggers OAuth if no token
        if svc:
            print(f"  ✓ {email}")
            auth_ok[email] = True
        else:
            print(f"  ✗ {email} — auth failed (will retry in monitor)")
            auth_ok[email] = False

    print("\nAll accounts authenticated. Starting dashboard in 2 seconds...")
    print("Note: monitor will process all unread emails from the last 7 days on first poll.\n")
    await asyncio.sleep(2)

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
