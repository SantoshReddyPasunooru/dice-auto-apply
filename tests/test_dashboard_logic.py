"""
Tests for pure filter/sort/markup logic in dashboard.py.
"""
import pytest
import sys, pathlib, csv, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
from dashboard import (
    _parse_ts, _sort_rows, _status_markup, _log_markup,
    _load_rows, _infer_exp, _infer_industry, _is_us_location,
    SORT_MODES,
)


class TestParseTs:
    def test_iso_format(self):
        from datetime import datetime
        result = _parse_ts("2026-09-08T12:00:00")
        assert isinstance(result, datetime)

    def test_invalid_returns_min(self):
        from datetime import datetime
        result = _parse_ts("not-a-date")
        assert result == datetime.min

    def test_empty_returns_min(self):
        from datetime import datetime
        result = _parse_ts("")
        assert result == datetime.min


class TestSortRows:
    @pytest.fixture
    def rows(self):
        return [
            {"timestamp": "2026-01-01", "job_title": "Engineer",   "company": "Zebra",  "status": "applied"},
            {"timestamp": "2026-03-01", "job_title": "Analyst",    "company": "Apple",  "status": "error: timeout"},
            {"timestamp": "2026-02-01", "job_title": "Manager",    "company": "Microsoft", "status": "skipped"},
        ]

    def test_recent_first(self, rows):
        result = _sort_rows(rows, "recent_first")
        assert result[0]["timestamp"] == "2026-03-01"

    def test_recent_last(self, rows):
        result = _sort_rows(rows, "recent_last")
        assert result[0]["timestamp"] == "2026-01-01"

    def test_az(self, rows):
        result = _sort_rows(rows, "az")
        assert result[0]["job_title"] == "Analyst"

    def test_za(self, rows):
        result = _sort_rows(rows, "za")
        assert result[0]["job_title"] == "Manager"

    def test_company_az(self, rows):
        result = _sort_rows(rows, "company_az")
        assert result[0]["company"] == "Apple"

    def test_status(self, rows):
        result = _sort_rows(rows, "status")
        assert result[0]["status"] == "applied"

    def test_unknown_mode_returns_unchanged(self, rows):
        result = _sort_rows(rows, "nonexistent")
        assert result == rows


class TestStatusMarkup:
    def test_applied_is_green(self):
        markup = _status_markup("applied")
        assert "green" in markup

    def test_submitted_is_green(self):
        markup = _status_markup("submitted")
        assert "green" in markup

    def test_error_is_red(self):
        markup = _status_markup("error: timeout")
        assert "red" in markup

    def test_skipped_is_dim(self):
        markup = _status_markup("skipped - already applied")
        assert "dim" in markup

    def test_unknown_returns_truncated(self):
        markup = _status_markup("some unknown status")
        assert "some unknown" in markup


class TestLogMarkup:
    def test_profile_line_is_dim_green(self):
        markup = _log_markup("[profile] 'First Name*' → 'John'")
        assert "green" in markup

    def test_saved_line_is_dim_green(self):
        markup = _log_markup("[saved] 'Phone*' → '1234567890'")
        assert "green" in markup

    def test_ollama_line_is_cyan(self):
        markup = _log_markup("[ollama] 'Why Anthropic?' → (generated 200 chars)")
        assert "cyan" in markup

    def test_applied_line_is_bold_green(self):
        markup = _log_markup("→ applied")
        assert "green" in markup

    def test_error_line_is_bold_red(self):
        markup = _log_markup("→ error: submit button not found")
        assert "red" in markup

    def test_verification_line_is_yellow(self):
        markup = _log_markup("→ [Verification] Code screen detected")
        assert "yellow" in markup

    def test_brackets_escaped(self):
        markup = _log_markup("some [bracket] text")
        assert "\\[" in markup


class TestLoadRows:
    @pytest.fixture
    def csv_file(self, tmp_path):
        path = tmp_path / "applied.csv"
        rows = [
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "a@b.com",
             "job_title": "Senior Engineer", "company": "Acme",
             "location": "Austin, TX", "status": "applied"},
            {"timestamp": "2026-09-02T10:00:00", "profile_email": "a@b.com",
             "job_title": "Data Analyst", "company": "Beta",
             "location": "Remote, Canada", "status": "error: timeout"},
            {"timestamp": "2026-09-03T10:00:00", "profile_email": "other@x.com",
             "job_title": "Product Manager", "company": "Gamma",
             "location": "New York, NY", "status": "applied"},
        ]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        # Monkeypatch APPLIED_LOG_PATH
        import dashboard as dash
        dash.APPLIED_LOG_PATH = path
        yield path

    def test_filters_by_email(self, csv_file):
        rows = _load_rows("a@b.com", "", "", "", "")
        assert len(rows) == 2

    def test_filters_by_keyword(self, csv_file):
        rows = _load_rows("a@b.com", "engineer", "", "", "")
        assert all("engineer" in r["job_title"].lower() for r in rows)
        assert len(rows) == 1

    def test_filters_by_us_only(self, csv_file):
        rows = _load_rows("a@b.com", "", "__us_only__", "", "")
        locations = [r["location"] for r in rows]
        assert "Remote, Canada" not in locations

    def test_filters_by_industry(self, csv_file):
        rows = _load_rows("a@b.com", "", "", "", "", f_industry="engineering")
        assert all(_infer_industry(r["job_title"]) == "engineering" for r in rows)

    def test_no_filters_returns_all_for_email(self, csv_file):
        rows = _load_rows("a@b.com", "", "", "", "")
        assert len(rows) == 2

    def test_nonexistent_email_returns_empty(self, csv_file):
        rows = _load_rows("nobody@x.com", "", "", "", "")
        assert rows == []
