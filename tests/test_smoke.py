"""
SMOKE TESTS — dashboard.py
Quickly confirm the most critical features work after any build/change.
If any smoke test fails the dashboard is broken for every user.
"""
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
from dashboard import (
    _parse_ts, _sort_rows, _status_markup, _log_markup,
    _is_us_location, _infer_exp, _infer_industry,
    _load_rows, _load_saved, _save_filters, _load_company_options,
    SORT_MODES, _US_ONLY_SENTINEL, _COMPANY_OPTIONS,
    _EXP_OPTIONS, _DAYS_OPTIONS, _LOC_OPTIONS, _INDUSTRY_OPTIONS,
)
from datetime import datetime


class TestSmoke:
    """Critical happy-path checks — if these fail, nothing works."""

    def test_module_imports_cleanly(self):
        import dashboard as d
        assert d is not None

    def test_parse_ts_returns_datetime(self):
        result = _parse_ts("2026-09-08T12:00:00")
        assert isinstance(result, datetime)

    def test_sort_rows_does_not_crash(self):
        rows = [{"timestamp": "2026-01-01", "job_title": "A", "company": "X", "status": ""}]
        for mode, _ in SORT_MODES:
            result = _sort_rows(rows, mode)
            assert isinstance(result, list)

    def test_status_markup_all_statuses(self):
        for s in ("applied", "submitted", "skipped", "error: timeout", "pending"):
            result = _status_markup(s)
            assert isinstance(result, str)

    def test_log_markup_does_not_crash_on_normal_line(self):
        result = _log_markup("→ applied to Software Engineer at Acme")
        assert isinstance(result, str)

    def test_is_us_location_common_inputs(self):
        assert _is_us_location("Austin, TX") is True
        assert _is_us_location("Remote") is True
        assert _is_us_location("Toronto, ON") is False

    def test_infer_exp_common_titles(self):
        assert _infer_exp("Senior Engineer") == "Senior"
        assert _infer_exp("Junior Developer") == "Junior"
        assert _infer_exp("Software Engineer") == "Mid"

    def test_infer_industry_common_titles(self):
        assert _infer_industry("Software Engineer") == "engineering"
        assert _infer_industry("Data Scientist") == "data"
        assert _infer_industry("VP of Engineering") == "management"

    def test_load_rows_missing_file_returns_empty(self, tmp_path):
        dashboard.APPLIED_LOG_PATH = tmp_path / "no_file.csv"
        result = _load_rows("u@x.com", "", "", "", "")
        assert result == []

    def test_load_saved_missing_file_returns_empty(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "no_file.json"
        result = _load_saved("u@x.com")
        assert result == {}

    def test_load_company_options_missing_file_returns_empty(self, tmp_path):
        orig_applicable = dashboard.APPLICABLE_COMPANIES
        orig_db = dashboard.COMPANY_DB
        try:
            dashboard.APPLICABLE_COMPANIES = tmp_path / "no_applicable.json"
            dashboard.COMPANY_DB = tmp_path / "no_db.json"
            result = _load_company_options()
            assert result == []
        finally:
            dashboard.APPLICABLE_COMPANIES = orig_applicable
            dashboard.COMPANY_DB = orig_db

    def test_us_only_sentinel_constant_is_string(self):
        assert isinstance(_US_ONLY_SENTINEL, str)
        assert _US_ONLY_SENTINEL == "__us_only__"

    def test_sort_modes_all_have_key_and_label(self):
        for key, label in SORT_MODES:
            assert isinstance(key, str) and key
            assert isinstance(label, str) and label

    def test_dropdown_options_are_tuples(self):
        for opts in (_LOC_OPTIONS, _EXP_OPTIONS, _DAYS_OPTIONS, _INDUSTRY_OPTIONS):
            for label, val in opts:
                assert isinstance(label, str) and isinstance(val, str)

    def test_infer_exp_never_returns_none(self):
        for title in ("", "Engineer", "Senior ML Lead", "Intern", "Unknown Role XYZ"):
            assert _infer_exp(title) is not None

    def test_infer_industry_never_returns_none(self):
        for title in ("", "Engineer", "CEO", "Barista", "Unknown XYZ 123"):
            assert _infer_industry(title) is not None

    def test_parse_ts_never_crashes_on_garbage(self):
        for s in ("", "not-a-date", "9999-99-99", "2026", None):
            # should return datetime.min, not raise
            try:
                result = _parse_ts(s)
                assert result == datetime.min
            except Exception as e:
                assert False, f"_parse_ts({s!r}) raised: {e}"

    def test_status_markup_never_crashes_on_garbage(self):
        for s in ("", "x" * 100, "applied\n\tapplied", "中文", "🚀 applied"):
            result = _status_markup(s)
            assert isinstance(result, str)
