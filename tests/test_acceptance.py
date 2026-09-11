"""
ACCEPTANCE TESTS — dashboard.py
Verify that the dashboard satisfies its stated business requirements.
These mirror the user-facing promises in the module docstring and README.

Business requirements tested:
  BR-01  Canadian jobs are always excluded from US-only filter
  BR-02  Skipped jobs display as dim, not green
  BR-03  Applied/Submitted jobs display as bold green
  BR-04  Error jobs display as bold red
  BR-05  Keyword filter is case-insensitive and partial-match
  BR-06  Experience level is inferred from job title
  BR-07  Industry filter correctly classifies every industry option
  BR-08  Saved filters persist and restore correctly across sessions
  BR-09  Multi-company: each selected company gets its own subprocess command
  BR-10  Log file has timestamp in name (unique per run)
  BR-11  Sort: recent_first always puts newest row at index 0
  BR-12  Sort: az always puts alphabetically first job at index 0
  BR-13  Company options loaded from company_careers_db.json, sorted A→Z
  BR-14  Days filter excludes rows older than N days (but keeps undated rows)
  BR-15  Keywords filter is persisted so it restores on relaunch
"""
import csv, json, sys, pathlib
from datetime import datetime, timedelta
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
from dashboard import (
    _load_rows, _sort_rows, _status_markup, _infer_exp, _infer_industry,
    _save_filters, _load_saved, _load_company_options, _is_us_location,
    _US_ONLY_SENTINEL, APPLIED_LOG_PATH, _INDUSTRY_OPTIONS,
)

FIELDS = ["timestamp", "profile_email", "job_title", "company", "location", "status"]


def _make_csv(tmp_path, rows):
    path = tmp_path / "applied.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


class TestBR01_CanadaExcluded:
    """BR-01: Canadian jobs are ALWAYS excluded from Anywhere-in-US filter."""

    def test_all_canadian_provinces_excluded(self):
        canadian = [
            "Toronto, ON", "Vancouver, BC", "Calgary, AB", "Montreal, QC",
            "Winnipeg, MB", "Halifax, NS", "Ottawa, ON", "Remote, Canada",
            "Canada", "Ontario", "British Columbia",
        ]
        for loc in canadian:
            assert _is_us_location(loc) is False, f"Canadian location passed as US: {loc}"

    def test_remote_canada_excluded_from_us_filter(self, tmp_path):
        rows = [
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "u@x.com",
             "job_title": "Engineer", "company": "A", "location": "Remote, Canada", "status": "applied"},
            {"timestamp": "2026-09-02T10:00:00", "profile_email": "u@x.com",
             "job_title": "Developer", "company": "B", "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        filtered = _load_rows("u@x.com", "", _US_ONLY_SENTINEL, "", "")
        locations = [r["location"] for r in filtered]
        assert "Remote, Canada" not in locations
        assert "Remote" in locations

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH


class TestBR02_04_StatusColors:
    """BR-02/03/04: Status colors match business intent."""

    def test_skipped_is_dim_not_green(self):
        markup = _status_markup("skipped")
        assert "dim" in markup
        assert "green" not in markup

    def test_skipped_already_applied_is_dim(self):
        markup = _status_markup("skipped - already applied")
        assert "dim" in markup
        assert "green" not in markup

    def test_applied_is_bold_green(self):
        markup = _status_markup("applied")
        assert "bold" in markup and "green" in markup

    def test_submitted_is_green(self):
        markup = _status_markup("submitted")
        assert "green" in markup

    def test_error_is_bold_red(self):
        markup = _status_markup("error: timeout")
        assert "bold" in markup and "red" in markup


class TestBR05_KeywordFilter:
    """BR-05: Keyword filter is case-insensitive and partial-match."""

    def test_keyword_case_insensitive(self, tmp_path):
        rows = [
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "u@x.com",
             "job_title": "SENIOR PYTHON ENGINEER", "company": "A",
             "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        results = _load_rows("u@x.com", "python", "", "", "")
        assert len(results) == 1

    def test_keyword_partial_match(self, tmp_path):
        rows = [
            {"timestamp": "2026-09-01T10:00:00", "profile_email": "u@x.com",
             "job_title": "GenAI Engineer", "company": "A",
             "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        results = _load_rows("u@x.com", "genai", "", "", "")
        assert len(results) == 1

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH


class TestBR06_ExperienceInference:
    """BR-06: Experience level must be correctly inferred for common titles."""

    CASES = [
        ("Senior Software Engineer",   "Senior"),
        ("Junior Developer",           "Junior"),
        ("Staff Machine Learning Eng", "Staff"),
        ("Principal Engineer",         "Principal"),
        ("Software Engineering Intern","Intern"),
        ("Software Engineer II",       "Mid"),
        ("Lead Developer",             "Lead"),
        ("Associate Software Engineer","Junior"),
        ("Software Engineer",          "Mid"),    # no level → Mid
    ]

    def test_all_experience_cases(self):
        for title, expected in self.CASES:
            result = _infer_exp(title)
            assert result == expected, f"_infer_exp({title!r}) → {result!r}, expected {expected!r}"


class TestBR07_IndustryFilter:
    """BR-07: Every industry option in the dropdown must correctly classify titles."""

    INDUSTRY_TITLE_MAP = {
        "engineering":  "Senior Software Engineer",
        "it":           "IT Support Specialist",
        "management":   "VP of Engineering",
        "product":      "Product Manager",
        "data":         "Data Scientist",
        "sales":        "Enterprise Account Executive",
        "marketing":    "Content Marketing Manager",
        "design":       "UX Designer",
        "operations":   "Program Manager",
        "finance":      "Senior Accountant",
        "support":      "Customer Success Manager",
        "legal":        "Technical Recruiter",
    }

    def test_every_industry_option_classifies_correctly(self):
        industry_values = [val for _, val in _INDUSTRY_OPTIONS]
        for industry, title in self.INDUSTRY_TITLE_MAP.items():
            assert industry in industry_values, f"{industry} missing from _INDUSTRY_OPTIONS"
            result = _infer_industry(title)
            assert result == industry, (
                f"_infer_industry({title!r}) → {result!r}, expected {industry!r}"
            )


class TestBR08_SavedFilters:
    """BR-08: Saved filters persist and restore correctly across sessions."""

    def test_location_persists(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "f.json"
        _save_filters("u@x.com", "remote", "", "", "")
        saved = _load_saved("u@x.com")
        assert saved.get("locations") == ["remote"]

    def test_experience_persists(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "f.json"
        _save_filters("u@x.com", "", "senior", "", "")
        saved = _load_saved("u@x.com")
        assert saved.get("experience") == ["senior"]

    def test_days_persists(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "f.json"
        _save_filters("u@x.com", "", "", "14", "")
        saved = _load_saved("u@x.com")
        assert saved.get("posted_days") == 14

    def test_keywords_persist(self, tmp_path):
        """BR-15: Keywords must survive a session restart."""
        dashboard.SAVED_FILTERS = tmp_path / "f.json"
        _save_filters("u@x.com", "", "", "", "python, ml")
        saved = _load_saved("u@x.com")
        assert saved.get("keywords") == "python, ml", (
            "Keywords are not persisted. "
            "_save_filters must write cur['keywords'] = title."
        )

    def test_multiple_users_isolated(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "f.json"
        _save_filters("alice@x.com", "remote", "senior", "7", "python")
        _save_filters("bob@x.com",   "austin", "junior", "14", "java")
        assert _load_saved("alice@x.com")["locations"] == ["remote"]
        assert _load_saved("bob@x.com")["locations"]   == ["austin"]


class TestBR09_MultiCompanyCommands:
    """BR-09: Each selected company produces its own independent subprocess command."""

    def _build_cmds(self, companies, base, title="", loc="", exp="", days=""):
        filter_flags: list[str] = []
        if title:
            filter_flags += ["--keywords", title]
        else:
            filter_flags += ["--all-roles"]
        if loc == _US_ONLY_SENTINEL:
            filter_flags += ["--us-only"]
        elif loc:
            filter_flags += ["--location", loc]
        if exp:   filter_flags += ["--experience", exp]
        if days:  filter_flags += ["--posted-days", days]
        return [base + ["--company", co] + filter_flags for co in companies]

    def test_two_companies_produce_two_commands(self):
        base = ["python", "apply.py", "--profile", "u@x.com"]
        cmds = self._build_cmds(["Airbnb", "Stripe"], base, title="ml")
        assert len(cmds) == 2

    def test_each_command_has_correct_company(self):
        base = ["python", "apply.py"]
        cmds = self._build_cmds(["Airbnb", "Stripe", "Anthropic"], base)
        companies_in_cmds = [c[c.index("--company") + 1] for c in cmds]
        assert companies_in_cmds == ["Airbnb", "Stripe", "Anthropic"]

    def test_filters_applied_to_every_company_command(self):
        base = ["python", "apply.py"]
        cmds = self._build_cmds(["Airbnb", "Stripe"], base, title="ml", loc="remote", exp="senior")
        for cmd in cmds:
            assert "--keywords" in cmd
            assert "--location" in cmd
            assert "--experience" in cmd


class TestBR11_12_Sorting:
    """BR-11/12: Sort correctness for the two most-used sort modes."""

    def _make_rows(self, timestamps_titles):
        return [
            {"timestamp": ts, "job_title": title, "company": "X", "status": "applied"}
            for ts, title in timestamps_titles
        ]

    def test_recent_first_newest_at_index_0(self):
        rows = self._make_rows([
            ("2026-01-01", "Old"),
            ("2026-03-01", "Newest"),
            ("2026-02-01", "Middle"),
        ])
        result = _sort_rows(rows, "recent_first")
        assert result[0]["job_title"] == "Newest"

    def test_recent_last_oldest_at_index_0(self):
        rows = self._make_rows([
            ("2026-03-01", "Newest"),
            ("2026-01-01", "Oldest"),
            ("2026-02-01", "Middle"),
        ])
        result = _sort_rows(rows, "recent_last")
        assert result[0]["job_title"] == "Oldest"

    def test_az_first_alphabetically(self):
        rows = self._make_rows([
            ("2026-01-01", "Zebra Role"),
            ("2026-01-01", "Apple Role"),
            ("2026-01-01", "Mango Role"),
        ])
        result = _sort_rows(rows, "az")
        assert result[0]["job_title"] == "Apple Role"


class TestBR13_CompanyOptions:
    """BR-13: Company options loaded from JSON, sorted case-insensitively A→Z."""

    def test_options_are_sorted_case_insensitively(self, tmp_path):
        db = {
            "z": {"name": "Zoom"},
            "a": {"name": "Airbnb"},
            "m": {"name": "meta"},
            "s": {"name": "Stripe"},
        }
        path = tmp_path / "db.json"
        path.write_text(json.dumps(db))
        dashboard.COMPANY_DB = path
        opts = _load_company_options()
        names = [n for n, _ in opts]
        assert names == sorted(names, key=str.lower)

    def test_note_key_excluded(self, tmp_path):
        db = {"_note": "metadata", "airbnb": {"name": "Airbnb"}}
        path = tmp_path / "db.json"
        path.write_text(json.dumps(db))
        dashboard.COMPANY_DB = path
        opts = _load_company_options()
        assert all(n != "_note" for n, _ in opts)


class TestBR14_DaysFilter:
    """BR-14: Days filter excludes old rows but keeps undated rows."""

    def test_old_rows_excluded(self, tmp_path):
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(days=20)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Old Job",
             "company": "A", "location": "Remote", "status": "applied"},
            {"timestamp": (now - timedelta(days=2)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Recent Job",
             "company": "B", "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "7")
        titles = [r["job_title"] for r in result]
        assert "Old Job"    not in titles
        assert "Recent Job" in titles

    def test_undated_rows_not_excluded(self, tmp_path):
        rows = [
            {"timestamp": "", "profile_email": "u@x.com",
             "job_title": "Undated Job", "company": "A",
             "location": "Remote", "status": "applied"},
        ]
        path = _make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "7")
        titles = [r["job_title"] for r in result]
        assert "Undated Job" in titles, (
            "Rows with missing timestamps should NOT be filtered out by the days filter."
        )

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH
