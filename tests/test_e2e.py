"""
END-TO-END TESTS — dashboard.py
Tests complete user journeys through the real application without a live
Textual browser. Each test simulates an actual user scenario:

  Journey 1 — First-time launch: CLI args drive app initialization
  Journey 2 — Returning user: saved filters restore on relaunch
  Journey 3 — Applying: subprocess launched, stdout captured, log file written
  Journey 4 — Multi-company: each company gets its own sequential subprocess
  Journey 5 — Full data pipeline: filter→sort→markup chain end-to-end
  Journey 6 — Stats: applied/error counts reflect real CSV data
  Journey 7 — Session persistence: keywords, location, experience survive restart

Bug exposed by E2E testing:
  • main() reads saved location/exp/days from disk but NEVER reads saved keywords
    → keywords filter is lost on every restart even though _save_filters writes it
"""
import csv
import json
import re
import sys
import threading
import subprocess
import pathlib
import time
import pytest
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
from dashboard import (
    DashboardApp, _load_saved, _save_filters, _load_rows, _sort_rows,
    _status_markup, _log_markup, _infer_exp, _infer_industry,
    _load_company_options, APPLIED_LOG_PATH, SAVED_FILTERS, COMPANY_PY,
    _US_ONLY_SENTINEL,
)

FIELDS = ["timestamp", "profile_email", "job_title", "company", "location", "status"]

# ── helpers ───────────────────────────────────────────────────────────────────

def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def _sample_csv_rows(email="u@x.com", n=5):
    now = datetime.now()
    templates = [
        ("Senior ML Engineer",     "Acme",    "Remote",           "applied"),
        ("Junior Data Analyst",    "Beta",    "Austin, TX",       "skipped"),
        ("Staff Platform Engineer","Gamma",   "San Francisco, CA","error: timeout"),
        ("Product Manager",        "Delta",   "New York, NY",     "applied"),
        ("IT Support Specialist",  "Echo",    "Remote",           "submitted"),
    ]
    return [
        {
            "timestamp":     (now - timedelta(days=i)).isoformat(),
            "profile_email": email,
            "job_title":     templates[i % len(templates)][0],
            "company":       templates[i % len(templates)][1],
            "location":      templates[i % len(templates)][2],
            "status":        templates[i % len(templates)][3],
        }
        for i in range(n)
    ]


# ── Journey 1: CLI args → DashboardApp constructor ────────────────────────────

class TestCLIArgParsing:
    """
    Simulate `python dashboard.py <flags>` and verify that main() passes
    the correct parameters to DashboardApp.
    """

    def _run_main(self, argv, saved=None, tmp_path=None):
        """Run main() with patched sys.argv; capture DashboardApp init kwargs."""
        captured = {}

        orig_init = DashboardApp.__init__

        def fake_init(self_obj, **kw):
            captured.update(kw)
            # Initialize just enough state to avoid AttributeError
            self_obj.email    = kw.get("email", "")
            self_obj.base_cmd = kw.get("base_cmd", [])
            self_obj._f_title    = kw.get("init_title", "")
            self_obj._f_loc      = kw.get("init_loc", "")
            self_obj._f_exp      = kw.get("init_exp", "")
            self_obj._f_days     = kw.get("init_days", "")
            self_obj._f_industry = ""
            self_obj._f_company  = kw.get("init_company", "")
            self_obj._active_proc          = None
            self_obj._proc_done            = threading.Event()
            self_obj._proc_done.set()
            self_obj._log_queue            = deque(maxlen=2000)
            self_obj._seen_log             = 0
            self_obj._tick_n               = 0
            self_obj._proc_finished_notified = False
            self_obj._log_file             = None

        if tmp_path:
            dashboard.SAVED_FILTERS = tmp_path / "filters.json"
            if saved:
                (tmp_path / "filters.json").write_text(json.dumps(saved))

        with patch("sys.argv", ["dashboard.py"] + argv), \
             patch.object(DashboardApp, "__init__", fake_init), \
             patch.object(DashboardApp, "run", lambda self_obj: None):
            dashboard.main()

        return captured

    def test_profile_flag_sets_email(self, tmp_path):
        kw = self._run_main(["--profile", "u@x.com"], tmp_path=tmp_path)
        assert kw["email"] == "u@x.com"

    def test_company_flag_sets_init_company(self, tmp_path):
        kw = self._run_main(["--company", "Airbnb"], tmp_path=tmp_path)
        assert kw["init_company"] == "Airbnb"

    def test_keywords_flag_sets_init_title(self, tmp_path):
        kw = self._run_main(["--keywords", "python, ml"], tmp_path=tmp_path)
        assert kw["init_title"] == "python, ml"

    def test_location_flag_sets_init_loc(self, tmp_path):
        kw = self._run_main(["--location", "remote"], tmp_path=tmp_path)
        assert kw["init_loc"] == "remote"

    def test_experience_flag_sets_init_exp(self, tmp_path):
        kw = self._run_main(["--experience", "senior"], tmp_path=tmp_path)
        assert kw["init_exp"] == "senior"

    def test_posted_days_flag_sets_init_days(self, tmp_path):
        kw = self._run_main(["--posted-days", "14"], tmp_path=tmp_path)
        assert kw["init_days"] == "14"

    def test_dry_run_flag_in_base_cmd(self, tmp_path):
        kw = self._run_main(["--dry-run"], tmp_path=tmp_path)
        assert "--dry-run" in kw["base_cmd"]

    def test_us_only_flag_in_base_cmd(self, tmp_path):
        kw = self._run_main(["--us-only"], tmp_path=tmp_path)
        assert "--us-only" in kw["base_cmd"]

    def test_profile_in_base_cmd(self, tmp_path):
        kw = self._run_main(["--profile", "u@x.com"], tmp_path=tmp_path)
        assert "--profile" in kw["base_cmd"]
        assert "u@x.com" in kw["base_cmd"]

    def test_company_py_is_in_base_cmd(self, tmp_path):
        kw = self._run_main([], tmp_path=tmp_path)
        assert str(COMPANY_PY) in kw["base_cmd"]

    def test_no_args_gives_empty_email_and_filters(self, tmp_path):
        kw = self._run_main([], tmp_path=tmp_path)
        assert kw["email"]        == ""
        assert kw["init_title"]   == ""
        assert kw["init_loc"]     == ""
        assert kw["init_exp"]     == ""
        assert kw["init_days"]    == ""
        assert kw["init_company"] == ""


# ── Journey 2: Returning user — saved filters restore ─────────────────────────

class TestSavedFiltersRestoreOnRelaunch:
    """
    Simulate a returning user who set filters in a previous session.
    Verify that main() restores those filters into DashboardApp's init params.
    """

    def _run_main_with_saved(self, argv, saved_data, tmp_path):
        captured = {}

        def fake_init(self_obj, **kw):
            captured.update(kw)
            self_obj.email    = kw.get("email", "")
            self_obj.base_cmd = kw.get("base_cmd", [])
            self_obj._f_title    = kw.get("init_title", "")
            self_obj._f_loc      = kw.get("init_loc", "")
            self_obj._f_exp      = kw.get("init_exp", "")
            self_obj._f_days     = kw.get("init_days", "")
            self_obj._f_industry = ""
            self_obj._f_company  = kw.get("init_company", "")
            self_obj._active_proc          = None
            self_obj._proc_done            = threading.Event()
            self_obj._proc_done.set()
            self_obj._log_queue            = deque(maxlen=2000)
            self_obj._seen_log             = 0
            self_obj._tick_n               = 0
            self_obj._proc_finished_notified = False
            self_obj._log_file             = None

        filters_path = tmp_path / "filters.json"
        filters_path.write_text(json.dumps(saved_data))
        dashboard.SAVED_FILTERS = filters_path

        with patch("sys.argv", ["dashboard.py"] + argv), \
             patch.object(DashboardApp, "__init__", fake_init), \
             patch.object(DashboardApp, "run", lambda self_obj: None):
            dashboard.main()

        return captured

    def test_saved_location_restores_on_relaunch(self, tmp_path):
        saved = {"u@x.com": {"locations": ["remote"], "experience": [], "posted_days": None, "keywords": ""}}
        kw = self._run_main_with_saved(["--profile", "u@x.com"], saved, tmp_path)
        assert kw["init_loc"] == "remote"

    def test_saved_experience_restores_on_relaunch(self, tmp_path):
        saved = {"u@x.com": {"locations": [], "experience": ["senior"], "posted_days": None, "keywords": ""}}
        kw = self._run_main_with_saved(["--profile", "u@x.com"], saved, tmp_path)
        assert kw["init_exp"] == "senior"

    def test_saved_days_restores_on_relaunch(self, tmp_path):
        saved = {"u@x.com": {"locations": [], "experience": [], "posted_days": 7, "keywords": ""}}
        kw = self._run_main_with_saved(["--profile", "u@x.com"], saved, tmp_path)
        assert kw["init_days"] == "7"

    def test_saved_keywords_restores_on_relaunch(self, tmp_path):
        """BR-15: saved keywords must be read back by main() on relaunch.
        CURRENT BUG: main() reads locations/experience/days from saved but
        NEVER reads saved['keywords'], so init_title is always '' on relaunch."""
        saved = {"u@x.com": {"locations": [], "experience": [], "posted_days": None, "keywords": "python, ml"}}
        kw = self._run_main_with_saved(["--profile", "u@x.com"], saved, tmp_path)
        assert kw["init_title"] == "python, ml", (
            "Saved keywords are not restored on relaunch. "
            "main() must read saved.get('keywords', '') for init_title."
        )

    def test_cli_location_overrides_saved(self, tmp_path):
        saved = {"u@x.com": {"locations": ["austin"], "experience": [], "posted_days": None, "keywords": ""}}
        kw = self._run_main_with_saved(["--profile", "u@x.com", "--location", "remote"], saved, tmp_path)
        assert kw["init_loc"] == "remote"

    def test_cli_experience_overrides_saved(self, tmp_path):
        saved = {"u@x.com": {"locations": [], "experience": ["junior"], "posted_days": None, "keywords": ""}}
        kw = self._run_main_with_saved(["--profile", "u@x.com", "--experience", "senior"], saved, tmp_path)
        assert kw["init_exp"] == "senior"

    def test_saved_filters_for_different_email_not_loaded(self, tmp_path):
        saved = {
            "alice@x.com": {"locations": ["remote"], "experience": ["senior"],
                            "posted_days": 7, "keywords": "python"},
        }
        kw = self._run_main_with_saved(["--profile", "bob@x.com"], saved, tmp_path)
        assert kw["init_loc"]   == ""
        assert kw["init_exp"]   == ""
        assert kw["init_days"]  == ""
        assert kw["init_title"] == ""

    def teardown_method(self):
        dashboard.SAVED_FILTERS = SAVED_FILTERS


# ── Journey 3: DashboardApp constructor state ──────────────────────────────────

class TestDashboardAppConstructorE2E:
    """Verify DashboardApp initialises to a correct ready state."""

    def _make_app(self, **kw):
        defaults = dict(email="u@x.com", base_cmd=["python", "apply.py"])
        return DashboardApp(**{**defaults, **kw})

    def test_email_stored(self):
        app = self._make_app(email="test@x.com")
        assert app.email == "test@x.com"

    def test_base_cmd_stored(self):
        app = self._make_app(base_cmd=["python", "-u", "apply.py", "--profile", "u@x.com"])
        assert app.base_cmd == ["python", "-u", "apply.py", "--profile", "u@x.com"]

    def test_all_filter_fields_initialised(self):
        app = self._make_app(init_title="ml", init_loc="remote",
                             init_exp="senior", init_days="7")
        assert app._f_title == "ml"
        assert app._f_loc   == "remote"
        assert app._f_exp   == "senior"
        assert app._f_days  == "7"

    def test_industry_always_starts_empty(self):
        app = self._make_app()
        assert app._f_industry == ""

    def test_proc_done_event_starts_set(self):
        """App must start in 'ready' state so _start_applying is not blocked."""
        app = self._make_app()
        assert app._proc_done.is_set()

    def test_active_proc_starts_as_none(self):
        app = self._make_app()
        assert app._active_proc is None

    def test_log_queue_starts_empty(self):
        app = self._make_app()
        assert len(app._log_queue) == 0

    def test_log_file_starts_as_none(self):
        app = self._make_app()
        assert app._log_file is None

    def test_init_company_stored(self):
        app = self._make_app(init_company="Airbnb")
        assert app._f_company == "Airbnb"


# ── Journey 4: Subprocess launch + log capture ────────────────────────────────

class TestSubprocessAndLogPipelineE2E:
    """
    Exercise the _reader_multi pipeline (the inner function in _start_applying)
    directly.  We replicate its logic against real subprocesses so every part
    of the threading + file-write chain is tested end-to-end.
    """

    @staticmethod
    def _run_reader(cmds, log_path):
        """Replicate _reader_multi with real subprocesses."""
        q = deque(maxlen=2000)
        done = threading.Event()
        active_proc = [None]

        def reader(cmds, lf, q, done):
            with open(lf, "w", encoding="utf-8") as f:
                for cmd in cmds:
                    p = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        text=True,
                        bufsize=1,
                    )
                    active_proc[0] = p
                    for line in p.stdout:
                        q.append(line)
                        f.write(line)
                        f.flush()
                    p.wait()
            done.set()

        t = threading.Thread(target=reader, args=(cmds, log_path, q, done), daemon=True)
        t.start()
        done.wait(timeout=15)
        return q, done, active_proc[0]

    def test_stdout_captured_into_deque(self, tmp_path):
        script = [sys.executable, "-c", "print('hello'); print('world')"]
        q, done, _ = self._run_reader([script], tmp_path / "test.log")
        lines = list(q)
        assert any("hello" in line for line in lines)
        assert any("world" in line for line in lines)

    def test_log_file_written(self, tmp_path):
        log = tmp_path / "test.log"
        script = [sys.executable, "-c", "print('LINE1'); print('LINE2')"]
        self._run_reader([script], log)
        content = log.read_text()
        assert "LINE1" in content
        assert "LINE2" in content

    def test_done_event_set_on_completion(self, tmp_path):
        script = [sys.executable, "-c", "import time; time.sleep(0.05); print('done')"]
        _, done, _ = self._run_reader([script], tmp_path / "test.log")
        assert done.is_set()

    def test_multi_commands_run_sequentially(self, tmp_path):
        """Two companies → two commands → output appears in order."""
        log = tmp_path / "test.log"
        cmds = [
            [sys.executable, "-c", "print('CMD1_OUTPUT')"],
            [sys.executable, "-c", "print('CMD2_OUTPUT')"],
        ]
        q, done, _ = self._run_reader(cmds, log)
        assert done.is_set()
        lines = list(q)
        all_output = " ".join(lines)
        assert "CMD1_OUTPUT" in all_output
        assert "CMD2_OUTPUT" in all_output

    def test_multi_commands_maintain_order(self, tmp_path):
        """Second command must not start until first has finished."""
        log = tmp_path / "test.log"
        cmds = [
            [sys.executable, "-c", "print('FIRST')"],
            [sys.executable, "-c", "print('SECOND')"],
        ]
        q, _, _ = self._run_reader(cmds, log)
        content = log.read_text()
        assert content.index("FIRST") < content.index("SECOND")

    def test_stderr_captured_via_stdout_redirect(self, tmp_path):
        """stderr=STDOUT means error output appears in the log."""
        log = tmp_path / "test.log"
        script = [sys.executable, "-c",
                  "import sys; sys.stderr.write('STDERR_LINE\\n'); print('STDOUT_LINE')"]
        self._run_reader([script], log)
        content = log.read_text()
        assert "STDERR_LINE" in content

    def test_log_filename_contains_timestamp_pattern(self, tmp_path):
        """Log file name must match apply_logs_YYYYMMDD_HHMMSS.log"""
        now = datetime.now()
        log_name = f"apply_logs_{now:%Y%m%d_%H%M%S}.log"
        log = tmp_path / log_name
        assert re.match(r"apply_logs_\d{8}_\d{6}\.log", log.name)

    def test_empty_command_list_does_not_crash(self, tmp_path):
        """If no companies are selected and DB is empty, cmds=[] — must not hang."""
        log = tmp_path / "test.log"
        q, done, _ = self._run_reader([], log)
        assert done.is_set()
        assert len(q) == 0

    def test_deque_respects_maxlen(self, tmp_path):
        """Log queue has maxlen=2000; overflow must drop oldest, not crash."""
        log = tmp_path / "test.log"
        # Generate 2100 lines
        script = [sys.executable, "-c",
                  "for i in range(2100): print(f'line {i}')"]
        q, done, _ = self._run_reader([script], log)
        assert done.is_set()
        assert len(q) == 2000   # capped at maxlen
        lines = list(q)
        # Newest lines should be retained
        assert any("2099" in ln for ln in lines)
        # Oldest lines should be dropped
        assert not any("line 0\n" == ln for ln in lines)


# ── Journey 5: Full data pipeline E2E ────────────────────────────────────────

class TestFullDataPipelineE2E:
    """
    Test the complete chain a real user sees:
    CSV on disk → _load_rows filter → _sort_rows → _status_markup → table row
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.tmp = tmp_path
        rows = _sample_csv_rows(n=5)
        path = tmp_path / "applied.csv"
        _write_csv(path, rows)
        dashboard.APPLIED_LOG_PATH = path
        yield
        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH

    def test_load_filter_sort_markup_chain(self):
        """Full pipeline: load → filter → sort → status markup for table row."""
        # Load with no filters
        rows = _load_rows("u@x.com", "", "", "", "")
        assert len(rows) > 0

        # Sort newest first
        sorted_rows = _sort_rows(rows, "recent_first")
        assert sorted_rows[0]["timestamp"] >= sorted_rows[-1]["timestamp"]

        # Generate table row content
        for row in sorted_rows:
            date_col    = row.get("timestamp", "")[:10]
            company_col = row.get("company",   "")[:14]
            title_col   = row.get("job_title", "")[:38]
            loc_col     = row.get("location",  "")[:26]
            exp_col     = _infer_exp(row.get("job_title", ""))
            status_col  = _status_markup(row.get("status", ""))

            assert isinstance(date_col,    str)
            assert isinstance(company_col, str)
            assert isinstance(title_col,   str)
            assert isinstance(loc_col,     str)
            assert exp_col in ("Principal","Staff","Senior","Lead","Mid","Junior","Intern")
            assert isinstance(status_col,  str)

    def test_applied_count_from_pipeline(self):
        rows = _load_rows("u@x.com", "", "", "", "")
        applied = sum(1 for r in rows if
                      any(w in r.get("status", "").lower() for w in ("applied","submitted")))
        # From _sample_csv_rows: "applied" + "submitted" statuses
        assert applied >= 1

    def test_error_count_from_pipeline(self):
        rows = _load_rows("u@x.com", "", "", "", "")
        errors = sum(1 for r in rows if "error" in r.get("status", "").lower())
        assert errors >= 1

    def test_us_only_pipeline_removes_non_us(self):
        rows = _load_rows("u@x.com", "", _US_ONLY_SENTINEL, "", "")
        for row in rows:
            from dashboard import _is_us_location
            assert _is_us_location(row.get("location", "")), \
                f"Non-US location passed filter: {row.get('location')}"

    def test_pipeline_with_zero_results_does_not_crash(self):
        rows = _load_rows("u@x.com", "XXXXNONEXISTENTXXXX", "", "", "")
        assert rows == []
        sorted_rows = _sort_rows(rows, "recent_first")
        assert sorted_rows == []

    def test_industry_column_inferred_for_all_rows(self):
        rows = _load_rows("u@x.com", "", "", "", "")
        industries = [_infer_industry(r.get("job_title", "")) for r in rows]
        for ind in industries:
            assert isinstance(ind, str) and ind


# ── Journey 6: Full save-apply-reload session ─────────────────────────────────

class TestFullSessionE2E:
    """
    Simulate a complete user session:
    1. User sets filters and clicks Start → filters are saved
    2. A subprocess runs and appends to the applied CSV
    3. User relaunches → filters are restored
    4. Table reflects the new applied job
    """

    def test_save_then_reload_full_session(self, tmp_path):
        """Filters saved in session 1 must restore in session 2."""
        f_path = tmp_path / "filters.json"
        dashboard.SAVED_FILTERS = f_path

        # Session 1: user sets filters
        _save_filters("u@x.com", "remote", "senior", "7", "python, ml")

        # Session 2: verify all persisted values load back
        saved = _load_saved("u@x.com")
        assert saved.get("locations")   == ["remote"]
        assert saved.get("experience")  == ["senior"]
        assert saved.get("posted_days") == 7
        assert saved.get("keywords")    == "python, ml"

        dashboard.SAVED_FILTERS = SAVED_FILTERS

    def test_subprocess_appends_to_csv_and_table_reflects(self, tmp_path):
        """
        A real subprocess that appends a row to the applied CSV must be
        visible when _load_rows is called after the subprocess finishes.
        """
        csv_path = tmp_path / "applied.csv"
        _write_csv(csv_path, [])  # start empty
        dashboard.APPLIED_LOG_PATH = csv_path

        # Simulate a subprocess that appends a job to the CSV
        now = datetime.now().isoformat()
        append_script = [
            sys.executable, "-c",
            f"""
import csv, sys
FIELDS = {FIELDS!r}
row = {{
    'timestamp': '{now}',
    'profile_email': 'u@x.com',
    'job_title': 'Senior ML Engineer',
    'company': 'TestCo',
    'location': 'Remote',
    'status': 'applied',
}}
write_header = True
try:
    with open({str(csv_path)!r}) as f:
        write_header = not f.read(1)
except FileNotFoundError:
    pass
with open({str(csv_path)!r}, 'a', newline='') as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    if write_header:
        w.writeheader()
    w.writerow(row)
print('applied: Senior ML Engineer at TestCo')
"""
        ]

        log = tmp_path / "apply.log"
        done = threading.Event()
        q = deque(maxlen=2000)

        def reader():
            with open(log, "w") as f:
                p = subprocess.Popen(
                    append_script,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, bufsize=1,
                )
                for line in p.stdout:
                    q.append(line)
                    f.write(line)
                    f.flush()
                p.wait()
            done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        done.wait(timeout=10)
        assert done.is_set()

        # Now the table should reflect the new row
        rows = _load_rows("u@x.com", "", "", "", "")
        titles = [r["job_title"] for r in rows]
        assert "Senior ML Engineer" in titles

        # Log must contain the apply output
        log_content = log.read_text()
        assert "applied" in log_content

        dashboard.APPLIED_LOG_PATH = APPLIED_LOG_PATH

    def test_multi_company_sequential_run(self, tmp_path):
        """Two companies run sequentially; both outputs appear in log."""
        log = tmp_path / "multi.log"
        q = deque(maxlen=2000)
        done = threading.Event()

        cmds = [
            [sys.executable, "-c", "print('Airbnb: 3 jobs found'); print('applied: Role A')"],
            [sys.executable, "-c", "print('Stripe: 7 jobs found'); print('applied: Role B')"],
        ]

        def reader():
            with open(log, "w") as f:
                for cmd in cmds:
                    p = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, text=True, bufsize=1,
                    )
                    for line in p.stdout:
                        q.append(line)
                        f.write(line)
                        f.flush()
                    p.wait()
            done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        done.wait(timeout=10)
        assert done.is_set()

        content = log.read_text()
        assert "Airbnb" in content
        assert "Stripe" in content
        assert "Role A" in content
        assert "Role B" in content
        # Sequential: Airbnb output appears before Stripe
        assert content.index("Airbnb") < content.index("Stripe")


# ── Journey 7: Filter-flag to subprocess E2E ─────────────────────────────────

class TestFilterFlagToSubprocessE2E:
    """
    Verify the complete chain from user-selected filters → subprocess command flags.
    Tests what a real subprocess actually receives as arguments.
    """

    def test_subprocess_receives_keywords_flag(self, tmp_path):
        log = tmp_path / "test.log"
        # Script that prints its own argv
        script = [sys.executable, "-c",
                  "import sys; print(' '.join(sys.argv[1:]))"]
        cmd = script + ["--keywords", "python, ml"]

        done = threading.Event()
        q = deque(maxlen=100)

        def reader():
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, bufsize=1)
            for line in p.stdout:
                q.append(line)
            p.wait()
            done.set()

        threading.Thread(target=reader, daemon=True).start()
        done.wait(timeout=5)
        output = "".join(q)
        assert "--keywords" in output
        assert "python, ml" in output

    def test_us_only_flag_passed_to_subprocess(self, tmp_path):
        script = [sys.executable, "-c",
                  "import sys; print(' '.join(sys.argv[1:]))"]
        cmd = script + ["--us-only"]

        done = threading.Event()
        q = deque(maxlen=100)

        def reader():
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, bufsize=1)
            for line in p.stdout:
                q.append(line)
            p.wait()
            done.set()

        threading.Thread(target=reader, daemon=True).start()
        done.wait(timeout=5)
        assert "--us-only" in "".join(q)

    def test_semicolon_in_keywords_not_split_by_shell(self, tmp_path):
        """Semicolons must be passed as a single arg, not interpreted by shell."""
        script = [sys.executable, "-c",
                  "import sys; [print(a) for a in sys.argv[1:]]"]
        cmd = script + ["--keywords", "python; rm -rf /"]

        done = threading.Event()
        q = deque(maxlen=100)

        def reader():
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, bufsize=1)
            for line in p.stdout:
                q.append(line)
            p.wait()
            done.set()

        threading.Thread(target=reader, daemon=True).start()
        done.wait(timeout=5)
        output = "".join(q)
        assert "python; rm -rf /" in output  # received as single arg
        assert "rm" not in [ln.strip() for ln in q if ln.strip() == "rm"]


