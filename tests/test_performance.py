"""
PERFORMANCE TESTS — dashboard.py
Verify that functions called frequently (per-tick, per-row) complete within
acceptable time bounds even under high load.

Thresholds are intentionally generous to avoid flakiness on slow CI boxes.
"""
import csv, sys, pathlib, time, random, string
from datetime import datetime, timedelta
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
from dashboard import (
    _load_rows, _sort_rows, _parse_ts, _status_markup,
    _log_markup, _infer_exp, _infer_industry, _is_us_location,
    APPLIED_LOG_PATH, SORT_MODES,
)

FIELDS = ["timestamp", "profile_email", "job_title", "company", "location", "status"]


def _make_large_csv(path, n_rows=5_000, email="u@x.com"):
    statuses = ["applied", "skipped", "error: timeout", "submitted"]
    locations = ["Austin, TX", "Remote", "San Francisco, CA",
                 "New York, NY", "Remote, Canada", "Toronto, ON"]
    titles = [
        "Senior Software Engineer", "Junior Data Analyst", "Staff ML Engineer",
        "DevOps Engineer", "Product Manager", "UX Designer", "VP of Engineering",
        "Customer Success Manager", "IT Support Specialist", "Senior Accountant",
    ]
    now = datetime.now()
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for i in range(n_rows):
            w.writerow({
                "timestamp": (now - timedelta(days=i % 60)).isoformat(),
                "profile_email": email if i % 10 != 0 else "other@x.com",
                "job_title": titles[i % len(titles)],
                "company": f"Company{i % 50}",
                "location": locations[i % len(locations)],
                "status": statuses[i % len(statuses)],
            })


class TestLoadRowsPerformance:

    def test_load_5k_rows_under_2_seconds(self, tmp_path):
        path = tmp_path / "applied.csv"
        _make_large_csv(path, n_rows=5_000)
        dashboard.APPLIED_LOG_PATH = path

        start = time.perf_counter()
        rows = _load_rows("u@x.com", "", "", "", "")
        elapsed = time.perf_counter() - start

        assert elapsed < 2.0, f"_load_rows took {elapsed:.2f}s on 5K rows (limit: 2.0s)"
        assert len(rows) > 0

    def test_load_rows_with_all_filters_under_2_seconds(self, tmp_path):
        path = tmp_path / "applied.csv"
        _make_large_csv(path, n_rows=5_000)
        dashboard.APPLIED_LOG_PATH = path

        start = time.perf_counter()
        rows = _load_rows("u@x.com", "engineer", "__us_only__", "senior", "30",
                          f_industry="engineering")
        elapsed = time.perf_counter() - start

        assert elapsed < 2.0, f"_load_rows (all filters) took {elapsed:.2f}s (limit: 2.0s)"

    def test_double_load_rows_simulating_tick_refresh_under_3_seconds(self, tmp_path):
        """_load_table calls _load_rows TWICE per refresh cycle (once for total count,
        once for filtered rows).  Verify that round-trip is acceptable."""
        path = tmp_path / "applied.csv"
        _make_large_csv(path, n_rows=5_000)
        dashboard.APPLIED_LOG_PATH = path

        start = time.perf_counter()
        _load_rows("u@x.com", "", "", "", "")       # call 1: total count
        _load_rows("u@x.com", "engineer", "", "senior", "30")  # call 2: filtered
        elapsed = time.perf_counter() - start

        assert elapsed < 3.0, f"double _load_rows took {elapsed:.2f}s (limit: 3.0s)"

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH


class TestSortPerformance:

    def test_sort_5k_rows_recent_first_under_1_second(self, tmp_path):
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(days=i)).isoformat(),
             "job_title": "Engineer", "company": "Acme", "status": "applied"}
            for i in range(5_000)
        ]

        start = time.perf_counter()
        _sort_rows(rows, "recent_first")
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0, f"_sort_rows (recent_first) took {elapsed:.2f}s (limit: 1.0s)"

    def test_sort_5k_rows_az_under_1_second(self):
        rows = [
            {"timestamp": "2026-01-01", "job_title": f"Role{i:05d}",
             "company": f"Co{i}", "status": "applied"}
            for i in range(5_000)
        ]
        start = time.perf_counter()
        _sort_rows(rows, "az")
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"_sort_rows (az) took {elapsed:.2f}s (limit: 1.0s)"

    def test_all_sort_modes_under_1_second(self):
        now = datetime.now()
        rows = [
            {"timestamp": (now - timedelta(days=i % 100)).isoformat(),
             "job_title": f"Role{i}", "company": f"Co{i % 20}", "status": "applied"}
            for i in range(2_000)
        ]
        for mode, _ in SORT_MODES:
            start = time.perf_counter()
            _sort_rows(rows, mode)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"_sort_rows ({mode}) took {elapsed:.2f}s"


class TestInferFunctionPerformance:

    def test_infer_industry_10k_calls_under_1_second(self):
        titles = [
            "Senior Software Engineer", "VP of Engineering", "Data Scientist",
            "Content Marketing Manager", "IT Support Specialist", "UX Designer",
            "Customer Success Manager", "Program Manager", "Senior Accountant",
            "Machine Learning Engineer",
        ]
        start = time.perf_counter()
        for i in range(10_000):
            _infer_industry(titles[i % len(titles)])
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"10K _infer_industry calls took {elapsed:.2f}s"

    def test_infer_exp_10k_calls_under_1_second(self):
        titles = [
            "Senior Engineer", "Junior Developer", "Staff Engineer",
            "Intern", "Software Engineer II", "Principal Architect",
        ]
        start = time.perf_counter()
        for i in range(10_000):
            _infer_exp(titles[i % len(titles)])
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"10K _infer_exp calls took {elapsed:.2f}s"

    def test_is_us_location_10k_calls_under_1_second(self):
        locs = [
            "Austin, TX", "Remote", "Toronto, ON", "San Francisco, CA",
            "Remote, Canada", "United States", "New York, NY", "London, UK",
        ]
        start = time.perf_counter()
        for i in range(10_000):
            _is_us_location(locs[i % len(locs)])
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"10K _is_us_location calls took {elapsed:.2f}s"

    def test_parse_ts_10k_calls_under_1_second(self):
        now = datetime.now()
        timestamps = [(now - timedelta(days=i)).isoformat() for i in range(100)]
        start = time.perf_counter()
        for i in range(10_000):
            _parse_ts(timestamps[i % len(timestamps)])
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"10K _parse_ts calls took {elapsed:.2f}s"

    def test_log_markup_2k_calls_under_half_second(self):
        """2000 = deque maxlen; simulates flushing a full log buffer."""
        lines = [
            "[profile] 'Name' → 'John'",
            "→ applied",
            "→ error: timeout",
            "[ollama] generating cover letter",
            "Fetching 45 jobs for Senior Engineer",
            "─────────────────────────────",
            "some plain text line without markup",
        ]
        start = time.perf_counter()
        for i in range(2_000):
            _log_markup(lines[i % len(lines)])
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, f"2K _log_markup calls took {elapsed:.2f}s"


class TestMemoryBoundary:

    def test_load_rows_with_very_long_job_title(self, tmp_path):
        """A job title of 10K characters must not crash — just load slowly."""
        long_title = "Senior " + ("Engineer " * 1000)
        path = tmp_path / "applied.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerow({
                "timestamp": "2026-09-01T10:00:00",
                "profile_email": "u@x.com",
                "job_title": long_title,
                "company": "Acme",
                "location": "Remote",
                "status": "applied",
            })
        dashboard.APPLIED_LOG_PATH = path
        rows = _load_rows("u@x.com", "", "", "", "")
        assert len(rows) == 1

    def teardown_method(self):
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH
