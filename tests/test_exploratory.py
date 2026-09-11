"""
EXPLORATORY TESTS — dashboard.py
Systematically probe unexpected, boundary, and adversarial inputs that a
formal spec might not anticipate.  Organised into:
  • Fuzz / random-input stability
  • Boundary values (0, 1, max, empty, whitespace-only)
  • Surprising valid inputs (unicode, emoji, very long strings, nulls)
  • Logic edge-cases discovered through exploration
  • Regression traps (things that look fine but broke before)
"""
import csv, sys, pathlib, random, string
from datetime import datetime, timedelta
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
from dashboard import (
    _parse_ts, _sort_rows, _status_markup, _log_markup,
    _is_us_location, _infer_exp, _infer_industry,
    _load_rows, _save_filters, _load_saved, _load_company_options,
    APPLIED_LOG_PATH,
)

FIELDS = ["timestamp", "profile_email", "job_title", "company", "location", "status"]


def _make_csv(tmp_path, rows):
    path = tmp_path / "applied.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


# ── Fuzz: random inputs must never raise unhandled exceptions ─────────────────

class TestFuzzNoCrash:

    @staticmethod
    def _random_str(n=50):
        chars = string.printable + "中文にほんご한국어🚀💥⚠️"
        return "".join(random.choice(chars) for _ in range(n))

    def test_parse_ts_random_strings_never_crash(self):
        for _ in range(200):
            s = self._random_str()
            try:
                result = _parse_ts(s)
                assert isinstance(result, datetime)
            except Exception as e:
                assert False, f"_parse_ts raised on {s!r}: {e}"

    def test_status_markup_random_strings_never_crash(self):
        for _ in range(200):
            s = self._random_str()
            try:
                result = _status_markup(s)
                assert isinstance(result, str)
            except Exception as e:
                assert False, f"_status_markup raised on {s!r}: {e}"

    def test_log_markup_random_strings_never_crash(self):
        for _ in range(200):
            s = self._random_str()
            try:
                result = _log_markup(s)
                assert isinstance(result, str)
            except Exception as e:
                assert False, f"_log_markup raised on {s!r}: {e}"

    def test_is_us_location_random_strings_never_crash(self):
        for _ in range(200):
            s = self._random_str()
            try:
                result = _is_us_location(s)
                assert isinstance(result, bool)
            except Exception as e:
                assert False, f"_is_us_location raised on {s!r}: {e}"

    def test_infer_industry_random_strings_never_crash(self):
        for _ in range(200):
            s = self._random_str()
            try:
                result = _infer_industry(s)
                assert isinstance(result, str)
            except Exception as e:
                assert False, f"_infer_industry raised on {s!r}: {e}"

    def test_infer_exp_random_strings_never_crash(self):
        for _ in range(200):
            s = self._random_str()
            try:
                result = _infer_exp(s)
                assert isinstance(result, str)
            except Exception as e:
                assert False, f"_infer_exp raised on {s!r}: {e}"

    def test_log_markup_none_does_not_crash(self):
        """None passed to _log_markup must not raise AttributeError on .rstrip()."""
        try:
            result = _log_markup(None)
            assert isinstance(result, str)
        except Exception as e:
            assert False, f"_log_markup(None) raised: {e}"

    def test_sort_rows_with_malformed_dicts_does_not_crash(self):
        rows = [
            {},                          # empty dict
            {"job_title": None},         # None value
            {"timestamp": 12345},        # wrong type
            {"company": ["list", "val"]} # wrong type
        ]
        for mode, _ in [("recent_first", ""), ("az", ""), ("company_az", "")]:
            try:
                result = _sort_rows(rows, mode)
                assert isinstance(result, list)
            except Exception as e:
                assert False, f"_sort_rows({mode!r}) crashed on malformed rows: {e}"


# ── Boundary values ───────────────────────────────────────────────────────────

class TestBoundaryValues:

    def test_parse_ts_exactly_at_epoch(self):
        result = _parse_ts("1970-01-01T00:00:00")
        assert result == datetime(1970, 1, 1, 0, 0, 0)

    def test_parse_ts_far_future(self):
        result = _parse_ts("9999-12-31T23:59:59")
        assert result == datetime(9999, 12, 31, 23, 59, 59)

    def test_status_markup_exactly_18_chars(self):
        s = "a" * 18
        result = _status_markup(s)
        assert s in result  # all 18 chars present

    def test_status_markup_exactly_19_chars_truncated(self):
        s = "a" * 19
        result = _status_markup(s)
        assert "a" * 18 in result
        # The 19th char must not appear (truncated at 18)
        # Note: unknown status returns s[:18] directly
        assert result == s[:18]

    def test_sort_rows_two_items(self):
        rows = [
            {"timestamp": "2026-01-01", "job_title": "B", "company": "X", "status": ""},
            {"timestamp": "2026-03-01", "job_title": "A", "company": "Y", "status": ""},
        ]
        result = _sort_rows(rows, "recent_first")
        assert result[0]["timestamp"] == "2026-03-01"
        assert result[1]["timestamp"] == "2026-01-01"

    def test_load_rows_days_one_includes_recent_excludes_old(self, tmp_path):
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(hours=12)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Recent",
             "company": "A", "location": "Remote", "status": "applied"},
            {"timestamp": (now - timedelta(days=3)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Old",
             "company": "B", "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "1")
        titles = [r["job_title"] for r in result]
        assert "Recent" in titles
        assert "Old" not in titles

    def test_load_rows_days_very_large_includes_all(self, tmp_path):
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(days=365 * 5)).isoformat(),
             "profile_email": "u@x.com", "job_title": "VeryOld",
             "company": "A", "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "9999")
        assert any(r["job_title"] == "VeryOld" for r in result)

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH


# ── Surprising valid inputs ───────────────────────────────────────────────────

class TestSurprisingInputs:

    def test_is_us_location_whitespace_only(self):
        result = _is_us_location("   ")
        assert isinstance(result, bool)

    def test_is_us_location_all_caps_state(self):
        assert _is_us_location("AUSTIN, TX") is True

    def test_is_us_location_mixed_case(self):
        assert _is_us_location("Austin, Tx") is True

    def test_is_us_location_with_emoji(self):
        result = _is_us_location("🇺🇸 Remote, TX")
        assert isinstance(result, bool)

    def test_infer_industry_all_caps(self):
        result = _infer_industry("SENIOR SOFTWARE ENGINEER")
        assert result == "engineering"

    def test_infer_exp_all_caps(self):
        result = _infer_exp("SENIOR ENGINEER")
        assert result == "Senior"

    def test_infer_industry_with_punctuation(self):
        result = _infer_industry("Sr. Software Engineer (Remote)")
        assert result == "engineering"

    def test_status_markup_with_newlines(self):
        result = _status_markup("applied\napplied")
        assert isinstance(result, str)

    def test_status_markup_with_tabs(self):
        result = _status_markup("applied\tapplied")
        assert isinstance(result, str)

    def test_log_markup_empty_string(self):
        result = _log_markup("")
        assert result == ""

    def test_log_markup_only_spaces(self):
        result = _log_markup("   ")
        assert isinstance(result, str)

    def test_log_markup_only_brackets(self):
        result = _log_markup("[[[]]]]")
        assert isinstance(result, str)
        assert "\\[" in result  # must be escaped

    def test_log_markup_unicode_arrow(self):
        result = _log_markup("→ applied")
        assert "green" in result

    def test_infer_industry_number_in_title(self):
        result = _infer_industry("Level 4 Software Engineer")
        assert result == "engineering"

    def test_infer_exp_with_roman_numeral_collision(self):
        # "III" in title → Senior; make sure "VIII" doesn't match "II" as Mid
        result = _infer_exp("Software Engineer VIII")
        # "viii" — does \biii\b match? No. does \biv\b match? No.
        # Should fall through to Mid (default)
        assert result in ("Senior", "Mid")  # both are acceptable


# ── Logic edge-cases ──────────────────────────────────────────────────────────

class TestLogicEdgeCases:

    def test_us_only_filters_europe_cities(self, tmp_path):
        now = datetime.now()
        rows = [
            {"timestamp": now.isoformat(), "profile_email": "u@x.com",
             "job_title": "Engineer", "company": "A",
             "location": "London, UK", "status": "applied"},
            {"timestamp": now.isoformat(), "profile_email": "u@x.com",
             "job_title": "Engineer", "company": "B",
             "location": "Berlin, Germany", "status": "applied"},
            {"timestamp": now.isoformat(), "profile_email": "u@x.com",
             "job_title": "Engineer", "company": "C",
             "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "__us_only__", "", "")
        locations = [r["location"] for r in result]
        assert "London, UK"     not in locations
        assert "Berlin, Germany" not in locations
        assert "Remote"         in locations

    def test_industry_filter_does_not_match_other_category(self, tmp_path):
        rows = [
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "u@x.com",
             "job_title": "Barista",  # → "other" industry
             "company": "A", "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "", f_industry="engineering")
        assert len(result) == 0

    def test_days_filter_silent_with_invalid_value(self, tmp_path):
        """Invalid days value ('abc') must be silently ignored — all rows returned."""
        rows = [
            {"timestamp": "2020-01-01T00:00:00", "profile_email": "u@x.com",
             "job_title": "Old Job", "company": "A",
             "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "abc")
        # Invalid days value → filter ignored → old job is returned
        assert len(result) == 1

    def test_sort_preserves_all_rows(self, tmp_path):
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(days=i)).isoformat(),
             "profile_email": "u@x.com", "job_title": f"Job{i}",
             "company": "A", "location": "Remote", "status": "applied"}
            for i in range(20)
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        loaded = _load_rows("u@x.com", "", "", "", "")
        for mode, _ in [("recent_first", ""), ("az", ""), ("status", ""), ("company_az", "")]:
            sorted_rows = _sort_rows(loaded, mode)
            assert len(sorted_rows) == 20, f"Sort mode {mode!r} dropped rows"

    def test_email_filter_is_exact_match_not_substring(self, tmp_path):
        rows = [
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "u@x.com",
             "job_title": "A", "company": "A", "location": "Remote", "status": "applied"},
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "longu@x.com",
             "job_title": "B", "company": "B", "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "")
        assert len(result) == 1  # "longu@x.com" must NOT match "u@x.com"

    def test_keyword_filter_does_not_match_company_field(self, tmp_path):
        """Keyword filter must only match job_title, not company or location."""
        rows = [
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "u@x.com",
             "job_title": "Designer",  # no "engineer" in title
             "company": "Engineering Corp",  # "engineer" in company name
             "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "engineer", "", "", "")
        # Company name match must NOT count — only job_title
        assert len(result) == 0

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH


# ── Regression traps ──────────────────────────────────────────────────────────

class TestRegressionTraps:
    """Things that look fine but have broken before or are easy to re-break."""

    def test_skipped_applied_combo_does_not_count_as_applied(self):
        markup = _status_markup("skipped - already applied")
        assert "dim" in markup  # must be dim
        assert "bold green" not in markup  # must NOT be bold green

    def test_remote_canada_excluded_but_remote_us_included(self, tmp_path):
        rows = [
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "u@x.com",
             "job_title": "A", "company": "X", "location": "Remote, Canada", "status": "applied"},
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "u@x.com",
             "job_title": "B", "company": "Y", "location": "Remote in US", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "__us_only__", "", "")
        locs = [r["location"] for r in result]
        assert "Remote, Canada" not in locs
        assert "Remote in US" in locs

    def test_engineering_manager_is_management_not_engineering(self):
        # "Engineering Manager" — "Engineering" alone doesn't match \bengineer\b
        assert _infer_industry("Engineering Manager") == "management"

    def test_vp_engineering_is_management_not_engineering(self):
        assert _infer_industry("VP of Engineering") == "management"

    def test_sort_recent_first_uses_actual_timestamps_not_order(self):
        # Rows intentionally in non-chronological order
        rows = [
            {"timestamp": "2026-06-01", "job_title": "June",   "company": "A", "status": ""},
            {"timestamp": "2026-01-01", "job_title": "January", "company": "B", "status": ""},
            {"timestamp": "2026-09-01", "job_title": "Sept",   "company": "C", "status": ""},
        ]
        result = _sort_rows(rows, "recent_first")
        assert result[0]["job_title"] == "Sept"
        assert result[-1]["job_title"] == "January"

    def test_sort_recent_last_uses_actual_timestamps_not_order(self):
        rows = [
            {"timestamp": "2026-09-01", "job_title": "Sept",    "company": "A", "status": ""},
            {"timestamp": "2026-06-01", "job_title": "June",    "company": "B", "status": ""},
            {"timestamp": "2026-01-01", "job_title": "January", "company": "C", "status": ""},
        ]
        result = _sort_rows(rows, "recent_last")
        assert result[0]["job_title"] == "January"
        assert result[-1]["job_title"] == "Sept"

    def test_content_marketing_manager_is_not_management(self):
        assert _infer_industry("Content Marketing Manager") == "marketing"

    def test_customer_success_manager_is_not_management(self):
        assert _infer_industry("Customer Success Manager") == "support"

    def test_program_manager_is_not_management(self):
        assert _infer_industry("Program Manager") == "operations"

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH
