"""
COMPATIBILITY TESTS — dashboard.py
Verify correct behaviour across:
  • CSV encodings: UTF-8 BOM, UTF-8 no-BOM, Latin-1
  • CSV line endings: Unix LF, Windows CRLF
  • CSV structure: missing columns, extra columns, headers-only
  • Platform-specific code paths (Darwin vs Linux opener)
  • JSON edge cases: BOM, pretty-printed, minified
  • Timestamp format variations
"""
import csv, json, sys, pathlib, platform
from datetime import datetime, timedelta
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
from dashboard import (
    _load_rows, _parse_ts, _sort_rows, _save_filters, _load_saved,
    _load_company_options, APPLIED_LOG_PATH,
)

FIELDS = ["timestamp", "profile_email", "job_title", "company", "location", "status"]


def _write_rows(path, rows, encoding="utf-8", bom=False):
    with open(path, "w", encoding=encoding, newline="") as f:
        if bom:
            f.write("﻿")  # BOM character
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def _sample_rows(email="u@x.com"):
    now = datetime.now()
    return [
        {
            "timestamp": (now - timedelta(days=1)).isoformat(),
            "profile_email": email,
            "job_title": "Senior Engineer",
            "company": "Acme",
            "location": "Austin, TX",
            "status": "applied",
        },
        {
            "timestamp": (now - timedelta(days=5)).isoformat(),
            "profile_email": email,
            "job_title": "Data Analyst",
            "company": "Beta",
            "location": "Remote",
            "status": "skipped",
        },
    ]


class TestCsvEncodings:

    def test_utf8_no_bom_reads_correctly(self, tmp_path):
        path = tmp_path / "applied.csv"
        _write_rows(path, _sample_rows())
        dashboard.APPLIED_LOG_PATH = path
        rows = _load_rows("u@x.com", "", "", "", "")
        assert len(rows) == 2

    def test_utf8_with_bom_timestamps_parsed(self, tmp_path):
        """BOM-prefixed CSV must still return parseable timestamps.
        Python's csv module with encoding='utf-8' includes the BOM as part of
        the first field name, breaking column lookups by name.
        Fix: use encoding='utf-8-sig' which strips the BOM automatically."""
        path = tmp_path / "applied.csv"
        _write_rows(path, _sample_rows(), bom=True)
        dashboard.APPLIED_LOG_PATH = path
        rows = _load_rows("u@x.com", "", "", "", "")
        assert len(rows) == 2
        # If BOM is not handled, timestamps will be empty strings
        timestamps = [r.get("timestamp", "") for r in rows]
        assert all(ts != "" for ts in timestamps), (
            "Timestamps are empty — BOM in CSV broke column name lookup. "
            "Fix: use encoding='utf-8-sig' in _load_rows."
        )

    def test_utf8_with_bom_sort_by_date_works(self, tmp_path):
        """BOM breaks date-sort if timestamps are empty strings (all parse to datetime.min)."""
        path = tmp_path / "applied.csv"
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(days=3)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Older Job",
             "company": "A", "location": "Remote", "status": "applied"},
            {"timestamp": (now - timedelta(days=1)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Newer Job",
             "company": "B", "location": "Remote", "status": "applied"},
        ]
        _write_rows(path, rows, bom=True)
        dashboard.APPLIED_LOG_PATH = path
        loaded = _load_rows("u@x.com", "", "", "", "")
        sorted_rows = _sort_rows(loaded, "recent_first")
        assert sorted_rows[0]["job_title"] == "Newer Job", (
            "recent_first sort is broken — BOM corrupted timestamp column."
        )

    def test_windows_crlf_csv_reads_correctly(self, tmp_path):
        """Windows-style CRLF line endings must be handled by csv.DictReader."""
        path = tmp_path / "applied.csv"
        header = ",".join(FIELDS)
        row = "2026-09-01T10:00:00,u@x.com,Engineer,Acme,Remote,applied"
        content = f"{header}\r\n{row}\r\n"
        path.write_bytes(content.encode("utf-8"))
        dashboard.APPLIED_LOG_PATH = path
        rows = _load_rows("u@x.com", "", "", "", "")
        assert len(rows) == 1
        assert rows[0]["job_title"] == "Engineer"

    def test_latin1_csv_does_not_crash(self, tmp_path):
        """Non-UTF-8 encoded CSV must not raise — _load_rows silently returns []."""
        path = tmp_path / "applied.csv"
        header = ",".join(FIELDS) + "\n"
        row = "2026-09-01T10:00:00,u@x.com,Ingénieur,Acme,Remote,applied\n"
        path.write_bytes(header.encode("utf-8") + row.encode("latin-1"))
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "")
        # Either returns the rows (if byte sequence happens to be valid UTF-8) or []
        assert isinstance(result, list)

    def test_csv_with_extra_columns_does_not_crash(self, tmp_path):
        """CSV with additional columns beyond the expected schema must load gracefully."""
        path = tmp_path / "applied.csv"
        extended_fields = FIELDS + ["salary", "recruiter_email"]
        row = {
            "timestamp": "2026-09-01T10:00:00",
            "profile_email": "u@x.com",
            "job_title": "Engineer",
            "company": "Acme",
            "location": "Remote",
            "status": "applied",
            "salary": "150000",
            "recruiter_email": "hr@acme.com",
        }
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=extended_fields)
            w.writeheader()
            w.writerow(row)
        dashboard.APPLIED_LOG_PATH = path
        rows = _load_rows("u@x.com", "", "", "", "")
        assert len(rows) == 1
        assert rows[0]["job_title"] == "Engineer"

    def test_csv_with_missing_optional_columns_does_not_crash(self, tmp_path):
        """CSV that is missing non-critical columns must not crash."""
        path = tmp_path / "applied.csv"
        minimal_fields = ["profile_email", "job_title", "status"]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=minimal_fields)
            w.writeheader()
            w.writerow({"profile_email": "u@x.com", "job_title": "Engineer", "status": "applied"})
        dashboard.APPLIED_LOG_PATH = path
        rows = _load_rows("u@x.com", "", "", "", "")
        assert isinstance(rows, list)

    def test_csv_headers_only_no_data_rows(self, tmp_path):
        """CSV with only header row and no data should return empty list."""
        path = tmp_path / "applied.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
        dashboard.APPLIED_LOG_PATH = path
        rows = _load_rows("u@x.com", "", "", "", "")
        assert rows == []

    def test_empty_csv_file_does_not_crash(self, tmp_path):
        """Completely empty file must not crash."""
        path = tmp_path / "applied.csv"
        path.write_text("")
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "")
        assert isinstance(result, list)

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH


class TestTimestampFormats:
    """Various ISO 8601 timestamp variants that may appear in the CSV."""

    def test_datetime_with_microseconds(self):
        r = _parse_ts("2026-09-08T12:30:45.123456")
        assert r.year == 2026 and r.microsecond == 123456

    def test_datetime_without_time(self):
        r = _parse_ts("2026-09-08")
        assert r.year == 2026 and r.month == 9 and r.day == 8

    def test_datetime_with_timezone_utc(self):
        # Python 3.11+ fromisoformat handles +00:00
        r = _parse_ts("2026-09-08T12:00:00+00:00")
        assert isinstance(r, datetime)

    def test_none_input_returns_min(self):
        """None passed to _parse_ts must not raise — should return datetime.min."""
        result = _parse_ts(None)
        assert result == datetime.min

    def test_integer_input_returns_min(self):
        result = _parse_ts(12345)
        assert result == datetime.min

    def test_z_suffix_timestamp(self):
        """Some APIs return Z suffix for UTC — must not crash."""
        r = _parse_ts("2026-09-08T12:00:00Z")
        # May fail on older Python; should at least not raise unhandled exception
        assert isinstance(r, datetime)


class TestJsonCompatibility:
    """Saved-filters JSON must survive various encodings and formats."""

    def test_unicode_in_filter_values_round_trips(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "filters.json"
        _save_filters("u@x.com", "东京", "senior", "7", "機械学習")
        saved = _load_saved("u@x.com")
        assert saved["locations"] == ["東京"] or saved["locations"] == ["东京"]
        assert "機械学習" in saved.get("keywords", "")

    def test_emoji_in_filter_values_round_trips(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "filters.json"
        _save_filters("u@x.com", "🌍 Remote", "", "", "")
        saved = _load_saved("u@x.com")
        assert saved["locations"] == ["🌍 Remote"]

    def test_malformed_json_returns_empty(self, tmp_path):
        path = tmp_path / "filters.json"
        path.write_text("{this is not valid json}")
        dashboard.SAVED_FILTERS = path
        result = _load_saved("u@x.com")
        assert result == {}

    def test_truncated_json_returns_empty(self, tmp_path):
        path = tmp_path / "filters.json"
        path.write_text('{"u@x.com": {"locations": [')  # truncated
        dashboard.SAVED_FILTERS = path
        result = _load_saved("u@x.com")
        assert result == {}


class TestCompanyDbCompatibility:
    """Company DB JSON variations."""

    def test_company_db_with_unicode_names(self, tmp_path):
        db = {"airbnb": {"name": "Airbnb"}, "東京": {"name": "東京Tech"}}
        path = tmp_path / "company_db.json"
        path.write_text(json.dumps(db, ensure_ascii=False))
        dashboard.COMPANY_DB = path
        opts = _load_company_options()
        names = [n for n, _ in opts]
        assert "Airbnb" in names

    def test_company_db_missing_name_field_skipped_gracefully(self, tmp_path):
        """Records without 'name' key must not crash _load_company_options."""
        db = {
            "good":    {"name": "GoodCo"},
            "bad":     {"title": "no name field here"},  # missing "name"
            "alsobad": {},
        }
        path = tmp_path / "company_db.json"
        path.write_text(json.dumps(db))
        dashboard.COMPANY_DB = path
        # Should not raise; either returns partial list or []
        result = _load_company_options()
        assert isinstance(result, list)
