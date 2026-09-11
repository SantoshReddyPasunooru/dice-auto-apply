#!/usr/bin/env python3
"""
apply_logger.py — Centralized, structured debug logger for the job automation system.

Usage:
    from apply_logger import log

    log.fn("filter_jobs", keywords=keywords, experience=experience)
    log.var("url_before", url_before)
    log.api("GET", url, status=200, snippet=response_text[:80])
    log.browser("click", "[data-automation-id='submitButton']", result="ok")
    log.db("read", "applied_urls", count=len(urls))
    log.step("Workday: My Information")
    log.ok("Applied successfully")
    log.warn("URL unchanged after click — retrying")
    log.err("Browser crashed", exc=e)
    log.state(page_url=page.url, heading="Review Application", buttons=["Submit"])
"""

import functools
import inspect
import logging
import os
import sys
import threading
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


# ── Constants ──────────────────────────────────────────────────────────────────

_LOG_DIR  = Path(__file__).parent / "debug_logs"
_LOG_DIR.mkdir(exist_ok=True)

_SESSION_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE   = _LOG_DIR / f"session_{_SESSION_TS}.log"

_CONTEXT = threading.local()   # per-thread context: company, profile, job, step


# ── ANSI colours (console only) ───────────────────────────────────────────────

_C = {
    "reset":  "\033[0m",
    "grey":   "\033[90m",
    "cyan":   "\033[96m",
    "green":  "\033[92m",
    "yellow": "\033[93m",
    "red":    "\033[91m",
    "blue":   "\033[94m",
    "magenta":"\033[95m",
    "bold":   "\033[1m",
    "dim":    "\033[2m",
}

def _c(colour: str, text: str) -> str:
    if not sys.stderr.isatty() and not sys.stdout.isatty():
        return text
    return f"{_C.get(colour,'')}{text}{_C['reset']}"


# ── Raw Python logger setup ───────────────────────────────────────────────────

_raw = logging.getLogger("apply_logger")
_raw.setLevel(logging.DEBUG)
_raw.propagate = False

# File handler — full detail, no colour
_fh = RotatingFileHandler(
    _LOG_FILE, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(
    fmt="%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_raw.addHandler(_fh)

# Console handler — concise, coloured
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.DEBUG)
_ch.setFormatter(logging.Formatter("%(message)s"))
_raw.addHandler(_ch)


# ── Context helpers ───────────────────────────────────────────────────────────

def _ctx() -> str:
    parts = []
    if getattr(_CONTEXT, "company", None):
        parts.append(_CONTEXT.company)
    if getattr(_CONTEXT, "profile", None):
        parts.append(_CONTEXT.profile)
    if getattr(_CONTEXT, "job", None):
        parts.append(_CONTEXT.job[:40])
    return " | ".join(parts) if parts else ""


def _caller(depth: int = 2) -> str:
    """Return 'file:line  function' of the caller at `depth` stack frames up."""
    try:
        frame = sys._getframe(depth)
        fname = Path(frame.f_code.co_filename).name
        lineno = frame.f_lineno
        func   = frame.f_code.co_name
        return f"{fname}:{lineno}  {func}()"
    except Exception:
        return ""


def _fmt_val(v: Any, max_len: int = 120) -> str:
    """Safely format any value for logging."""
    try:
        if v is None:
            return "None"
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            s = v.replace("\n", "↵").replace("\r", "")
            return f'"{s[:max_len]}{"…" if len(s) > max_len else ""}"'
        if isinstance(v, (list, tuple)):
            inner = ", ".join(_fmt_val(x, 40) for x in list(v)[:6])
            suffix = ", …" if len(v) > 6 else ""
            bracket = ("(", ")") if isinstance(v, tuple) else ("[", "]")
            return f"{bracket[0]}{inner}{suffix}{bracket[1]}  (len={len(v)})"
        if isinstance(v, dict):
            keys = list(v.keys())[:4]
            inner = ", ".join(f"{k}: {_fmt_val(v[k], 30)}" for k in keys)
            suffix = ", …" if len(v) > 4 else ""
            return f"{{{inner}{suffix}}}  (len={len(v)})"
        if isinstance(v, set):
            return f"set(len={len(v)})"
        if isinstance(v, Path):
            return str(v)
        return repr(v)[:max_len]
    except Exception:
        return "<unprintable>"


# ── Core logger class ─────────────────────────────────────────────────────────

class ApplyLogger:

    # ── Context setters ───────────────────────────────────────────────────────

    def set_context(self, *, company: str = "", profile: str = "", job: str = ""):
        if company:  _CONTEXT.company  = company
        if profile:  _CONTEXT.profile  = profile
        if job:      _CONTEXT.job      = job

    def clear_context(self):
        _CONTEXT.company = _CONTEXT.profile = _CONTEXT.job = ""

    # ── Primitive write ───────────────────────────────────────────────────────

    def _write(self, level: str, icon: str, colour: str,
               tag: str, msg: str, *, depth: int = 3):
        ctx    = _ctx()
        caller = _caller(depth)
        ctx_str = f"  [{ctx}]" if ctx else ""

        # File: full detail
        file_line = (
            f"[{tag}]{ctx_str}  {msg}"
            + (f"  ← {caller}" if caller else "")
        )
        getattr(_raw, level)(file_line)

        # Console: compact + colour
        ctx_part   = _c("dim",    f" [{ctx}]") if ctx else ""
        icon_part  = _c(colour,   f"{icon} {tag}")
        msg_part   = _c(colour,   msg) if level in ("error", "warning") else msg
        print(f"{icon_part}{ctx_part}  {msg_part}", flush=True)

    # ── High-level logging methods ────────────────────────────────────────────

    def step(self, name: str):
        """Log a major workflow step (e.g. 'Workday: My Information')."""
        banner = f"{'─'*6} {name} {'─'*6}"
        _raw.info(f"\n[STEP]  {banner}")
        print(_c("bold", f"\n  ══ {name}"), flush=True)

    def fn(self, name: str, **kwargs):
        """Log a function call with its arguments."""
        args_str = "  ".join(f"{k}={_fmt_val(v)}" for k, v in kwargs.items())
        self._write("debug", "→", "cyan", "CALL", f"{name}({args_str})", depth=3)

    def ret(self, name: str, value: Any):
        """Log a function's return value."""
        self._write("debug", "←", "cyan", "RETN", f"{name}() → {_fmt_val(value)}", depth=3)

    def var(self, name: str, value: Any, *, note: str = ""):
        """Log a variable's current value."""
        note_str = f"  # {note}" if note else ""
        self._write("debug", "·", "grey", "VAR ", f"{name} = {_fmt_val(value)}{note_str}", depth=3)

    def null(self, name: str, *, reason: str = ""):
        """Log a variable that is None/empty/missing — a common bug source."""
        reason_str = f"  ← {reason}" if reason else ""
        self._write("warning", "⚠", "yellow", "NULL", f"{name} is None/empty{reason_str}", depth=3)

    def api(self, method: str, url: str, *, status: int = 0,
            snippet: str = "", payload: Any = None, error: str = ""):
        """Log an outbound HTTP API call."""
        status_str = _c("green", str(status)) if 200 <= status < 300 else _c("red", str(status))
        payload_str = f"  payload={_fmt_val(payload, 60)}" if payload is not None else ""
        snippet_str = f"  resp={_fmt_val(snippet, 80)}" if snippet else ""
        err_str     = _c("red", f"  err={error}") if error else ""
        self._write("debug", "⇄", "blue", "API ",
                    f"{method} {url}  [{status_str}]{payload_str}{snippet_str}{err_str}", depth=3)

    def browser(self, action: str, target: str, *,
                value: Any = None, result: str = "", url: str = "", error: str = ""):
        """Log a Playwright browser interaction."""
        val_str    = f"  value={_fmt_val(value, 60)}" if value is not None else ""
        result_str = f"  → {_c('green', result)}" if result == "ok" else \
                     f"  → {_c('red',   result)}" if result in ("fail","error","not found") else \
                     f"  → {result}" if result else ""
        url_str    = f"  url={url[:60]}" if url else ""
        err_str    = f"  err={error}" if error else ""
        self._write("debug", "🖱", "magenta", "BRWSR",
                    f"{action}  {target}{val_str}{result_str}{url_str}{err_str}", depth=3)

    def nav(self, url: str, *, status: str = "", title: str = ""):
        """Log a page navigation."""
        status_str = f"  [{status}]" if status else ""
        title_str  = f"  title={_fmt_val(title, 60)}" if title else ""
        self._write("info", "→", "blue", "NAV ", f"{url}{status_str}{title_str}", depth=3)

    def state(self, **kwargs):
        """Log the current state of the page / variables (snapshot)."""
        parts = "  ".join(f"{k}={_fmt_val(v, 80)}" for k, v in kwargs.items())
        self._write("debug", "📸", "grey", "STAT", parts, depth=3)

    def db(self, operation: str, key: str, *, value: Any = None,
           count: int = -1, error: str = ""):
        """Log a database/file read or write."""
        val_str   = f"  value={_fmt_val(value, 80)}" if value is not None else ""
        count_str = f"  count={count}" if count >= 0 else ""
        err_str   = f"  error={error}" if error else ""
        self._write("debug", "💾", "grey", "DB  ",
                    f"{operation.upper()}  {key}{val_str}{count_str}{err_str}", depth=3)

    def ok(self, msg: str):
        """Log a successful outcome."""
        self._write("info", "✓", "green", "OK  ", msg, depth=3)

    def warn(self, msg: str, *, exc: BaseException = None):
        """Log a warning — non-fatal issue."""
        exc_str = f"\n{traceback.format_exc()}" if exc else ""
        self._write("warning", "⚠", "yellow", "WARN", f"{msg}{exc_str}", depth=3)

    def err(self, msg: str, *, exc: BaseException = None):
        """Log an error — something failed."""
        exc_str = f"\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}" \
                  if exc else ""
        self._write("error", "✗", "red", "ERR ", f"{msg}{exc_str}", depth=3)

    def skip(self, msg: str):
        """Log a deliberate skip."""
        self._write("info", "⊘", "grey", "SKIP", msg, depth=3)

    def info(self, msg: str):
        """General informational log."""
        self._write("info", "ℹ", "cyan", "INFO", msg, depth=3)

    # ── Decorator ─────────────────────────────────────────────────────────────

    def logged(self, func):
        """Decorator: auto-log function entry, exit, and exceptions."""
        is_async = inspect.iscoroutinefunction(func)
        name     = f"{func.__module__.split('.')[-1]}.{func.__qualname__}"

        if is_async:
            @functools.wraps(func)
            async def _async_wrapper(*args, **kwargs):
                sig = inspect.signature(func)
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                log_args = {k: v for k, v in list(bound.arguments.items())[:6]
                            if k not in ("self", "page", "ctx")}
                self._write("debug", "→", "cyan", "CALL", f"{name}({', '.join(f'{k}={_fmt_val(v,40)}' for k,v in log_args.items())})", depth=2)
                try:
                    result = await func(*args, **kwargs)
                    self._write("debug", "←", "cyan", "RETN", f"{name}() → {_fmt_val(result, 60)}", depth=2)
                    return result
                except Exception as exc:
                    self._write("error", "✗", "red", "ERR ",
                                f"{name}() raised {type(exc).__name__}: {exc}", depth=2)
                    raise
            return _async_wrapper
        else:
            @functools.wraps(func)
            def _sync_wrapper(*args, **kwargs):
                sig = inspect.signature(func)
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                log_args = {k: v for k, v in list(bound.arguments.items())[:6]
                            if k not in ("self",)}
                self._write("debug", "→", "cyan", "CALL", f"{name}({', '.join(f'{k}={_fmt_val(v,40)}' for k,v in log_args.items())})", depth=2)
                try:
                    result = func(*args, **kwargs)
                    self._write("debug", "←", "cyan", "RETN", f"{name}() → {_fmt_val(result, 60)}", depth=2)
                    return result
                except Exception as exc:
                    self._write("error", "✗", "red", "ERR ",
                                f"{name}() raised {type(exc).__name__}: {exc}", depth=2)
                    raise
            return _sync_wrapper

    # ── Session banner ────────────────────────────────────────────────────────

    def session_start(self, *, company: str = "", profile: str = "",
                      script: str = "", **kwargs):
        banner = "=" * 70
        _raw.info(f"\n{banner}")
        _raw.info(f"  SESSION START  {_SESSION_TS}")
        _raw.info(f"  script={script}  company={company}  profile={profile}")
        for k, v in kwargs.items():
            _raw.info(f"  {k}={_fmt_val(v)}")
        _raw.info(banner)
        print(_c("bold", f"\n{'='*50}"), flush=True)
        print(_c("green", f"  SESSION START  {_SESSION_TS}"), flush=True)
        if company: print(f"  company : {company}", flush=True)
        if profile: print(f"  profile : {profile}", flush=True)
        print(_c("bold", "="*50 + "\n"), flush=True)
        print(f"  Full debug log → {_LOG_FILE}\n", flush=True)

    def session_end(self, *, applied: int = 0, skipped: int = 0, errors: int = 0):
        banner = "=" * 70
        _raw.info(f"\n{banner}")
        _raw.info(f"  SESSION END   applied={applied}  skipped={skipped}  errors={errors}")
        _raw.info(banner + "\n")
        print(_c("bold", f"\n{'='*50}"), flush=True)
        print(_c("green" if errors == 0 else "yellow",
                 f"  SESSION END  applied={applied}  skipped={skipped}  errors={errors}"),
              flush=True)
        print(_c("bold", "="*50), flush=True)


# ── Singleton ─────────────────────────────────────────────────────────────────

log = ApplyLogger()


# ── Playwright browser wrapper ────────────────────────────────────────────────

class LoggedPage:
    """
    Thin proxy around a Playwright Page that logs every major interaction.

    Usage:
        lp = LoggedPage(page)
        await lp.goto("https://...")
        await lp.click("[data-automation-id='submitButton']")
        await lp.fill("#email", "user@example.com")
    """

    def __init__(self, page):
        self._page = page

    def __getattr__(self, name):
        return getattr(self._page, name)

    @property
    def url(self):
        return self._page.url

    async def goto(self, url: str, **kwargs):
        log.nav(url)
        try:
            result = await self._page.goto(url, **kwargs)
            status = result.status if result else "?"
            title  = ""
            try:
                title = await self._page.title()
            except Exception:
                pass
            log.nav(url, status=str(status), title=title)
            return result
        except Exception as exc:
            log.err(f"goto({url}) failed", exc=exc)
            raise

    async def click(self, selector: str, **kwargs):
        log.browser("click", selector)
        try:
            result = await self._page.click(selector, **kwargs)
            log.browser("click", selector, result="ok")
            return result
        except Exception as exc:
            log.browser("click", selector, result="fail", error=str(exc)[:80])
            raise

    async def fill(self, selector: str, value: str, **kwargs):
        log.browser("fill", selector, value=value)
        try:
            result = await self._page.fill(selector, value, **kwargs)
            log.browser("fill", selector, value=value, result="ok")
            return result
        except Exception as exc:
            log.browser("fill", selector, value=value, result="fail", error=str(exc)[:80])
            raise

    async def select_option(self, selector: str, value=None, **kwargs):
        log.browser("select_option", selector, value=value)
        try:
            result = await self._page.select_option(selector, value, **kwargs)
            log.browser("select_option", selector, value=value, result="ok")
            return result
        except Exception as exc:
            log.browser("select_option", selector, result="fail", error=str(exc)[:80])
            raise

    async def wait_for_selector(self, selector: str, **kwargs):
        log.browser("wait_for_selector", selector)
        try:
            result = await self._page.wait_for_selector(selector, **kwargs)
            log.browser("wait_for_selector", selector, result="ok")
            return result
        except Exception as exc:
            log.browser("wait_for_selector", selector, result="not found", error=str(exc)[:60])
            raise

    async def inner_text(self, selector: str = "body", **kwargs):
        try:
            text = await self._page.inner_text(selector, **kwargs)
            log.browser("inner_text", selector, result=f"{len(text)} chars")
            return text
        except Exception as exc:
            log.browser("inner_text", selector, result="fail", error=str(exc)[:60])
            raise

    async def screenshot(self, **kwargs):
        path = kwargs.get("path", "")
        log.browser("screenshot", str(path))
        result = await self._page.screenshot(**kwargs)
        log.browser("screenshot", str(path), result="saved")
        return result


# ── HTTP request wrapper ───────────────────────────────────────────────────────

def logged_get(url: str, *, timeout: int = 15, headers: dict = None) -> Any:
    """requests.get wrapper that logs the call and response."""
    import requests
    log.api("GET", url)
    try:
        r = requests.get(url, timeout=timeout, headers=headers or {})
        snippet = r.text[:120].replace("\n", " ") if r.text else ""
        log.api("GET", url, status=r.status_code, snippet=snippet)
        return r
    except Exception as exc:
        log.api("GET", url, status=0, error=str(exc)[:80])
        raise
