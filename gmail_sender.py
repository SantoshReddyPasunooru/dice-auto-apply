"""
Gmail integration for Dice Auto Apply.
- Sends recruiter outreach emails when an email address is found in a job posting
- Checks inbox for recruiter replies
- Tracks sent emails per profile to avoid duplicates
"""

import base64
import csv
import json
import re
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

CREDS_PATH    = Path(__file__).parent / "gmail_credentials.json"
RESUMES_JSON  = Path(__file__).parent / "resumes.json"
TOKEN_PATH: Path | None = None

SENT_CSV_HEADERS = ["timestamp", "to_email", "job_title", "company", "job_url", "resume_used"]

_service = None

SENT_EMAILS:   set[str] = set()
SENT_CSV:      Path | None = None
SENDER_NAME:   str = "Applicant"
SENDER_EMAIL:  str = ""


# ── Auth ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    global _service
    if _service:
        return _service
    if not CREDS_PATH.exists():
        return None
    if TOKEN_PATH is None:
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if TOKEN_PATH.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
                creds = flow.run_local_server(port=0)
            TOKEN_PATH.write_text(creds.to_json())

        _service = build("gmail", "v1", credentials=creds)
        return _service
    except Exception as e:
        print(f"  [Gmail] auth error: {e}")
        return None


# ── Per-profile init ─────────────────────────────────────────────────────────

_STOPWORDS = {"a","an","the","for","at","in","of","and","or","with","to",
              "is","be","as","on","by","from","this","that","are","was"}

_RESUME_FOLDER:  Path | None = None
_DEFAULT_RESUME: Path | None = None
_ALL_RESUMES:    list[Path]  = []   # all PDFs in the folder, scanned once


def init_gmail(profile_dir: Path, sender_name: str = "Applicant",
               sender_email: str = "", resume_path: str = ""):
    """Call once at startup with the profile's session directory."""
    global SENT_CSV, SENT_EMAILS, TOKEN_PATH, _service
    global SENDER_NAME, SENDER_EMAIL
    global _RESUME_FOLDER, _DEFAULT_RESUME, _ALL_RESUMES

    TOKEN_PATH    = profile_dir / "gmail_token.json"
    _service      = None
    SENT_CSV      = profile_dir / "sent_emails.csv"
    SENT_EMAILS   = set()
    SENDER_NAME   = sender_name.strip() or "Applicant"
    SENDER_EMAIL  = sender_email.strip().lower()

    # Load folder config from resumes.json
    _RESUME_FOLDER  = None
    _DEFAULT_RESUME = None
    _ALL_RESUMES    = []

    if RESUMES_JSON.exists():
        try:
            data    = json.loads(RESUMES_JSON.read_text())
            profile = data.get(SENDER_EMAIL, {})
            folder  = profile.get("resume_folder", "")
            default = profile.get("default_resume", "")

            if folder:
                fp = Path(folder).expanduser()
                if fp.is_dir():
                    _RESUME_FOLDER = fp
                    _ALL_RESUMES   = sorted(
                        list(fp.rglob("*.pdf")) + list(fp.rglob("*.docx"))
                    )
                    print(f"  Gmail resume folder: {fp} ({len(_ALL_RESUMES)} resumes found)")
                else:
                    print(f"  [Gmail] Resume folder not found: {fp}")

            if default:
                dp = Path(default).expanduser()
                _DEFAULT_RESUME = dp if dp.exists() else None
                if not _DEFAULT_RESUME:
                    print(f"  [Gmail] Default resume not found: {dp}")

        except Exception as e:
            print(f"  [Gmail] resumes.json error: {e}")

    if not SENT_CSV.exists():
        with open(SENT_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=SENT_CSV_HEADERS).writeheader()
        return

    try:
        with open(SENT_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                email = row.get("to_email", "").strip().lower()
                if email:
                    SENT_EMAILS.add(email)
    except Exception:
        pass

    if SENT_EMAILS:
        print(f"  Gmail tracker: {len(SENT_EMAILS)} recruiter(s) already contacted.\n")


def pick_resume(job_title: str) -> Path | None:
    """
    Scan all PDFs in the profile's resume folder and return the best match
    for the given job title. Scores each PDF by counting how many meaningful
    words from the job title appear in its path (folder names + filename).
    Falls back to default_resume if nothing scores above 0.
    """
    if not _ALL_RESUMES:
        return _DEFAULT_RESUME

    # Tokenise job title — skip stopwords and short words
    title_words = [
        w for w in re.sub(r"[^a-z0-9 ]", " ", job_title.lower()).split()
        if len(w) >= 2 and w not in _STOPWORDS
    ]
    if not title_words:
        return _DEFAULT_RESUME

    best_score, best_path = 0, None
    for pdf in _ALL_RESUMES:
        # Score against the full relative path (subfolder names + filename)
        path_text = pdf.as_posix().lower()
        score = sum(1 for w in title_words if w in path_text)
        if score > best_score:
            best_score, best_path = score, pdf

    if best_score > 0:
        return best_path
    return _DEFAULT_RESUME


def _log_sent(to_email: str, job_title: str, company: str, job_url: str,
              resume_label: str = ""):
    SENT_EMAILS.add(to_email.lower())
    if SENT_CSV:
        with open(SENT_CSV, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=SENT_CSV_HEADERS).writerow({
                "timestamp":   datetime.now().isoformat(timespec="seconds"),
                "to_email":    to_email,
                "job_title":   job_title,
                "company":     company,
                "job_url":     job_url,
                "resume_used": resume_label,
            })


# ── Email extraction ─────────────────────────────────────────────────────────

_SKIP = ["noreply", "no-reply", "donotreply", "support@", "info@", "hello@",
         "contact@", "careers@dice", "dice.com", "example.com", "sentry.io"]

def extract_recruiter_emails(text: str) -> list[str]:
    """Return unique recruiter email addresses found in text."""
    found = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    seen, result = set(), []
    for email in found:
        el = email.lower()
        if any(p in el for p in _SKIP):
            continue
        if el not in seen:
            seen.add(el)
            result.append(email)
    return result


# ── Email composition ────────────────────────────────────────────────────────

def _compose(to: str, sender: str, job_title: str, company: str,
             job_url: str, resume: Path | None = None) -> MIMEMultipart:
    company_str   = company.strip() if company.strip() else "your company"
    company_label = f" at {company.strip()}" if company.strip() else ""
    name          = SENDER_NAME

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"Application for {job_title}{company_label}"
    msg["From"]    = sender
    msg["To"]      = to

    # Text + HTML as an alternative sub-part
    alt = MIMEMultipart("alternative")

    has_resume = bool(resume and resume.exists())

    plain = f"""\
Hi,

I came across the {job_title} opening{company_label} on Dice.com and wanted to reach out directly.

I have 8+ years of software engineering experience specializing in Generative AI, LLMs, \
Python, RAG pipelines, LangChain, and AWS. I'm based in Austin, TX and open to both \
contract and full-time opportunities.

I've already submitted my application via Dice ({job_url}), but happy to connect directly \
if you'd like to discuss the role or my background further.

{"Please find my resume attached." if has_resume else ""}

Looking forward to hearing from you.

Best regards,
{name}
"""

    html = f"""\
<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222;">
<p>Hi,</p>
<p>I came across the <strong>{job_title}</strong> opening{company_label} on Dice.com \
and wanted to reach out directly.</p>
<p>I have <strong>8+ years of software engineering experience</strong> specializing in
<strong>Generative AI, LLMs, Python, RAG pipelines, LangChain, and AWS</strong>.
I'm based in Austin, TX and open to both contract and full-time opportunities.</p>
<p>I've already submitted my application via Dice
(<a href="{job_url}">{job_url}</a>), but happy to connect directly if you'd like
to discuss the role or my background further.</p>
{"<p>Please find my resume attached.</p>" if has_resume else ""}
<p>Looking forward to hearing from you.</p>
<p>Best regards,<br><strong>{name}</strong></p>
</body></html>"""

    alt.attach(MIMEText(plain, "plain"))
    alt.attach(MIMEText(html,  "html"))
    msg.attach(alt)

    # Attach resume (PDF or DOCX)
    if resume and resume.exists():
        try:
            subtype = "pdf" if resume.suffix.lower() == ".pdf" \
                else "vnd.openxmlformats-officedocument.wordprocessingml.document"
            with open(resume, "rb") as f:
                part = MIMEApplication(f.read(), _subtype=subtype)
                part.add_header("Content-Disposition", "attachment", filename=resume.name)
                msg.attach(part)
        except Exception as e:
            print(f"      → [Gmail] resume attach error: {e}")

    return msg


# ── Public API ───────────────────────────────────────────────────────────────

def send_recruiter_email(to: str, job_title: str, company: str,
                         job_url: str, sender_email: str) -> bool:
    """Send an outreach email with the best-matching resume attached."""
    if to.lower() in SENT_EMAILS:
        return False
    try:
        service = get_gmail_service()
        if not service:
            return False
        resume = pick_resume(job_title)
        msg = _compose(to, sender_email, job_title, company, job_url, resume)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        resume_label = resume.name if resume else "none"
        _log_sent(to, job_title, company, job_url, resume_label)
        if resume:
            print(f"      → [Gmail] Resume attached: {resume.name}")
        return True
    except Exception as e:
        print(f"      → [Gmail] send error: {e}")
        return False


def check_inbox_replies() -> list[dict]:
    """
    Scan inbox for replies from any recruiter we previously emailed.
    Returns a list of dicts with keys: from, subject, date, snippet.
    """
    replies = []
    if not SENT_EMAILS:
        return replies
    try:
        service = get_gmail_service()
        if not service:
            return replies
        # Build a query: from:email1 OR from:email2 ...
        from_parts = " OR ".join(f"from:{e}" for e in list(SENT_EMAILS)[:20])
        query = f"({from_parts}) in:inbox"
        result = service.users().messages().list(
            userId="me", q=query, maxResults=20
        ).execute()
        for ref in result.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            replies.append({
                "from":    headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date":    headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            })
    except Exception as e:
        print(f"  [Gmail] inbox check error: {e}")
    return replies
