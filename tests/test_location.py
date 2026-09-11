"""
Tests for US/Canada location detection in company_apply.py and dashboard.py.
"""
import pytest
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from company_apply import _is_canada_location, _is_us_location


# ── Canada detection ──────────────────────────────────────────────────────────

class TestIsCanadaLocation:
    def test_explicit_canada(self):
        assert _is_canada_location("Remote, Canada") is True

    def test_province_abbreviation_on(self):
        assert _is_canada_location("Toronto, ON") is True

    def test_province_abbreviation_bc(self):
        assert _is_canada_location("Vancouver, BC") is True

    def test_province_abbreviation_qc(self):
        assert _is_canada_location("Montreal, QC") is True

    def test_province_abbreviation_ab(self):
        assert _is_canada_location("Calgary, AB") is True

    def test_province_keyword_ontario(self):
        assert _is_canada_location("Ottawa, Ontario") is True

    def test_province_keyword_nova_scotia(self):
        assert _is_canada_location("Halifax, Nova Scotia") is True

    def test_city_keyword_toronto(self):
        assert _is_canada_location("Toronto") is True

    def test_city_keyword_vancouver(self):
        assert _is_canada_location("Vancouver") is True

    def test_city_keyword_montreal(self):
        assert _is_canada_location("Montreal") is True

    def test_city_keyword_winnipeg(self):
        assert _is_canada_location("Winnipeg, MB") is True

    def test_empty_string(self):
        assert _is_canada_location("") is False

    def test_us_state_tx(self):
        assert _is_canada_location("Austin, TX") is False

    def test_us_remote(self):
        assert _is_canada_location("Remote") is False

    def test_us_remote_usa(self):
        assert _is_canada_location("Remote, USA") is False

    def test_us_new_york(self):
        assert _is_canada_location("New York, NY") is False

    def test_us_san_francisco(self):
        assert _is_canada_location("San Francisco, CA") is False

    def test_uk(self):
        assert _is_canada_location("London, UK") is False


# ── US detection ──────────────────────────────────────────────────────────────

class TestIsUsLocation:
    # True cases
    def test_state_abbreviation_tx(self):
        assert _is_us_location("Austin, TX") is True

    def test_state_abbreviation_ca(self):
        assert _is_us_location("San Francisco, CA") is True

    def test_state_abbreviation_ny(self):
        assert _is_us_location("New York, NY") is True

    def test_state_abbreviation_wa(self):
        assert _is_us_location("Seattle, WA") is True

    def test_united_states_keyword(self):
        assert _is_us_location("Remote in United States") is True

    def test_usa_keyword(self):
        assert _is_us_location("Remote, USA") is True

    def test_remote_alone(self):
        assert _is_us_location("Remote") is True

    def test_remote_us(self):
        assert _is_us_location("Remote in US") is True

    def test_empty_included_by_default(self):
        # No location info → include the job (return True)
        assert _is_us_location("") is True

    def test_none_like_whitespace(self):
        assert _is_us_location("   ") is True

    # Canada must be excluded even when "remote" appears
    def test_remote_canada_excluded(self):
        assert _is_us_location("Remote, Canada") is False

    def test_toronto_on_excluded(self):
        assert _is_us_location("Toronto, ON") is False

    def test_vancouver_bc_excluded(self):
        assert _is_us_location("Vancouver, BC") is False

    def test_montreal_qc_excluded(self):
        assert _is_us_location("Montreal, QC") is False

    def test_canada_keyword_excluded(self):
        assert _is_us_location("Ottawa, Ontario, Canada") is False

    # Other countries
    def test_uk_excluded(self):
        assert _is_us_location("London, UK") is False

    def test_germany_excluded(self):
        assert _is_us_location("Berlin, Germany") is False

    def test_india_excluded(self):
        assert _is_us_location("Bangalore, India") is False


# ── Dashboard has its OWN _is_us_location — verify it also excludes Canada ───

class TestDashboardIsUsLocation:
    """
    dashboard.py has a separate _is_us_location that was written before the
    Canada-exclusion logic was added to company_apply.py.
    If these tests fail, the dashboard still has the Canada bug.
    """
    def setup_method(self):
        import dashboard
        self.fn = dashboard._is_us_location

    def test_remote_canada_excluded(self):
        # BUG: dashboard._is_us_location matches "remote" before checking Canada
        assert self.fn("Remote, Canada") is False

    def test_toronto_on_excluded(self):
        assert self.fn("Toronto, ON") is False

    def test_austin_tx_included(self):
        assert self.fn("Austin, TX") is True

    def test_remote_included(self):
        assert self.fn("Remote") is True
