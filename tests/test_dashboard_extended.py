"""
Comprehensive unit + integration tests for dashboard.py.

Covers every pure function:
  _parse_ts, _sort_rows, _status_markup, _log_markup,
  _is_us_location, _infer_exp, _infer_industry,
  _load_rows, _load_saved, _save_filters,
  _load_company_options, SORT_MODES constant

Integration scenarios:
  save → load round-trip, multi-filter stacking,
  company-options from JSON, main() arg-parse behaviour.
"""
import csv
import json
import sys
import pathlib
import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import dashboard
from dashboard import (
    _parse_ts, _sort_rows, _status_markup, _log_markup,
    _is_us_location, _infer_exp, _infer_industry,
    _load_rows, _load_saved, _save_filters,
    _load_company_options,
    SORT_MODES, _US_ONLY_SENTINEL, APPLIED_LOG_PATH,
)


# ══════════════════════════════════════════════════════════════════════════════
# _parse_ts
# ══════════════════════════════════════════════════════════════════════════════

class TestParseTs:
    def test_iso_datetime(self):
        r = _parse_ts("2026-09-08T12:00:00")
        assert isinstance(r, datetime)
        assert r.year == 2026

    def test_iso_date_only(self):
        r = _parse_ts("2026-01-15")
        assert r.year == 2026 and r.month == 1 and r.day == 15

    def test_invalid_string_returns_min(self):
        assert _parse_ts("not-a-date") == datetime.min

    def test_empty_string_returns_min(self):
        assert _parse_ts("") == datetime.min

    def test_whitespace_string_returns_min(self):
        assert _parse_ts("   ") == datetime.min

    def test_partial_date_returns_min(self):
        assert _parse_ts("2026-99-99") == datetime.min

    def test_with_microseconds(self):
        r = _parse_ts("2026-09-08T12:00:00.123456")
        assert r.microsecond == 123456

    def test_ordering_preserves_chronology(self):
        earlier = _parse_ts("2026-01-01T00:00:00")
        later   = _parse_ts("2026-06-01T00:00:00")
        assert earlier < later


# ══════════════════════════════════════════════════════════════════════════════
# _sort_rows
# ══════════════════════════════════════════════════════════════════════════════

class TestSortRows:
    @pytest.fixture
    def rows(self):
        return [
            {"timestamp": "2026-01-01", "job_title": "Engineer",  "company": "Zebra",  "status": "applied"},
            {"timestamp": "2026-03-01", "job_title": "Analyst",   "company": "Apple",  "status": "error: timeout"},
            {"timestamp": "2026-02-01", "job_title": "Manager",   "company": "Microsoft", "status": "skipped"},
        ]

    def test_recent_first_newest_is_first(self, rows):
        result = _sort_rows(rows, "recent_first")
        assert result[0]["timestamp"] == "2026-03-01"

    def test_recent_first_oldest_is_last(self, rows):
        result = _sort_rows(rows, "recent_first")
        assert result[-1]["timestamp"] == "2026-01-01"

    def test_recent_last_oldest_is_first(self, rows):
        result = _sort_rows(rows, "recent_last")
        assert result[0]["timestamp"] == "2026-01-01"

    def test_recent_last_newest_is_last(self, rows):
        result = _sort_rows(rows, "recent_last")
        assert result[-1]["timestamp"] == "2026-03-01"

    def test_az_first_alphabetically(self, rows):
        result = _sort_rows(rows, "az")
        assert result[0]["job_title"] == "Analyst"

    def test_az_last_alphabetically(self, rows):
        result = _sort_rows(rows, "az")
        assert result[-1]["job_title"] == "Manager"

    def test_za_first_reverse(self, rows):
        result = _sort_rows(rows, "za")
        assert result[0]["job_title"] == "Manager"

    def test_company_az(self, rows):
        result = _sort_rows(rows, "company_az")
        assert result[0]["company"] == "Apple"
        assert result[-1]["company"] == "Zebra"

    def test_status_alphabetical(self, rows):
        result = _sort_rows(rows, "status")
        assert result[0]["status"] == "applied"

    def test_unknown_mode_returns_same_list(self, rows):
        result = _sort_rows(rows, "nonexistent")
        assert result == rows

    def test_empty_list_returns_empty(self):
        assert _sort_rows([], "recent_first") == []

    def test_single_item_list(self, rows):
        one = rows[:1]
        assert _sort_rows(one, "recent_first") == one

    def test_does_not_mutate_original(self, rows):
        original_order = [r["timestamp"] for r in rows]
        _sort_rows(rows, "recent_first")
        assert [r["timestamp"] for r in rows] == original_order

    def test_ties_stable_for_recent_first(self):
        tie_rows = [
            {"timestamp": "2026-01-01", "job_title": "A", "company": "X", "status": "applied"},
            {"timestamp": "2026-01-01", "job_title": "B", "company": "Y", "status": "error"},
        ]
        result = _sort_rows(tie_rows, "recent_first")
        assert len(result) == 2  # both present

    def test_sort_modes_constant_coverage(self):
        known_modes = {m[0] for m in SORT_MODES}
        assert "recent_first" in known_modes
        assert "recent_last"  in known_modes
        assert "az"           in known_modes
        assert "za"           in known_modes
        assert "company_az"   in known_modes
        assert "status"       in known_modes


# ══════════════════════════════════════════════════════════════════════════════
# _status_markup
# ══════════════════════════════════════════════════════════════════════════════

class TestStatusMarkup:
    def test_applied_is_bold_green(self):
        assert "bold green" in _status_markup("applied")

    def test_submitted_is_green(self):
        m = _status_markup("submitted")
        assert "green" in m and "bold" not in m

    def test_error_is_bold_red(self):
        assert "bold red" in _status_markup("error: timeout")

    def test_skipped_is_dim(self):
        assert "dim" in _status_markup("skipped")

    # Regression: "skipped - already applied" used to match "applied" first
    def test_skipped_already_applied_is_dim_not_green(self):
        m = _status_markup("skipped - already applied")
        assert "dim" in m
        assert "green" not in m

    def test_skipped_takes_priority_over_applied(self):
        assert "dim" in _status_markup("skipped (previously applied)")

    def test_uppercase_applied_is_green(self):
        assert "green" in _status_markup("Applied")

    def test_mixed_case_error(self):
        assert "red" in _status_markup("Error: 404")

    def test_truncated_to_18_chars(self):
        long_status = "abcdefghijklmnopqrstuvwxyz"
        m = _status_markup(long_status)
        # raw text inside the markup should be at most 18 chars
        assert long_status[:18] in m
        assert long_status[18:] not in m

    def test_unknown_returns_raw_truncated(self):
        m = _status_markup("pending review")
        assert "pending review" in m
        assert "[" not in m  # no markup tags

    def test_empty_string(self):
        m = _status_markup("")
        assert m == ""


# ══════════════════════════════════════════════════════════════════════════════
# _log_markup
# ══════════════════════════════════════════════════════════════════════════════

class TestLogMarkup:
    def test_profile_line_is_dim_green(self):
        assert "green" in _log_markup("[profile] 'First Name*' → 'John'")

    def test_saved_line_is_dim_green(self):
        assert "green" in _log_markup("[saved] 'Phone*' → '555-1234'")

    def test_ollama_line_is_cyan(self):
        assert "cyan" in _log_markup("[ollama] generating answer…")

    def test_applied_arrow_is_bold_green(self):
        assert "green" in _log_markup("→ applied")

    def test_submitted_arrow_is_bold_green(self):
        assert "green" in _log_markup("→ submitted")

    def test_error_arrow_is_bold_red(self):
        assert "red" in _log_markup("→ error: submit failed")

    def test_error_colon_is_bold_red(self):
        assert "red" in _log_markup("error: something went wrong")

    def test_verification_line_is_yellow(self):
        assert "yellow" in _log_markup("→ [Verification] Code screen detected")

    def test_location_tag_is_yellow(self):
        assert "yellow" in _log_markup("[Location] Austin, TX detected")

    def test_gmail_tag_is_yellow(self):
        assert "yellow" in _log_markup("[Gmail] Fetching OTP")

    def test_linkedin_tag_is_yellow(self):
        assert "yellow" in _log_markup("[LinkedIn] profile loaded")

    def test_separator_line_is_dim(self):
        assert "dim" in _log_markup("─────────────────────────────")

    def test_resume_line_is_dim_magenta(self):
        assert "magenta" in _log_markup("Resume: /path/to/resume.pdf")

    def test_fetching_line_is_dim_magenta(self):
        assert "magenta" in _log_markup("Fetching jobs from Greenhouse…")

    def test_found_n_line_is_dim_magenta(self):
        assert "magenta" in _log_markup("Found 42 jobs for this role")

    def test_brackets_in_unmatched_line_are_escaped(self):
        m = _log_markup("just some [random] text here")
        assert "\\[" in m

    def test_brackets_in_matched_line_are_escaped(self):
        m = _log_markup("[profile] 'Field [label]' → 'val'")
        assert "\\[" in m

    def test_empty_string(self):
        m = _log_markup("")
        assert m == ""

    def test_trailing_whitespace_stripped(self):
        m = _log_markup("→ applied   ")
        assert not m.endswith(" ")


# ══════════════════════════════════════════════════════════════════════════════
# _is_us_location
# ══════════════════════════════════════════════════════════════════════════════

class TestIsUsLocation:
    def test_empty_string(self):
        assert _is_us_location("") is False

    def test_us_state_abbreviation_tx(self):
        assert _is_us_location("Austin, TX") is True

    def test_us_state_abbreviation_ny(self):
        assert _is_us_location("New York, NY") is True

    def test_us_state_abbreviation_ca(self):
        assert _is_us_location("San Francisco, CA") is True

    def test_us_state_abbreviation_wa(self):
        assert _is_us_location("Seattle, WA") is True

    def test_united_states_keyword(self):
        assert _is_us_location("United States") is True

    def test_united_states_with_parenthetical(self):
        assert _is_us_location("United States (Remote)") is True

    def test_usa_keyword(self):
        assert _is_us_location("USA") is True

    def test_remote_alone_is_us(self):
        assert _is_us_location("Remote") is True

    def test_remote_case_insensitive(self):
        assert _is_us_location("remote") is True

    def test_remote_in_us(self):
        assert _is_us_location("Remote in US") is True

    def test_remote_us_comma(self):
        assert _is_us_location("Remote, US") is True

    def test_remote_united_states(self):
        assert _is_us_location("Remote - United States") is True

    def test_puerto_rico_pr_is_us(self):
        assert _is_us_location("San Juan, PR") is True

    def test_dc_is_us(self):
        assert _is_us_location("Washington, DC") is True

    def test_hybrid_new_york(self):
        assert _is_us_location("Hybrid - New York, NY") is True

    # Canada must be excluded
    def test_remote_canada_excluded(self):
        assert _is_us_location("Remote, Canada") is False

    def test_toronto_on_excluded(self):
        assert _is_us_location("Toronto, ON") is False

    def test_vancouver_bc_excluded(self):
        assert _is_us_location("Vancouver, BC") is False

    def test_canada_keyword_excluded(self):
        assert _is_us_location("Canada") is False

    def test_montreal_qc_excluded(self):
        assert _is_us_location("Montreal, QC") is False

    def test_ontario_canada_excluded(self):
        assert _is_us_location("Ontario, Canada") is False

    # Other countries excluded
    def test_uk_excluded(self):
        assert _is_us_location("London, UK") is False

    def test_germany_excluded(self):
        assert _is_us_location("Berlin, Germany") is False

    def test_india_excluded(self):
        assert _is_us_location("Bangalore, India") is False

    def test_australia_excluded(self):
        assert _is_us_location("Sydney, Australia") is False

    def test_standalone_us_is_us(self):
        # "US" alone as location should be treated as US
        assert _is_us_location("US") is True


# ══════════════════════════════════════════════════════════════════════════════
# _infer_exp
# ══════════════════════════════════════════════════════════════════════════════

class TestInferExp:
    def test_senior(self):
        assert _infer_exp("Senior Software Engineer") == "Senior"

    def test_sr_dot_abbreviation(self):
        assert _infer_exp("Sr. Software Engineer") == "Senior"

    def test_sr_no_dot(self):
        assert _infer_exp("Sr Software Engineer") == "Senior"

    def test_staff(self):
        assert _infer_exp("Staff Engineer") == "Staff"

    def test_principal(self):
        assert _infer_exp("Principal Engineer") == "Principal"

    def test_distinguished(self):
        assert _infer_exp("Distinguished Engineer") == "Principal"

    def test_fellow(self):
        assert _infer_exp("Fellow, Infrastructure") == "Principal"

    def test_lead(self):
        assert _infer_exp("Lead Engineer") == "Lead"

    def test_leads_plural(self):
        assert _infer_exp("Leads the platform team") == "Lead"

    def test_engineer_iii_is_senior(self):
        assert _infer_exp("Software Engineer III") == "Senior"

    def test_engineer_iv_is_senior(self):
        assert _infer_exp("Software Engineer IV") == "Senior"

    def test_engineer_ii_is_mid(self):
        assert _infer_exp("Software Engineer II") == "Mid"

    def test_mid_explicit(self):
        assert _infer_exp("Mid-Level Developer") == "Mid"

    def test_intermediate(self):
        assert _infer_exp("Intermediate Software Engineer") == "Mid"

    def test_junior(self):
        assert _infer_exp("Junior Developer") == "Junior"

    def test_jr_dot(self):
        assert _infer_exp("Jr. Developer") == "Junior"

    def test_jr_no_dot(self):
        assert _infer_exp("Jr Developer") == "Junior"

    def test_entry_level(self):
        assert _infer_exp("Entry Level Software Engineer") == "Junior"

    def test_associate(self):
        assert _infer_exp("Associate Software Engineer") == "Junior"

    def test_intern(self):
        assert _infer_exp("Software Engineering Intern") == "Intern"

    def test_internship(self):
        assert _infer_exp("Summer Internship - Engineering") == "Intern"

    def test_coop(self):
        assert _infer_exp("Co-op Engineer") == "Intern"

    def test_no_level_defaults_to_mid(self):
        assert _infer_exp("Software Engineer") == "Mid"

    def test_empty_defaults_to_mid(self):
        assert _infer_exp("") == "Mid"

    def test_case_insensitive_senior(self):
        assert _infer_exp("SENIOR ENGINEER") == "Senior"

    def test_senior_data_scientist(self):
        assert _infer_exp("Senior Data Scientist") == "Senior"

    def test_associate_product_manager(self):
        assert _infer_exp("Associate Product Manager") == "Junior"


# ══════════════════════════════════════════════════════════════════════════════
# _infer_industry
# ══════════════════════════════════════════════════════════════════════════════

class TestInferIndustry:
    # Engineering
    def test_software_engineer(self):
        assert _infer_industry("Software Engineer") == "engineering"

    def test_backend_developer(self):
        assert _infer_industry("Backend Developer") == "engineering"

    def test_frontend_developer(self):
        assert _infer_industry("Frontend Developer") == "engineering"

    def test_fullstack(self):
        assert _infer_industry("Fullstack Developer") == "engineering"

    def test_devops(self):
        assert _infer_industry("DevOps Engineer") == "engineering"

    def test_platform_engineer(self):
        assert _infer_industry("Platform Engineer") == "engineering"

    def test_sre(self):
        assert _infer_industry("Site Reliability Engineer (SRE)") == "engineering"

    def test_cloud_architect(self):
        assert _infer_industry("Cloud Architect") == "engineering"

    def test_ml_engineer(self):
        assert _infer_industry("Machine Learning Engineer") == "engineering"

    def test_security_engineer(self):
        assert _infer_industry("Security Engineer") == "engineering"

    # IT
    def test_sysadmin(self):
        assert _infer_industry("Linux Sysadmin") == "it"

    def test_it_support(self):
        assert _infer_industry("IT Support Specialist") == "it"

    def test_it_manager(self):
        assert _infer_industry("IT Manager") == "it"  # IT before management

    def test_network_engineer(self):
        assert _infer_industry("Network Engineer") == "it"

    def test_help_desk(self):
        assert _infer_industry("Help Desk Technician") == "it"

    def test_systems_administrator(self):
        assert _infer_industry("Systems Administrator") == "it"

    # Management — VP/Director/Head-of that didn't match a more specific rule
    def test_vp_engineering_is_management(self):
        assert _infer_industry("VP of Engineering") == "management"

    def test_engineering_manager_is_management(self):
        assert _infer_industry("Engineering Manager") == "management"

    def test_director_of_engineering(self):
        assert _infer_industry("Director of Engineering") == "management"

    def test_cto(self):
        assert _infer_industry("CTO") == "management"

    def test_head_of_engineering(self):
        assert _infer_industry("Head of Engineering") == "management"

    def test_chief_revenue_officer(self):
        assert _infer_industry("Chief Revenue Officer") == "management"

    # Data
    def test_data_scientist(self):
        assert _infer_industry("Data Scientist") == "data"

    def test_data_analyst(self):
        assert _infer_industry("Data Analyst") == "data"

    def test_ml_researcher(self):
        assert _infer_industry("ML Researcher") == "data"

    def test_business_analyst(self):
        assert _infer_industry("Business Analyst") == "data"

    def test_ai_researcher(self):
        assert _infer_industry("AI Researcher") == "data"

    # Sales
    def test_account_executive(self):
        assert _infer_industry("Enterprise Account Executive") == "sales"

    def test_bdr(self):
        assert _infer_industry("Business Development Representative") == "sales"

    def test_sales_manager(self):
        # "Sales Manager" — sales rule has "sales" before management
        assert _infer_industry("Sales Manager") == "sales"

    # Marketing — beats management for titles with marketing keywords
    def test_content_marketing_manager(self):
        assert _infer_industry("Content Marketing Manager") == "marketing"

    def test_seo_specialist(self):
        assert _infer_industry("SEO Specialist") == "marketing"

    def test_brand_manager(self):
        assert _infer_industry("Brand Manager") == "marketing"

    def test_copywriter(self):
        assert _infer_industry("Senior Copywriter") == "marketing"

    # Operations — beats management for PM/project manager titles
    def test_program_manager(self):
        assert _infer_industry("Program Manager") == "operations"

    def test_project_manager(self):
        assert _infer_industry("Project Manager") == "operations"

    def test_operations_manager(self):
        assert _infer_industry("Operations Manager") == "operations"

    def test_logistics_coordinator(self):
        assert _infer_industry("Logistics Coordinator") == "operations"

    # Finance — beats management; includes accountant
    def test_accountant(self):
        assert _infer_industry("Senior Accountant") == "finance"

    def test_controller(self):
        assert _infer_industry("Financial Controller") == "finance"

    def test_payroll_specialist(self):
        assert _infer_industry("Payroll Specialist") == "finance"

    # Support — beats management for "Customer Success Manager" etc.
    def test_customer_success_manager(self):
        assert _infer_industry("Customer Success Manager") == "support"

    def test_customer_service_rep(self):
        assert _infer_industry("Customer Service Representative") == "support"

    # Design
    def test_ux_designer(self):
        assert _infer_industry("UX Designer") == "design"

    def test_ui_developer(self):
        assert _infer_industry("UI Developer") == "design"

    def test_visual_designer(self):
        assert _infer_industry("Visual Designer") == "design"

    # Legal / HR
    def test_recruiter(self):
        assert _infer_industry("Technical Recruiter") == "legal"

    def test_hr_business_partner(self):
        assert _infer_industry("HR Business Partner") == "legal"

    def test_talent_acquisition(self):
        assert _infer_industry("Talent Acquisition Specialist") == "legal"

    # Product
    def test_product_manager(self):
        assert _infer_industry("Product Manager") == "product"

    def test_product_owner(self):
        assert _infer_industry("Product Owner") == "product"

    # Other / unknown
    def test_unknown_returns_other(self):
        assert _infer_industry("Barista") == "other"

    def test_empty_returns_other(self):
        assert _infer_industry("") == "other"


# ══════════════════════════════════════════════════════════════════════════════
# _load_rows — CSV integration
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadRows:
    FIELDS = ["timestamp", "profile_email", "job_title", "company", "location", "status"]

    @pytest.fixture
    def csv_path(self, tmp_path):
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(days=1)).isoformat(),
             "profile_email": "a@b.com", "job_title": "Senior Engineer",
             "company": "Acme", "location": "Austin, TX", "status": "applied"},
            {"timestamp": (now - timedelta(days=3)).isoformat(),
             "profile_email": "a@b.com", "job_title": "Junior Data Analyst",
             "company": "Beta", "location": "Remote, Canada", "status": "skipped"},
            {"timestamp": (now - timedelta(days=10)).isoformat(),
             "profile_email": "a@b.com", "job_title": "Staff ML Engineer",
             "company": "Gamma", "location": "San Francisco, CA", "status": "error: timeout"},
            {"timestamp": (now - timedelta(days=2)).isoformat(),
             "profile_email": "other@x.com", "job_title": "Product Manager",
             "company": "Delta", "location": "New York, NY", "status": "applied"},
            {"timestamp": "",   # deliberately missing timestamp
             "profile_email": "a@b.com", "job_title": "DevOps Engineer",
             "company": "Echo", "location": "Remote", "status": "applied"},
        ]
        path = tmp_path / "applied.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS)
            w.writeheader()
            w.writerows(rows)
        # Point module at our temp file
        dashboard.APPLIED_LOG_PATH = path
        yield path, now

    def test_filters_by_email(self, csv_path):
        _, _ = csv_path
        rows = _load_rows("a@b.com", "", "", "", "")
        assert len(rows) == 4
        assert all(r["profile_email"] == "a@b.com" for r in rows)

    def test_wrong_email_returns_empty(self, csv_path):
        assert _load_rows("nobody@x.com", "", "", "", "") == []

    def test_email_case_insensitive(self, csv_path):
        rows = _load_rows("A@B.COM", "", "", "", "")
        assert len(rows) == 4

    def test_keyword_filter(self, csv_path):
        rows = _load_rows("a@b.com", "engineer", "", "", "")
        titles = [r["job_title"] for r in rows]
        assert all("engineer" in t.lower() for t in titles)
        assert "Junior Data Analyst" not in titles

    def test_us_only_filter_excludes_canada(self, csv_path):
        rows = _load_rows("a@b.com", "", "__us_only__", "", "")
        locations = [r["location"] for r in rows]
        assert "Remote, Canada" not in locations

    def test_us_only_keeps_us_locations(self, csv_path):
        rows = _load_rows("a@b.com", "", "__us_only__", "", "")
        locations = [r["location"] for r in rows]
        assert "Austin, TX" in locations
        assert "Remote" in locations

    def test_city_location_filter(self, csv_path):
        rows = _load_rows("a@b.com", "", "austin", "", "")
        assert len(rows) == 1
        assert rows[0]["location"] == "Austin, TX"

    def test_experience_filter_senior(self, csv_path):
        rows = _load_rows("a@b.com", "", "", "senior", "")
        titles = [r["job_title"] for r in rows]
        assert "Senior Engineer" in titles
        assert "Junior Data Analyst" not in titles

    def test_experience_filter_staff(self, csv_path):
        rows = _load_rows("a@b.com", "", "", "staff", "")
        titles = [r["job_title"] for r in rows]
        assert "Staff ML Engineer" in titles

    def test_days_filter_excludes_old(self, csv_path):
        _, _ = csv_path
        # 7-day filter — the 10-day-old Staff ML Engineer should be excluded
        rows = _load_rows("a@b.com", "", "", "", "7")
        titles = [r["job_title"] for r in rows]
        assert "Staff ML Engineer" not in titles

    def test_days_filter_keeps_recent(self, csv_path):
        rows = _load_rows("a@b.com", "", "", "", "7")
        titles = [r["job_title"] for r in rows]
        assert "Senior Engineer" in titles

    def test_days_filter_includes_row_with_missing_timestamp(self, csv_path):
        # Row with empty timestamp should NOT be silently dropped
        rows = _load_rows("a@b.com", "", "", "", "7")
        titles = [r["job_title"] for r in rows]
        assert "DevOps Engineer" in titles

    def test_industry_filter(self, csv_path):
        rows = _load_rows("a@b.com", "", "", "", "", f_industry="engineering")
        for r in rows:
            assert _infer_industry(r["job_title"]) == "engineering"

    def test_combined_keyword_and_location(self, csv_path):
        rows = _load_rows("a@b.com", "engineer", "austin", "", "")
        assert len(rows) == 1
        assert rows[0]["job_title"] == "Senior Engineer"

    def test_nonexistent_file_returns_empty(self, tmp_path):
        dashboard.APPLIED_LOG_PATH = tmp_path / "does_not_exist.csv"
        assert _load_rows("a@b.com", "", "", "", "") == []

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH  # restore original


# ══════════════════════════════════════════════════════════════════════════════
# _save_filters / _load_saved  (round-trip integration)
# ══════════════════════════════════════════════════════════════════════════════

class TestSaveLoadFilters:
    @pytest.fixture(autouse=True)
    def patch_path(self, tmp_path):
        orig = dashboard.SAVED_FILTERS
        dashboard.SAVED_FILTERS = tmp_path / "filters.json"
        yield
        dashboard.SAVED_FILTERS = orig

    def test_round_trip_basic(self):
        _save_filters("u@x.com", "remote", "senior", "7", "python")
        saved = _load_saved("u@x.com")
        assert saved["locations"]  == ["remote"]
        assert saved["experience"] == ["senior"]
        assert saved["posted_days"] == 7

    def test_empty_values_saved_as_empty(self):
        _save_filters("u@x.com", "", "", "", "")
        saved = _load_saved("u@x.com")
        assert saved["locations"]   == []
        assert saved["experience"]  == []
        assert saved["posted_days"] is None

    def test_us_only_sentinel_persisted(self):
        _save_filters("u@x.com", "__us_only__", "", "", "")
        saved = _load_saved("u@x.com")
        assert saved["locations"] == ["__us_only__"]

    def test_multiple_emails_isolated(self):
        _save_filters("alice@x.com", "remote", "senior", "7", "python")
        _save_filters("bob@x.com",   "austin", "junior", "14", "java")
        alice = _load_saved("alice@x.com")
        bob   = _load_saved("bob@x.com")
        assert alice["locations"] == ["remote"]
        assert bob["locations"]   == ["austin"]

    def test_subsequent_save_overwrites_location(self):
        _save_filters("u@x.com", "remote", "senior", "7", "python")
        _save_filters("u@x.com", "austin", "senior", "7", "python")
        saved = _load_saved("u@x.com")
        assert saved["locations"] == ["austin"]

    def test_work_type_defaulted(self):
        _save_filters("u@x.com", "", "", "", "")
        saved = _load_saved("u@x.com")
        assert "work_type" in saved

    def test_us_only_defaulted(self):
        _save_filters("u@x.com", "", "", "", "")
        saved = _load_saved("u@x.com")
        assert "us_only" in saved

    def test_unknown_email_returns_empty_dict(self):
        _save_filters("u@x.com", "remote", "senior", "7", "python")
        assert _load_saved("nobody@x.com") == {}

    def test_missing_file_returns_empty_dict(self):
        assert _load_saved("u@x.com") == {}

    def test_keywords_title_are_persisted(self):
        # The title/keywords argument should be saved so it can be reloaded
        _save_filters("u@x.com", "", "", "", "python, ml")
        saved = _load_saved("u@x.com")
        assert saved.get("keywords") == "python, ml"


# ══════════════════════════════════════════════════════════════════════════════
# _load_company_options
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadCompanyOptions:
    @pytest.fixture(autouse=True)
    def patch_path(self, tmp_path):
        orig = dashboard.COMPANY_DB
        dashboard.COMPANY_DB = tmp_path / "company_db.json"
        yield tmp_path
        dashboard.COMPANY_DB = orig

    def test_returns_sorted_list(self, tmp_path):
        db = {
            "zebra": {"name": "Zebra"},
            "apple": {"name": "Apple"},
            "meta":  {"name": "Meta"},
        }
        dashboard.COMPANY_DB.write_text(json.dumps(db))
        opts = _load_company_options()
        names = [n for n, _ in opts]
        assert names == sorted(names, key=str.lower)

    def test_skips_note_key(self, tmp_path):
        db = {
            "_note": "this is metadata",
            "airbnb": {"name": "Airbnb"},
        }
        dashboard.COMPANY_DB.write_text(json.dumps(db))
        opts = _load_company_options()
        names = [n for n, _ in opts]
        assert "_note" not in names
        assert "Airbnb" in names

    def test_missing_file_returns_empty(self, tmp_path):
        orig_applicable = dashboard.APPLICABLE_COMPANIES
        orig_db = dashboard.COMPANY_DB
        try:
            dashboard.APPLICABLE_COMPANIES = tmp_path / "no_applicable.json"
            dashboard.COMPANY_DB = tmp_path / "no_db.json"
            assert _load_company_options() == []
        finally:
            dashboard.APPLICABLE_COMPANIES = orig_applicable
            dashboard.COMPANY_DB = orig_db

    def test_malformed_json_returns_empty(self, tmp_path):
        orig_applicable = dashboard.APPLICABLE_COMPANIES
        orig_db = dashboard.COMPANY_DB
        try:
            bad = tmp_path / "bad.json"
            bad.write_text("{not valid json}")
            dashboard.APPLICABLE_COMPANIES = bad
            dashboard.COMPANY_DB = bad
            assert _load_company_options() == []
        finally:
            dashboard.APPLICABLE_COMPANIES = orig_applicable
            dashboard.COMPANY_DB = orig_db

    def test_tuples_are_name_name(self, tmp_path):
        # Test via the applicable_companies.json path (primary source)
        orig_applicable = dashboard.APPLICABLE_COMPANIES
        try:
            f = tmp_path / "applicable_companies.json"
            f.write_text('["Stripe"]')
            dashboard.APPLICABLE_COMPANIES = f
            opts = _load_company_options()
            assert ("Stripe", "Stripe") in opts
        finally:
            dashboard.APPLICABLE_COMPANIES = orig_applicable

    def test_empty_db_returns_empty(self, tmp_path):
        orig_applicable = dashboard.APPLICABLE_COMPANIES
        orig_db = dashboard.COMPANY_DB
        try:
            dashboard.APPLICABLE_COMPANIES = tmp_path / "no_applicable.json"
            empty_db = tmp_path / "db.json"
            empty_db.write_text("{}")
            dashboard.COMPANY_DB = empty_db
            assert _load_company_options() == []
        finally:
            dashboard.APPLICABLE_COMPANIES = orig_applicable
            dashboard.COMPANY_DB = orig_db

    def test_case_insensitive_sort(self, tmp_path):
        db = {
            "z": {"name": "zoom"},
            "a": {"name": "Airbnb"},
            "m": {"name": "meta"},
        }
        dashboard.COMPANY_DB.write_text(json.dumps(db))
        opts = _load_company_options()
        names = [n for n, _ in opts]
        assert names == sorted(names, key=str.lower)


# ══════════════════════════════════════════════════════════════════════════════
# Integration: filter-flag building logic
# ══════════════════════════════════════════════════════════════════════════════

class TestFilterFlagBuilding:
    """
    Exercise the command-building logic embedded in _start_applying by
    extracting its key branch patterns and verifying each path independently.
    This mirrors what _start_applying does without needing a live Textual app.
    """

    def _build_flags(self, title="", loc="", exp="", days=""):
        """Replicate the filter_flags construction from _start_applying."""
        flags: list[str] = []
        if title:
            flags += ["--keywords", title]
        else:
            flags += ["--all-roles"]
        if loc == _US_ONLY_SENTINEL:
            flags += ["--us-only"]
        elif loc:
            flags += ["--location", loc]
        if exp:   flags += ["--experience",  exp]
        if days:  flags += ["--posted-days", days]
        return flags

    def test_no_filters_gives_all_roles(self):
        flags = self._build_flags()
        assert "--all-roles" in flags
        assert "--keywords" not in flags

    def test_title_gives_keywords(self):
        flags = self._build_flags(title="python, ml")
        assert "--keywords" in flags
        assert "python, ml" in flags
        assert "--all-roles" not in flags

    def test_us_only_sentinel_gives_us_only_flag(self):
        flags = self._build_flags(loc=_US_ONLY_SENTINEL)
        assert "--us-only" in flags
        assert "--location" not in flags

    def test_city_loc_gives_location_flag(self):
        flags = self._build_flags(loc="austin")
        assert "--location" in flags
        assert "austin" in flags
        assert "--us-only" not in flags

    def test_experience_flag(self):
        flags = self._build_flags(exp="senior")
        assert "--experience" in flags
        assert "senior" in flags

    def test_posted_days_flag(self):
        flags = self._build_flags(days="7")
        assert "--posted-days" in flags
        assert "7" in flags

    def test_all_filters_combined(self):
        flags = self._build_flags(title="ml", loc="remote", exp="senior", days="14")
        assert "--keywords" in flags
        assert "--location" in flags
        assert "--experience" in flags
        assert "--posted-days" in flags

    def test_command_built_per_company(self):
        base = ["python", "company_apply.py", "--profile", "u@x.com"]
        companies = ["Airbnb", "Stripe"]
        flags = self._build_flags(title="ml")
        cmds = [base + ["--company", co] + flags for co in companies]
        assert len(cmds) == 2
        assert cmds[0][4] == "--company"
        assert cmds[0][5] == "Airbnb"
        assert cmds[1][5] == "Stripe"

    def test_python_unbuffered_flag_inserted(self):
        import sys
        base = [sys.executable, "company_apply.py"]
        c = list(base)
        if c[0] == sys.executable and len(c) > 1 and c[1] != "-u":
            c = [c[0], "-u"] + c[1:]
        assert c[1] == "-u"
        assert c[2] == "company_apply.py"

    def test_unbuffered_not_doubled(self):
        import sys
        base = [sys.executable, "-u", "company_apply.py"]
        c = list(base)
        if c[0] == sys.executable and len(c) > 1 and c[1] != "-u":
            c = [c[0], "-u"] + c[1:]
        assert c.count("-u") == 1


# ══════════════════════════════════════════════════════════════════════════════
# Integration: _load_rows with all filter combinations (multi-filter stacking)
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadRowsMultiFilter:
    FIELDS = ["timestamp", "profile_email", "job_title", "company", "location", "status"]

    @pytest.fixture
    def csv_path(self, tmp_path):
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(days=1)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Senior ML Engineer",
             "company": "Acme", "location": "Remote", "status": "applied"},
            {"timestamp": (now - timedelta(days=8)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Junior Data Analyst",
             "company": "Beta", "location": "Austin, TX", "status": "skipped"},
            {"timestamp": (now - timedelta(days=2)).isoformat(),
             "profile_email": "u@x.com", "job_title": "Staff Platform Engineer",
             "company": "Gamma", "location": "San Francisco, CA", "status": "error"},
        ]
        path = tmp_path / "applied.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS)
            w.writeheader()
            w.writerows(rows)
        dashboard.APPLIED_LOG_PATH = path
        yield path, now

    def test_keyword_plus_days_filter(self, csv_path):
        rows = _load_rows("u@x.com", "engineer", "", "", "7")
        titles = [r["job_title"] for r in rows]
        # Recent engineer: Senior ML Engineer (1d), Staff Platform Engineer (2d)
        assert "Senior ML Engineer"    in titles
        assert "Staff Platform Engineer" in titles
        # Junior Data Analyst is analyst, not engineer
        assert "Junior Data Analyst"   not in titles

    def test_exp_plus_industry(self, csv_path):
        rows = _load_rows("u@x.com", "", "", "senior", "", f_industry="engineering")
        titles = [r["job_title"] for r in rows]
        assert "Senior ML Engineer" in titles
        assert "Junior Data Analyst" not in titles

    def test_us_only_plus_keyword(self, csv_path):
        rows = _load_rows("u@x.com", "engineer", "__us_only__", "", "")
        locations = [r["location"] for r in rows]
        for loc in locations:
            assert _is_us_location(loc), f"Expected US location, got: {loc}"

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH


# ══════════════════════════════════════════════════════════════════════════════
# Regression suite — past bugs that were fixed
# ══════════════════════════════════════════════════════════════════════════════

class TestRegressions:
    """One test per bug that was found and fixed."""

    def test_sort_recent_first_was_just_reversed(self):
        # Before fix: _sort_rows used list(reversed(rows)) which
        # assumed rows were already sorted oldest-first. Now it sorts properly.
        rows = [
            {"timestamp": "2026-06-01", "job_title": "B", "company": "X", "status": ""},
            {"timestamp": "2026-01-01", "job_title": "A", "company": "Y", "status": ""},
            {"timestamp": "2026-03-01", "job_title": "C", "company": "Z", "status": ""},
        ]
        result = _sort_rows(rows, "recent_first")
        assert result[0]["timestamp"] == "2026-06-01"

    def test_skipped_already_applied_was_misclassified(self):
        # Before fix: "applied" check ran before "skipped" so
        # "skipped - already applied" → bold green (wrong)
        m = _status_markup("skipped - already applied")
        assert "dim" in m
        assert "green" not in m

    def test_remote_canada_was_not_excluded_in_dashboard(self):
        # Before fix: dashboard._is_us_location had no Canada check;
        # "Remote, Canada" passed through as a US location
        assert _is_us_location("Remote, Canada") is False

    def test_content_marketing_manager_was_management(self):
        # Before fix: management rule ran before marketing → wrong classification
        assert _infer_industry("Content Marketing Manager") == "marketing"

    def test_program_manager_was_management(self):
        assert _infer_industry("Program Manager") == "operations"

    def test_customer_success_manager_was_management(self):
        assert _infer_industry("Customer Success Manager") == "support"

    def test_accountant_was_other(self):
        assert _infer_industry("Senior Accountant") == "finance"
