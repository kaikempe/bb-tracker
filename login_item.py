"""Manage BB Tracker as a macOS login item.

Writes a LaunchAgent plist at ~/Library/LaunchAgents/com.bbtracker.app.plist
that launches the app via `open -b com.bbtracker.app` on next login. Using
the bundle id (rather than a hardcoded path) survives the user moving the
app between folders, since Launch Services finds it by id.

Why a LaunchAgent and not SMAppService: SMAppService needs macOS 13+ and
an extra PyObjC framework dependency. A plist is universal back to 10.6
and requires zero new deps.

Quit-and-reopen fix: We deliberately omit RunAtLoad from the plist. On
modern macOS (11+), launchd auto-discovers files dropped into LaunchAgents/
and fires RunAtLoad immediately — which caused the app to reopen seconds
after the user quit. Without RunAtLoad the job only fires at the next
macOS login, which is the intended "launch at login" behaviour.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.bbtracker.app.plist"
BUNDLE_ID = "com.bbtracker.app"


def _bootout() -> None:
    """Unload the job from launchd (best-effort, ignores errors)."""
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True,
    )


def _bootstrap() -> None:
    """Load the job into launchd so it fires at next login (best-effort)."""
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True,
    )


def is_enabled() -> bool:
    return PLIST_PATH.exists()


def enable() -> bool:
    try:
        PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Unload any previously-loaded version first so we can safely
        # replace the file (e.g. an old plist that had RunAtLoad: true).
        _bootout()
        plist = {
            "Label": BUNDLE_ID,
            "ProgramArguments": ["/usr/bin/open", "-b", BUNDLE_ID],
            # No RunAtLoad — prevents launchd from immediately reopening the
            # app when the plist is first written to LaunchAgents/.
        }
        with open(PLIST_PATH, "wb") as f:
            plistlib.dump(plist, f)
        _bootstrap()
        return True
    except Exception:
        return False


def disable() -> bool:
    try:
        _bootout()
        if PLIST_PATH.exists():
            PLIST_PATH.unlink()
        return True
    except Exception:
        return False
