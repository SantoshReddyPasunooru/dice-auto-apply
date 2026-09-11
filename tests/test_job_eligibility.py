"""
Unit tests for job_eligibility.early_career_rejection_reason.
Covers: None/empty inputs, level filtering, years-experience filtering,
        interaction between level filter and years filter.
"""
import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from job_eligibility import early_career_rejection_reason


class TestNoneAndEmptyInputs:
    def test_none_title_returns_none(self):
        assert early_career_rejection_reason(None) is None

    def test_empty_title_returns_none(self):
        assert early_career_rejection_reason("") is None

    def test_none_description_is_safe(self):
        # description is optional; explicit None should not crash
        result = early_career_rejection_reason("Software Engineer", None)
        assert result is None

    def test_empty_description_is_safe(self):
        assert early_career_rejection_reason("Software Engineer", "") is None


class TestLevelFiltering:
    def test_senior_blocked_when_not_in_allowed(self):
        reason = early_career_rejection_reason(
            "Senior Software Engineer", allowed_levels=["entry", "mid"]
        )
        assert reason is not None
        assert "senior" in reason

    def test_staff_blocked_when_not_in_allowed(self):
        reason = early_career_rejection_reason(
            "Staff Engineer", allowed_levels=["entry"]
        )
        assert reason is not None
        assert "staff" in reason

    def test_senior_allowed_when_in_list(self):
        reason = early_career_rejection_reason(
            "Senior Software Engineer", allowed_levels=["senior", "mid"]
        )
        assert reason is None

    def test_junior_expands_to_entry_new_grad_early_career(self):
        # Passing "junior" in allowed_levels should also allow "entry" titles
        reason = early_career_rejection_reason(
            "Entry Level Developer", allowed_levels=["junior"]
        )
        assert reason is None

    def test_no_level_filter_allows_senior(self):
        # allowed_levels=None means no level restriction — senior should pass
        reason = early_career_rejection_reason(
            "Senior Software Engineer", allowed_levels=None
        )
        assert reason is None

    def test_no_level_filter_allows_staff(self):
        reason = early_career_rejection_reason(
            "Staff Engineer", allowed_levels=None
        )
        assert reason is None

    def test_intern_title_blocked_when_not_allowed(self):
        reason = early_career_rejection_reason(
            "Software Engineering Intern", allowed_levels=["entry", "mid"]
        )
        assert reason is not None
        assert "intern" in reason

    def test_title_with_no_level_keyword_passes_any_filter(self):
        reason = early_career_rejection_reason(
            "Software Engineer", allowed_levels=["entry"]
        )
        assert reason is None


class TestYearsFiltering:
    def test_blocks_when_years_exceed_max(self):
        desc = "Minimum 6 years of experience required."
        reason = early_career_rejection_reason("Software Engineer", desc, max_required_years=4)
        assert reason is not None
        assert "6" in reason

    def test_passes_when_years_below_max(self):
        desc = "At least 2 years of experience preferred."
        reason = early_career_rejection_reason("Software Engineer", desc, max_required_years=4)
        assert reason is None

    def test_years_at_exact_max_passes(self):
        desc = "Requires 4 years of experience."
        reason = early_career_rejection_reason("Software Engineer", desc, max_required_years=4)
        assert reason is None

    def test_years_without_experience_context_ignored(self):
        # "5 years" without context words like "required/minimum" should be ignored
        desc = "The product has been on the market for 5 years."
        reason = early_career_rejection_reason("Software Engineer", desc, max_required_years=4)
        assert reason is None

    def test_range_uses_max_of_range(self):
        # "3-7 years" → max is 7; should be blocked when max_required_years=4
        desc = "3-7 years of professional experience required."
        reason = early_career_rejection_reason("Software Engineer", desc, max_required_years=4)
        assert reason is not None
        assert "7" in reason

    def test_years_in_title_plus_years_in_desc(self):
        # Level filter fires first on title if allowed_levels is set
        desc = "10 years experience minimum."
        reason = early_career_rejection_reason(
            "Senior Software Engineer", desc,
            max_required_years=4, allowed_levels=["entry"]
        )
        assert reason is not None  # blocked by level, not years


class TestNoLevelFilterYearsOnly:
    def test_no_level_filter_still_checks_years(self):
        # allowed_levels=None skips level check but still applies years check
        desc = "Minimum 8 years of experience required."
        reason = early_career_rejection_reason(
            "Staff Engineer", desc, max_required_years=4, allowed_levels=None
        )
        assert reason is not None
        assert "8" in reason

    def test_no_level_filter_passes_low_years(self):
        desc = "1-2 years of experience required."
        reason = early_career_rejection_reason(
            "Staff Engineer", desc, max_required_years=4, allowed_levels=None
        )
        assert reason is None
