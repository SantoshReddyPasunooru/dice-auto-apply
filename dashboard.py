#!/usr/bin/env python3
"""
Interactive dashboard for company_apply.py.

Usage:
  python dashboard.py --company Airbnb --profile user@gmail.com

Layout:
  ┌─ Job Search Criteria ──────────────────────────────────────────────────────┐
  │  Keywords: [______]  Location: [▼ All]  Experience: [▼ All]  Days: [▼ Any] │
  │                                               [▶ Start Applying]  [Clear]  │
  └────────────────────────────────────────────────────────────────────────────┘
  ┌─ Applied Jobs ─────────────────────────────────────────────────────────────┐
  └────────────────────────────────────────────────────────────────────────────┘
  ┌─ Live Logs ─────────────────────────────────────────────────────────────────┐
  └────────────────────────────────────────────────────────────────────────────┘

Keyboard:
  Tab      — cycle focus between panels
  S        — cycle sort order in table
  R        — refresh table
  O        — open log file in system viewer (macOS: open; Linux: xdg-open)
  C        — copy log file path to clipboard
  Q/Ctrl+C — quit (terminates apply process)
"""

import csv
import json
import platform
import re
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button, DataTable, Footer, Header, Input,
    Label, RichLog, Select, SelectionList, Static,
)

try:
    from textual.widgets._select import NoSelection as _NoSelection
except ImportError:
    _NoSelection = type(None)  # fallback: treat None as blank

HERE                  = Path(__file__).parent
APPLIED_LOG_PATH      = HERE / "external_applied_jobs.csv"
SAVED_FILTERS         = HERE / "saved_filters.json"
COMPANY_PY            = HERE / "company_apply.py"
COMPANY_DB            = HERE / "company_careers_db.json"
APPLICABLE_COMPANIES  = HERE / "applicable_companies.json"


def _load_company_options() -> list[tuple[str, str]]:
    try:
        names = json.loads(APPLICABLE_COMPANIES.read_text())
        return sorted([(n, n) for n in names], key=lambda x: x[0].lower())
    except Exception:
        pass
    try:
        db = json.loads(COMPANY_DB.read_text())
        return sorted(
            [(rec["name"], rec["name"]) for k, rec in db.items() if k != "_note"],
            key=lambda x: x[0].lower(),
        )
    except Exception:
        return []

_COMPANY_OPTIONS: list[tuple[str, str]] = _load_company_options()

# ── Sort modes ─────────────────────────────────────────────────────────────────

SORT_MODES = [
    ("recent_first", "Date ↓ newest first"),
    ("recent_last",  "Date ↑ oldest first"),
    ("az",           "Role A → Z"),
    ("za",           "Role Z → A"),
    ("company_az",   "Company A → Z"),
    ("status",       "Status"),
]

# ── Dropdown options ───────────────────────────────────────────────────────────

_LOC_OPTIONS: list[tuple[str, str]] = [
    ("Anywhere in US",     "__us_only__"),   # → --us-only flag
    ("Remote",             "remote"),
    ("Austin, TX",         "austin"),
    ("San Francisco, CA",  "san francisco"),
    ("New York, NY",       "new york"),
    ("Seattle, WA",        "seattle"),
    ("Chicago, IL",        "chicago"),
    ("Boston, MA",         "boston"),
    ("Los Angeles, CA",    "los angeles"),
    ("Denver, CO",         "denver"),
    ("Atlanta, GA",        "atlanta"),
]

# Sentinel value used internally when "Anywhere in US" is selected
_US_ONLY_SENTINEL = "__us_only__"

# US state abbreviations + territories for dashboard-side table filtering
_US_STATES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in",
    "ia","ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv",
    "nh","nj","nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn",
    "tx","ut","vt","va","wa","wv","wi","wy","dc","pr","vi","gu","mp","as",
}

_CA_KEYWORDS = {
    "canada", "ontario", "british columbia", "alberta", "quebec", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
    "toronto", "vancouver", "montreal", "calgary", "edmonton", "ottawa",
    "winnipeg", "mississauga", "brampton",
}
_CA_PROVINCE_ABBREVS = {"on", "bc", "ab", "qc", "mb", "sk", "ns", "nb", "nl", "pe", "nt", "nu", "yt"}

_EXP_OPTIONS: list[tuple[str, str]] = [
    ("Principal", "principal"),
    ("Staff",     "staff"),
    ("Senior",    "senior"),
    ("Lead",      "lead"),
    ("Mid",       "mid"),
    ("Junior",    "junior"),
    ("Intern",    "intern"),
]

_DAYS_OPTIONS: list[tuple[str, str]] = [
    ("Last 7 days",  "7"),
    ("Last 14 days", "14"),
    ("Last 30 days", "30"),
    ("Last 60 days", "60"),
    ("Last 90 days", "90"),
]

# ── Experience inference ───────────────────────────────────────────────────────

_EXP_RULES = [
    (re.compile(r"\b(principal|distinguished|fellow)\b", re.I), "Principal"),
    (re.compile(r"\b(staff)\b",                              re.I), "Staff"),
    (re.compile(r"\b(senior|sr\.?)\b",                       re.I), "Senior"),
    (re.compile(r"\b(lead|leads)\b",                         re.I), "Lead"),
    (re.compile(r"\b(iii|iv)\b",                             re.I), "Senior"),
    (re.compile(r"\b(ii)\b",                                 re.I), "Mid"),
    (re.compile(r"\b(mid|intermediate)\b",                   re.I), "Mid"),
    (re.compile(r"\b(junior|jr\.?|entry|associate)\b",       re.I), "Junior"),
    (re.compile(r"\b(intern|internship|co-?op)\b",           re.I), "Intern"),
]

def _infer_exp(title: str) -> str:
    for pat, label in _EXP_RULES:
        if pat.search(title):
            return label
    return "Mid"


_INDUSTRY_OPTIONS: list[tuple[str, str]] = [
    ("Engineering",   "engineering"),
    ("IT / Sysadmin", "it"),
    ("Management",    "management"),
    ("Product",       "product"),
    ("Data/Analytics","data"),
    ("Sales",         "sales"),
    ("Marketing",     "marketing"),
    ("Design",        "design"),
    ("Operations",    "operations"),
    ("Finance",       "finance"),
    ("Support",       "support"),
    ("Legal / HR",    "legal"),
]

_INDUSTRY_RULES = [
    # IT first — "IT Manager" must be IT, not management
    (re.compile(r"\b(it\b|information technology|sysadmin|sys.admin|system.admin|network.admin|helpdesk|help.desk|desktop.support|it.analyst|it.manager|it.support|network.engineer|systems.administrator|infrastructure.admin)\b", re.I), "it"),
    # C-suite / VP / Director — unambiguously executive; run BEFORE domain-specific rules
    # so "Chief Revenue Officer" → management (not sales) and "VP of Engineering" → management
    (re.compile(r"\b(cto\b|ceo\b|cfo\b|coo\b|vp\b|vice.president|chief|president|director|head of)\b", re.I), "management"),
    # Domain-specific compound phrases — beat the generic "manager" catch-all at the bottom
    (re.compile(r"\b(product manager|product owner|\bpm\b)\b", re.I), "product"),
    (re.compile(r"\b(marketing|content|communications|\bpr\b|social media|campaigns|seo|copywriter|brand)\b", re.I), "marketing"),
    (re.compile(r"\b(operations|ops\b|coordinator|program manager|project manager|logistics)\b", re.I), "operations"),
    (re.compile(r"\b(finance|accounting|accountant|controller|treasury|payroll|tax)\b", re.I), "finance"),
    (re.compile(r"\b(support|customer success|customer service|help desk)\b", re.I), "support"),
    (re.compile(r"\b(legal|counsel|\bhr\b|recruiter|recruiting|talent|people ops)\b", re.I), "legal"),
    # Sales — before engineering so "Sales Manager" → sales (generic manager catch-all is last)
    (re.compile(r"\b(sales|account executive|business development|bdr\b|sdr\b|revenue|partnerships|growth)\b", re.I), "sales"),
    # Design — before engineering so "UI Developer" / "UX Engineer" → design
    (re.compile(r"\b(designer|design\b|ux\b|ui\b|visual|creative)\b", re.I), "design"),
    # Engineering and data
    (re.compile(r"\b(engineer|developer|dev\b|backend|frontend|fullstack|full.stack|infrastructure|platform\b|sre\b|devops|software|programmer|architect)\b", re.I), "engineering"),
    (re.compile(r"\b(data|analyst|analytics|scientist|researcher|research)\b", re.I), "data"),
    # Generic "manager" catch-all — anything with "manager" that didn't match a domain above
    (re.compile(r"\bmanager\b", re.I), "management"),
]

def _infer_industry(title: str) -> str:
    for pat, label in _INDUSTRY_RULES:
        if pat.search(title):
            return label
    return "other"


def _is_us_location(loc: str) -> bool:
    """Return True if the location string looks like a US location."""
    if not loc:
        return False
    lo = loc.lower()
    # Exclude Canadian locations first — "Remote, Canada" must not pass as US
    if any(k in lo for k in _CA_KEYWORDS):
        return False
    parts = [p.strip().lower() for p in re.split(r"[,/|]", lo)]
    if parts[-1] in _CA_PROVINCE_ABBREVS:
        return False
    if lo.strip() == "us":
        return True
    if any(k in lo for k in ("united states", "usa", ", us", "remote")):
        return True
    return any(p in _US_STATES for p in parts)


# ── CSV helpers ────────────────────────────────────────────────────────────────

def _load_rows(email: str, f_title: str, f_loc: str,
               f_exp: str, f_days: str, f_industry: str = "") -> list[dict]:
    if not APPLIED_LOG_PATH.exists():
        return []
    try:
        with open(APPLIED_LOG_PATH, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if email:
            rows = [r for r in rows if r.get("profile_email", "").lower() == email.lower()]
        # Apply same filters used for job search
        if f_title:
            tl = f_title.lower()
            rows = [r for r in rows if tl in r.get("job_title", "").lower()]
        if f_loc == _US_ONLY_SENTINEL:
            rows = [r for r in rows if _is_us_location(r.get("location", ""))]
        elif f_loc:
            ll = f_loc.lower()
            rows = [r for r in rows if ll in r.get("location", "").lower()]
        if f_exp:
            el = f_exp.lower()
            rows = [r for r in rows if
                    el in _infer_exp(r.get("job_title", "")).lower()
                    or el in r.get("job_title", "").lower()]
        if f_days:
            try:
                n = int(f_days)
                cutoff = datetime.now() - timedelta(days=n)
                def _in_range(r, _cutoff=cutoff):
                    ts = r.get("timestamp", "").strip()
                    if not ts:
                        return True  # no timestamp — don't filter out
                    return _parse_ts(ts) >= _cutoff
                rows = [r for r in rows if _in_range(r)]
            except ValueError:
                pass
        if f_industry:
            rows = [r for r in rows if _infer_industry(r.get("job_title", "")) == f_industry.lower()]
        return rows
    except Exception:
        return []


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.min


def _sort_rows(rows: list[dict], mode: str) -> list[dict]:
    def _s(v): return str(v).lower() if v is not None else ""
    def _ts(r):    return _parse_ts(r.get("timestamp") or "")
    def _title(r): return _s(r.get("job_title"))
    def _co(r):    return _s(r.get("company"))
    def _st(r):    return _s(r.get("status"))
    if mode == "recent_first":  return sorted(rows, key=_ts, reverse=True)
    if mode == "recent_last":   return sorted(rows, key=_ts)
    if mode == "az":            return sorted(rows, key=_title)
    if mode == "za":            return sorted(rows, key=_title, reverse=True)
    if mode == "company_az":    return sorted(rows, key=_co)
    if mode == "status":        return sorted(rows, key=_st)
    return rows


# ── Saved-filter persistence ───────────────────────────────────────────────────

def _load_saved(email: str) -> dict:
    try:
        return json.loads(SAVED_FILTERS.read_text()).get(email, {})
    except Exception:
        return {}


def _save_filters(email: str, loc: str, exp: str, days: str, title: str):
    try:
        try:
            data = json.loads(SAVED_FILTERS.read_text())
        except Exception:
            data = {}
        cur = data.get(email, {})
        cur["locations"]   = [loc] if loc else []
        cur["experience"]  = [exp] if exp else []
        cur["posted_days"] = int(days) if days else None
        cur["keywords"]    = title
        cur.setdefault("work_type", [])
        cur.setdefault("us_only", True)
        data[email] = cur
        SAVED_FILTERS.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ── Markup helpers ─────────────────────────────────────────────────────────────

def _status_markup(s: str) -> str:
    sl = s.lower()
    safe = s[:18].replace("[", "\\[")  # escape Textual markup in raw content
    if "skipped" in sl:   return f"[dim]{safe}[/dim]"
    if "applied" in sl:   return f"[bold green]{safe}[/bold green]"
    if "submitted" in sl: return f"[green]{safe}[/green]"
    if "error" in sl:     return f"[bold red]{safe}[/bold red]"
    return safe


_LOG_RULES = [
    (re.compile(r"\[profile\]|\[saved\]"),      "dim green"),
    (re.compile(r"\[ollama\]"),                 "dim cyan"),
    (re.compile(r"→ applied|→ submitted|✓"),    "bold green"),
    (re.compile(r"→ error|error:", re.I),       "bold red"),
    (re.compile(r"\[Verification\]"),           "bold yellow"),
    (re.compile(r"\[Location\]|\[Gmail\]|\[LinkedIn\]"), "yellow"),
    (re.compile(r"─{5,}"),                      "dim"),
    (re.compile(r"Resume:|Fetching|Found \d"),  "dim magenta"),
]

def _log_markup(line: str) -> str:
    if not line:
        return ""
    s = line.rstrip()
    for pat, style in _LOG_RULES:
        if pat.search(s):
            safe = s.replace("[", "\\[")
            return f"[{style}]{safe}[/{style}]"
    return s.replace("[", "\\[")


# ── App ────────────────────────────────────────────────────────────────────────

class DashboardApp(App):

    CSS = """
    Screen { layout: vertical; }

    /* ── Filter panel — two filter rows + one action row ── */
    #filter-panel {
        height: 20;
        background: $boost;
        border: double $accent;
        border-title-color: $accent;
        padding: 1 2;
        layout: vertical;
    }

    /* Row 1: Keywords · Location · Experience · Days */
    #filter-row1 {
        height: 3;
        layout: horizontal;
        align: left middle;
        margin-bottom: 1;
    }

    /* Row 2: Company checkboxes (left) + Industry & status (right) */
    #filter-row2 {
        height: 9;
        layout: horizontal;
        margin-bottom: 1;
    }

    #company-panel {
        width: 30;
        height: 100%;
        layout: vertical;
        margin: 0 4 0 0;
    }
    #company-panel .f-label { margin-bottom: 0; }
    #company-header {
        height: 1;
        layout: horizontal;
        align: left middle;
    }
    #btn-select-all {
        width: auto;
        min-width: 10;
        height: 1;
        background: transparent;
        border: none;
        color: $accent;
        padding: 0 1;
        margin: 0 0 0 1;
    }
    #f-company-list {
        height: 1fr;
        border: solid $accent-darken-2;
    }

    #industry-status-panel {
        width: 1fr;
        height: 100%;
        layout: vertical;
    }
    #industry-row {
        height: 3;
        layout: horizontal;
        align: left middle;
    }

    /* Row 3: action buttons aligned right */
    #filter-row3 {
        height: 3;
        layout: horizontal;
        align: right middle;
    }

    .f-label {
        width: auto;
        padding: 0 1 0 0;
        color: $text-muted;
    }

    /* Row-1 widgets */
    #f-title    { width: 28; margin: 0 4 0 0; }
    #f-loc      { width: 22; margin: 0 4 0 0; }
    #f-exp      { width: 18; margin: 0 4 0 0; }
    #f-days     { width: 18; margin: 0 0 0 0; }

    /* Row-2 right-side widgets */
    #f-industry { width: 24; margin: 0 4 0 0; }
    #proc-status {
        width: 1fr;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    /* Row-3 buttons */
    #btn-start {
        margin: 0 1;
        min-width: 20;
        background: $success;
        color: white;
    }
    #btn-clear {
        margin: 0 1;
        min-width: 10;
    }

    /* ── Applied jobs panel ── */
    #top {
        height: 1fr;
        border: solid cyan;
        padding: 0;
        layout: vertical;
    }
    #sort-bar  { height: 1; background: $surface; padding: 0 1; color: $text-muted; }
    #stats-bar { height: 1; background: $surface; padding: 0 1; }
    DataTable  { height: 1fr; }
    DataTable > .datatable--header { background: $surface; color: cyan; }

    /* ── Logs panel ── */
    #bottom {
        height: 1fr;
        border: solid yellow;
        padding: 0;
        layout: vertical;
    }
    #log-path { height: 1; background: $surface; padding: 0 1; color: $text-muted; }
    RichLog   { height: 1fr; }
    """

    BINDINGS = [
        Binding("tab",    "focus_next",    "Switch panel"),
        Binding("s",      "cycle_sort",    "Cycle sort"),
        Binding("r",      "refresh_dash",  "Refresh"),
        Binding("o",      "open_log",      "Open log file"),
        Binding("c",      "copy_log_path", "Copy log path"),
        Binding("q",      "quit",          "Quit"),
        Binding("ctrl+c", "quit",          "Quit", show=False),
    ]

    sort_mode_idx: reactive[int] = reactive(0)

    def __init__(self, email: str, base_cmd: list[str],
                 init_title: str = "", init_loc: str = "",
                 init_exp: str = "", init_days: str = "",
                 init_company: str = "", **kw):
        super().__init__(**kw)
        self.email    = email
        self.base_cmd = base_cmd          # [python, company_apply.py, --profile Y, ...]

        self._f_title    = init_title
        self._f_loc      = init_loc
        self._f_exp      = init_exp
        self._f_days     = init_days
        self._f_industry = ""
        self._f_company  = init_company

        self._active_proc: subprocess.Popen | None = None  # currently running subprocess
        self._proc_done                     = threading.Event()
        self._proc_done.set()              # start in "done" state so running check works
        self._log_queue: deque[str]         = deque(maxlen=2000)
        self._seen_log                      = 0
        self._tick_n                        = 0
        self._proc_finished_notified        = False
        self._log_file: Path | None         = None

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="filter-panel"):
            # Row 1 — primary search filters
            with Horizontal(id="filter-row1"):
                yield Label("Keywords",    classes="f-label")
                yield Input(placeholder="python, ai, ml…", id="f-title",
                            value=self._f_title)
                yield Label("Location",    classes="f-label")
                yield Select(_LOC_OPTIONS,   allow_blank=True, prompt="All locations", id="f-loc")
                yield Label("Experience",  classes="f-label")
                yield Select(_EXP_OPTIONS,   allow_blank=True, prompt="All levels",    id="f-exp")
                yield Label("Days posted", classes="f-label")
                yield Select(_DAYS_OPTIONS,  allow_blank=True, prompt="Any date",      id="f-days")
            # Row 2 — company multi-select (left) + industry/status (right)
            with Horizontal(id="filter-row2"):
                with Vertical(id="company-panel"):
                    with Horizontal(id="company-header"):
                        yield Label("Company", classes="f-label")
                        yield Button("Select All", id="btn-select-all")
                    yield SelectionList[str](
                        *[(name, name) for _, name in _COMPANY_OPTIONS],
                        id="f-company-list",
                    )
                with Vertical(id="industry-status-panel"):
                    with Horizontal(id="industry-row"):
                        yield Label("Industry", classes="f-label")
                        yield Select(_INDUSTRY_OPTIONS, allow_blank=True,
                                     prompt="All industries", id="f-industry")
                    yield Static("Ready — configure filters then click Start Applying",
                                 id="proc-status", markup=True)
            # Row 3 — action buttons
            with Horizontal(id="filter-row3"):
                yield Button("▶ Start Applying", id="btn-start", variant="success")
                yield Button("Clear",            id="btn-clear",  variant="default")

        with Vertical(id="top"):
            yield Static("", id="sort-bar")
            yield DataTable(id="jobs-table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="stats-bar")

        with Vertical(id="bottom"):
            yield Static("Logs  (press O to open in viewer · C to copy path)",
                         id="log-path", markup=False)
            yield RichLog(id="log-panel", highlight=False, markup=True,
                          wrap=True, auto_scroll=True)

        yield Footer()

    def on_mount(self) -> None:
        t: DataTable = self.query_one("#jobs-table", DataTable)
        t.add_columns("Date", "Company", "Role", "Location", "Exp", "Salary", "Status")
        self._restore_selects()
        self._load_table()
        self._update_sort_bar()
        self.set_interval(0.5, self._tick)

    def _restore_selects(self):
        """Set Select dropdowns from saved init values."""
        if self._f_loc:
            for _, val in _LOC_OPTIONS:
                if val == self._f_loc or val in self._f_loc or self._f_loc in val:
                    self._safe_set_select("#f-loc", val)
                    break
        if self._f_exp:
            for _, val in _EXP_OPTIONS:
                if val == self._f_exp or val in self._f_exp:
                    self._safe_set_select("#f-exp", val)
                    break
        if self._f_days:
            for _, val in _DAYS_OPTIONS:
                if val == self._f_days:
                    self._safe_set_select("#f-days", val)
                    break
        if self._f_industry:
            self._safe_set_select("#f-industry", self._f_industry)
        if self._f_company:
            try:
                sl: SelectionList = self.query_one("#f-company-list", SelectionList)
                sl.select(self._f_company)
            except Exception:
                pass

    def _safe_set_select(self, widget_id: str, value: str):
        try:
            self.query_one(widget_id, Select).value = value
        except Exception:
            pass

    # ── Select value reader (handles Select.NULL / NoSelection sentinel) ───────

    def _read_select(self, widget_id: str) -> str:
        try:
            v = self.query_one(widget_id, Select).value
            # In Textual 8.x the "nothing selected" value is a NoSelection instance;
            # Select.BLANK (= False) is used only to SET back to blank.
            if isinstance(v, _NoSelection) or v is Select.BLANK or not v:
                return ""
            s = str(v)
            # Guard against any serialised sentinel leaking through
            if s in ("False", "None", "Select.NULL", "Select.BLANK", ""):
                return ""
            return s
        except Exception:
            return ""

    # ── Table loading ──────────────────────────────────────────────────────────

    def _load_table(self):
        all_rows = _load_rows(self.email, "", "", "", "")
        total_all = len(all_rows)
        rows = _load_rows(self.email, self._f_title, self._f_loc,
                          self._f_exp, self._f_days, self._f_industry)
        rows = _sort_rows(rows, SORT_MODES[self.sort_mode_idx][0])

        t: DataTable = self.query_one("#jobs-table", DataTable)
        t.clear()
        for row in rows:
            t.add_row(
                row.get("timestamp", "")[:10],
                row.get("company",   "")[:14],
                row.get("job_title", "")[:38],
                row.get("location",  "")[:26],
                _infer_exp(row.get("job_title", "")),
                "—",
                _status_markup(row.get("status", "")),
                height=1,
            )

        total   = len(rows)
        applied = sum(1 for r in rows if any(
            w in r.get("status", "").lower() for w in ("applied", "submitted")))
        errors  = sum(1 for r in rows if "error" in r.get("status", "").lower())
        self.query_one("#stats-bar", Static).update(
            f"[dim]Total in DB: {total_all}  |  Matching filters: {total}[/dim]   "
            f"[bold green]{applied} applied[/bold green]  "
            f"[bold red]{errors} errors[/bold red]"
        )

    def _update_sort_bar(self):
        label = SORT_MODES[self.sort_mode_idx][1]
        self.query_one("#sort-bar", Static).update(
            f"[cyan]Sort:[/cyan] {label}   "
            f"[dim]Tab=switch  S=cycle  R=refresh  O=open log  C=copy path  Q=quit[/dim]"
        )

    # ── Log flushing ───────────────────────────────────────────────────────────

    def _flush_logs(self):
        lines = list(self._log_queue)
        new   = lines[self._seen_log:]
        if new:
            log: RichLog = self.query_one("#log-panel", RichLog)
            for line in new:
                log.write(_log_markup(line))
            self._seen_log = len(lines)

    # ── Tick ───────────────────────────────────────────────────────────────────

    def _tick(self):
        self._flush_logs()
        self._tick_n += 1
        if self._tick_n % 4 == 0:
            self._load_table()
        if self._active_proc and self._proc_done.is_set() and not self._proc_finished_notified:
            self._proc_finished_notified = True
            log: RichLog = self.query_one("#log-panel", RichLog)
            log.write("[bold dim]── process finished ──[/bold dim]")
            self.query_one("#proc-status", Static).update(
                "[bold green]✓ Done[/bold green]  — press Q to quit or adjust filters and start again"
            )
            self.query_one("#btn-start", Button).label = "▶ Start Applying"
            self._load_table()

    # ── Subprocess launch ──────────────────────────────────────────────────────

    def _start_applying(self):
        if not self._proc_done.is_set():
            # Already running — ignore double-click
            return

        # Read current filter values
        self._f_title    = self.query_one("#f-title", Input).value.strip()
        self._f_loc      = self._read_select("#f-loc")
        self._f_exp      = self._read_select("#f-exp")
        self._f_days     = self._read_select("#f-days")
        self._f_industry = self._read_select("#f-industry")

        # Read selected companies from SelectionList
        try:
            selected_cos = list(self.query_one("#f-company-list", SelectionList).selected)
        except Exception:
            selected_cos = []

        # Persist filters
        _save_filters(self.email, self._f_loc, self._f_exp, self._f_days, self._f_title)

        # Determine which companies to run (none checked = all)
        companies = selected_cos if selected_cos else [n for _, n in _COMPANY_OPTIONS]

        # Build filter flags (shared across all company runs)
        filter_flags: list[str] = []
        if self._f_title:
            filter_flags += ["--keywords", self._f_title]
        else:
            filter_flags += ["--all-roles"]
        if self._f_loc == _US_ONLY_SENTINEL:
            filter_flags += ["--us-only"]
        elif self._f_loc:
            filter_flags += ["--location", self._f_loc]
        if self._f_exp:   filter_flags += ["--experience",  self._f_exp]
        if self._f_days:  filter_flags += ["--posted-days", self._f_days]

        # Build one cmd per company
        cmds: list[list[str]] = []
        for co in companies:
            c = list(self.base_cmd) + ["--company", co] + filter_flags
            if c[0] == sys.executable and len(c) > 1 and c[1] != "-u":
                c = [c[0], "-u"] + c[1:]
            cmds.append(c)

        # Reset state for new run
        self._log_file                = HERE / f"apply_logs_{datetime.now():%Y%m%d_%H%M%S}.log"
        self._log_queue               = deque(maxlen=2000)
        self._seen_log                = 0
        self._proc_done               = threading.Event()
        self._proc_finished_notified  = False
        self._active_proc             = None

        # Clear log panel
        self.query_one("#log-panel", RichLog).clear()

        # Update log path label
        self.query_one("#log-path", Static).update(
            f"Logs → {self._log_file}  (O=open · C=copy path)"
        )

        import os as _os
        _env = _os.environ.copy()
        _env["PYTHONUNBUFFERED"] = "1"

        def _reader_multi(cmds: list, lf: Path, q: deque,
                          done: threading.Event, app_ref):
            with open(lf, "w", encoding="utf-8") as f:
                for cmd in cmds:
                    p = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        text=True,
                        bufsize=1,
                        env=_env,
                    )
                    app_ref._active_proc = p
                    for line in p.stdout:
                        q.append(line)
                        f.write(line)
                        f.flush()
                    p.wait()
            done.set()

        threading.Thread(
            target=_reader_multi,
            args=(cmds, self._log_file, self._log_queue, self._proc_done, self),
            daemon=True,
        ).start()

        # Build human-readable filter summary for status bar
        loc_label = "Anywhere in US" if self._f_loc == _US_ONLY_SENTINEL else self._f_loc
        parts = []
        if selected_cos:    parts.append(f"companies={', '.join(selected_cos)}")
        else:               parts.append(f"all {len(companies)} companies")
        if self._f_title:   parts.append(f"keywords={self._f_title!r}")
        if loc_label:       parts.append(f"location={loc_label}")
        if self._f_exp:     parts.append(f"experience={self._f_exp}")
        if self._f_days:    parts.append(f"days={self._f_days}")
        summary = ", ".join(parts) if parts else "all roles"

        self.query_one("#proc-status", Static).update(
            f"[bold yellow]● Running[/bold yellow]  {summary}"
        )
        self.query_one("#btn-start", Button).label = "● Running…"
        self._load_table()

    # ── Button handler ─────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-start":
            self._start_applying()
        elif event.button.id == "btn-clear":
            self._clear_filters()
        elif event.button.id == "btn-select-all":
            self._toggle_select_all()

    def _toggle_select_all(self):
        sl: SelectionList = self.query_one("#f-company-list", SelectionList)
        btn: Button = self.query_one("#btn-select-all", Button)
        if len(sl.selected) < len(_COMPANY_OPTIONS):
            sl.select_all()
            btn.label = "Deselect All"
        else:
            sl.deselect_all()
            btn.label = "Select All"

    def _clear_filters(self):
        self.query_one("#f-title", Input).value = ""
        for wid in ("#f-loc", "#f-exp", "#f-days", "#f-industry"):
            try:
                self.query_one(wid, Select).value = Select.BLANK
            except Exception:
                pass
        try:
            sl: SelectionList = self.query_one("#f-company-list", SelectionList)
            sl.deselect_all()
            self.query_one("#btn-select-all", Button).label = "Select All"
        except Exception:
            pass
        self._f_title = self._f_loc = self._f_exp = self._f_days = ""
        self._f_industry = ""
        self._f_company  = ""
        self._load_table()

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self._start_applying()

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_cycle_sort(self) -> None:
        self.sort_mode_idx = (self.sort_mode_idx + 1) % len(SORT_MODES)
        self._load_table()
        self._update_sort_bar()

    def action_refresh_dash(self) -> None:
        self._load_table()

    def action_open_log(self) -> None:
        if not self._log_file or not self._log_file.exists():
            self.query_one("#proc-status", Static).update(
                "[dim]No log file yet — start applying first[/dim]"
            )
            return
        opener = "open" if platform.system() == "Darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, str(self._log_file)])
        except Exception as e:
            self.query_one("#proc-status", Static).update(f"[red]Cannot open: {e}[/red]")

    def action_copy_log_path(self) -> None:
        if not self._log_file:
            self.query_one("#proc-status", Static).update(
                "[dim]No log file yet — start applying first[/dim]"
            )
            return
        path_str = str(self._log_file)
        try:
            if platform.system() == "Darwin":
                subprocess.run(["pbcopy"], input=path_str, text=True, check=True)
            else:
                subprocess.run(["xclip", "-selection", "clipboard"],
                               input=path_str, text=True, check=True)
            self.query_one("#proc-status", Static).update(
                f"[green]✓ Path copied to clipboard[/green]  {path_str}"
            )
        except Exception as e:
            self.query_one("#proc-status", Static).update(
                f"[yellow]Path: {path_str}[/yellow]  (could not auto-copy: {e})"
            )

    def action_quit(self) -> None:
        if self._active_proc:
            try:
                self._active_proc.terminate()
            except Exception:
                pass
        self.exit()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Dashboard for company_apply.py — configure filters, then Start Applying."
    )
    ap.add_argument("--company",      help="Company name")
    ap.add_argument("--profile",      help="Gmail address of profile to use")
    ap.add_argument("--keywords",     help="Pre-fill keywords filter")
    ap.add_argument("--location",     help="Pre-fill location filter")
    ap.add_argument("--experience",   help="Pre-fill experience filter")
    ap.add_argument("--posted-days",  type=int, help="Pre-fill days filter")
    ap.add_argument("--dry-run",      action="store_true")
    ap.add_argument("--us-only",      action="store_true")
    # Absorb any extra flags we don't know about (pass-through)
    args, extra = ap.parse_known_args()

    email   = args.profile  or ""
    company = args.company  or ""

    # Build base command (profile only; company + filters are added at launch time)
    base_cmd = [sys.executable, str(COMPANY_PY)]
    if email:            base_cmd += ["--profile",  email]
    if args.dry_run:     base_cmd += ["--dry-run"]
    if args.us_only:     base_cmd += ["--us-only"]
    base_cmd.extend(extra)  # pass through any other flags

    # Determine initial filter values (CLI takes precedence, else saved)
    saved        = _load_saved(email)
    saved_locs   = saved.get("locations",   [])
    saved_exps   = saved.get("experience",  [])
    saved_days   = saved.get("posted_days")
    saved_kw     = saved.get("keywords",    "")

    init_title = args.keywords  or saved_kw or ""
    init_loc   = args.location  or (saved_locs[0] if saved_locs else "")
    init_exp   = args.experience or (saved_exps[0] if saved_exps else "")
    init_days  = (str(args.posted_days) if args.posted_days
                  else str(saved_days) if saved_days else "")

    app = DashboardApp(
        email=email,
        base_cmd=base_cmd,
        init_title=init_title,
        init_loc=init_loc,
        init_exp=init_exp,
        init_days=init_days,
        init_company=company,
    )
    app.run()


if __name__ == "__main__":
    main()
