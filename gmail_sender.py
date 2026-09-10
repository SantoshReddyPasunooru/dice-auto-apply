"""
Gmail integration for Dice Auto Apply.
Each profile gets its own GmailSender instance so parallel runs
never share state (service, sent-email set, resume pool).

Module-level functions delegate to a default instance for backward
compatibility with main.py (which is single-profile / sequential).
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
    "https://www.googleapis.com/auth/gmail.modify",   # needed for archive/labels/mark-read
]

CREDS_PATH   = Path(__file__).parent / "gmail_credentials.json"
RESUMES_JSON = Path(__file__).parent / "resumes.json"

SENT_CSV_HEADERS = ["timestamp", "to_email", "job_title", "company", "job_url", "resume_used"]

_STOPWORDS = {"a","an","the","for","at","in","of","and","or","with","to",
              "is","be","as","on","by","from","this","that","are","was"}
_SKIP_ADDRS = ["noreply","no-reply","donotreply","support@","info@","hello@",
               "contact@","careers@dice","dice.com","example.com","sentry.io"]


# ── Per-profile class ─────────────────────────────────────────────────────────

class GmailSender:
    """Isolated Gmail state for one sender profile."""

    def __init__(self):
        self._service      = None
        self.sent_emails:  set[str]     = set()
        self.sent_csv:     Path | None  = None
        self.sender_name:  str          = "Applicant"
        self.sender_email: str          = ""
        self.token_path:   Path | None  = None
        self._resume_folder:  Path | None = None
        self._default_resume: Path | None = None
        self._all_resumes:    list[Path]  = []

    # ── Init ──────────────────────────────────────────────────────────────────

    def init(self, profile_dir: Path, sender_name: str = "Applicant",
             sender_email: str = "", resume_path: str = ""):
        self.token_path    = profile_dir / "gmail_token.json"
        self._service      = None
        self.sent_csv      = profile_dir / "sent_emails.csv"
        self.sent_emails   = set()
        self.sender_name   = sender_name.strip() or "Applicant"
        self.sender_email  = sender_email.strip().lower()
        self._resume_folder  = None
        self._default_resume = None
        self._all_resumes    = []

        if RESUMES_JSON.exists():
            try:
                data    = json.loads(RESUMES_JSON.read_text())
                profile = data.get(self.sender_email, {})
                folder  = profile.get("resume_folder", "")
                default = profile.get("default_resume", "")

                if folder:
                    fp = Path(folder).expanduser()
                    if fp.is_dir():
                        self._resume_folder = fp
                        self._all_resumes   = sorted(
                            list(fp.rglob("*.pdf")) + list(fp.rglob("*.docx"))
                        )
                        print(f"  Gmail resume folder: {fp} ({len(self._all_resumes)} resumes found)")
                    else:
                        print(f"  [Gmail] Resume folder not found: {fp}")

                if default:
                    dp = Path(default).expanduser()
                    self._default_resume = dp if dp.exists() else None
                    if not self._default_resume:
                        print(f"  [Gmail] Default resume not found: {dp}")

            except Exception as e:
                print(f"  [Gmail] resumes.json error: {e}")

        if not self.sent_csv.exists():
            with open(self.sent_csv, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=SENT_CSV_HEADERS).writeheader()
            return

        try:
            with open(self.sent_csv, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    email = row.get("to_email", "").strip().lower()
                    if email:
                        self.sent_emails.add(email)
        except Exception:
            pass

        if self.sent_emails:
            print(f"  Gmail tracker: {len(self.sent_emails)} recruiter(s) already contacted.\n")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def get_service(self, max_retries: int = 3):
        if self._service:
            return self._service
        if not CREDS_PATH.exists():
            return None
        if self.token_path is None:
            return None

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        for attempt in range(1, max_retries + 1):
            try:
                creds = None
                if self.token_path.exists():
                    creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

                if not creds or not creds.valid:
                    if creds and creds.expired and creds.refresh_token:
                        try:
                            creds.refresh(Request())
                        except Exception as ref_err:
                            # Stale token (scope change, revoked, invalid_grant) —
                            # delete it and fall through to full OAuth re-auth below.
                            print(f"  [Gmail] Token refresh failed: {ref_err}")
                            print(f"  [Gmail] Deleting stale token — re-authorizing...")
                            if self.token_path and self.token_path.exists():
                                self.token_path.unlink(missing_ok=True)
                            creds = None

                    if not creds or not creds.valid:
                        # Print clearly which account to choose in the browser
                        print(f"\n  ┌─ Gmail OAuth for: {self.sender_email}")
                        print(f"  │  IMPORTANT: sign in as  >>>  {self.sender_email}  <<<")
                        print(f"  └─ Opening browser now...\n")
                        flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
                        creds = flow.run_local_server(port=0)
                    self.token_path.write_text(creds.to_json())

                svc = build("gmail", "v1", credentials=creds)

                if self.sender_email:
                    prof   = svc.users().getProfile(userId="me").execute()
                    actual = prof.get("emailAddress", "").lower()
                    if actual and actual != self.sender_email:
                        print(f"\n  [Gmail] Wrong account: signed in as '{actual}', need '{self.sender_email}'")
                        # Delete the bad token so the next attempt re-opens OAuth
                        self.token_path.unlink(missing_ok=True)
                        if attempt < max_retries:
                            print(f"  [Gmail] Retrying... ({attempt}/{max_retries})\n")
                        continue
                    print(f"  [Gmail] ✓ Authenticated as: {actual}")

                self._service = svc
                return self._service

            except Exception as e:
                print(f"  [Gmail] auth error (attempt {attempt}): {e}")
                if attempt >= max_retries:
                    return None

        return None

    # ── Resume picker + tailor ────────────────────────────────────────────────

    def _pick_best_master(self, job_title: str) -> Path | None:
        """Pick the best-matching DOCX master (preferred) or any file."""
        if not self._all_resumes:
            return self._default_resume
        title_words = [
            w for w in re.sub(r"[^a-z0-9 ]", " ", job_title.lower()).split()
            if len(w) >= 2 and w not in _STOPWORDS
        ]
        # Prefer DOCX files for tailoring; score by filename keyword match
        docx_files = [p for p in self._all_resumes if p.suffix.lower() == ".docx"]
        pool = docx_files if docx_files else self._all_resumes
        if not title_words:
            return pool[0] if pool else self._default_resume
        best_score, best_path = 0, None
        for f in pool:
            path_text = f.as_posix().lower()
            score = sum(1 for w in title_words if w in path_text)
            if score > best_score:
                best_score, best_path = score, f
        return best_path if best_score > 0 else (pool[0] if pool else self._default_resume)

    def pick_resume(self, job_title: str) -> Path | None:
        """Legacy: pick by filename only, no tailoring."""
        return self._pick_best_master(job_title)

    def pick_and_tailor_resume(self, job_title: str, jd_text: str = "") -> Path | None:
        """
        Pick the best-matching master DOCX and tailor it to the JD.
        Falls back to plain pick_resume if tailoring fails or JD is empty.
        """
        master = self._pick_best_master(job_title)
        if not master or not jd_text or master.suffix.lower() != ".docx":
            return master
        try:
            from resume_tailor import tailor_resume
            return tailor_resume(master, jd_text, output_dir=self._resume_folder)
        except Exception as e:
            print(f"  [Tailor] Error — falling back to untailored resume: {e}")
            return master

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _log_sent(self, to_email: str, job_title: str, company: str,
                  job_url: str, resume_label: str = ""):
        self.sent_emails.add(to_email.lower())
        if self.sent_csv:
            with open(self.sent_csv, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=SENT_CSV_HEADERS).writerow({
                    "timestamp":   datetime.now().isoformat(timespec="seconds"),
                    "to_email":    to_email,
                    "job_title":   job_title,
                    "company":     company,
                    "job_url":     job_url,
                    "resume_used": resume_label,
                })

    @staticmethod
    def _attach_resume(msg: MIMEMultipart, resume: Path):
        try:
            subtype = "pdf" if resume.suffix.lower() == ".pdf" \
                else "vnd.openxmlformats-officedocument.wordprocessingml.document"
            with open(resume, "rb") as f:
                part = MIMEApplication(f.read(), _subtype=subtype)
                part.add_header("Content-Disposition", "attachment", filename=resume.name)
                msg.attach(part)
        except Exception as e:
            print(f"      → [Gmail] resume attach error: {e}")

    def _compose_dice(self, to: str, job_title: str, company: str,
                      job_url: str, resume: Path | None,
                      profile: dict | None = None) -> MIMEMultipart:
        company_label    = f" at {company.strip()}" if company.strip() else ""
        p                = profile or {}
        name             = p.get("name", self.sender_name) or self.sender_name
        work_auth        = p.get("work_auth", "OPT")
        visa_expiry      = p.get("visa_expiry", "")
        w2_c2c           = p.get("w2_c2c", "W2")
        open_to_f2f      = p.get("open_to_f2f", "Yes")
        years            = p.get("years_experience", 3)
        skills           = (p.get("skills", "") or "")[:200]
        location         = p.get("location", "")
        available        = p.get("available_to_start", "immediately")
        linkedin         = p.get("linkedin_url", "")
        phone            = p.get("phone", "")
        previous_clients = p.get("previous_clients", "")

        bullets = [f"• Location: {location}", f"• Work Authorization: {work_auth}"]
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
        bullets_plain = "\n".join(bullets)
        bullets_html  = "".join(f"<li>{b[2:]}</li>" for b in bullets)

        msg = MIMEMultipart("mixed")
        msg["Subject"] = f"Application for {job_title}{company_label} — {work_auth} | {years}+ yrs exp"
        msg["From"]    = self.sender_email
        msg["To"]      = to

        plain = (
            f"Hi,\n\n"
            f"I came across the {job_title} opening{company_label} on Dice.com "
            f"and wanted to share my profile directly.\n\n"
            f"Candidate Details:\n{bullets_plain}\n\n"
            f"Please find my updated resume attached. "
            f"Would you be interested in moving forward with my profile?\n\n"
            f"Best regards,\n{name}"
        )
        html = (
            '<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222;">'
            "<p>Hi,</p>"
            f"<p>I came across the <strong>{job_title}</strong> opening{company_label} on Dice.com "
            "and wanted to share my profile directly.</p>"
            f"<p><strong>Candidate Details:</strong></p><ul>{bullets_html}</ul>"
            "<p>Please find my updated resume attached. "
            "Would you be interested in moving forward with my profile?</p>"
            f"<p>Best regards,<br><strong>{name}</strong></p>"
            "</body></html>"
        )

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(plain, "plain"))
        alt.attach(MIMEText(html, "html"))
        msg.attach(alt)
        if resume and resume.exists():
            self._attach_resume(msg, resume)
        return msg

    @staticmethod
    def _compose_cold_msg(to: str, sender_email: str, subject: str,
                          body: str, resume: Path | None) -> MIMEMultipart:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = sender_email
        msg["To"]      = to
        html_body = body.replace("\n\n", "</p><p>").replace("\n", "<br>")
        html = (
            '<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222;">'
            f"<p>{html_body}</p></body></html>"
        )
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain"))
        alt.attach(MIMEText(html, "html"))
        msg.attach(alt)
        if resume and resume.exists():
            GmailSender._attach_resume(msg, resume)
        return msg

    # ── Public send API ───────────────────────────────────────────────────────

    def send_recruiter_email(self, to: str, job_title: str, company: str,
                             job_url: str) -> bool:
        """Dice outreach email with best-matching resume."""
        if to.lower() in self.sent_emails:
            return False
        try:
            svc = self.get_service()
            if not svc:
                return False
            # Load profile data for this sender so bullets use real info
            profile_data: dict = {}
            try:
                profiles_path = Path(__file__).parent / "profiles.json"
                if profiles_path.exists():
                    profile_data = json.loads(profiles_path.read_text()).get(
                        self.sender_email, {}
                    )
            except Exception:
                pass
            resume = self.pick_resume(job_title)
            msg    = self._compose_dice(to, job_title, company, job_url, resume, profile_data)
            raw    = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            label  = resume.name if resume else "none"
            self._log_sent(to, job_title, company, job_url, label)
            if resume:
                print(f"      → [Gmail] Resume attached: {resume.name}")
            return True
        except Exception as e:
            print(f"      → [Gmail] send error: {e}")
            return False

    def send_cold_email(self, to: str, subject: str, body: str,
                        resume: Path | None, source: str = "linkedin") -> bool:
        """LinkedIn / cold outreach email."""
        if to.lower() in self.sent_emails:
            return False
        try:
            svc = self.get_service()
            if not svc:
                return False
            msg = self._compose_cold_msg(to, self.sender_email, subject, body, resume)
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            self._log_sent(to, subject, source, "", resume.name if resume else "none")
            return True
        except Exception as e:
            print(f"      → [Gmail] cold email error: {e}")
            return False

    def fetch_verification_code(self, max_age_seconds: int = 120,
                               to_email: str = "") -> str | None:
        """
        Search recent Gmail inbox for an application email-verification code.
        Supports Greenhouse (6-digit numeric) and Stripe (8-char alphanumeric).
        Does NOT restrict sender domain — Stripe sends from stripe.com,
        Greenhouse from greenhouse.io; both are covered by the subject filter.
        """
        import time
        try:
            svc = self.get_service()
            if not svc:
                return None

            # Broad subject filter — no from: restriction so Stripe emails are included
            q = (
                "(subject:verification OR subject:code OR subject:confirm "
                "OR subject:security OR subject:\"your application\") "
                "newer_than:1d"
            )
            if to_email:
                q += f" to:{to_email}"
            result = svc.users().messages().list(
                userId="me", q=q, maxResults=5
            ).execute()

            cutoff_ms = (time.time() - max_age_seconds) * 1000

            for ref in result.get("messages", []):
                # Quick freshness check on metadata before fetching the full body
                meta = svc.users().messages().get(
                    userId="me", id=ref["id"], format="metadata"
                ).execute()
                if int(meta.get("internalDate", 0)) < cutoff_ms:
                    continue

                # Check snippet first — avoids a full fetch if snippet has the code
                snippet = meta.get("snippet", "")
                code = self._extract_code(snippet)
                if code:
                    return code

                # Full body fetch for the most recent matching message
                msg  = svc.users().messages().get(
                    userId="me", id=ref["id"], format="full"
                ).execute()
                body = self._extract_msg_body(msg)
                code = self._extract_code(body + "\n" + snippet)
                if code:
                    return code

                break  # only check the most recent qualifying message

        except Exception as e:
            print(f"  [Gmail] fetch_verification_code error: {e}", flush=True)
        return None

    @staticmethod
    def _extract_code(text: str) -> str | None:
        """Extract an 8-char or 6-digit verification code from email text/snippet."""
        # Greenhouse pattern: "application: NakeSwk3" (mixed-case 8-char)
        m = re.search(r'application[:\s]+([A-Za-z0-9]{8})\b', text, re.IGNORECASE)
        if m:
            return m.group(1)
        # Generic "code: XXXXXXXX" (mixed or uppercase, 8-char)
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

    @staticmethod
    def _extract_msg_body(msg: dict) -> str:
        """Decode the plain-text body from a Gmail API message dict."""
        def _decode(part: dict) -> str:
            data = part.get("body", {}).get("data", "")
            if not data:
                return ""
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

        payload = msg.get("payload", {})
        if payload.get("mimeType") == "text/plain":
            return _decode(payload)

        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                return _decode(part)
            for sub in part.get("parts", []):
                if sub.get("mimeType") == "text/plain":
                    return _decode(sub)

        # Fallback: join all decodable parts
        texts = []
        for part in payload.get("parts", []):
            t = _decode(part)
            if t:
                texts.append(t)
        return "\n".join(texts) or msg.get("snippet", "")

    def check_inbox_replies(self) -> list[dict]:
        replies = []
        if not self.sent_emails:
            return replies
        try:
            svc = self.get_service()
            if not svc:
                return replies
            from_parts = " OR ".join(f"from:{e}" for e in list(self.sent_emails)[:20])
            result = svc.users().messages().list(
                userId="me", q=f"({from_parts}) in:inbox", maxResults=20
            ).execute()
            for ref in result.get("messages", []):
                m = svc.users().messages().get(
                    userId="me", id=ref["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in m["payload"]["headers"]}
                replies.append({
                    "from":    headers.get("From", ""),
                    "subject": headers.get("Subject", ""),
                    "date":    headers.get("Date", ""),
                    "snippet": m.get("snippet", ""),
                })
        except Exception as e:
            print(f"  [Gmail] inbox check error: {e}")
        return replies


# ── Module-level helpers (email extraction — stateless) ───────────────────────

def extract_recruiter_emails(text: str) -> list[str]:
    found = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    seen, result = set(), []
    for email in found:
        el = email.lower()
        if any(p in el for p in _SKIP_ADDRS):
            continue
        if el not in seen:
            seen.add(el)
            result.append(email)
    return result


# ── Backward-compat module-level API (used by main.py) ───────────────────────
# main.py is sequential / single-profile so a shared default instance is fine.

_default = GmailSender()

# Expose the sent_emails set at module level so main.py can read it directly
@property
def _sent_emails_prop(self):
    return _default.sent_emails

SENT_EMAILS: set[str] = _default.sent_emails   # live reference — same object


def init_gmail(profile_dir: Path, sender_name: str = "Applicant",
               sender_email: str = "", resume_path: str = ""):
    global SENT_EMAILS
    _default.init(profile_dir, sender_name, sender_email, resume_path)
    SENT_EMAILS = _default.sent_emails          # re-point after init resets the set


def get_gmail_service():
    return _default.get_service()


def pick_resume(job_title: str) -> Path | None:
    return _default.pick_resume(job_title)


def send_recruiter_email(to: str, job_title: str, company: str,
                         job_url: str, sender_email: str) -> bool:
    return _default.send_recruiter_email(to, job_title, company, job_url)


def send_cold_email(to: str, subject: str, body: str,
                    resume: "Path | None", sender_email: str,
                    source: str = "linkedin") -> bool:
    return _default.send_cold_email(to, subject, body, resume, source)


def fetch_verification_code(max_age_seconds: int = 120,
                            to_email: str = "") -> str | None:
    return _default.fetch_verification_code(max_age_seconds, to_email=to_email)


def check_inbox_replies() -> list[dict]:
    return _default.check_inbox_replies()
