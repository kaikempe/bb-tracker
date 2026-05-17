#!/usr/bin/env bash
# BB Tracker installer — works on any Mac, no prior setup needed.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.bbtracker.app.plist"

step() { echo ""; echo "▶  $1"; }
ok()   { echo "   ✓ $1"; }
fail() { echo ""; echo "✗  $1"; echo "   Please screenshot this and send it to get help."; exit 1; }

clear
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   BB Tracker — Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. Homebrew ──────────────────────────────────────────────────────────────
step "Checking for Homebrew..."
if ! command -v brew &>/dev/null; then
    echo "   Homebrew not found — installing it now."
    echo "   This may ask for your Mac password. That's normal."
    echo ""
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
        || fail "Homebrew install failed."
    # Add to PATH for Apple Silicon Macs
    eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || \
    eval "$(/usr/local/bin/brew shellenv)"    2>/dev/null || true
fi
ok "Homebrew ready"

# ── 2. Python 3.12+ ──────────────────────────────────────────────────────────
step "Checking for Python 3..."
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        if "$candidate" -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "   Python 3.12 not found — installing via Homebrew..."
    brew install python@3.12 --quiet || fail "Python install failed."
    PYTHON="python3.12"
fi
ok "Python ready ($(${PYTHON} --version))"

# ── 3. Virtual environment & packages ────────────────────────────────────────
step "Installing packages..."
cd "$INSTALL_DIR"
"$PYTHON" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt \
    || fail "Package install failed."
ok "Packages installed"

# ── 4. Playwright browser (Chromium) ────────────────────────────────────────
step "Installing browser (needed to access Blackboard)..."
.venv/bin/python3 -m playwright install chromium \
    || fail "Browser install failed."
ok "Browser installed"

# ── 5. LaunchAgent (auto-start on login) ────────────────────────────────────
step "Setting up auto-start..."
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" << PLIST_CONTENT
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.bbtracker.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>${INSTALL_DIR}/.venv/bin/python3</string>
        <string>${INSTALL_DIR}/menubar.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/menubar.log</string>
    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/menubar.log</string>
</dict>
</plist>
PLIST_CONTENT

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load   "$PLIST"
ok "Will start automatically on login"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "   ✓  BB Tracker is now running!"
echo ""
echo "   Look for the  ≡  icon in your menu bar."
echo "   A browser window will open — log in to Blackboard"
echo "   and approve the Authenticator prompt."
echo ""
echo "   When asked 'Stay signed in?' → click YES"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
