"""
Tests for recruiter_db.py — pure logic and CSV-backed CRUD.
"""
import pytest
import sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from recruiter_db import _clean_name, _company_from_email, RecruiterDB


class TestCleanName:
    def test_strips_email_angle_brackets(self):
        assert _clean_name("John Smith <john@acme.com>") == "John Smith"

    def test_strips_surrounding_quotes(self):
        assert _clean_name('"Jane Doe"') == "Jane Doe"

    def test_plain_name(self):
        assert _clean_name("Alice Johnson") == "Alice Johnson"

    def test_generic_token_returns_empty(self):
        assert _clean_name("Hiring Manager") == ""

    def test_noreply_returns_empty(self):
        assert _clean_name("noreply") == ""

    def test_recruiter_token_returns_empty(self):
        assert _clean_name("Recruiter") == ""

    def test_empty_string_returns_empty(self):
        assert _clean_name("") == ""

    def test_only_whitespace_returns_empty(self):
        assert _clean_name("   ") == ""

    def test_mixed_generic_and_real(self):
        # "Talent Acquisition" — both tokens are generic → returns ""
        assert _clean_name("Talent Acquisition") == ""

    def test_real_name_with_email(self):
        result = _clean_name("Bob Martinez <bob@startup.io>")
        assert result == "Bob Martinez"


class TestCompanyFromEmail:
    def test_simple_domain(self):
        assert _company_from_email("john@acme.com") == "Acme"

    def test_subdomain_stripped(self):
        assert _company_from_email("john@mail.google.com") == "Google"

    def test_hyphenated_domain(self):
        assert _company_from_email("hr@my-startup.io") == "My Startup"

    def test_generic_gmail_returns_empty(self):
        assert _company_from_email("john@gmail.com") == ""

    def test_generic_yahoo_returns_empty(self):
        assert _company_from_email("jane@yahoo.com") == ""

    def test_generic_outlook_returns_empty(self):
        assert _company_from_email("bob@outlook.com") == ""

    def test_no_at_sign_returns_empty(self):
        assert _company_from_email("notanemail") == ""

    def test_empty_string_returns_empty(self):
        assert _company_from_email("") == ""

    def test_title_case_applied(self):
        result = _company_from_email("hr@openai.com")
        assert result == "Openai"

    def test_two_part_domain(self):
        assert _company_from_email("hr@stripe.com") == "Stripe"


class TestRecruiterDB:
    @pytest.fixture
    def db(self, tmp_path):
        return RecruiterDB(path=tmp_path / "recruiters.csv")

    def test_upsert_and_get(self, db):
        db.upsert("alice@acme.com", name="Alice", company="Acme",
                  title="Recruiter", source="linkedin")
        rec = db.get("alice@acme.com")
        assert rec is not None
        assert rec["name"] == "Alice"
        assert rec["company"] == "Acme"

    def test_upsert_updates_existing(self, db):
        db.upsert("alice@acme.com", name="Alice", company="Acme",
                  title="Recruiter", source="linkedin")
        db.upsert("alice@acme.com", name="Alice B", company="Acme",
                  title="Senior Recruiter", source="linkedin")
        rec = db.get("alice@acme.com")
        assert rec["title"] == "Senior Recruiter"
        assert rec["name"] == "Alice B"

    def test_get_nonexistent_returns_none(self, db):
        assert db.get("nobody@nowhere.com") is None

    def test_count_increments(self, db):
        assert db.count() == 0
        db.upsert("a@a.com", source="linkedin")
        assert db.count() == 1
        db.upsert("b@b.com", source="linkedin")
        assert db.count() == 2

    def test_upsert_deduplicates_email(self, db):
        db.upsert("same@co.com", source="linkedin")
        db.upsert("same@co.com", source="gmail_inbox")
        assert db.count() == 1

    def test_times_contacted_increments(self, db):
        db.upsert("x@co.com", source="linkedin")
        rec1 = db.get("x@co.com")
        db.upsert("x@co.com", source="linkedin")
        rec2 = db.get("x@co.com")
        assert int(rec2.get("times_contacted", 0)) > int(rec1.get("times_contacted", 0))

    def test_all_records_returns_list(self, db):
        db.upsert("a@a.com", source="linkedin")
        db.upsert("b@b.com", source="linkedin")
        records = db.all_records()
        assert isinstance(records, list)
        assert len(records) == 2

    def test_stats_structure(self, db):
        db.upsert("a@a.com", source="linkedin", status="contacted")
        db.upsert("b@b.com", source="gmail_inbox", status="replied")
        stats = db.stats()
        assert isinstance(stats, dict)
        assert "total" in stats

    def test_persists_to_disk(self, tmp_path):
        path = tmp_path / "r.csv"
        db1 = RecruiterDB(path=path)
        db1.upsert("persist@co.com", name="Persist", source="linkedin")

        db2 = RecruiterDB(path=path)
        db2._load()
        assert db2.get("persist@co.com") is not None

    def test_email_normalized_lowercase(self, db):
        db.upsert("UPPER@Co.COM", source="linkedin")
        rec = db.get("upper@co.com")
        assert rec is not None
