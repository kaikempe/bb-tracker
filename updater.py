"""In-place auto-updater for BB Tracker.

Downloads the latest DMG, replaces the running app bundle, strips the
quarantine xattr, and relaunches. Strips quarantine so Gatekeeper has
nothing to flag — without this, every update would re-trigger Apple's
"right-click → Open" dance because BB Tracker is unsigned.

This is a workaround for not being enrolled in the Apple Developer
Program. Once the app is signed + notarized, the quarantine-strip
becomes unnecessary (Apple's signature is permanently trusted).
"""

from __future__ import annotations

import os
import ssl
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from Foundation import NSBundle

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

BUNDLE_ID = "com.bbtracker.app"


class UpdateCancelled(Exception):
    """Raised by perform_update when is_cancelled() returns True mid-download."""


def _bundle_path() -> str:
    return str(NSBundle.mainBundle().bundlePath())


def _installed_path() -> str | None:
    """Real on-disk install path of BB Tracker, regardless of where the
    *running* instance is mounted. Why: macOS App Translocation runs
    unsigned+quarantined apps from a read-only /private/var/folders/...
    sandbox, so the running bundle isn't writable — but the user's actual
    installed copy at /Applications/BB Tracker.app is. Updating that path
    (and relaunching via bundle ID) is what makes auto-update seamless
    for users who downloaded the unsigned DMG.
    """
    try:
        from AppKit import NSWorkspace
        url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(BUNDLE_ID)
        if url is not None:
            p = str(url.path())
            if p and "/AppTranslocation/" not in p and p.endswith(".app"):
                return p
    except Exception:
        pass
    # Fallback: scan the standard install locations.
    for candidate in ("/Applications/BB Tracker.app",
                      os.path.expanduser("~/Applications/BB Tracker.app")):
        if os.path.exists(candidate):
            return candidate
    return None


def _resolve_target_path() -> str:
    """The .app path the updater will replace. When running translocated,
    falls through to the installed copy; otherwise just our own bundle."""
    running = _bundle_path()
    if "/AppTranslocation/" not in running:
        return running
    installed = _installed_path()
    return installed or running


def can_self_update() -> tuple[bool, str]:
    """(ok, reason). If False, caller should fall back to the browser flow."""
    target = _resolve_target_path()
    if "/AppTranslocation/" in target:
        return False, "running translocated and no installed copy found at /Applications"
    if not target.endswith(".app"):
        return False, f"unexpected bundle path: {target}"
    if not os.access(target, os.W_OK):
        return False, f"no write access to {target}"
    return True, ""


def perform_update(dmg_url: str, on_progress=None, is_cancelled=None) -> None:
    """Download the DMG, schedule the in-place replace, and return. Caller
    is expected to quit the app shortly after — the helper script waits
    for our PID to exit before touching the bundle.

    If is_cancelled() returns True during the download loop, the partial
    download is removed and UpdateCancelled is raised before the in-place
    replace is scheduled.
    """
    bundle_path = _resolve_target_path()
    tmp_dir = Path(tempfile.mkdtemp(prefix="bbtracker-update-"))
    dmg_path = tmp_dir / "BBTracker.dmg"

    try:
        with urllib.request.urlopen(dmg_url, timeout=60, context=_SSL_CTX) as r:
            total = int(r.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(dmg_path, "wb") as f:
                while True:
                    if is_cancelled and is_cancelled():
                        raise UpdateCancelled()
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)
    except UpdateCancelled:
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        finally:
            raise

    support_dir = Path.home() / "Library" / "Application Support" / "BBTracker"
    support_dir.mkdir(parents=True, exist_ok=True)
    log_path = support_dir / "updater.log"
    pid = os.getpid()
    script_path = tmp_dir / "apply-update.sh"
    script_body = f"""#!/bin/bash
exec >"{log_path}" 2>&1
set -x

# Wait for the running BB Tracker (PID {pid}) to exit, max 30s
for i in $(seq 1 30); do
    if ! kill -0 {pid} 2>/dev/null; then break; fi
    sleep 1
done

# Mount the DMG
MOUNT_OUTPUT=$(hdiutil attach -nobrowse -noverify -noautoopen "{dmg_path}")
MOUNT_POINT=$(echo "$MOUNT_OUTPUT" | grep -Eo '/Volumes/[^[:space:]].*' | tail -1)
if [ -z "$MOUNT_POINT" ] || [ ! -d "$MOUNT_POINT/BB Tracker.app" ]; then
    echo "ERROR: failed to mount DMG or BB Tracker.app missing"
    [ -n "$MOUNT_POINT" ] && hdiutil detach "$MOUNT_POINT" -force || true
    open "{bundle_path}"
    exit 1
fi

# Replace the bundle
TARGET="{bundle_path}"
STAGING="$TARGET.update-staging"
rm -rf "$STAGING"
cp -R "$MOUNT_POINT/BB Tracker.app" "$STAGING"
xattr -dr com.apple.quarantine "$STAGING" 2>/dev/null || true
rm -rf "$TARGET"
mv "$STAGING" "$TARGET"

hdiutil detach "$MOUNT_POINT" -force || true
rm -rf "{tmp_dir}"
open "$TARGET"
"""
    script_path.write_text(script_body)
    script_path.chmod(0o755)

    subprocess.Popen(
        ["/bin/bash", str(script_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
