"""
company_apply package
=====================
ATS automation split into per-system modules:

  common.py     — shared utilities (profiles, logging, answer_for, filters, ollama)
  greenhouse.py — Greenhouse ATS form filler
  workday.py    — Workday ATS form filler
  lever.py      — Lever ATS form filler
  ashby.py      — Ashby ATS form filler
  cli.py        — apply_to_company() orchestrator + CLI entry point
"""

from .common import *          # noqa: F401, F403
from .greenhouse import *      # noqa: F401, F403
from .workday import *         # noqa: F401, F403
from .lever import *           # noqa: F401, F403
from .ashby import *           # noqa: F401, F403
from .cli import apply_to_company, main, setup_profiles  # noqa: F401
