"""
Tests for filter_jobs in company_apply.py.
"""
import pytest
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from company_apply import filter_jobs


@pytest.fixture
def jobs():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone.utc)
    def ts(days): return (now - timedelta(days=days)).isoformat()

    return [
        {"title": "Senior Software Engineer", "location": "Austin, TX",        "url": "https://a.io/1", "posted_at": ts(1)},
        {"title": "Junior Data Analyst",      "location": "Remote",            "url": "https://a.io/2", "posted_at": ts(3)},
        {"title": "Staff ML Engineer",        "location": "San Francisco, CA", "url": "https://a.io/3", "posted_at": ts(10)},
        {"title": "Product Manager",          "location": "Toronto, ON",       "url": "https://a.io/4", "posted_at": ts(2)},
        {"title": "DevOps Engineer",          "location": "Remote, Canada",    "url": "https://a.io/5", "posted_at": ts(5)},
        {"title": "Data Scientist",           "location": "New York, NY",      "url": "https://a.io/6", "posted_at": ts(0)},
        {"title": "Intern - Software",        "location": "Seattle, WA",       "url": "https://a.io/7", "posted_at": ts(1)},
        {"title": "Principal Engineer",       "location": "United States",     "url": "https://a.io/8", "posted_at": ts(30)},
    ]


class TestKeywordFilter:
    def test_keyword_match_returns_only_matching(self, jobs):
        result = filter_jobs(jobs, keywords=["engineer"], applied_urls=set())
        titles = [j["title"] for j in result]
        assert all("engineer" in t.lower() for t in titles)
        assert "Junior Data Analyst" not in titles
        assert "Product Manager" not in titles

    def test_multiple_keywords_any_match(self, jobs):
        result = filter_jobs(jobs, keywords=["data", "ml"], applied_urls=set())
        titles = [j["title"] for j in result]
        assert "Junior Data Analyst" in titles
        assert "Staff ML Engineer" in titles
        assert "Senior Software Engineer" not in titles

    def test_empty_keywords_returns_all(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set())
        assert len(result) == len(jobs)

    def test_keyword_case_insensitive(self, jobs):
        result = filter_jobs(jobs, keywords=["ENGINEER"], applied_urls=set())
        assert len(result) > 0


class TestAppliedUrlsFilter:
    def test_applied_url_skipped(self, jobs):
        applied = {"https://a.io/1", "https://a.io/2"}
        result = filter_jobs(jobs, keywords=[], applied_urls=applied)
        urls = [j["url"] for j in result]
        assert "https://a.io/1" not in urls
        assert "https://a.io/2" not in urls

    def test_empty_applied_returns_all(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set())
        assert len(result) == len(jobs)


class TestUsOnlyFilter:
    def test_us_only_excludes_canada(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), us_only=True)
        locations = [j["location"] for j in result]
        assert "Toronto, ON" not in locations
        assert "Remote, Canada" not in locations

    def test_us_only_keeps_us_states(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), us_only=True)
        locations = [j["location"] for j in result]
        assert "Austin, TX" in locations
        assert "New York, NY" in locations
        assert "Remote" in locations

    def test_us_only_keeps_united_states(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), us_only=True)
        locations = [j["location"] for j in result]
        assert "United States" in locations

    def test_us_only_false_keeps_canada(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), us_only=False)
        locations = [j["location"] for j in result]
        assert "Toronto, ON" in locations


class TestPostedDaysFilter:
    def test_posted_days_7_excludes_old(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), posted_days=7)
        urls = [j["url"] for j in result]
        # Posted 10 days ago and 30 days ago should be excluded
        assert "https://a.io/3" not in urls  # 10 days
        assert "https://a.io/8" not in urls  # 30 days

    def test_posted_days_7_keeps_recent(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), posted_days=7)
        urls = [j["url"] for j in result]
        assert "https://a.io/1" in urls  # 1 day
        assert "https://a.io/6" in urls  # 0 days

    def test_posted_days_none_keeps_all(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), posted_days=None)
        assert len(result) == len(jobs)

    def test_missing_posted_at_included(self, jobs):
        jobs_no_date = [{"title": "Engineer", "location": "Remote", "url": "x", "posted_at": ""}]
        result = filter_jobs(jobs_no_date, keywords=[], applied_urls=set(), posted_days=1)
        assert len(result) == 1


class TestExperienceFilter:
    def test_senior_filter(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), experience=["senior"])
        titles = [j["title"] for j in result]
        assert "Senior Software Engineer" in titles
        assert "Junior Data Analyst" not in titles

    def test_junior_filter_uses_synonyms(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), experience=["junior"])
        titles = [j["title"] for j in result]
        assert "Junior Data Analyst" in titles
        # Intern should also be included via synonym expansion
        assert "Intern - Software" in titles

    def test_staff_filter(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), experience=["staff"])
        titles = [j["title"] for j in result]
        assert "Staff ML Engineer" in titles
        assert "Senior Software Engineer" not in titles

    def test_no_experience_filter_returns_all(self, jobs):
        result = filter_jobs(jobs, keywords=[], applied_urls=set(), experience=None)
        assert len(result) == len(jobs)


class TestCombinedFilters:
    def test_keyword_and_us_only(self, jobs):
        result = filter_jobs(jobs, keywords=["engineer"], applied_urls=set(), us_only=True)
        for j in result:
            assert "engineer" in j["title"].lower()
            assert j["location"] not in ("Toronto, ON", "Remote, Canada")

    def test_all_filters_combined(self, jobs):
        result = filter_jobs(
            jobs, keywords=["engineer"], applied_urls={"https://a.io/1"},
            us_only=True, posted_days=7, experience=["senior"],
        )
        # Senior Software Engineer at Austin TX (1 day ago) → excluded (applied_urls)
        # Staff ML Engineer (10 days ago) → excluded (posted_days=7)
        # Principal Engineer (30 days ago) → excluded (posted_days=7)
        # DevOps Engineer Remote Canada → excluded (us_only)
        assert len(result) == 0
