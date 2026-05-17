#!/usr/bin/env bash
# Build, sign, notarize, and package BB Tracker
#
# ── One-time setup ───────────────────────────────────────────────────────────
# 1. Enroll in Apple Developer Program (developer.apple.com, $99/yr)
# 2. In Xcode → Settings → Accounts, download your "Developer ID Application" cert
# 3. Fill in the three variables below:
#
TEAM_ID=""                      # e.g. "AB12CD34EF"  (10-char code from developer.apple.com)
APPLE_ID=""                     # your Apple ID email used for the Dev account
APP_PASSWORD=""                 # app-specific password from appleid.apple.com → Security
#
# Leave all three empty to do a dev/local build (ad-hoc signed, Gatekeeper rejected).
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail
cd "$(dirname "$0")"

SIGN_IDENTITY="Developer ID Application: $TEAM_ID"
DMG_NAME="BBTracker.dmg"
APP="dist/BB Tracker.app"

step() { echo ""; echo "▶  $1"; }
ok()   { echo "   ✓ $1"; }
warn() { echo "   ⚠  $1"; }

# ── 1. Icon ───────────────────────────────────────────────────────────────────
# Two icon sources, on purpose:
#   - app_icon.png   = full-bleed dark-bg artwork (white bars) used for .icns
#                      (Finder, Dock, /Applications)
#   - icon.png       = template-style (black bars on transparent) for the menu
#                      bar — macOS auto-themes templates to match the menu bar.
step "Generating app icon..."
if [ -f app_icon.png ]; then
    mkdir -p AppIcon.iconset
    for SIZE in 16 32 64 128 256 512; do
        sips -z $SIZE $SIZE app_icon.png \
            --out "AppIcon.iconset/icon_${SIZE}x${SIZE}.png"    >/dev/null 2>&1
        sips -z $((SIZE*2)) $((SIZE*2)) app_icon.png \
            --out "AppIcon.iconset/icon_${SIZE}x${SIZE}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns AppIcon.iconset -o AppIcon.icns
    rm -rf AppIcon.iconset
    ok "AppIcon.icns created (from app_icon.png — white bars on dark)"
elif [ -f icon.png ]; then
    warn "app_icon.png missing — falling back to icon.png (template style)"
    mkdir -p AppIcon.iconset
    for SIZE in 16 32 64 128 256 512; do
        sips -z $SIZE $SIZE icon.png \
            --out "AppIcon.iconset/icon_${SIZE}x${SIZE}.png"    >/dev/null 2>&1
        sips -z $((SIZE*2)) $((SIZE*2)) icon.png \
            --out "AppIcon.iconset/icon_${SIZE}x${SIZE}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns AppIcon.iconset -o AppIcon.icns
    rm -rf AppIcon.iconset
fi

# ── 2. Clean ──────────────────────────────────────────────────────────────────
step "Cleaning previous build..."
rm -rf build dist
ok "Cleaned"

# ── 3. Build .app ─────────────────────────────────────────────────────────────
step "Building .app bundle (takes ~1 min)..."
python3.14 -c "
import sys
sys.path.insert(0, '.venv/lib/python3.14/site-packages')
import PyInstaller.__main__
sys.argv = ['pyinstaller', 'BBTracker.spec', '--noconfirm']
PyInstaller.__main__.run()
" 2>&1 | grep -E "^[0-9]+ (INFO|ERROR|WARNING)" | grep -E "(ERROR|WARNING|Build complete)" | tail -5
ok "App bundle: $APP"

# ── 4. Code-sign ─────────────────────────────────────────────────────────────
if [ -n "$TEAM_ID" ]; then
    step "Code-signing..."
    # Entitlements needed for hardened runtime + Playwright's Chromium
    cat > /tmp/bbtracker.entitlements.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>com.apple.security.cs.allow-jit</key><true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
    <key>com.apple.security.cs.disable-library-validation</key><true/>
    <key>com.apple.security.network.client</key><true/>
    <key>com.apple.security.network.server</key><true/>
</dict></plist>
EOF
    codesign --deep --force --options runtime \
        --entitlements /tmp/bbtracker.entitlements.plist \
        --sign "$SIGN_IDENTITY" \
        "$APP"
    ok "Signed with $SIGN_IDENTITY"
else
    warn "TEAM_ID not set — skipping code-sign (ad-hoc only, Gatekeeper will reject)"
fi

# ── 5. Package DMG ───────────────────────────────────────────────────────────
step "Creating DMG..."
rm -f "$DMG_NAME"

# Build a DMG with an Applications symlink for the standard drag-to-install UX
STAGING="$(mktemp -d)/dmg-staging"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
[ -f INSTALL.txt ] && cp INSTALL.txt "$STAGING/READ ME FIRST.txt"

hdiutil create \
    -volname "BB Tracker" \
    -srcfolder "$STAGING" \
    -ov -format UDZO \
    "$DMG_NAME" >/dev/null

rm -rf "$STAGING"
ok "DMG: $DMG_NAME"

# ── 6. Notarize ───────────────────────────────────────────────────────────────
if [ -n "$TEAM_ID" ] && [ -n "$APPLE_ID" ] && [ -n "$APP_PASSWORD" ]; then
    step "Notarizing (submitting to Apple — takes 1-5 min)..."
    xcrun notarytool submit "$DMG_NAME" \
        --apple-id "$APPLE_ID" \
        --team-id  "$TEAM_ID" \
        --password "$APP_PASSWORD" \
        --wait
    ok "Notarization accepted"

    step "Stapling notarization ticket..."
    xcrun stapler staple "$DMG_NAME"
    ok "Stapled — Gatekeeper will accept this DMG on any Mac"
else
    warn "APPLE_ID/APP_PASSWORD not set — skipping notarization"
    warn "Users will see a Gatekeeper warning. Fill in the vars at the top of this script."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "   ✓  Build complete: $DMG_NAME"
if [ -n "$TEAM_ID" ]; then
echo "   ✓  Signed + notarized — ready to distribute"
else
echo "   ⚠  Not signed — fill in TEAM_ID / APPLE_ID / APP_PASSWORD to ship"
fi
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
