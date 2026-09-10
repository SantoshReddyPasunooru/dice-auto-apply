"""
Recruiter Database
==================
Shared, thread-safe CSV database of every recruiter encountered by
linkedin_outreach.py and gmail_monitor.py.

Each recruiter is keyed by email address. Records are upserted on every
interaction so the file is always up-to-date after each run.

Usage:
  from recruiter_db import recruiter_db
  recruiter_db.upsert(
      email="sarah@acme.com",
      name="Sarah Johnson",
      company="Acme Corp",
      title="Senior Python Engineer",
      source="linkedin",
      status="contacted",
  )
  print(recruiter_db.count())   # total unique recruiters

Standalone:
  python recruiter_db.py          — print summary
  python recruiter_db.py --stats  — detailed breakdown
"""

import csv
import re
import threading
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).parent
DB_PATH = _HERE / "recruiters.csv"

FIELDS = [
    "email",
    "name",
    "company",
    "title",
    "location",
    "source",           # linkedin | gmail_inbox | gmail_promotions | gmail_social | gmail_updates
    "first_seen",
    "last_seen",
    "times_contacted",
    "last_status",      # contacted | replied | interview | rtr | offer | junk
    "notes",
]

# Email domains that don't tell us the company name
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "aol.com", "protonmail.com", "icloud.com", "mail.com",
    "ymail.com", "live.com", "msn.com", "me.com",
}

_GENERIC_NAME_TOKENS = {
    "hiring", "manager", "team", "hr", "recruiter", "talent", "acquisition",
    "staffing", "noreply", "no-reply", "hello", "info", "jobs", "careers",
    "support", "admin", "contact", "dear", "there", "notifications",
}


def _clean_name(raw: str) -> str:
    """Strip email part from 'Full Name <email>' and return the display name."""
    name = re.sub(r"<[^>]+>", "", raw).strip().strip('"').strip("'")
    # Reject strings that are clearly not names
    tokens = name.lower().split()
    if not tokens or all(t in _GENERIC_NAME_TOKENS for t in tokens):
        return ""
    return name


def _company_from_email(email: str) -> str:
    """Best-effort company name from the email domain."""
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1].lower()
    if domain in _GENERIC_DOMAINS:
        return ""
    # Drop subdomain if present (mail.acme.com → acme)
    parts = domain.split(".")
    name  = parts[-2] if len(parts) >= 2 else parts[0]
    return name.replace("-", " ").title()


class RecruiterDB:
    def __init__(self, path: Path = DB_PATH):
        self.path  = path
        self._db:  dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    email = row.get("email", "").strip().lower()
                    if email:
                        # Fill missing fields so older rows still work
                        for field in FIELDS:
                            row.setdefault(field, "")
                        self._db[email] = dict(row)
        except Exception as e:
            print(f"[RecruiterDB] load error: {e}")

    def _save_locked(self):
        """Write CSV — must be called while holding self._lock."""
        try:
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(self._db.values())
        except Exception as e:
            print(f"[RecruiterDB] save error: {e}")

    # ── Public API ────────────────────────────────────────────────────────────

    def upsert(
        self,
        email:    str,
        name:     str = "",
        company:  str = "",
        title:    str = "",
        location: str = "",
        source:   str = "",
        status:   str = "",
        notes:    str = "",
    ):
        """
        Insert a new recruiter or update an existing one.
        - name/company: only overwrite if the stored value is blank
        - source: accumulated as comma-separated list of unique values
        - times_contacted: incremented on every call
        - last_status: always updated when provided
        """
        email = email.strip().lower()
        if not email or "@" not in email:
            return

        # Auto-fill company from domain if caller didn't provide one
        if not company:
            company = _company_from_email(email)

        # Clean display name
        name = _clean_name(name) if name else ""

        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        with self._lock:
            if email in self._db:
                rec = self._db[email]
                if name:
                    rec["name"] = name
                if company and not rec.get("company"):
                    rec["company"] = company
                if title:
                    rec["title"] = title
                if location and not rec.get("location"):
                    rec["location"] = location
                # Accumulate sources without duplicates
                if source:
                    existing_sources = [s.strip() for s in rec.get("source", "").split(",") if s.strip()]
                    if source not in existing_sources:
                        existing_sources.append(source)
                    rec["source"] = ",".join(existing_sources)
                rec["last_seen"]       = now
                rec["times_contacted"] = str(int(rec.get("times_contacted") or 0) + 1)
                if status:
                    rec["last_status"] = status
                if notes:
                    rec["notes"] = notes
            else:
                self._db[email] = {
                    "email":           email,
                    "name":            name,
                    "company":         company,
                    "title":           title,
                    "location":        location,
                    "source":          source,
                    "first_seen":      now,
                    "last_seen":       now,
                    "times_contacted": "1",
                    "last_status":     status or "contacted",
                    "notes":           notes,
                }
            self._save_locked()

    def get(self, email: str) -> dict | None:
        rec = self._db.get(email.strip().lower())
        return dict(rec) if rec is not None else None

    def count(self) -> int:
        return len(self._db)

    def all_records(self) -> list[dict]:
        with self._lock:
            return list(self._db.values())

    def stats(self) -> dict:
        records = self.all_records()
        by_status:  dict[str, int] = {}
        by_source:  dict[str, int] = {}
        by_company: dict[str, int] = {}
        for r in records:
            s = r.get("last_status", "")
            by_status[s] = by_status.get(s, 0) + 1
            for src in r.get("source", "").split(","):
                src = src.strip()
                if src:
                    by_source[src] = by_source.get(src, 0) + 1
            co = r.get("company", "")
            if co:
                by_company[co] = by_company.get(co, 0) + 1
        return {
            "total":      len(records),
            "by_status":  by_status,
            "by_source":  by_source,
            "by_company": dict(sorted(by_company.items(), key=lambda x: -x[1])[:10]),
        }


# ── Module-level singleton ────────────────────────────────────────────────────
recruiter_db = RecruiterDB()


# ── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    db = RecruiterDB()
    s  = db.stats()

    print(f"\n  Recruiter Database  —  {db.path}")
    print(f"  Total unique recruiters: {s['total']}\n")

    if "--stats" in sys.argv or "-s" in sys.argv:
        print("  By status:")
        for k, v in sorted(s["by_status"].items(), key=lambda x: -x[1]):
            print(f"    {k:<20} {v}")
        print("\n  By source:")
        for k, v in sorted(s["by_source"].items(), key=lambda x: -x[1]):
            print(f"    {k:<25} {v}")
        print("\n  Top 10 companies:")
        for k, v in s["by_company"].items():
            print(f"    {k:<30} {v}")
    else:
        print("  Run with --stats for a detailed breakdown.")

    print()
