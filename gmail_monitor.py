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

POLL_INTERVAL   = 1      # seconds between history polls (incremental — cheap)
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

# ── Verification-code pub/sub bus ──────────────────────────────────────────────
# keyed by monitored email; company_apply.py can subscribe via subscribe_verification_code()
_verification_bus: dict[str, asyncio.Queue] = {}

# ── Cached authenticated Gmail services (email → service object) ───────────────
_services: dict[str, object] = {}


def get_service(email: str):
    """Return the cached Gmail service for *email*, or None if monitor hasn't authed it yet."""
    return _services.get(email)


async def subscribe_verification_code(email: str, timeout: float = 90.0) -> str | None:
    """
    Wait up to *timeout* seconds for a verification code to arrive for *email*.
    Returns the code string, or None on timeout.
    Useful when running company_apply.py and gmail_monitor in the same process.
    """
    if email not in _verification_bus:
        _verification_bus[email] = asyncio.Queue()
    try:
        return await asyncio.wait_for(_verification_bus[email].get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def _detect_code_in_text(text: str) -> str | None:
    """Extract a verification/security code from email text. Returns None if not a code email."""
    lower = text.lower()
    if not any(k in lower for k in [
        "verification", "verify", "confirm", "security code",
        "one-time", "passcode", "your code", "enter the code", "access code",
    ]):
        return None
    # Greenhouse: "application: NakeSwk3" (mixed-case 8-char)
    m = re.search(r'application[:\s]+([A-Za-z0-9]{8})\b', text, re.IGNORECASE)
    if m:
        return m.group(1)
    # Generic "code: XXXXXXXX" (mixed or uppercase)
    m = re.search(r'\bcode[:\s]+([A-Za-z0-9]{8})\b', text, re.IGNORECASE)
    if m:
        return m.group(1)
    # Uppercase-only 8-char fallback
    m = re.search(r'\b([A-Z][A-Z0-9]{7}|[A-Z0-9]{7}[A-Z])\b', text)
    if m:
        return m.group(1)
    # 6-digit numeric
    codes = re.findall(r'\b(\d{6})\b', text)
    if codes:
        return codes[0]
    return None


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


def _get_thread_context(svc, thread_id: str, my_email: str, max_messages: int = 5) -> list[dict]:
    """
    Fetch the last *max_messages* messages from a thread.
    Returns list of {role, from, body} — role is 'me' or 'recruiter'.
    Used to give the reply generator full conversation context.
    """
    try:
        thread = svc.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
        messages = thread.get("messages", [])[-max_messages:]
        result = []
        for msg in messages:
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            from_h  = headers.get("From", "")
            m2      = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_h)
            sender  = m2.group().lower() if m2 else ""
            role    = "me" if my_email.lower() in sender else "recruiter"

            body_parts: list[str] = []
            def _ex(payload):
                if payload.get("mimeType") == "text/plain":
                    data = payload.get("body", {}).get("data", "")
                    if data:
                        body_parts.append(
                            base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                        )
                for part in payload.get("parts", []):
                    _ex(part)
            _ex(msg["payload"])
            raw_body = "".join(body_parts) if body_parts else msg.get("snippet", "")
            # Strip quoted reply chains (lines starting with ">")
            clean = "\n".join(
                l for l in raw_body.splitlines() if not l.strip().startswith(">")
            ).strip()
            result.append({"role": role, "from": from_h, "body": clean[:400]})
        return result
    except Exception:
        return []


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
    "do-not-reply", "donotreply", "do", "not", "reply", "notifications",
    "notification", "alerts", "automated", "mailer", "bounce", "postmaster",
    "updates", "news", "no", "career", "brew", "digest", "weekly", "daily",
    "newsletter", "insider", "report",
}


def _extract_first_name(from_raw: str) -> str:
    """
    Pull first name from 'Full Name <email@domain.com>' or plain name string.
    Returns "" if no clean first name can be determined.
    """
    name = re.sub(r"<[^>]+>", "", from_raw).strip().strip('"').strip("'")
    first = name.split()[0] if name else ""
    first = first.rstrip("-.,;:")          # strip trailing punctuation (e.g. "Diya-")
    if not first or first.lower() in _GENERIC_NAMES or not re.match(r"^[A-Za-z']{2,}$", first):
        return ""
    return first.capitalize()


_FWD_HEADER_RE = re.compile(
    r"-{3,}\s*Forwarded message\s*-{3,}|Begin forwarded message",
    re.I,
)
_FWD_FROM_RE = re.compile(
    r"^From:\s*(.+?)\s*$",
    re.I | re.MULTILINE,
)

def _resolve_recruiter(from_raw: str, subject: str, body: str) -> tuple[str, str]:
    """
    If the email is a forward, extract the original recruiter's From line from
    the body and return (recruiter_from_raw, recruiter_email).
    Otherwise return the direct sender unchanged.
    """
    is_fwd = subject.strip().lower().startswith(("fwd:", "fw:")) or \
             bool(_FWD_HEADER_RE.search(body))
    if not is_fwd:
        m = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_raw)
        return from_raw, (m.group().lower() if m else from_raw.lower())

    # Find the first "From:" line inside the forwarded block
    fwd_start = _FWD_HEADER_RE.search(body)
    search_text = body[fwd_start.start():] if fwd_start else body
    matches = _FWD_FROM_RE.findall(search_text)
    if matches:
        recruiter_raw = matches[0].strip()
        m = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", recruiter_raw)
        recruiter_email = m.group().lower() if m else recruiter_raw.lower()
        return recruiter_raw, recruiter_email

    # Fallback to original sender
    m = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_raw)
    return from_raw, (m.group().lower() if m else from_raw.lower())


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
    "myworkdayjobs.com", "myworkday.com", "email.myworkdayjobs.com",
    "successfactors.com", "taleo.net",
    "jobright.ai", "leoforce.com", "careers.leoforce.com",
    "substack.com", "beehiiv.com", "mailchimp.com", "constantcontact.com",
    "sendgrid.net", "mailgun.org",
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
                                    "click here to unsubscribe", "opt out",
                                    # Body-shop / training-placement detection
                                    "training and placement", "training program",
                                    "not a direct job", "not a direct client",
                                    "bench sales", "we place candidates",
                                    "staffing and training", "our program is designed",
                                    "placement program", "placement fee",
                                    "pay for training"]):
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

_PLACEHOLDER_RE = re.compile(
    r"\[(?:Hiring Manager|Recruiter(?: Name)?|Your Name|Candidate Name|Name|"
    r"First Name|Recipient|Insert Name|Title|Position|Company)[^\]]*\]",
    re.IGNORECASE,
)


def _clean_reply(text: str, greeting: str, name: str) -> str:
    """Strip model artifacts: placeholder brackets, subject lines, wrong greetings."""
    text = re.sub(r"(?i)^subject\s*:.*\n?", "", text).strip()
    text = _PLACEHOLDER_RE.sub("", text)          # remove [Hiring Manager] etc.
    text = re.sub(r"\[[^\]]{1,200}\]", "", text)  # catch any remaining [...] (incl. long instructions)
    text = re.sub(r"  +", " ", text)              # collapse double spaces from removed placeholders
    text = re.sub(r" +([.,!?])", r"\1", text)     # fix orphaned punctuation after removal
    text = re.sub(                                 # remove dangling prepositions before punctuation
        r"\s+\b(in|about|on|for|with|at|to|of|regarding|around|by)\b\s*([.,!?])",
        r"\2", text
    )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not re.match(r"^Hi\b", text, re.IGNORECASE):
        text = f"{greeting}\n\n{text}"
    # Make sure it closes with sender name
    if name.lower() not in text.lower()[-60:]:
        text = text.rstrip() + f"\n\nBest regards,\n{name}"
    return text.strip()


def _generate_reply_sync(
    recruiter_body: str,
    subject: str,
    profile: dict,
    clf: dict,
    recruiter_first_name: str = "",
    thread_history: list | None = None,
) -> str:
    category         = clf.get("category", "REPLY_NEEDED")
    name             = profile.get("name", "Applicant")
    work_auth        = profile.get("work_auth", "OPT")
    visa_expiry      = profile.get("visa_expiry", "")
    w2_c2c           = profile.get("w2_c2c", "W2")
    open_to_f2f      = profile.get("open_to_f2f", "Yes")
    phone            = profile.get("phone", "")
    skills           = (profile.get("skills", "") or "")[:200]
    location         = profile.get("location", "")
    available        = profile.get("available_to_start", "immediately")
    years            = profile.get("years_experience", 3)
    title            = profile.get("current_title", "Software Engineer")
    linkedin         = profile.get("linkedin_url", "")
    previous_clients = profile.get("previous_clients", "")

    greeting = f"Hi {recruiter_first_name}," if recruiter_first_name else "Hi,"

    history_msgs = thread_history or []
    is_followup  = len(history_msgs) > 1

    # ── Build prior-conversation block for follow-ups ─────────────────────────
    history_block = ""
    if is_followup:
        lines = ["PRIOR CONVERSATION (most recent last):"]
        for msg in history_msgs[:-1]:
            role = "Me" if msg["role"] == "me" else "Recruiter"
            lines.append(f"[{role}]: {msg['body'][:280]}")
            lines.append("---")
        history_block = "\n".join(lines) + "\n\n"

    # ── Profile block (used in both modes) ────────────────────────────────────
    visa_line     = f"  Visa/Work auth : {work_auth}" + (f", expires {visa_expiry}" if visa_expiry else "")
    clients_line  = f"  Previous clients : {previous_clients}" if previous_clients else ""
    phone_line    = f"  Phone          : {phone}" if phone else ""
    linkedin_line = f"  LinkedIn       : {linkedin}" if linkedin else ""
    profile_block = (
        f"MY PROFILE:\n"
        f"  Full name      : {name}\n"
        f"  Title          : {title}  |  {years} yrs experience\n"
        f"  Skills         : {skills}\n"
        f"{visa_line}\n"
        + (f"{clients_line}\n" if clients_line else "")
        + f"  Location       : {location}\n"
        f"  Available      : {available}\n"
        + (f"{phone_line}\n" if phone_line else "")
        + (f"{linkedin_line}\n" if linkedin_line else "")
    )

    recruiter_name_line = (
        f"Recruiter first name: {recruiter_first_name}\n"
        if recruiter_first_name
        else "Recruiter name unknown — use 'Hi,' as greeting, never invent a name or title\n"
    )

    common_rules = (
        f"\nRULES (follow exactly):\n"
        f"- Start with exactly: {greeting}\n"
        f"- NEVER use [placeholder], [Name], [Hiring Manager], or any text in square brackets\n"
        f"- NEVER reference a company name unless it was explicitly mentioned in the recruiter's message\n"
        f"- NEVER say 'I hope this email finds you well' or similar filler openers\n"
        f"- End with: Best regards,\n{name}\n"
        f"- Output the email body ONLY — no subject line, no preamble\n"
    )

    # ══════════════════════════════════════════════════════════════════════════
    # MODE 1 — FIRST CONTACT: structured bullet-point profile pitch
    # Built directly — no Ollama needed, format must be exact every time.
    # ══════════════════════════════════════════════════════════════════════════
    if not is_followup:
        rtr_opener = (
            "You have my authorization to represent me for this position.\n\n"
            if category == "RTR" else ""
        )
        bullets = [
            f"• Location: {location}",
            f"• Work Authorization: {work_auth}",
        ]
        if visa_expiry:
            bullets.append(f"• Visa / EAD Expiry: {visa_expiry}")
        bullets.append(f"• W2 / C2C: {w2_c2c}")
        bullets.append(f"• Open to F2F Interview: {open_to_f2f}")
        if previous_clients:
            bullets.append(f"• Previous Clients: {previous_clients}")
        if linkedin:
            bullets.append(f"• LinkedIn: {linkedin}")
        if phone:
            bullets.append(f"• Phone: {phone}")
        bullets.append(f"• Experience: {years}+ years ({skills})")
        bullets.append(f"• Availability: {available}")

        rtr_close = (
            "\nPlease go ahead and submit my profile. "
            "Could you share the JD and pay rate if you haven't already?"
            if category == "RTR" else
            "\nPlease find my updated resume attached. "
            "Would you be interested in moving forward with my profile?"
        )

        body = (
            f"{greeting}\n\n"
            f"{rtr_opener}"
            f"Here are my details:\n\n"
            f"Candidate Details:\n"
            + "\n".join(bullets)
            + f"\n{rtr_close}\n\n"
            f"Best regards,\n{name}"
        )
        return body

    # ══════════════════════════════════════════════════════════════════════════
    # MODE 2 — FOLLOW-UP: answer the specific question concisely
    # ══════════════════════════════════════════════════════════════════════════
    else:
        if category == "INTERVIEW":
            task = (
                "The recruiter wants to schedule an interview or call.\n"
                "Write a short reply (3-4 sentences) that:\n"
                "- Confirms you are available and excited\n"
                "- Proposes 2-3 specific time slots (e.g. 'Monday 2-5 PM CT or Tuesday anytime')\n"
                f"- Mentions you can start {available}\n"
            )
            fallback = (
                f"{greeting}\n\n"
                f"Absolutely, I'd love to connect! I'm available Monday through Friday, "
                f"flexible on timing — Monday 2–5 PM CT or Tuesday anytime work well for me. "
                f"Please share a slot and I'll confirm right away.\n\nBest regards,\n{name}"
            )
        elif category == "RTR":
            task = (
                "The recruiter is requesting RTR authorization.\n"
                "Write a crisp reply (3-4 sentences) that:\n"
                f"- Immediately grants authorization, states full name: {name}\n"
                f"- States work auth: {work_auth}" + (f", expires {visa_expiry}" if visa_expiry else "") + "\n"
                + (f"- Includes phone: {phone}\n" if phone else "")
                + "- Asks for job description / rate if not already shared\n"
            )
            fallback = (
                f"{greeting}\n\n"
                f"You have my authorization to represent me — full name: {name}. "
                f"Work auth: {work_auth}"
                + (f", EAD expires {visa_expiry}" if visa_expiry else "")
                + (f". Phone: {phone}." if phone else ".")
                + f" Please go ahead and submit. Could you share the JD and rate if you haven't already?\n\n"
                f"Best regards,\n{name}"
            )
        else:
            # Specific follow-up question — answer only what was asked
            task = (
                "The recruiter is asking a specific follow-up question in an ongoing conversation.\n"
                "Read their message carefully and answer ONLY what they asked. Keep it to 2-3 sentences.\n"
                "Do NOT re-introduce yourself. Do NOT send a generic profile pitch.\n"
                "Examples of specific answers:\n"
                "  - If they ask visa expiry → state the exact date\n"
                "  - If they ask about face-to-face → say yes/no + location\n"
                "  - If they ask rate/salary → state your expectation\n"
                "  - If they ask availability → give a specific date or timeframe\n"
            )
            fallback = (
                f"{greeting}\n\n"
                f"Happy to clarify — I'm on {work_auth}"
                + (f", EAD expires {visa_expiry}" if visa_expiry else "")
                + f", based in {location}, and available {available}. "
                + (f"You can reach me at {phone}. " if phone else "")
                + f"Let me know if you need anything else!\n\nBest regards,\n{name}"
            )

        prompt = (
            f"{history_block}"
            f"CURRENT RECRUITER MESSAGE:\n{recruiter_body[:600]}\n\n"
            f"TASK:\n{task}\n"
            f"{profile_block}\n"
            f"{recruiter_name_line}"
            f"{common_rules}"
            f"- 2-4 sentences for follow-up replies — be concise and specific\n"
            f"- Do NOT repeat information already exchanged in the prior conversation\n"
        )

    try:
        import ollama
        resp = ollama.chat(model=OLLAMA_MODEL, messages=[{"role": "user", "content": prompt}])
        body = resp.message.content.strip()
        return _clean_reply(body, greeting, name)
    except Exception:
        return _clean_reply(fallback, greeting, name)


# ── Core monitor loop (one per profile) ──────────────────────────────────────

async def _handle_new_message(
    email: str, svc, gs: gmail_sender.GmailSender,
    profile_data: dict, stats: "ProfileStats", tag: str,
    msg_id: str, processed_ids: set, thread_reply_counts: dict,
):
    """
    Classify and act on a single new message.
    Checks for verification codes first (pushes to bus), then handles recruiter emails.
    """
    if msg_id in processed_ids:
        return
    processed_ids.add(msg_id)
    await asyncio.to_thread(_save_processed_ids, email, processed_ids)

    # Cheap metadata fetch for both verification-code detection and junk filter
    try:
        meta = await asyncio.to_thread(
            lambda mid=msg_id: svc.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["Subject", "From", "To"],
            ).execute()
        )
    except Exception:
        return

    snippet = meta.get("snippet", "")
    headers = {h["name"]: h["value"] for h in meta["payload"]["headers"]}
    subject_quick = headers.get("Subject", "")
    from_raw_quick = headers.get("From", "")
    m = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_raw_quick)
    from_email_quick = m.group().lower() if m else from_raw_quick.lower()

    # Check for verification code BEFORE the junk filter
    # (Stripe/Greenhouse may be in the junk sender list but we still need their codes)
    code = _detect_code_in_text(f"{subject_quick} {snippet}")
    if code:
        if email not in _verification_bus:
            _verification_bus[email] = asyncio.Queue()
        await _verification_bus[email].put(code)
        _log(tag, "🔑", "Verification code → bus", f"{code[:4]}... | {subject_quick[:30]}")
        return

    # Silently skip junk senders (no archive, no log, no extra API calls)
    if _is_junk_sender(from_email_quick):
        return

    # Full message fetch for recruiter classification
    try:
        from_raw, from_email, subject, body, thread_id, tab = \
            await asyncio.to_thread(_get_full_message, svc, msg_id)
    except Exception:
        return

    # If this is a forwarded email, reply to the original recruiter, not the forwarder
    from_raw, from_email = _resolve_recruiter(from_raw, subject, body)

    clf      = await asyncio.to_thread(_classify_sync, from_raw, subject, body)
    category = clf["category"]
    tab_label = f"[{tab}]" if tab != "inbox" else ""

    # Fetch full thread history for context-aware reply generation
    thread_history = await asyncio.to_thread(
        _get_thread_context, svc, thread_id, email
    )
    is_followup = len(thread_history) > 1

    # Guard: if our reply is already the last message in this thread, skip — avoids
    # duplicate sends when the original recruiter message remains unread across restarts.
    if thread_history and thread_history[-1]["role"] == "me":
        _log(tag, "⏭", "Already replied to thread — skipping duplicate",
             f"{from_email} | {subject[:30]}")
        await asyncio.to_thread(_mark_read, svc, msg_id)
        return

    async with _lock:
        now_str = datetime.now().strftime("%H:%M:%S")
        stats.last_from       = from_email[:30]
        stats.last_subject    = subject[:40]
        stats.last_action_time = now_str
        sender_short          = email.split("@")[0]

        await asyncio.to_thread(
            recruiter_db.upsert,
            from_email, from_raw, "", subject, f"gmail_{tab}", category.lower(),
        )

        if category == "JUNK":
            await asyncio.to_thread(_archive,     svc, msg_id)
            await asyncio.to_thread(_mark_read,   svc, msg_id)
            await asyncio.to_thread(_apply_label, svc, msg_id, "outreach/junk")
            stats.archived   += 1
            stats.last_action = "Archived (junk)"
            _log(tag, "🗑", f"Archived {tab_label}",
                 f"{from_email} → {sender_short} [{subject[:30]}]")
        else:
            stats.replies_received += 1
            if clf.get("is_rtr") or category == "RTR":
                stats.rtrs_received += 1
            if clf.get("is_offer"):
                stats.offers += 1

            # Hard cap: stop auto-replying after MAX_AUTO_REPLIES_PER_THREAD sends
            # in this thread. Human must take over beyond that point.
            auto_sent = thread_reply_counts.get(thread_id, 0)
            if auto_sent >= MAX_AUTO_REPLIES_PER_THREAD:
                await asyncio.to_thread(_mark_read, svc, msg_id)
                _log(tag, "🛑", f"Auto-reply limit ({MAX_AUTO_REPLIES_PER_THREAD}) reached — human needed",
                     f"{from_email} | {subject[:35]}")
                return

            ctx_tag = " [follow-up]" if is_followup else " [first contact]"
            _log(tag, "📨", f"Received [{category}]{ctx_tag} {tab_label}",
                 f"{from_email} → {sender_short} | {subject[:35]}")

            recruiter_first = _extract_first_name(from_raw)
            reply_body = await asyncio.to_thread(
                _generate_reply_sync, body, subject, profile_data, clf,
                recruiter_first, thread_history,
            )

            # First contact: always attach resume.
            # Follow-up: only attach if recruiter explicitly asked for it.
            wants_resume = (
                not is_followup
                or clf.get("wants_resume", False)
                or category == "INFO_REQUEST"
            )
            job_title = profile_data.get("current_title", "software engineer")
            resume = gs.pick_and_tailor_resume(job_title, body) if wants_resume else None

            await asyncio.sleep(random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX))

            ok = await asyncio.to_thread(
                _send_thread_reply, svc, email, from_raw, subject, reply_body, thread_id, resume
            )

            if ok:
                await asyncio.to_thread(_mark_read, svc, msg_id)
                stats.replies_sent += 1
                thread_reply_counts[thread_id] = auto_sent + 1
                await asyncio.to_thread(_save_thread_replies, email, thread_reply_counts)

                if category == "INTERVIEW" or clf.get("is_interview"):
                    await asyncio.to_thread(_apply_label, svc, msg_id, "outreach/interview")
                    stats.interviews  += 1
                    stats.last_action  = "Interview reply sent"
                    _log(tag, "🎯", f"Interview reply sent {tab_label}",
                         f"{sender_short} → {from_email}")
                    await asyncio.to_thread(
                        recruiter_db.upsert, from_email, from_raw, "", subject,
                        f"gmail_{tab}", "interview",
                    )
                elif category == "RTR" or clf.get("is_rtr"):
                    await asyncio.to_thread(_apply_label, svc, msg_id, "outreach/rtr")
                    stats.rtrs_replied += 1
                    stats.last_action   = "RTR authorized"
                    _log(tag, "📋", f"RTR authorized {tab_label}",
                         f"{sender_short} → {from_email}")
                    await asyncio.to_thread(
                        recruiter_db.upsert, from_email, from_raw, "", subject,
                        f"gmail_{tab}", "rtr",
                    )
                else:
                    await asyncio.to_thread(_apply_label, svc, msg_id, "outreach/active")
                    stats.last_action = "Replied"
                    _log(tag, "✉", f"Reply sent {tab_label}",
                         f"{sender_short} → {from_email} | {subject[:30]}")
                    await asyncio.to_thread(
                        recruiter_db.upsert, from_email, from_raw, "", subject,
                        f"gmail_{tab}", "replied",
                    )
            else:
                stats.errors     += 1
                stats.last_action = "Reply failed"
                _log(tag, "✗", "Reply failed", from_email)


def _processed_ids_path(email: str) -> Path:
    safe = re.sub(r"[^a-z0-9]", "_", email.lower())
    return Path.home() / f".dice-playwright-profile-{safe}" / "gmail_processed_ids.json"


def _load_processed_ids(email: str) -> set[str]:
    p = _processed_ids_path(email)
    try:
        if p.exists():
            data = json.loads(p.read_text())
            return set(data.get("ids", []))
    except Exception:
        pass
    return set()


def _save_processed_ids(email: str, ids: set[str]):
    p = _processed_ids_path(email)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Keep only the last 5000 IDs to avoid unbounded growth
        trimmed = list(ids)[-5000:]
        p.write_text(json.dumps({"ids": trimmed}))
    except Exception:
        pass


MAX_AUTO_REPLIES_PER_THREAD = 2   # initial reply + one follow-up; human takes over after


def _thread_replies_path(email: str) -> Path:
    safe = re.sub(r"[^a-z0-9]", "_", email.lower())
    return Path.home() / f".dice-playwright-profile-{safe}" / "gmail_thread_replies.json"


def _load_thread_replies(email: str) -> dict[str, int]:
    p = _thread_replies_path(email)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _save_thread_replies(email: str, counts: dict[str, int]):
    p = _thread_replies_path(email)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Keep only last 2000 threads to avoid unbounded growth
        if len(counts) > 2000:
            keys = sorted(counts, key=lambda k: counts[k])[-2000:]
            counts = {k: counts[k] for k in keys}
        p.write_text(json.dumps(counts))
    except Exception:
        pass


async def _monitor_profile(email: str, profile_data: dict,
                           gs: gmail_sender.GmailSender):
    tag   = email.split("@")[0][:12]
    stats = _stats[email]
    stats.name         = profile_data.get("name", email.split("@")[0])
    stats.uptime_start = datetime.now().strftime("%H:%M:%S")

    processed_ids:      set[str]      = _load_processed_ids(email)
    thread_reply_counts: dict[str, int] = _load_thread_replies(email)
    history_id:    str | None = None
    svc = None

    while True:
        # ── Authenticate ─────────────────────────────────────────────────────
        if svc is None:
            stats.status     = "authenticating..."
            stats.last_check = datetime.now().strftime("%H:%M:%S")
            svc = await asyncio.to_thread(gs.get_service)
            if not svc:
                stats.errors += 1
                _log(tag, "✗", "Gmail auth failed", email)
                for i in range(60, 0, -1):
                    stats.status = f"auth error — retry in {i}s"
                    await asyncio.sleep(1)
                continue
            _services[email] = svc  # expose for external consumers

        # ── Initial full scan (runs once to set history baseline) ────────────
        if history_id is None:
            stats.status = "initial scan..."
            try:
                profile_info = await asyncio.to_thread(
                    lambda: svc.users().getProfile(userId="me").execute()
                )
                history_id = str(profile_info["historyId"])
            except Exception as e:
                _log(tag, "✗", "getProfile failed", str(e)[:60])
                svc = None
                await asyncio.sleep(10)
                continue

            # Process existing unreads for recruiter monitoring (not for code detection)
            try:
                messages = await asyncio.to_thread(_list_unread, svc)
                new_msgs = [r for r in messages if r["id"] not in processed_ids]
                _log(tag, "🔍", "Initial scan complete",
                     f"{len(messages)} unread (7d) | {len(new_msgs)} new | historyId={history_id}")
                for ref in new_msgs:
                    await _handle_new_message(
                        email, svc, gs, profile_data, stats, tag,
                        ref["id"], processed_ids, thread_reply_counts,
                    )
            except Exception as e:
                _log(tag, "✗", "Initial scan error", str(e)[:60])

            stats.status = "live (1s polling)"
            stats.last_check = datetime.now().strftime("%H:%M:%S")
            continue  # immediately start the history loop

        # ── History-based 1-second incremental poll ───────────────────────────
        await asyncio.sleep(POLL_INTERVAL)
        stats.last_check = datetime.now().strftime("%H:%M:%S")

        try:
            history = await asyncio.to_thread(
                lambda hid=history_id: svc.users().history().list(
                    userId="me",
                    startHistoryId=hid,
                    historyTypes=["messageAdded"],
                    maxResults=50,
                ).execute()
            )
            new_hid = history.get("historyId", history_id)
            if new_hid != history_id:
                history_id = new_hid
                stats.status = "live (1s polling)"

            for record in history.get("history", []):
                for msg_ref in record.get("messagesAdded", []):
                    await _handle_new_message(
                        email, svc, gs, profile_data, stats, tag,
                        msg_ref["message"]["id"], processed_ids, thread_reply_counts,
                    )

        except Exception as exc:
            err = str(exc)
            if "Invalid historyId" in err or "404" in err:
                _log(tag, "↺", "historyId expired — re-syncing", "")
                history_id = None   # triggers full re-scan on next iteration
            elif "rateLimitExceeded" in err or "429" in err:
                _log(tag, "⚠", "Rate limit — backing off 30s", "")
                stats.status = "rate limited"
                await asyncio.sleep(30)
            elif "401" in err or "invalid_grant" in err:
                _log(tag, "✗", "Auth expired — re-authing", "")
                svc = None
                history_id = None
            else:
                stats.errors     += 1
                stats.last_action = f"Poll error: {err[:35]}"
                _log(tag, "✗", "Poll error", err[:60])


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
    stbl.add_column("Status",      style="yellow", no_wrap=True, min_width=18)
    stbl.add_column("Last From",   style="white",  no_wrap=True, min_width=22)
    stbl.add_column("Last Action", style="white",  no_wrap=True, min_width=22)
    stbl.add_column("Last Check",  style="dim",    no_wrap=True, min_width=8)

    for p in profiles:
        name_cell = p.name or p.email.split("@")[0]
        stbl.add_row(
            name_cell,
            p.status,
            p.last_from    or "[dim]—[/dim]",
            p.last_action  or "[dim]—[/dim]",
            p.last_check   or "[dim]—[/dim]",
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
