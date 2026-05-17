"""Single shared logger for BB Tracker — file-based, thread-aware.

Both menubar.py and scraper.py append to the same file at
~/Library/Application Support/BBTracker/menubar.log so the full sequence
of events (UI clicks → worker threads → Playwright steps) lives in one
place. This is the only debugging surface in a packaged .app, where
print()/stderr go to /dev/null.

Format:
    HH:MM:SS [tag] [thread] message

Use:
    from log import log
    log("startup", "App launched (PID=%s)", os.getpid())
    log("scrape", "Course %d/%d: %s", i, total, name)
    log.exc("scrape", "Worker crashed", exc)
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

# Mirror scraper.py's DATA_DIR resolution so the log lands in the same place
# whether running as a packaged .app or from source.
_IS_PACKAGED = bool(getattr(sys, "frozen", None))
if _IS_PACKAGED:
    _LOG_DIR = Path.home() / "Library" / "Application Support" / "BBTracker"
else:
    _LOG_DIR = Path(__file__).parent

_LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = _LOG_DIR / "menubar.log"

# Cap log size at ~2 MB so it doesn't grow forever — rotate to .old.
_MAX_BYTES = 2 * 1024 * 1024
_lock = threading.Lock()


def _rotate_if_needed() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > _MAX_BYTES:
            old = LOG_PATH.with_suffix(".log.old")
            if old.exists():
                old.unlink()
            LOG_PATH.rename(old)
    except Exception:
        pass


def _write(line: str) -> None:
    with _lock:
        _rotate_if_needed()
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass
        # Also print when running from source so dev iteration is fast
        if not _IS_PACKAGED:
            try:
                print(line, flush=True)
            except Exception:
                pass


def log(tag: str, msg: str, *args) -> None:
    """Log a single line with a [tag] prefix. Cheap; safe from any thread."""
    if args:
        try:
            msg = msg % args
        except Exception:
            msg = f"{msg}  (args={args!r})"
    ts = datetime.now().strftime("%H:%M:%S")
    th = threading.current_thread().name
    _write(f"{ts} [{tag:9s}] [{th}] {msg}")


def exc(tag: str, msg: str, exc_obj: BaseException) -> None:
    """Log a one-line summary of an exception plus the full traceback."""
    log(tag, "%s: %s: %s", msg, type(exc_obj).__name__, exc_obj)
    tb = "".join(traceback.format_exception(type(exc_obj), exc_obj, exc_obj.__traceback__))
    for line in tb.rstrip().split("\n"):
        log(tag, "    %s", line)


# Convenience attribute access so callers can do `log.exc(...)` or `log(...)`.
log.exc = exc  # type: ignore[attr-defined]
log.path = str(LOG_PATH)  # type: ignore[attr-defined]
