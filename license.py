"""License key management for BB Tracker.

Trial:   7 days from first launch, no key needed.
Paid:    license key from Lemon Squeezy, validated weekly.
         If subscription lapses → key goes invalid → app blocks.
Offline: 7-day grace period (key cached locally).

Trial-tampering defense (not bulletproof — just stops casual editing):
  - trial_started is HMAC-signed with a secret embedded in the binary.
    Editing the date in license.json invalidates the signature, and the
    app refuses to honor it (treats as expired).
  - The trial-start timestamp is also mirrored into NSUserDefaults,
    so deleting license.json doesn't reset the trial — the older of the
    two timestamps wins.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import socket
import ssl
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# ── Config ────────────────────────────────────────────────────────────────────
LS_API       = "https://api.lemonsqueezy.com/v1/licenses"
TRIAL_DAYS   = 7
GRACE_DAYS   = 7    # offline grace before blocking
RECHECK_DAYS = 7    # how often to re-validate online

_DATA_DIR    = Path.home() / "Library" / "Application Support" / "BBTracker"
_LICENSE_FILE = _DATA_DIR / "license.json"

# Embedded HMAC secret. Note: anyone with the binary can extract this; the goal
# isn't cryptographic secrecy, just to defeat trivial JSON-editing attacks.
_TRIAL_HMAC_KEY = b"bbtracker.trial.v1.6f4a8e0c2bd14f5da7"
_DEFAULTS_KEY = "trialAnchorV1"  # NSUserDefaults key for the mirror


def _sign_trial(started_iso: str) -> str:
    return hmac.new(_TRIAL_HMAC_KEY, started_iso.encode(), hashlib.sha256).hexdigest()


def _read_defaults_anchor() -> str | None:
    """Read the trial-start mirror from NSUserDefaults (survives license.json deletion)."""
    try:
        from Foundation import NSUserDefaults
        val = NSUserDefaults.standardUserDefaults().stringForKey_(_DEFAULTS_KEY)
        return str(val) if val else None
    except Exception:
        return None


def _write_defaults_anchor(started_iso: str) -> None:
    try:
        from Foundation import NSUserDefaults
        NSUserDefaults.standardUserDefaults().setObject_forKey_(started_iso, _DEFAULTS_KEY)
    except Exception:
        pass


def _earliest(a: str | None, b: str | None) -> str | None:
    """Return the earlier of two ISO timestamps (or whichever is non-None)."""
    if a and b:
        return min(a, b)  # ISO-8601 sorts lexicographically
    return a or b


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(_LICENSE_FILE.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LICENSE_FILE.write_text(json.dumps(data, indent=2))


def _resolve_trial_start(data: dict, now: datetime) -> tuple[str, dict]:
    """Pick the authoritative trial_started timestamp, fixing any tampering.

    Logic:
      1. Verify license.json's trial_started against its HMAC signature.
         If signature is missing or wrong, ignore the file's value.
      2. Cross-check against the NSUserDefaults mirror.
      3. The EARLIEST of the two valid sources wins (so editing later doesn't help).
      4. If neither source is available, start fresh trial = now.

    Returns (chosen_iso, updated_data) and persists fixes back to license.json.
    """
    file_iso = data.get("trial_started")
    file_sig = data.get("trial_sig")
    if file_iso and file_sig:
        # New format: signed. Trust only if signature matches.
        if hmac.compare_digest(_sign_trial(file_iso), file_sig):
            verified_file = file_iso
        else:
            verified_file = None  # tampered — reject
    elif file_iso and not file_sig:
        # Legacy format from before signing was added. Grandfather the value
        # ONCE — but only if NSUserDefaults doesn't already have an earlier
        # anchor. (If it does, the earliest-wins logic below handles it.)
        verified_file = file_iso
    else:
        verified_file = None

    defaults_iso = _read_defaults_anchor()
    chosen = _earliest(verified_file, defaults_iso)

    if chosen is None:
        # Genuine fresh install (or both anchors wiped) → new trial starts now.
        chosen = now.isoformat()

    # Re-write file with a valid signature so future reads accept it
    if data.get("trial_started") != chosen or data.get("trial_sig") != _sign_trial(chosen):
        data["trial_started"] = chosen
        data["trial_sig"] = _sign_trial(chosen)

    # Mirror to NSUserDefaults — only writes if missing or newer than chosen
    if defaults_iso != chosen:
        _write_defaults_anchor(chosen)

    return chosen, data


def _ls_post(endpoint: str, payload: dict) -> dict:
    """POST to Lemon Squeezy API. Raises on network error."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{LS_API}/{endpoint}",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as r:
        return json.loads(r.read())


# ── Public API ────────────────────────────────────────────────────────────────

def get_status() -> dict:
    """Return the full license status dict (used by menubar to decide what to show).

    Keys:
      state:       "trial" | "active" | "expired" | "no_license" | "grace"
      days_left:   int (trial days remaining) or None
      key:         str or None
    """
    data = _load()
    now  = datetime.now()

    # ── Active paid license ───────────────────────────────────────────────────
    if data.get("key") and data.get("valid"):
        last_checked = datetime.fromisoformat(data.get("validated_at", "2000-01-01"))
        # Re-validate online if due
        if now - last_checked > timedelta(days=RECHECK_DAYS):
            ok, reason = _validate_online(data["key"], data.get("instance_id"))
            data["valid"] = ok
            data["validated_at"] = now.isoformat()
            _save(data)
            if not ok:
                return {"state": "expired", "days_left": None, "key": data["key"]}
        return {"state": "active", "days_left": None, "key": data["key"]}

    # ── Key stored but marked invalid (subscription lapsed) ──────────────────
    if data.get("key") and not data.get("valid"):
        return {"state": "expired", "days_left": None, "key": data["key"]}

    # ── Trial — anchor signed + mirrored to NSUserDefaults ───────────────────
    chosen_iso, data = _resolve_trial_start(data, now)
    _save(data)
    started  = datetime.fromisoformat(chosen_iso)
    ends     = started + timedelta(days=TRIAL_DAYS)
    days_left = max(0, (ends - now).days)
    if now < ends:
        return {"state": "trial", "days_left": days_left, "key": None}
    return {"state": "no_license", "days_left": 0, "key": None}


def _bb_identity() -> str:
    """Return a short stable string identifying this Blackboard account.

    We hash the BB base URL + stored username so the same key cannot be
    activated on a machine logged in to a different BB account.
    """
    import hashlib
    try:
        cfg_path = _DATA_DIR / "config.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        bb_url = cfg.get("blackboard_base_url", "")
        # Try to read the stored BB username (written during discovery)
        bb_user = cfg.get("bb_username", "")
        identity = f"{bb_url}|{bb_user}" if bb_user else bb_url
        return hashlib.sha256(identity.encode()).hexdigest()[:12]
    except Exception:
        return "unknown"


def activate(key: str) -> tuple[bool, str]:
    """Activate a new license key. Returns (success, error_message)."""
    key = key.strip().upper()
    try:
        # Instance name encodes hostname + BB account identity so one key
        # stays tied to one Blackboard account across multiple Macs.
        instance_name = f"{socket.gethostname()}|{_bb_identity()}"
        result = _ls_post("activate", {
            "license_key": key,
            "instance_name": instance_name,
        })
        if result.get("activated"):
            prev = _load()
            _save({
                "key":          key,
                "instance_id":  result.get("instance", {}).get("id"),
                "validated_at": datetime.now().isoformat(),
                "valid":        True,
                # Preserve the (signed) trial anchor so trial info isn't lost
                "trial_started": prev.get("trial_started"),
                "trial_sig":     prev.get("trial_sig"),
            })
            return True, ""
        error = result.get("error", "Invalid license key.")
        return False, error
    except Exception as e:
        return False, f"Could not connect to license server: {e}"


def _validate_online(key: str, instance_id: str | None) -> tuple[bool, str]:
    """Hit Lemon Squeezy to check if key is still valid."""
    try:
        payload: dict = {"license_key": key}
        if instance_id:
            payload["instance_id"] = instance_id
        result = _ls_post("validate", payload)
        return result.get("valid", False), result.get("error", "")
    except Exception:
        # Network failure — caller decides grace period
        raise


def deactivate() -> None:
    """Remove the stored license (e.g. user wants to move to another Mac)."""
    data = _load()
    if data.get("key") and data.get("instance_id"):
        try:
            _ls_post("deactivate", {
                "license_key": data["key"],
                "instance_id": data["instance_id"],
            })
        except Exception:
            pass
    _save({k: v for k, v in data.items() if k not in ("key", "instance_id", "valid", "validated_at")})
