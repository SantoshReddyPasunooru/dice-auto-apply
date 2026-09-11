"""
Tests for filter_jobs() with max_required_years parameter.
Verifies that:
- max_required_years without experience filter only checks years, not levels
- max_required_years with experience filter applies both
- Job descriptions with year requirements are properly parsed
"""
import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from company_apply import filter_jobs


def _make_job(title, desc="", location="Remote", url=None):
    return {
        "title": title,
        "description": desc,
        "location": location,
        "url": url or f"https://jobs.io/{title.replace(' ', '-').lower()}",
        "posted_at": "2026-09-11T00:00:00Z",
    }


class TestMaxRequiredYearsWithoutExperienceFilter:
    def test_senior_not_blocked_by_years_filter_alone(self):
        """max_required_years without experience= should not block senior titles."""
        jobs = [_make_job("Senior Software Engineer", "2 years of experience required.")]
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), max_required_years=4)
        assert len(result) == 1

    def test_staff_not_blocked_by_years_filter_alone(self):
        jobs = [_make_job("Staff Engineer", "3 years of experience minimum.")]
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), max_required_years=4)
        assert len(result) == 1

    def test_high_years_requirement_blocks_regardless_of_level(self):
        jobs = [_make_job("Junior Developer", "7 years of experience required.")]
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), max_required_years=4)
        assert len(result) == 0

    def test_no_years_in_description_always_passes(self):
        jobs = [_make_job("Senior Engineer", "Great opportunity for growth.")]
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), max_required_years=4)
        assert len(result) == 1

    def test_none_max_years_skips_years_filter(self):
        jobs = [_make_job("Engineer", "10 years of experience required.")]
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), max_required_years=None)
        assert len(result) == 1


class TestMaxRequiredYearsWithExperienceFilter:
    def test_senior_blocked_by_experience_filter(self):
        jobs = [_make_job("Senior Software Engineer")]
        result = filter_jobs(
            jobs, keywords=[], applied_urls=set(),
            experience=["junior"], max_required_years=4
        )
        assert len(result) == 0

    def test_entry_level_passes_both_filters(self):
        jobs = [_make_job("Entry Level Developer", "1-2 years of experience preferred.")]
        result = filter_jobs(
            jobs, keywords=[], applied_urls=set(),
            experience=["junior"], max_required_years=4
        )
        assert len(result) == 1

    def test_high_years_blocks_even_junior_title(self):
        jobs = [_make_job("Junior Developer", "8 years of experience required.")]
        result = filter_jobs(
            jobs, keywords=[], applied_urls=set(),
            experience=["junior"], max_required_years=4
        )
        assert len(result) == 0

    def test_multiple_jobs_mixed_results(self):
        jobs = [
            _make_job("Senior Engineer", "2 years required."),    # blocked by level
            _make_job("Junior Developer", "1 year required."),    # passes both
            _make_job("Software Engineer", "6 years minimum."),   # blocked by years
            _make_job("Associate Engineer", "2 years required."), # passes both
        ]
        result = filter_jobs(
            jobs, keywords=[], applied_urls=set(),
            experience=["junior"], max_required_years=4
        )
        titles = [j["title"] for j in result]
        assert "Junior Developer" in titles
        assert "Associate Engineer" in titles
        assert "Senior Engineer" not in titles
        assert "Software Engineer" not in titles
