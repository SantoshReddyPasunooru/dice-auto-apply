"""
Shared fixtures and import helpers.

Heavy optional deps (playwright, ollama, pdfplumber, google-api) are stubbed
before the modules under test are imported, so tests run without a browser,
local LLM, or live Gmail account.
"""
import sys
import types
import pytest
from unittest.mock import MagicMock


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ── Stub heavy dependencies BEFORE any project module is imported ─────────────

for dep in [
    "playwright", "playwright.async_api",
    "ollama",
    "pdfplumber",
    "docx",
    "google", "google.auth", "google.auth.transport", "google.auth.transport.requests",
    "google.oauth2", "google.oauth2.credentials",
    "google_auth_oauthlib", "google_auth_oauthlib.flow",
    "googleapiclient", "googleapiclient.discovery",
    "rich", "rich.live", "rich.table", "rich.console", "rich.panel",
    "rich.progress", "rich.text", "rich.columns",
    "textual", "textual.app", "textual.widgets", "textual.widgets._select",
    "textual.reactive", "textual.containers", "textual.binding",
    "dotenv",
]:
    if dep not in sys.modules:
        _stub(dep)

# dotenv.load_dotenv must be callable
sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None

# google.oauth2.credentials.Credentials
sys.modules["google.oauth2.credentials"].Credentials = MagicMock()
# google.auth.transport.requests.Request
sys.modules["google.auth.transport.requests"].Request = MagicMock()
# googleapiclient.discovery.build
sys.modules["googleapiclient.discovery"].build = MagicMock()
# google_auth_oauthlib.flow.InstalledAppFlow
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = MagicMock()

# playwright.async_api
for attr in ("async_playwright", "Page", "Frame", "BrowserContext"):
    setattr(sys.modules["playwright.async_api"], attr, MagicMock())

# ── Textual stubs (dashboard.py does `from textual.xxx import Yyy`) ───────────
# Use a real class (not MagicMock) so DashboardApp subclasses it as a real
# Python class, making patch.object and direct instantiation work in E2E tests.
class _App:
    CSS = ""
    BINDINGS = []
    def __init__(self, **kw): pass
    def run(self, *a, **kw): pass
    @classmethod
    def __init_subclass__(cls, **kw): pass

sys.modules["textual.app"].App            = _App
sys.modules["textual.app"].ComposeResult  = MagicMock()

for w in ("Button", "DataTable", "Footer", "Header", "Input",
          "Label", "RichLog", "Select", "SelectionList", "Static"):
    setattr(sys.modules["textual.widgets"], w, MagicMock())

sys.modules["textual.widgets._select"].NoSelection = type("NoSelection", (), {})
sys.modules["textual.reactive"].reactive            = MagicMock(return_value=0)
sys.modules["textual.binding"].Binding              = MagicMock()

for c in ("Horizontal", "Vertical"):
    setattr(sys.modules["textual.containers"], c, MagicMock())


def pytest_configure(config):
    for mark in ("smoke", "unit", "integration", "e2e", "network"):
        config.addinivalue_line("markers", f"{mark}: {mark} test layer")


@pytest.fixture
def sample_profile():
    return {
        "name": "Santosh Reddy Pasunooru",
        "current_title": "Gen AI Engineer",
        "current_company": "JP Morgan",
        "work_auth": "OPT",
        "needs_sponsorship": True,
        "years_experience": 5,
        "location": "Austin, TX",
        "skills": "python, ai, ml, llm",
        "preferred_work": "Remote",
        "open_to_relocation": True,
        "available_to_start": "2 weeks",
        "expected_salary": "open to discussion",
        "phone": "7047268793",
        "linkedin_url": "https://www.linkedin.com/in/santoshreddypas/",
        "github_url": "https://github.com/santosh",
        "website_url": "",
        "school": "University of North Carolina - Charlotte",
        "degree": "Master's Degree",
        "graduation_year": "2024",
    }


@pytest.fixture
def sample_jobs():
    """A small list of job dicts covering various filter edge-cases."""
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc)

    def ts(days_ago: int) -> str:
        from datetime import timedelta
        return (now - timedelta(days=days_ago)).isoformat()

    return [
        {"title": "Senior Software Engineer", "location": "Austin, TX",           "url": "https://co.io/1", "posted_at": ts(1)},
        {"title": "Junior Data Analyst",      "location": "Remote",               "url": "https://co.io/2", "posted_at": ts(3)},
        {"title": "Staff ML Engineer",        "location": "San Francisco, CA",    "url": "https://co.io/3", "posted_at": ts(10)},
        {"title": "Product Manager",          "location": "Toronto, ON",          "url": "https://co.io/4", "posted_at": ts(2)},
        {"title": "DevOps Engineer",          "location": "Remote, Canada",       "url": "https://co.io/5", "posted_at": ts(5)},
        {"title": "Data Scientist",           "location": "New York, NY",         "url": "https://co.io/6", "posted_at": ts(0)},
        {"title": "Intern - Software",        "location": "Seattle, WA",          "url": "https://co.io/7", "posted_at": ts(1)},
        {"title": "Principal Engineer",       "location": "United States",        "url": "https://co.io/8", "posted_at": ts(30)},
        {"title": "VP of Engineering",        "location": "Remote in US",         "url": "https://co.io/9", "posted_at": ts(0)},
    ]
