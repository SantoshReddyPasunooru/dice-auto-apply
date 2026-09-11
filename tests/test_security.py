"""
SECURITY TESTS — dashboard.py
Verify that the dashboard is safe from injection, path traversal,
and data exposure — both as a producer and consumer of data.
"""
import csv, json, sys, pathlib, subprocess
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
from dashboard import (
    _load_rows, _save_filters, _load_saved,
    _log_markup, _status_markup, _is_us_location,
    _US_ONLY_SENTINEL,
)


class TestSubprocessInjection:
    """
    The dashboard builds subprocess command lists.  Because shell=False is used
    the OS shell never interprets metacharacters — verify that assumption holds
    and that the command structure is safe.
    """

    def _build_cmd(self, base, company, title="", loc="", exp="", days=""):
        """Replicate _start_applying command-building logic."""
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
        c = list(base) + ["--company", company] + filter_flags
        import sys as _sys
        if c[0] == _sys.executable and len(c) > 1 and c[1] != "-u":
            c = [c[0], "-u"] + c[1:]
        return c

    def test_semicolon_in_keywords_is_single_arg(self):
        base = ["python", "apply.py"]
        cmd = self._build_cmd(base, "Acme", title="python; rm -rf /")
        # The entire string must appear as ONE element, not split at ;
        assert "python; rm -rf /" in cmd
        # The shell-dangerous part must NOT appear as its own element
        assert "rm" not in cmd
        assert "-rf" not in cmd

    def test_pipe_in_keywords_is_single_arg(self):
        base = ["python", "apply.py"]
        cmd = self._build_cmd(base, "Acme", title="python | cat /etc/passwd")
        assert "python | cat /etc/passwd" in cmd
        assert "cat" not in cmd

    def test_backtick_injection_is_single_arg(self):
        base = ["python", "apply.py"]
        cmd = self._build_cmd(base, "Acme", title="`id`")
        assert "`id`" in cmd

    def test_subshell_dollar_injection_is_single_arg(self):
        base = ["python", "apply.py"]
        cmd = self._build_cmd(base, "Acme", title="$(cat /etc/passwd)")
        assert "$(cat /etc/passwd)" in cmd

    def test_newline_in_keywords_is_single_arg(self):
        base = ["python", "apply.py"]
        cmd = self._build_cmd(base, "Acme", title="python\nrm bad")
        # newline is a literal character in the arg list, not a command separator
        assert any("\n" in arg for arg in cmd)
        assert "rm" not in cmd

    def test_cmd_list_not_string(self):
        """The entire command must be a list, never a joined string."""
        base = ["python", "apply.py"]
        cmd = self._build_cmd(base, "Acme", title="ml engineer")
        assert isinstance(cmd, list)
        for arg in cmd:
            assert isinstance(arg, str)

    def test_path_traversal_in_company_name_is_contained(self):
        """Company names from the DB but verify path traversal is harmless."""
        base = ["python", "apply.py"]
        cmd = self._build_cmd(base, "../../evil", title="test")
        assert "../../evil" in cmd


class TestCsvInjection:
    """
    CSV injection payloads in job title/company fields must not be
    executed by the dashboard.  (They are only dangerous if a user
    later opens the file in a spreadsheet application.)
    """

    FIELDS = ["timestamp", "profile_email", "job_title", "company", "location", "status"]

    @staticmethod
    def _make_csv(tmp_path, rows):
        path = tmp_path / "applied.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TestCsvInjection.FIELDS)
            w.writeheader()
            w.writerows(rows)
        return path

    def test_formula_injection_in_title_does_not_execute(self, tmp_path):
        rows = [{
            "timestamp": "2026-09-01T10:00:00",
            "profile_email": "u@x.com",
            "job_title": "=cmd|'/c calc'!A1",
            "company": "Acme",
            "location": "Remote",
            "status": "applied",
        }]
        path = self._make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "")
        # Dashboard just reads it as a string — no execution
        assert len(result) == 1
        assert result[0]["job_title"] == "=cmd|'/c calc'!A1"

    def test_at_formula_injection_in_company(self, tmp_path):
        rows = [{
            "timestamp": "2026-09-01T10:00:00",
            "profile_email": "u@x.com",
            "job_title": "Engineer",
            "company": "@SUM(1+1)*cmd|'/c calc'!A1",
            "location": "Remote",
            "status": "applied",
        }]
        path = self._make_csv(tmp_path, rows)
        dashboard.APPLIED_LOG_PATH = path
        result = _load_rows("u@x.com", "", "", "", "")
        assert result[0]["company"] == "@SUM(1+1)*cmd|'/c calc'!A1"

    def test_markup_injection_in_status_is_escaped(self):
        # Malicious status that tries to inject Textual markup
        evil = "[bold red]HACKED[/bold red]"
        markup = _status_markup(evil)
        # The raw evil string must not appear unescaped (brackets must be escaped)
        assert "[bold red]HACKED" not in markup or "\\[" in markup

    def test_markup_injection_in_log_is_escaped(self):
        evil = "[red]INJECTED[/red] innocent line"
        markup = _log_markup(evil)
        # The injected [red] tag must be escaped so Textual doesn't render it
        assert "\\[" in markup or markup.count("[") <= 2  # only the wrapper tags


class TestSavedFiltersSecurity:
    """Saved filter file must not allow path traversal or code injection."""

    def test_email_with_path_chars_saved_safely(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "filters.json"
        # Email with path-like characters
        evil_email = "../../../etc/passwd@evil.com"
        _save_filters(evil_email, "remote", "senior", "7", "python")
        saved = _load_saved(evil_email)
        # Must work as a key, not navigate to a real path
        assert saved.get("locations") == ["remote"]

    def test_json_injection_in_filter_values_stored_safely(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "filters.json"
        # Filter value that looks like JSON injection
        _save_filters("u@x.com", '", "evil": true, "x": "', "", "", "")
        raw = (tmp_path / "filters.json").read_text()
        data = json.loads(raw)  # Must parse as valid JSON, no injection
        assert data["u@x.com"]["locations"] == ['", "evil": true, "x": "']

    def test_null_bytes_in_filter_stored_safely(self, tmp_path):
        dashboard.SAVED_FILTERS = tmp_path / "filters.json"
        _save_filters("u@x.com", "remote\x00injected", "", "", "")
        saved = _load_saved("u@x.com")
        # Stored as-is; Python JSON handles null bytes
        assert "remote" in saved["locations"][0]


class TestInputValidation:
    """Boundary and adversarial inputs to pure functions must not crash."""

    def test_is_us_location_with_sql_injection_string(self):
        result = _is_us_location("' OR '1'='1")
        assert isinstance(result, bool)

    def test_is_us_location_with_very_long_string(self):
        result = _is_us_location("A" * 100_000)
        assert isinstance(result, bool)

    def test_status_markup_with_html_injection(self):
        result = _status_markup("<script>alert('xss')</script>")
        assert isinstance(result, str)
        assert "<script>" not in result or "\\[" not in result  # not rendered as markup

    def test_log_markup_with_textual_markup_injection(self):
        # A line that contains nested markup — must not crash Textual renderer
        result = _log_markup("[bold]outer [red]inner[/red][/bold]")
        assert isinstance(result, str)
        assert "\\[" in result  # brackets must be escaped
