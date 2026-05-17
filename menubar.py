"""Menu-bar app for Blackboard attendance and grades.

Click a course to expand its submenu:

  With syllabus weights loaded (after Sync), partial coverage:
    Grade so far: 83.0%
    Based on 30% of course graded (30 of 100 weight pts)
    If you score 0 on rest: 24.9%
    ─────
    Intermediate Tests (20%)  —  82% · 2 items
    Other (10%)               —  93% · 1 item
    Final Exam (40%)          —  not yet
    Workgroups (25%)          —  not yet
    ─────
    Absent: 4/6  ·  2 remaining

  Without weights (raw scores):
    Grade: 89.9%
    (from Blackboard — no syllabus weights)
    82.5%   MID TERM EXAM
    96.7%   Quiz avg
    ─────
    Absent: 4/6  ·  2 remaining

Run "Sync courses from Blackboard" to discover courses, fetch syllabus
weights and update favorite status.  Set "track_favorites_only": true in
config.json to show only favorited courses.
"""

from __future__ import annotations

# PyInstaller bundles don't ship macOS system CA certs — point Python at
# certifi's bundle so urllib HTTPS calls (license, version check, DMG
# download) verify on every Mac, not just ones with system certs available.
import os as _os
try:
    import certifi as _certifi
    _os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    _os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
except Exception:
    pass

import asyncio
import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import struct
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs

import rumps
import objc
from Foundation import NSBundle, NSObject
from AppKit import NSAppleEventManager

# Hide Python from the Dock — menu-bar-only app
NSBundle.mainBundle().infoDictionary()["LSUIElement"] = "1"

from scraper import (
    CONFIG_PATH, DATA_PATH, COURSES_PATH, ANNOUNCEMENTS_PATH,
    BASE_DIR,
    build_grade_categories, bundled_weights_for,
    load_config, save_config,
    load_courses,
    load_announcements,
    run_scrape, run_discover, run_login,
)
import login_item
import notifier as _notifier


def _on_main(fn) -> None:
    """Run fn() on the main thread. Required for AppKit calls (NSWindow.close,
    etc) when invoked from a worker thread — calling them off-main can tear
    down the whole process. Why: a closed onboarding window from the login
    worker killed the app for an early user."""
    from Foundation import NSOperationQueue
    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
from version import APP_VERSION, VERSION_URL, DOWNLOAD_URL, FEEDBACK_EMAIL
from version import STORE_MONTHLY, STORE_YEARLY, STORE_LIFETIME
import license as _lic
from log import log
from main_window import MainWindow

ICON_PATH = str(BASE_DIR / "icon.png")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_data() -> dict | None:
    if not DATA_PATH.exists():
        return None
    try:
        with open(DATA_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def load_weights_map() -> dict[str, dict]:
    """Return {course_name: grade_weights_dict}.

    Live syllabus scrape is preferred — but when it failed for a course
    (e.g. hidden Syllabus tool, image-only PDF, or a user whose discovery
    run errored on a particular course), we fall back to bundled defaults
    keyed by course name. Why: standardized IE Bachelor program courses
    have the same syllabus structure across sections, so a single user
    seeing weighted grades while their classmate sees nothing was a real
    user complaint. Falling back at display time means the user doesn't
    have to re-trigger discovery — the moment they update, their menu
    populates correctly.
    """
    courses = []
    if COURSES_PATH.exists():
        try:
            courses = json.loads(COURSES_PATH.read_text())
        except Exception:
            courses = []
    out: dict[str, dict] = {}
    for c in courses:
        name = c.get("name") or ""
        weights = c.get("grade_weights") or {}
        if not weights:
            weights = bundled_weights_for(name)
        out[name] = weights
    return out


def _is_mini_test_category(category_name: str) -> bool:
    """True if a grade category name looks like it covers mini-tests."""
    n = category_name.lower()
    return ("mini" in n and "test" in n) or ("mini" in n and "quiz" in n)


def _att_status(remaining: float | None) -> tuple[str, str]:
    if remaining is None:
        return "⚪", "n/a"
    if remaining <= 0:
        return "🔴", "at limit" if remaining == 0 else "OVER limit"
    if remaining <= 2:
        return "🟡", f"{remaining:g} left"
    return "🟢", f"{remaining:g} left"


def _att_status_from_pct(att_pct: float | None, max_absence_pct: float) -> tuple[str, str]:
    """Status when we only have an attendance percentage (no session counts).

    Used when the course tracks attendance via a gradebook column (e.g.
    QWAttendance) — we know the percentage but not how many sessions or
    how many were missed. Compared against the syllabus threshold
    (1 - max_absence_pct).
    """
    if att_pct is None:
        return "⚪", "n/a"
    threshold = (1.0 - max_absence_pct) * 100.0   # e.g. 80%
    margin = att_pct - threshold
    label = f"{att_pct:.0f}% present"
    if margin < 0:
        return "🔴", f"{att_pct:.0f}% — below {threshold:.0f}%"
    if margin < 5:
        return "🟡", label
    return "🟢", label


_TITLE_LOWER = {"for", "of", "the", "and", "in", "a", "an", "to", "at", "by", "with"}
_ACRONYMS    = {"IE", "CS", "PC", "IT", "AI", "ML", "UI", "UX", "HR", "PR", "NLP"}


def _smart_title(s: str) -> str:
    """Title-case preserving known acronyms (IE, CS…) and lowercasing prepositions."""
    out = []
    for i, w in enumerate(s.split()):
        wu = w.upper()
        if wu in _ACRONYMS:
            out.append(wu)                     # IE, CS → uppercase
        elif i > 0 and w.lower() in _TITLE_LOWER:
            out.append(w.lower())              # for, of, the → lowercase
        else:
            out.append(w.capitalize())         # everything else → Capitalize
    return " ".join(out)


def _fmt_score(score: float, possible: float | None) -> str:
    if possible is None:
        return f"{score:.1f}"
    if possible == 100.0:
        return f"{score:.0f}%"
    s = f"{score:.1f}".rstrip("0").rstrip(".")
    p = f"{possible:.1f}".rstrip("0").rstrip(".")
    return f"{s}/{p}"


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _item(text: str) -> rumps.MenuItem:
    """Non-clickable menu item that renders in WHITE (not grayed-out).
    macOS disables items with no callback; a no-op fixes that."""
    return rumps.MenuItem(text, callback=lambda _: None)


def _format_ts(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    delta = datetime.now() - dt
    if delta < timedelta(minutes=2):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} min ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} hr ago"
    return f"{delta.days}d ago"


# ---------------------------------------------------------------------------
# Onboarding window
# ---------------------------------------------------------------------------

_ONBOARDING_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0a0a0f;color:#e8e8f0;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased;
       user-select:none;height:100vh;display:flex;flex-direction:column}
  .page{display:none;flex-direction:column;flex:1;padding:36px 40px 32px;animation:fade .25s}
  .page.active{display:flex}
  @keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

  /* Welcome */
  .icon{font-size:52px;margin-bottom:16px;line-height:1}
  h1{font-size:22px;font-weight:700;letter-spacing:-.02em;margin-bottom:8px}
  .sub{color:#6b6b85;font-size:14px;margin-bottom:24px}
  .features{display:flex;flex-direction:column;gap:10px;margin-bottom:28px}
  .feature{display:flex;align-items:flex-start;gap:12px;
           background:#13131a;border:1px solid #1e1e2e;border-radius:10px;padding:12px 14px}
  .feature-icon{font-size:20px;line-height:1;flex-shrink:0;margin-top:1px}
  .feature-text strong{display:block;font-size:13px;margin-bottom:1px}
  .feature-text span{color:#6b6b85;font-size:12px}

  /* Steps */
  .steps{display:flex;flex-direction:column;gap:12px;margin-bottom:28px}
  .step{display:flex;align-items:flex-start;gap:14px}
  .step-num{width:26px;height:26px;border-radius:50%;background:#7c6fff;color:#fff;
            font-size:12px;font-weight:700;display:flex;align-items:center;
            justify-content:center;flex-shrink:0;margin-top:1px}
  .step-text{font-size:13.5px;padding-top:3px}
  .step-text strong{color:#a78bfa}

  /* Waiting */
  .spinner{width:40px;height:40px;border:3px solid #1e1e2e;border-top-color:#7c6fff;
           border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 20px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .waiting-steps{display:flex;flex-direction:column;gap:8px;
                 background:#13131a;border:1px solid #1e1e2e;
                 border-radius:12px;padding:16px;margin-bottom:16px}
  .waiting-step{display:flex;align-items:flex-start;gap:10px;font-size:13px}
  .waiting-step .num{color:#7c6fff;font-weight:700;flex-shrink:0;width:18px}
  .note{font-size:12px;color:#6b6b85;text-align:center}

  /* Button */
  .btn{display:block;width:100%;padding:13px;border-radius:10px;border:none;
       font-size:14px;font-weight:600;cursor:pointer;text-align:center;
       background:#7c6fff;color:#fff;margin-top:auto;transition:opacity .15s}
  .btn:hover{opacity:.85}
  .btn:active{opacity:.7}
  .btn-muted{background:#13131a;color:#6b6b85;border:1px solid #1e1e2e;margin-top:8px}
</style>
</head>
<body>

<!-- Page 1: Welcome -->
<div id="p-welcome" class="page active">
  <div class="icon">≡</div>
  <h1>Welcome to BB Tracker</h1>
  <p class="sub">Your Blackboard grades and attendance, always visible in your menu bar — updated automatically in the background.</p>
  <div class="features">
    <div class="feature">
      <div class="feature-icon">📊</div>
      <div class="feature-text">
        <strong>Live grades</strong>
        <span>Reads your syllabus weights and shows your real grade per course</span>
      </div>
    </div>
    <div class="feature">
      <div class="feature-icon">📅</div>
      <div class="feature-text">
        <strong>Attendance tracking</strong>
        <span>Shows exactly how many absences you have left before the limit</span>
      </div>
    </div>
    <div class="feature">
      <div class="feature-icon">🔄</div>
      <div class="feature-text">
        <strong>Auto-sync</strong>
        <span>Updates in the background — just click the icon to see your data</span>
      </div>
    </div>
  </div>
  <button class="btn" onclick="showSetup()">Get Started →</button>
</div>

<!-- Page 2: Setup instructions -->
<div id="p-setup" class="page">
  <h1>Setup</h1>
  <p class="sub">Log in to Blackboard once. The session stays active for ~1–3 months — when Microsoft rotates it, BB Tracker will show <strong>🔑 Session expired</strong> and you'll log in again.</p>
  <div class="steps">
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-text">Click the button below — a <strong>browser window</strong> will open</div>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-text">Sign in with your <strong>IE University email and password</strong></div>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-text">Open <strong>Microsoft Authenticator</strong> on your phone and approve the prompt</div>
    </div>
    <div class="step">
      <div class="step-num">4</div>
      <div class="step-text">When asked "Stay signed in?" → click <strong>YES</strong> (very important!)</div>
    </div>
    <div class="step">
      <div class="step-num">5</div>
      <div class="step-text">The <strong>browser closes itself</strong> — you're done, nothing else to do</div>
    </div>
  </div>
  <button class="btn" id="login-btn" onclick="startLogin()">Open Blackboard &amp; Log In →</button>
</div>

<!-- Page 3: Installing browser component -->
<div id="p-installing" class="page">
  <div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center">
    <div class="spinner"></div>
    <h1 style="margin-bottom:10px">Setting up…</h1>
    <p style="color:#6b6b85;font-size:13px">
      Downloading a required browser component.<br>
      This only happens once and takes about a minute.<br>
      Please keep the app open.
    </p>
  </div>
</div>

<!-- Page 4: Waiting for login -->
<div id="p-waiting" class="page">
  <h1 style="margin-bottom:6px">Browser is open</h1>
  <p class="sub">Follow these steps in the browser window:</p>
  <div class="waiting-steps">
    <div class="waiting-step"><span class="num">1</span><span>Sign in with your IE University email and password</span></div>
    <div class="waiting-step"><span class="num">2</span><span>Open <strong>Microsoft Authenticator</strong> on your phone and tap Approve</span></div>
    <div class="waiting-step"><span class="num">3</span><span>When asked <strong>"Stay signed in?"</strong> — click <strong style="color:#4ade80">YES</strong> (this is important)</span></div>
    <div class="waiting-step"><span class="num">4</span><span>The browser will close itself — <strong>do not close it manually</strong></span></div>
  </div>
  <p class="note">⏱ This window closes automatically once you're logged in</p>
</div>

<script>
function showSetup() {
  show('p-setup');
}
function startLogin() {
  window.webkit.messageHandlers.onboarding.postMessage('start_login');
}
function show(id) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}
// Called from Python to advance the UI state
function showInstalling() { show('p-installing'); }
function showWaiting()    { show('p-waiting'); }
</script>
</body>
</html>"""


class _OnboardingHandler(NSObject):
    _callback = None

    def initWithCallback_(self, cb):
        self = objc.super(_OnboardingHandler, self).init()
        if self is None:
            return None
        self._callback = cb
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        if self._callback:
            self._callback(message.body())


class OnboardingWindow:
    """Full-screen first-run onboarding window. Stays open during BB login."""

    def __init__(self, on_complete):
        self._on_complete = on_complete
        self._window = None
        self._webview = None

    def show(self):
        from AppKit import (NSApp, NSColor, NSMakeRect, NSScreen,
                            NSWindow, NSWindowStyleMaskTitled,
                            NSBackingStoreBuffered)
        from WebKit import (WKWebView, WKWebViewConfiguration,
                            WKUserContentController)

        W, H = 480, 520
        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width  - W) / 2
        y = (screen.size.height - H) / 2

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H),
            NSWindowStyleMaskTitled,   # no close button — window controls the flow
            NSBackingStoreBuffered, False,
        )
        win.setTitle_("BB Tracker — Setup")
        win.setReleasedWhenClosed_(False)
        win.setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.039, 0.039, 0.059, 1.0)
        )

        ucc = WKUserContentController.alloc().init()
        self._handler = _OnboardingHandler.alloc().initWithCallback_(self._on_message)
        ucc.addScriptMessageHandler_name_(self._handler, "onboarding")

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)

        wv = WKWebView.alloc().initWithFrame_configuration_(
            win.contentView().bounds(), cfg
        )
        wv.setAutoresizingMask_(18)
        wv.loadHTMLString_baseURL_(_ONBOARDING_HTML, None)
        win.contentView().addSubview_(wv)

        self._window = win
        self._webview = wv
        self._window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def _js(self, code: str) -> None:
        if self._webview:
            self._webview.evaluateJavaScript_completionHandler_(code, None)

    def show_installing(self) -> None:
        self._js("showInstalling()")

    def show_waiting(self) -> None:
        self._js("showWaiting()")

    def close(self) -> None:
        if self._window:
            self._window.close()
            self._window = None
            self._webview = None

    def _on_message(self, body: str) -> None:
        if body == "start_login" and self._on_complete:
            self._on_complete()


# ---------------------------------------------------------------------------
# Update download progress window
# ---------------------------------------------------------------------------

_UPDATE_PROGRESS_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body { margin: 0; padding: 0; height: 100%;
               background: rgb(28, 28, 32);
               color: #f5f5f7;
               font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
               -webkit-font-smoothing: antialiased; }
  .wrap { display: flex; align-items: center; gap: 16px; padding: 18px 20px; }
  .icon { width: 64px; height: 64px; flex: 0 0 64px;
          border-radius: 14px; }
  .info { flex: 1; min-width: 0; }
  .title { font-weight: 600; font-size: 15px; margin-bottom: 10px; }
  .bar { height: 8px; background: rgba(255,255,255,0.12);
         border-radius: 4px; overflow: hidden; }
  .fill { height: 100%; width: 0%;
          background: linear-gradient(90deg, #6d4aff 0%, #8e6dff 100%);
          transition: width 0.15s linear; border-radius: 4px; }
  .row { display: flex; align-items: center; justify-content: space-between;
         margin-top: 10px; font-size: 12px; color: #c7c7cc; }
  .size { font-variant-numeric: tabular-nums; }
  button { font-family: inherit; font-size: 13px; color: #f5f5f7;
           background: rgba(255,255,255,0.12);
           border: 1px solid rgba(255,255,255,0.08);
           border-radius: 6px; padding: 4px 14px; cursor: pointer; }
  button:hover { background: rgba(255,255,255,0.18); }
  button:disabled { opacity: 0.5; cursor: default; }
</style></head>
<body>
  <div class="wrap">
    <img id="icon" class="icon" alt="" src="__ICON_SRC__"/>
    <div class="info">
      <div class="title" id="title">Downloading update…</div>
      <div class="bar"><div class="fill" id="fill"></div></div>
      <div class="row">
        <span class="size" id="size">—</span>
        <button id="cancel" onclick="cancel()">Cancel</button>
      </div>
    </div>
  </div>
<script>
  function fmtMB(bytes) {
    if (!bytes && bytes !== 0) return '—';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }
  function setProgress(done, total) {
    const fill = document.getElementById('fill');
    const size = document.getElementById('size');
    if (total > 0) {
      fill.style.width = Math.min(100, (done / total) * 100) + '%';
      size.textContent = fmtMB(done) + ' of ' + fmtMB(total);
    } else {
      // Unknown total — animate indeterminately by oscillating
      size.textContent = fmtMB(done);
    }
  }
  function setCancelling() {
    const b = document.getElementById('cancel');
    b.disabled = true;
    b.textContent = 'Cancelling…';
    document.getElementById('title').textContent = 'Cancelling…';
  }
  function cancel() {
    window.webkit.messageHandlers.updateProgress.postMessage('cancel');
  }
</script>
</body></html>"""


class _UpdateProgressHandler(NSObject):
    _callback = None

    def initWithCallback_(self, cb):
        self = objc.super(_UpdateProgressHandler, self).init()
        if self is None:
            return None
        self._callback = cb
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        if self._callback:
            self._callback(message.body())


class UpdateProgressWindow:
    """Small floating window that shows DMG download progress, modelled after
    the macOS app-update sheet (icon + title + progress bar + size + Cancel).
    """

    def __init__(self, on_cancel=None):
        self._on_cancel = on_cancel
        self._window = None
        self._webview = None
        self._handler = None
        self._cancelled = False

    def show(self) -> None:
        from AppKit import (NSApp, NSColor, NSMakeRect, NSScreen,
                            NSWindow, NSWindowStyleMaskTitled,
                            NSBackingStoreBuffered)
        from WebKit import (WKWebView, WKWebViewConfiguration,
                            WKUserContentController)

        self._cancelled = False

        W, H = 460, 130
        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width  - W) / 2
        y = (screen.size.height - H) / 2 + 100  # bias upward so it isn't dead-center

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H),
            NSWindowStyleMaskTitled,  # no close button — Cancel is in-window
            NSBackingStoreBuffered, False,
        )
        win.setTitle_("Updating BB Tracker")
        win.setReleasedWhenClosed_(False)
        win.setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.110, 0.110, 0.125, 1.0)
        )

        ucc = WKUserContentController.alloc().init()
        self._handler = _UpdateProgressHandler.alloc().initWithCallback_(self._on_message)
        ucc.addScriptMessageHandler_name_(self._handler, "updateProgress")

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)

        wv = WKWebView.alloc().initWithFrame_configuration_(
            win.contentView().bounds(), cfg
        )
        wv.setAutoresizingMask_(18)

        # Bake the icon into the HTML as a data URL — embedding it via a
        # post-load setIcon() JS call races the page parser and leaves the
        # placeholder empty if eval fires before the function is defined.
        icon_src = ""
        try:
            import base64
            icon_path = BASE_DIR / "app_icon.png"
            if icon_path.exists():
                b64 = base64.b64encode(icon_path.read_bytes()).decode("ascii")
                icon_src = f"data:image/png;base64,{b64}"
        except Exception:
            pass
        html = _UPDATE_PROGRESS_HTML_TEMPLATE.replace("__ICON_SRC__", icon_src)
        wv.loadHTMLString_baseURL_(html, None)
        win.contentView().addSubview_(wv)

        self._window = win
        self._webview = wv
        self._window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def _js(self, code: str) -> None:
        if self._webview:
            self._webview.evaluateJavaScript_completionHandler_(code, None)

    def update_progress(self, done: int, total: int) -> None:
        # Called from worker thread — bounce to main thread for the JS eval
        _on_main(lambda: self._js(f"setProgress({int(done)}, {int(total)})"))

    def show_cancelling(self) -> None:
        _on_main(lambda: self._js("setCancelling()"))

    def is_cancelled(self) -> bool:
        return self._cancelled

    def close(self) -> None:
        def _close():
            if self._window:
                self._window.close()
                self._window = None
                self._webview = None
        _on_main(_close)

    def _on_message(self, body: str) -> None:
        if body == "cancel" and not self._cancelled:
            self._cancelled = True
            self.show_cancelling()
            if self._on_cancel:
                self._on_cancel()


# ---------------------------------------------------------------------------
# URL scheme handler  (bbtracker://activate?key=XXXX-XXXX-XXXX-XXXX)
# ---------------------------------------------------------------------------

def _fcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]

_kInternetEventClass = _fcc("GURL")
_kAEGetURL           = _fcc("GURL")
_keyDirectObject     = _fcc("----")


class _URLEventHandler(NSObject):
    """Receives bbtracker:// URL events from macOS.

    URLs can arrive before the app finishes initialising (e.g. when macOS
    launches the app in response to a link click).  We queue them and drain
    the queue once the callback is wired up.
    """
    _callback = None
    _pending: list = []

    def handleURL_withReply_(self, event, reply):
        try:
            url_str = event.paramDescriptorForKeyword_(_keyDirectObject).stringValue()
            if self._callback:
                self._callback(url_str)
            else:
                _URLEventHandler._pending.append(url_str)
        except Exception as exc:
            print(f"[url-scheme] {exc}")

    @classmethod
    def set_callback(cls, obj, callback):
        obj._callback = callback
        for url in cls._pending:
            callback(url)
        cls._pending.clear()


# ---------------------------------------------------------------------------
# Timer that fires in NSRunLoopCommonModes (i.e. also while menus are open)
# ---------------------------------------------------------------------------

from Foundation import NSTimer, NSRunLoop

class _TimerTarget(NSObject):
    """Minimal NSObject that receives NSTimer callbacks."""
    _callback = None

    def timerFired_(self, _timer):
        if self._callback:
            self._callback(None)


class _CommonModeTimer:
    """Schedules an NSTimer in NSRunLoopCommonModes (fires during menu tracking).

    Call start() to arm and stop() to disarm — NSTimer cannot be restarted after
    invalidation, so stop()/start() recreates the underlying timer each time.
    """

    def __init__(self, interval: float, callback):
        self._interval = interval
        self._target = _TimerTarget.alloc().init()
        self._target._callback = callback
        self._ns_timer = None

    def start(self):
        if self._ns_timer is not None:
            return
        self._ns_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            self._interval, self._target, b"timerFired:", None, True
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(self._ns_timer, "NSRunLoopCommonModes")

    def stop(self):
        if self._ns_timer is not None:
            self._ns_timer.invalidate()
            self._ns_timer = None


class _MenuDelegate(NSObject):
    """NSMenu delegate that rebuilds the menu fresh just before it's shown.

    Without this, AppKit caches the items added at first menu-open. While a
    background scrape runs we can update individual item titles in place
    (works), but full rebuilds (e.g. when the scrape completes and we want
    to swap "Starting browser…" for the actual course list) don't visibly
    refresh until the menu is closed and reopened.

    Setting an NSMenu delegate that implements menuNeedsUpdate_ tells AppKit
    to call us back every time the menu is about to display — at which point
    we rebuild against current state.
    """
    _on_open = None

    def menuNeedsUpdate_(self, _menu):
        cb = self._on_open
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:
            try:
                from log import log
                log.exc("menu", "menuNeedsUpdate callback crashed", exc)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Pricing window
# ---------------------------------------------------------------------------

_PRICING_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0a0a0f;color:#e8e8f0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;padding:28px 24px 24px;user-select:none}
  h2{font-size:20px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px}
  .sub{color:#6b6b85;font-size:13px;margin-bottom:24px}
  .plans{display:flex;gap:12px}
  .plan{flex:1;background:#13131a;border:1px solid #1e1e2e;border-radius:14px;
        padding:18px 16px;display:flex;flex-direction:column;gap:10px;transition:border-color .15s}
  .plan.featured{border-color:#7c6fff}
  .badge{background:#7c6fff;color:#fff;font-size:10px;font-weight:700;letter-spacing:.06em;
         text-transform:uppercase;padding:2px 8px;border-radius:100px;align-self:flex-start}
  .plan-name{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6b6b85}
  .price{font-size:26px;font-weight:800;letter-spacing:-.03em}
  .price span{font-size:13px;font-weight:400;color:#6b6b85}
  .desc{font-size:12px;color:#6b6b85}
  .btn{display:block;width:100%;padding:9px;border-radius:9px;border:none;font-size:13px;
       font-weight:600;cursor:pointer;text-align:center;margin-top:auto;transition:opacity .15s}
  .btn:hover{opacity:.85}
  .btn-primary{background:#7c6fff;color:#fff}
  .btn-outline{background:transparent;color:#e8e8f0;border:1px solid #1e1e2e}
  .footer{text-align:center;color:#6b6b85;font-size:11.5px;margin-top:16px}
  .footer a{color:#a78bfa;text-decoration:none;cursor:pointer}
</style>
</head>
<body>
<h2>Upgrade BB Tracker</h2>
<p class="sub">Your 7-day trial has ended. Pick a plan to keep your grades in your menu bar.</p>
<div class="plans">
  <div class="plan">
    <div class="plan-name">Monthly</div>
    <div class="price">€3<span>/mo</span></div>
    <div class="desc">Cancel anytime</div>
    <button class="btn btn-outline" onclick="buy('monthly')">Get Monthly</button>
  </div>
  <div class="plan featured">
    <div class="badge">Best value</div>
    <div class="plan-name">Yearly</div>
    <div class="price">€30<span>/yr</span></div>
    <div class="desc">Save 17% — €2.50/mo</div>
    <button class="btn btn-primary" onclick="buy('yearly')">Get Yearly</button>
  </div>
  <div class="plan">
    <div class="plan-name">Lifetime</div>
    <div class="price">€100<span> once</span></div>
    <div class="desc">Pay once, use forever</div>
    <button class="btn btn-outline" onclick="buy('lifetime')">Get Lifetime</button>
  </div>
</div>
<p class="footer">
  Already have a key?
  <a onclick="window.webkit.messageHandlers.pricing.postMessage('activate')">Enter license key →</a>
</p>
<script>
function buy(plan) {
  window.webkit.messageHandlers.pricing.postMessage('buy:' + plan);
}
</script>
</body>
</html>"""


class PricingWindow:
    """Full-screen upgrade prompt shown when trial expires."""

    def __init__(self, on_activate):
        self._on_activate = on_activate  # called when user clicks "Enter license key"
        self._window = None

    def show(self):
        from AppKit import NSApp, NSColor, NSMakeRect, NSObject, NSScreen
        from AppKit import NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable
        from AppKit import NSBackingStoreBuffered
        from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

        if self._window and self._window.isVisible():
            self._window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
            return

        W, H = 500, 330
        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width  - W) / 2
        y = (screen.size.height - H) / 2

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        win.setTitle_("BB Tracker — Upgrade")
        win.setReleasedWhenClosed_(False)
        win.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.039, 0.039, 0.059, 1.0))

        ucc = WKUserContentController.alloc().init()
        self._handler = _PricingHandler.alloc().initWithCallback_(self._on_message)
        ucc.addScriptMessageHandler_name_(self._handler, "pricing")

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)

        wv = WKWebView.alloc().initWithFrame_configuration_(win.contentView().bounds(), cfg)
        wv.setAutoresizingMask_(18)
        wv.loadHTMLString_baseURL_(_PRICING_HTML, None)
        win.contentView().addSubview_(wv)

        self._window = win
        self._window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def _on_message(self, body: str):
        if body == "activate":
            if self._window:
                self._window.close()
            if self._on_activate:
                self._on_activate()
        elif body.startswith("buy:"):
            plan = body[4:]
            urls = {"monthly": STORE_MONTHLY, "yearly": STORE_YEARLY, "lifetime": STORE_LIFETIME}
            subprocess.run(["open", urls.get(plan, STORE_YEARLY)])


class _PricingHandler(NSObject):
    _callback = None

    def initWithCallback_(self, cb):
        self = objc.super(_PricingHandler, self).init()
        if self is None:
            return None
        self._callback = cb
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        if self._callback:
            self._callback(message.body())


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class BBTrackerApp(rumps.App):
    def __init__(self):
        super().__init__("", icon=ICON_PATH, template=True, quit_button=None)
        self._data = load_data()
        self._weights_map = load_weights_map()
        self._refreshing = False
        self._pending_op: str | None = None   # "scrape" or "discover", queued while busy
        self._cancel_flag = threading.Event()  # set to interrupt the running scrape early
        self._status: str = "Refreshing"
        self._progress: tuple[int, int] | None = None
        self._last_rendered_ts = None
        self._config_dirty = False
        self._progress_item: rumps.MenuItem | None = None  # live-updated in place
        self._update_info: dict | None = None
        self._license = _lic.get_status()
        self._prev_over_limit: set[str] = set()  # courses already notified as over-limit
        self._cfg_max_absence_pct: float = 0.20  # refreshed each _render_menu
        self._main_win = MainWindow(
            on_change=self._on_settings_change,
            on_refresh=self._start_scrape,
            on_sync=self._start_discover,
            on_reveal_log=lambda: subprocess.run(["open", "-R", log.path]),
            on_self_update=self._start_self_update,
        )
        self._pricing_win     = PricingWindow(on_activate=self._open_license_settings)
        self._onboarding_win  = OnboardingWindow(on_complete=self._on_onboarding_login)

        # Register bbtracker:// URL scheme handler (drains any URLs queued at launch)
        self._url_handler = _URLEventHandler.alloc().init()
        _URLEventHandler.set_callback(self._url_handler, self._handle_url)
        NSAppleEventManager.sharedAppleEventManager()\
            .setEventHandler_andSelector_forEventClass_andEventID_(
                self._url_handler,
                objc.selector(
                    _URLEventHandler.handleURL_withReply_,
                    signature=b"v@:@@",
                ),
                _kInternetEventClass,
                _kAEGetURL,
            )

        self._render_menu()

        # Install NSMenu delegate so the menu rebuilds against current state
        # every time the user opens it. This is the canonical fix for the
        # "menu shows stale 'Starting browser…' after scrape finished" bug —
        # AppKit caches displayed items, so we need to be told *when* to refresh.
        try:
            self._menu_delegate = _MenuDelegate.alloc().init()
            self._menu_delegate._on_open = self._on_menu_will_open
            self.menu._menu.setDelegate_(self._menu_delegate)
            log("startup", "NSMenu delegate installed (rebuild-on-open)")
        except Exception as exc:
            log.exc("startup", "could not install menu delegate", exc)

        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        interval = max(60, int(cfg.get("refresh_interval_minutes", 120)) * 60)
        self._scrape_interval = interval

        # Common-mode timer — fires even while the menu is open.
        # Only active while a scrape is running; started/stopped around each
        # scrape so it doesn't burn CPU/disk at rest.
        self._ui_timer = _CommonModeTimer(0.5, self._tick_ui)
        self._scrape_timer = rumps.Timer(self._tick_scrape, interval)
        self._scrape_timer.start()

        # Use rumps.Timer for deferred UI so callbacks fire on the main thread
        first_run = self._is_first_run()
        log("startup", "App launched · version=%s · first_run=%s · license=%s",
            APP_VERSION, first_run, self._license.get("state"))

        # Auto-enable "Open at login" on the very first launch only. The flag
        # ensures we never re-enable after the user has explicitly turned it
        # off in Settings — only on the truly-first run does default-on apply.
        try:
            cfg = load_config()
            if not cfg.get("login_item_setup_done"):
                ok = login_item.enable()
                cfg["login_item_setup_done"] = True
                save_config(cfg)
                log("startup", "login-item default-enabled (ok=%s)", ok)
        except Exception as exc:
            log.exc("startup", "login-item setup failed", exc)

        # One-time migration: older plists had RunAtLoad=true which caused the
        # app to reopen seconds after the user quit. Re-write the plist without
        # RunAtLoad and reload it via launchctl so the fix takes effect without
        # requiring the user to toggle the setting off and on.
        try:
            cfg = load_config()
            if not cfg.get("login_item_no_run_at_load_done") and login_item.is_enabled():
                login_item.enable()   # rewrites plist + bootout/bootstrap
                cfg["login_item_no_run_at_load_done"] = True
                save_config(cfg)
                log("startup", "login-item RunAtLoad migration applied")
        except Exception as exc:
            log.exc("startup", "login-item migration failed", exc)

        # One-time migration: pre-1.0.14 builds hardcoded total_sessions=30
        # for every course, which produced wrong "X absences left" numbers
        # on lab/seminar courses. We now read the count from each syllabus.
        # Clear legacy stored values so the next syllabus sync repopulates
        # them; until then the menu shows a percentage instead of an
        # invented "X left". Marker is in config.json so the migration
        # runs exactly once per Mac.
        try:
            cfg = load_config()
            if not cfg.get("session_schema_v2_done") and COURSES_PATH.exists():
                courses = json.loads(COURSES_PATH.read_text())
                cleared = 0
                for c in courses:
                    if c.get("total_sessions") is not None:
                        c["total_sessions"] = None
                        cleared += 1
                if cleared:
                    COURSES_PATH.write_text(json.dumps(courses, indent=2))
                cfg["session_schema_v2_done"] = True
                save_config(cfg)
                log("startup", "session schema v2: cleared %d legacy total_sessions values", cleared)
        except Exception as exc:
            log.exc("startup", "session schema migration failed", exc)

        if first_run:
            log("startup", "→ scheduling onboarding window")
            self._startup_timer = rumps.Timer(self._on_startup_tick, 1.5)
            self._startup_timer.start()
        elif self._license["state"] in ("active", "trial", "grace"):
            # Self-heal for users whose onboarding was interrupted before
            # discover ran: if browser_data exists but courses.json is empty,
            # kick off discover instead of an immediate scrape (which would
            # bail with no_courses forever).
            try:
                has_courses = len(load_courses()) > 0
            except Exception:
                has_courses = False
            if has_courses:
                log("startup", "→ auto-starting scrape (license ok)")
                self._start_scrape()
            else:
                log("startup", "→ no courses on disk, auto-discovering")
                self._start_discover()
        else:
            log("startup", "→ scheduling paywall (license=%s)", self._license.get("state"))
            self._startup_timer = rumps.Timer(self._on_startup_tick, 1.5)
            self._startup_timer.start()

        self._check_update()  # on startup
        # Re-check every 24 hours so long-running instances get notified too
        self._update_check_timer = rumps.Timer(
            lambda _: self._check_update(), 86400
        )
        self._update_check_timer.start()

    def _on_startup_tick(self, sender) -> None:
        sender.stop()
        if self._is_first_run():
            self._onboarding_win.show()
        else:
            self._show_paywall()

    # ----- first-run -----

    def _is_first_run(self) -> bool:
        """True if no Blackboard session has been saved yet."""
        from scraper import BROWSER_DIR
        exists = BROWSER_DIR.exists()
        has_content = exists and any(BROWSER_DIR.iterdir())
        result = not exists or not has_content
        log("first_run", "BROWSER_DIR=%s exists=%s has_content=%s → first_run=%s",
            BROWSER_DIR, exists, has_content, result)
        return result

    def _ensure_browser_installed(self) -> bool:
        """Install Playwright's Chromium if not already present. Returns True on success."""
        try:
            # In a PyInstaller bundle sys.executable is the app binary, not Python.
            # Use the Node driver that Playwright bundles instead.
            node = BASE_DIR / "playwright" / "driver" / "node"
            cli  = BASE_DIR / "playwright" / "driver" / "package" / "cli.js"
            log("install", "ensure_browser_installed: node=%s (exists=%s) cli=%s (exists=%s)",
                node, node.exists(), cli, cli.exists())
            if node.exists() and cli.exists():
                log("install", "running: node cli.js install chromium (timeout=300s)")
                result = subprocess.run(
                    [str(node), str(cli), "install", "chromium"],
                    capture_output=True, timeout=300,
                    env={**__import__("os").environ},
                )
                log("install", "node install rc=%d  stdout=%r  stderr=%r",
                    result.returncode,
                    result.stdout[-500:].decode("utf-8", "replace") if result.stdout else "",
                    result.stderr[-500:].decode("utf-8", "replace") if result.stderr else "")
                return result.returncode == 0
            # Dev / venv mode: use the Python interpreter directly
            log("install", "fallback: %s -m playwright install chromium", sys.executable)
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, timeout=300,
            )
            log("install", "pip install rc=%d", result.returncode)
            return result.returncode == 0
        except Exception as exc:
            log.exc("install", "ensure_browser_installed failed", exc)
            return False

    def _on_onboarding_login(self) -> None:
        """Called from OnboardingWindow when user clicks 'Open Blackboard & Log In'."""
        import sys as _sys
        log("onboard", "▶ user clicked 'Open Blackboard & Log In' (frozen=%s)",
            bool(getattr(_sys, "frozen", None)))

        def _run():
            try:
                if getattr(_sys, "frozen", None):
                    log("onboard", "showing installing screen")
                    self._onboarding_win.show_installing()
                    ok = self._ensure_browser_installed()
                    if not ok:
                        log("onboard", "✗ browser install failed — closing onboarding")
                        self._onboarding_win.close()
                        rumps.notification(
                            "BB Tracker", "Setup failed",
                            "Could not download the browser component. "
                            "Check your internet connection and reopen the app.",
                        )
                        return
                log("onboard", "showing waiting screen, calling _start_login")
                self._onboarding_win.show_waiting()
                self._start_login(close_onboarding=True)
            except Exception as exc:
                log.exc("onboard", "_on_onboarding_login crashed", exc)

        threading.Thread(target=_run, daemon=True, name="onboard").start()

    # ----- background workers -----

    def _progress_label(self) -> str:
        label = self._status or "Refreshing"
        if self._progress and self._progress[1] > 0:
            done, total = self._progress
            return f"⏳  {label}… ({done}/{total})"
        return f"⏳  {label}…"

    def _on_settings_change(self) -> None:
        """Called from the WKWebView message handler (main thread) when config changes."""
        # Re-read config and re-render immediately — we're on the main thread here
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            # Update scrape timer interval if it changed
            new_interval = max(60, int(cfg.get("refresh_interval_minutes", 120)) * 60)
            if not hasattr(self, "_scrape_interval") or self._scrape_interval != new_interval:
                self._scrape_interval = new_interval
                self._scrape_timer.stop()
                self._scrape_timer = rumps.Timer(self._tick_scrape, new_interval)
                self._scrape_timer.start()
        except Exception:
            pass
        self._render_menu()

    def _handle_url(self, url: str) -> None:
        """Handle bbtracker://activate?key=XXXX-XXXX-XXXX-XXXX deep links."""
        try:
            parsed = _urlparse(url)
            if parsed.scheme != "bbtracker" or parsed.netloc != "activate":
                return
            key = _parse_qs(parsed.query).get("key", [""])[0].strip()
            if not key:
                return
            ok, err = _lic.activate(key)
            self._license = _lic.get_status()
            self._config_dirty = True
            self._tick_ui(None)
            if ok:
                rumps.notification(
                    "BB Tracker", "License activated ✓",
                    "Thanks for your purchase — enjoy BB Tracker!",
                )
            else:
                rumps.alert(
                    title="License activation failed",
                    message=err or "Could not activate this key. Check your internet and try again.",
                    ok="OK",
                )
        except Exception as exc:
            print(f"[url-scheme] handle error: {exc}")

    def _notify_grade_changes(self, new_data: dict) -> None:
        """Fire emergency "over the absence limit" notifications.

        Grade-change and approaching-limit notifications are handled by
        notifier.process_changes() in the scrape worker, which has access
        to the full before/after diff.  This method only handles the distinct
        case of a course going *past* the limit — a separate, higher-urgency
        event the user should see even if they disabled normal notifications.
        """
        if not new_data:
            return
        for course in (new_data.get("courses") or []):
            name      = course.get("name", "")
            remaining = course.get("absences_remaining")
            if remaining is not None and remaining < 0 and name not in self._prev_over_limit:
                self._prev_over_limit.add(name)
                over = abs(remaining)
                rumps.notification(
                    "BB Tracker — Absence Alert",
                    _smart_title(name),
                    f"You are {over:g} absence{'s' if over != 1 else ''} over the limit!",
                )
            elif remaining is not None and remaining >= 0:
                self._prev_over_limit.discard(name)

    def _set_status(self, label: str, done: int, total: int) -> None:
        # Called from background thread — only update state; _tick_ui renders on main thread
        self._status = label
        self._progress = (done, total) if total > 0 else None

    def _run_pending(self) -> None:
        """Called on the main thread after any op finishes to drain the queue."""
        op, self._pending_op = self._pending_op, None
        if op == "scrape":
            self._start_scrape()
        elif op == "discover":
            self._start_discover()

    def _start_scrape(self) -> None:
        if self._refreshing:
            log("scrape", "queued: will run after current op")
            self._pending_op = "scrape"
            return
        log("scrape", "▶ start_scrape")
        self._cancel_flag.clear()
        self._refreshing = True
        self._ui_timer.start()
        # Show something specific straight away — the user clicked Refresh and
        # the worker thread can take a beat before the first on_status fires.
        self._status = "Starting browser"
        self._progress = None
        _on_main(lambda: self._main_win.set_sync_state(True))

        def worker():
            # Snapshot old data BEFORE the scrape so we can diff afterwards.
            old_data = load_data()
            old_anns = load_announcements()
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(run_scrape(
                    on_progress=None,
                    on_status=self._set_status,
                    is_cancelled=self._cancel_flag.is_set,
                ))
                loop.close()
                log("scrape", "✓ run_scrape returned")
            except Exception as exc:
                log.exc("scrape", "worker crashed", exc)
            finally:
                self._refreshing = False
                self._status = "Refreshing"
                self._progress = None
                self._ui_timer.stop()
                _on_main(lambda: self._main_win.set_sync_state(False))
                self._data = load_data()
                # Fire change-detection notifications (grade changes,
                # new announcements, attendance warnings).  Runs on this
                # background thread so AppKit's main thread isn't blocked.
                try:
                    cfg = load_config()
                    new_anns = load_announcements()
                    _notifier.process_changes(old_data, self._data, old_anns, new_anns, cfg)
                except Exception as exc:
                    log.exc("scrape", "notifier failed", exc)
                self._config_dirty = True
                _on_main(lambda: self._tick_ui(None))
                _on_main(self._run_pending)
                self._check_update()
                if self._data and self._data.get("error"):
                    log("scrape", "finished with error=%s", self._data.get("error"))

        threading.Thread(target=worker, daemon=True, name="scrape").start()
        self._render_menu()

    def _check_update(self) -> None:
        """Fetch version.json and set _update_info if a newer version exists.
        Runs on a background thread — sets _config_dirty so _tick_ui re-renders
        on the main thread instead of touching the menu directly.
        """
        import ssl
        import urllib.request
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = ssl.create_default_context()

        def _v(s: str) -> tuple:
            try:
                return tuple(int(x) for x in s.split("."))
            except Exception:
                return (0,)

        def _fetch():
            try:
                with urllib.request.urlopen(VERSION_URL, timeout=8, context=ctx) as r:
                    data = json.loads(r.read())
                if _v(data.get("version", "0")) > _v(APP_VERSION):
                    self._update_info = data
                    self._config_dirty = True
                    _on_main(lambda: self._tick_ui(None))
            except Exception:
                pass

        threading.Thread(target=_fetch, daemon=True).start()

    def _start_login(self, close_onboarding: bool = False) -> None:
        if self._refreshing:
            log("login", "skipped: already refreshing")
            return
        log("login", "▶ start_login (close_onboarding=%s)", close_onboarding)
        self._refreshing = True
        self._ui_timer.start()
        self._status = "Waiting for login"
        self._progress = None
        _on_main(lambda: self._main_win.set_sync_state(True))

        def worker():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                log("login", "calling run_login()")
                ok = loop.run_until_complete(run_login())
                log("login", "run_login → %s", ok)
                if close_onboarding:
                    log("login", "closing onboarding window")
                    _on_main(self._onboarding_win.close)
                if ok:
                    log("login", "calling run_discover()")
                    discovered = loop.run_until_complete(
                        run_discover(on_status=self._set_status)
                    )
                    log("login", "run_discover → %s",
                        f"{len(discovered)} courses" if discovered else "None (auth)")
                    if discovered is not None:
                        self._weights_map = load_weights_map()
                        log("login", "calling run_scrape()")
                        loop.run_until_complete(run_scrape(
                            on_progress=None,
                            on_status=self._set_status,
                        ))
                        log("login", "run_scrape returned")
                loop.close()
            except Exception as exc:
                log.exc("login", "worker crashed", exc)
            finally:
                self.title = ""
                self._refreshing = False
                self._status = "Refreshing"
                self._progress = None
                self._ui_timer.stop()
                self._data = load_data()
                self._config_dirty = True
                _on_main(lambda: self._tick_ui(None))
                _on_main(self._run_pending)
                log("login", "✓ worker done")
                _on_main(lambda: self._main_win.set_sync_state(False))
                try:
                    n = len(load_courses())
                    if n > 0:
                        rumps.notification(
                            "BB Tracker", "Courses synced",
                            f"{n} course{'s' if n != 1 else ''} loaded from Blackboard.",
                        )
                except Exception as exc:
                    log.exc("login", "post-login notification failed", exc)

        threading.Thread(target=worker, daemon=True, name="login").start()
        self._render_menu()

    def _start_discover(self) -> None:
        if self._refreshing:
            log("discover", "interrupting current op — queued to run next")
            self._cancel_flag.set()   # signal the running scrape to stop between courses
            self._pending_op = "discover"
            return
        log("discover", "▶ start_discover")
        self._refreshing = True
        self._ui_timer.start()
        self._status = "Discovering courses"
        self._progress = None
        _on_main(lambda: self._main_win.set_sync_state(True))

        def worker():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(
                    run_discover(on_status=self._set_status)
                )
                log("discover", "run_discover → %s",
                    f"{len(result)} courses" if result else "None (auth)")
                # run_discover returns None on auth failure — don't overwrite error
                if result is not None:
                    self._weights_map = load_weights_map()
                    loop.run_until_complete(run_scrape(
                        on_progress=None,
                        on_status=self._set_status,
                    ))
                    log("discover", "run_scrape returned")
                loop.close()
            except Exception as exc:
                log.exc("discover", "worker crashed", exc)
            finally:
                self._weights_map = load_weights_map()
                self._refreshing = False
                self._status = "Refreshing"
                self._progress = None
                self._ui_timer.stop()
                self._data = load_data()
                self._config_dirty = True
                _on_main(lambda: self._tick_ui(None))
                _on_main(self._run_pending)
                log("discover", "✓ worker done")
                _on_main(lambda: self._main_win.set_sync_state(False))
                try:
                    n = len(load_courses())
                    if n > 0:
                        rumps.notification(
                            "BB Tracker", "Courses synced",
                            f"{n} course{'s' if n != 1 else ''} loaded from Blackboard.",
                        )
                except Exception as exc:
                    log.exc("discover", "post-discover notification failed", exc)

        threading.Thread(target=worker, daemon=True).start()
        self._render_menu()

    def _tick_scrape(self, _sender) -> None:
        # Re-read license in case it was activated/expired since last check
        self._license = _lic.get_status()
        log("timer", "scheduled tick fired · license=%s", self._license.get("state"))
        if self._license["state"] in ("active", "trial", "grace"):
            self._start_scrape()
        else:
            # Trial/subscription expired — stop the timer and show upgrade prompt
            _sender.stop()
            self._render_menu()
            self._show_paywall()

    # ----- UI polling -----

    def _on_menu_will_open(self) -> None:
        """Called by NSMenu delegate immediately before the menu is displayed.

        Pull the latest data + license state and rebuild from scratch, so the
        user always sees current state — even if a scrape just finished or
        the license just expired while the icon sat there idle.
        """
        try:
            self._data = load_data()
            self._weights_map = load_weights_map()
            self._license = _lic.get_status()
            new_ts = self._data.get("timestamp") if self._data else None
            self._last_rendered_ts = new_ts
            self._last_spin     = self._refreshing
            self._last_progress = self._progress
            self._last_status   = self._status
            self._config_dirty  = False
            self._render_menu()
        except Exception as exc:
            log.exc("menu", "menu rebuild on-open failed", exc)

    def _tick_ui(self, _sender) -> None:
        new_data = load_data()
        new_ts    = new_data.get("timestamp") if new_data else None
        ts_changed       = new_ts != self._last_rendered_ts
        spin_changed     = self._refreshing != getattr(self, "_last_spin", None)
        progress_changed = (self._progress != getattr(self, "_last_progress", None) or
                            self._status   != getattr(self, "_last_status", None))

        if not (ts_changed or spin_changed or progress_changed or self._config_dirty):
            return

        # ── Stale-menu bridge ───────────────────────────────────────────────
        # macOS / NSMenu doesn't visibly redraw a menu that's already open
        # when items are added/removed. So when a scrape finishes WHILE the
        # menu is on-screen, full re-render does nothing the user can see.
        # An item TITLE change DOES update live — so when refresh ends with
        # the progress_item still on screen, we hijack it with actionable
        # text instead of letting it remain frozen on "Starting browser…".
        if (spin_changed and not self._refreshing
                and self._progress_item is not None):
            err = (new_data or {}).get("error") if new_data else None
            if err:
                self._progress_item.title = f"⚠️  {err} — close & reopen menu"
            else:
                n = len((new_data or {}).get("courses") or [])
                self._progress_item.title = (
                    f"✓ Updated {n} course{'s' if n != 1 else ''}"
                    f"  —  close & reopen menu to view"
                )
            self._last_spin = self._refreshing
            self._last_progress = self._progress
            self._last_status = self._status
            # Defer full rebuild to the next menuWillOpen — once the user
            # closes the menu and reopens, _render_menu runs against fresh data.
            self._config_dirty = True
            return

        # If only progress/status changed and we have a live item reference,
        # update just that item's title — no full menu rebuild, no flicker.
        if progress_changed and not ts_changed and not spin_changed and not self._config_dirty:
            if self._progress_item is not None:
                self._progress_item.title = self._progress_label()
                self._last_progress = self._progress
                self._last_status   = self._status
                return

        # Full rebuild needed
        if ts_changed and new_data:
            self._notify_grade_changes(new_data)
        self._data = new_data
        self._weights_map = load_weights_map()
        self._last_rendered_ts = new_ts
        self._last_spin     = self._refreshing
        self._last_progress = self._progress
        self._last_status   = self._status
        self._config_dirty  = False
        self._render_menu()

    # ----- menu building -----

    def _render_menu(self) -> None:
        data = self._data
        self._progress_item = None  # cleared on every full rebuild
        self.menu.clear()

        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        hidden_courses = cfg.get("hidden_courses", [])
        course_display_modes = cfg.get("course_display_modes", {})
        # Cached for _course_submenu — used to colour percentage-only attendance.
        self._cfg_max_absence_pct = float(cfg.get("max_absence_pct", 0.20))

        self.menu.add(rumps.MenuItem("Open BB Tracker", callback=self._on_open_app))
        self.menu.add(rumps.separator)

        # Trial countdown banner
        lic = self._license
        if lic["state"] == "trial" and lic.get("days_left") is not None:
            d = lic["days_left"]
            label = f"🕐  Free trial — {d} day{'s' if d != 1 else ''} left"
            self.menu.add(rumps.MenuItem(label, callback=self._on_buy))
            self.menu.add(rumps.separator)
        elif lic["state"] in ("expired", "no_license"):
            self.menu.add(rumps.MenuItem("🔒  Subscription expired — click to renew",
                                         callback=self._on_buy))
            self.menu.add(rumps.separator)

        # Update banner (shown at very top when available)
        if self._update_info:
            v = self._update_info.get("version", "")
            self.menu.add(rumps.MenuItem(
                f"⬆  Update available — v{v}",
                callback=self._on_update,
            ))
            self.menu.add(rumps.separator)

        if self._refreshing:
            self._progress_item = _item(self._progress_label())
            self.menu.add(self._progress_item)
        elif data and data.get("error") == "auth_required":
            self.menu.add(rumps.MenuItem("🔑  Session expired — click to re-login",
                                         callback=self._on_relogin))
        elif data and data.get("error") == "no_courses":
            self.menu.add(_item("⚠️  No courses — click Sync below"))
        elif data and data.get("error") == "browser_launch_timeout":
            self.menu.add(_item("⚠️  Browser couldn't start — click Quit, then re-open"))
            self.menu.add(_item("(usually means an old BB Tracker is still running)"))
        elif data and data.get("error"):
            self.menu.add(_item(f"⚠️  {data['error']}"))

        # ── Today summary ─────────────────────────────────────────────────────
        # Show a single compact "what needs attention today" line instead of
        # burying action items inside per-course submenus.
        if data and not data.get("error") and not self._refreshing:
            summary_parts = []
            try:
                from datetime import date as _date
                today_str = _date.today().isoformat()
                anns = load_announcements()
                action_anns = [a for a in anns if a.get("action_required")
                               and (a.get("created") or "")[:10] >= today_str[:7]]
                if action_anns:
                    n = len(action_anns)
                    summary_parts.append(f"{n} announcement{'s' if n>1 else ''} need attention")
                # Count courses with low attendance
                courses_data = data.get("courses") or []
                hidden = set(cfg.get("hidden_courses", []))
                warn_att = [c["name"] for c in courses_data
                            if c["name"] not in hidden
                            and c.get("absences_remaining") is not None
                            and c.get("absences_remaining") <= 2]
                if warn_att:
                    n = len(warn_att)
                    summary_parts.append(f"{n} course{'s' if n>1 else ''} near absence limit")
            except Exception:
                pass
            if summary_parts:
                self.menu.add(_item("⚠  " + " · ".join(summary_parts)))
                self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("Refresh now", callback=self._on_refresh))
        self.menu.add(rumps.MenuItem("Open Blackboard", callback=self._on_open_bb))

        # Help & Feedback — sub-menu so each category produces an email tailored
        # to that issue. Avoids dumping every piece of debug info into a generic
        # "Send feedback" mailto.
        help_menu = rumps.MenuItem("Help & Feedback")
        help_menu.add(rumps.MenuItem(
            "Send general feedback",
            callback=lambda _: self._send_feedback("general")))
        help_menu.add(rumps.separator)
        help_menu.add(rumps.MenuItem(
            "Report missing grades or attendance",
            callback=lambda _: self._send_feedback("missing_data")))
        help_menu.add(rumps.MenuItem(
            "Report a crash or app not launching",
            callback=lambda _: self._send_feedback("crash")))
        help_menu.add(rumps.MenuItem(
            "Report a login / session issue",
            callback=lambda _: self._send_feedback("login")))
        help_menu.add(rumps.MenuItem(
            "Report a license or payment issue",
            callback=lambda _: self._send_feedback("license")))
        help_menu.add(rumps.MenuItem(
            "Other issue",
            callback=lambda _: self._send_feedback("other")))
        self.menu.add(help_menu)

        self.menu.add(rumps.MenuItem("Settings…", callback=self._on_settings))
        if self._license["state"] in ("expired", "no_license"):
            self.menu.add(rumps.MenuItem("Enter license key", callback=self._on_enter_license))
        self.menu.add(rumps.separator)

        visible_courses = [
            c for c in (data.get("courses") or [])
            if c["name"] not in hidden_courses
        ] if data else []

        if visible_courses:
            for c in visible_courses:
                mode = course_display_modes.get(c["name"], "both")
                self.menu.add(self._course_submenu(c, mode))
        elif data and data.get("courses") and hidden_courses:
            self.menu.add(_item("All courses hidden — check Settings"))
        else:
            self.menu.add(_item("No data yet — click Refresh"))

        self.menu.add(rumps.separator)
        ts = data.get("timestamp") if data else None
        self.menu.add(_item(f"Last updated: {_format_ts(ts)}"))
        self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))


    def _course_submenu(self, c: dict, display_mode: str = "both") -> rumps.MenuItem:
        name = _smart_title(c["name"])

        # Three attendance modes:
        #   1. session-level w/ known total → use absences_remaining ("X left")
        #   2. percentage-only (QWAttendance gradebook column or native
        #      counts but syllabus didn't disclose total sessions) → show %
        #   3. nothing tracked → status "n/a"
        att_pct = c.get("overall_score")
        present = c.get("present") or 0
        late    = c.get("late") or 0
        absent  = c.get("absent") or 0
        excused = c.get("excused") or 0
        recorded = present + late + absent + excused
        # If the syllabus didn't say how many sessions there are, fall
        # back to "% attended so far" computed from recorded sessions —
        # better than inventing an "X left" we can't justify.
        if att_pct is None and c.get("max_absences") is None and recorded > 0:
            att_pct = (present + late) / recorded * 100
        is_pct_only = (
            c.get("absences_remaining") is None
            and att_pct is not None
        )
        if is_pct_only:
            max_absence_pct = float(self._cfg_max_absence_pct or 0.20)
            att_emoji, att_label = _att_status_from_pct(att_pct, max_absence_pct)
            att_show = True
        else:
            att_emoji, att_label = _att_status(c.get("absences_remaining"))
            att_show = c.get("absences_remaining") is not None

        if display_mode != "none":
            grades = c.get("grades") or {}
            flat_weights = self._weights_map.get(c["name"], {})
            weighted_overall, _, _coverage = build_grade_categories(
                grades.get("assignments") or [], flat_weights
            )
            grade_pct = weighted_overall if weighted_overall is not None else grades.get("overall_pct")
            inline_parts = []
            if display_mode in ("both", "grade") and grade_pct is not None:
                # Tag as "so far" when we know less than ~95% of the course
                # has graded items — keeps the inline number from being
                # mistaken for a final grade.
                wt_total = _coverage.get("weight_total") or 0
                wt_graded = _coverage.get("weight_graded") or 0
                partial = wt_total > 0 and wt_graded < wt_total * 0.95
                inline_parts.append(f"{grade_pct:.0f}% so far" if partial else f"{grade_pct:.0f}%")
            if display_mode in ("both", "attendance") and att_show:
                inline_parts.append(att_label)
            suffix = f"  —  {' · '.join(inline_parts)}" if inline_parts else ""
            title = f"{att_emoji}  {name}{suffix}"
        else:
            title = f"{att_emoji}  {name}"

        parent = rumps.MenuItem(title)

        grades = c.get("grades") or {}
        assignments = grades.get("assignments") or []
        grade_error = grades.get("error")

        flat_weights = self._weights_map.get(c["name"], {})
        weighted_overall, categories, coverage = build_grade_categories(assignments, flat_weights)

        # ── Grades header ───────────────────────────────────────────────────
        # Goal: never let a partial grade look like a final one. When the
        # syllabus has weights and only some categories have scores, the
        # weighted overall is renormalized over the *graded portion* — so
        # we label it "so far", show how much of the course that covers,
        # and surface the worst-case floor (graded scores fixed, ungraded
        # = 0) so a student can't mistake the optimistic number for their
        # real standing.
        if weighted_overall is not None:
            wt_total = coverage.get("weight_total") or 0
            wt_graded = coverage.get("weight_graded") or 0
            partial = wt_total > 0 and wt_graded < wt_total * 0.95
            if partial:
                parent.add(_item(f"Grade so far: {weighted_overall:.1f}%"))
                parent.add(_item(
                    f"Based on {wt_graded:.0f}% of course graded "
                    f"({wt_graded:.0f} of {wt_total:.0f} weight pts)"
                ))
                if_zero = coverage.get("if_zero_pct")
                if if_zero is not None and (weighted_overall - if_zero) >= 1:
                    parent.add(_item(f"If you score 0 on rest: {if_zero:.1f}%"))
            else:
                parent.add(_item(f"Grade: {weighted_overall:.1f}%  (weighted)"))
            parent.add(rumps.separator)
        elif grades.get("overall_pct") is not None:
            parent.add(_item(f"Grade: {grades['overall_pct']:.1f}%"))
            if not flat_weights:
                # No syllabus weights → this number is whatever Blackboard
                # calculates, which depends on the prof's gradebook setup.
                parent.add(_item("(from Blackboard — no syllabus weights)"))
            parent.add(rumps.separator)

        # ── Grade detail rows ───────────────────────────────────────────────
        pearson_data = c.get("pearson") or {}
        mt_avg   = pearson_data.get("mini_tests_avg")
        mt_count = pearson_data.get("mini_tests_count", 0)
        if categories:
            for cat in categories:
                score = cat.get("score")
                weight = cat.get("weight", 0)
                count = cat.get("count", 0)
                cat_name = _shorten(_smart_title(cat["name"]), 26)
                if score is not None:
                    item_str = f"{count} item" if count == 1 else f"{count} items"
                    parent.add(_item(
                        f"{cat_name} ({weight:.0f}%)  —  {score:.0f}% · {item_str}"
                    ))
                elif mt_avg is not None and _is_mini_test_category(cat["name"]):
                    # Blackboard hasn't posted these yet — use Pearson scores
                    parent.add(_item(
                        f"{cat_name} ({weight:.0f}%)  —  {mt_avg:.1f}%"
                        f" · {mt_count} graded (MyLab)"
                    ))
                else:
                    parent.add(_item(f"{cat_name} ({weight:.0f}%)  —  not yet"))

            unmatched = coverage.get("unmatched") or []
            if unmatched:
                parent.add(rumps.separator)
                parent.add(_item("Not in weighted total:"))
                for a in unmatched[:5]:
                    score = a.get("score")
                    possible = a.get("possible")
                    score_str = _fmt_score(score, possible) if score is not None else "—"
                    short = _shorten(_smart_title(a["name"]), 32)
                    parent.add(_item(f"  {short}  —  {score_str}"))

        elif assignments:
            for a in assignments[:7]:
                score = a.get("score")
                possible = a.get("possible")
                score_str = _fmt_score(score, possible) if score is not None else "—"
                short = _shorten(_smart_title(a["name"]), 32)
                parent.add(_item(f"{short}  —  {score_str}"))

        elif not grades.get("overall_pct"):
            msg = ("No grades posted yet"
                   if not grade_error or grade_error == "no_grade_data"
                   else f"Grades: {grade_error}")
            parent.add(_item(msg))

        # ── Attendance ──────────────────────────────────────────────────────
        parent.add(rumps.separator)
        att_error = c.get("error")
        if att_error == "no_attendance_tracking":
            parent.add(_item("Attendance not tracked"))
        elif att_error == "no_course_id":
            parent.add(_item("Course not found"))
        elif att_error:
            parent.add(_item(f"Attendance error: {att_error}"))
        elif is_pct_only:
            src = c.get("source", "").split(":", 1)[1] if ":" in c.get("source", "") else "gradebook"
            parent.add(_item(f"Attendance: {att_pct:.1f}%  —  {att_label}"))
            parent.add(_item(f"(from gradebook column “{src}”)"))
        else:
            absent  = c.get("absent", 0)
            late    = c.get("late", 0)
            present = c.get("present", 0)
            excused = c.get("excused", 0)
            limit   = c.get("max_absences")
            effective = c.get("effective_absent", absent)
            if limit is not None:
                if late:
                    detail = f"{absent} absent, {late} late  =  {effective:g} / {limit}"
                else:
                    detail = f"{absent} / {limit}"
                parent.add(_item(f"Absences: {detail}  —  {att_label}"))
            else:
                # Total session count unknown (syllabus didn't say) — render
                # a percentage so the menu doesn't claim "X left" with no
                # basis. Computed against recorded sessions only, since we
                # genuinely don't know how many are still upcoming.
                recorded = present + late + absent + excused
                if recorded > 0:
                    pct = (present + late) / recorded * 100
                    parent.add(_item(f"Attendance: {pct:.0f}% so far  ({present + late}/{recorded} attended)"))
                if absent or late:
                    parent.add(_item(f"  {absent} absent · {late} late"))
                parent.add(_item("(no session count in syllabus)"))

        # ── MyLab / Pearson ─────────────────────────────────────────────────
        pearson = c.get("pearson")
        if pearson:
            parent.add(rumps.separator)
            avg          = pearson.get("avg")
            graded_count = pearson.get("graded_count", 0)
            total_count  = pearson.get("total_count", 0)
            if avg is not None:
                parent.add(_item(f"MyLab: {avg:.1f}%  —  {graded_count}/{total_count} graded"))
            else:
                parent.add(_item("MyLab: no grades yet"))
            for a in (pearson.get("assignments") or []):
                short = _shorten(_smart_title(a["name"]), 34)
                score = a.get("score")
                counted = a.get("counted", score is not None)
                if score is not None and not counted:
                    # Superseded by a graded retake — show greyed out
                    parent.add(_item(f"  {short}  —  {score:.1f}% (replaced)"))
                elif score is not None:
                    parent.add(_item(f"  {short}  —  {score:.1f}%"))
                else:
                    parent.add(_item(f"  {short}  —  submitted"))

        return parent

    # ----- callbacks -----

    def _on_open_app(self, _sender) -> None:
        self._main_win.show(tab="dashboard")

    def _on_settings(self, _sender) -> None:
        self._main_win.show(tab="settings")
        if self._refreshing:
            self._main_win.set_sync_state(True)

    def _on_enter_license(self, _sender) -> None:
        self._main_win.show(tab="settings", subtab="license")

    def _on_relogin(self, _sender) -> None:
        rumps.alert(
            title="Blackboard — Re-login",
            message=(
                "A browser window will open.\n\n"
                "1. Sign in with your IE account\n"
                "2. Approve the Authenticator prompt\n"
                "3. When asked 'Stay signed in?' — click YES\n"
                "4. Wait for your courses page to appear\n"
                "5. Do NOT close the window — it closes itself"
            ),
            ok="Open browser",
        )
        self._start_login()

    def _show_paywall(self) -> None:
        """Shown when trial or subscription has expired — opens pricing window."""
        self._pricing_win.show()

    def _open_license_settings(self) -> None:
        self._main_win.show(tab="settings", subtab="license")

    def _on_buy(self, _sender) -> None:
        self._pricing_win.show()

    def _on_update(self, _sender) -> None:
        url = (self._update_info.get("url") or
               self._update_info.get("download_url") or
               DOWNLOAD_URL) if self._update_info else DOWNLOAD_URL
        version = (self._update_info or {}).get("version", "")
        self._start_self_update(url, version)

    def _start_self_update(self, url: str, version: str) -> None:
        """Try in-place update; fall back to opening the DMG URL in the
        browser if the running bundle can't be replaced (e.g. translocated)."""
        import updater
        ok, reason = updater.can_self_update()
        if not ok:
            log("update", "self-update unavailable (%s) — opening browser", reason)
            subprocess.run(["open", url])
            return

        resp = rumps.alert(
            title=f"Update BB Tracker to v{version}?",
            message="The new version will download in the background, then "
                    "BB Tracker will restart. Your courses, login, and "
                    "settings are kept.",
            ok="Update now", cancel="Later",
        )
        if resp != 1:
            return

        self._status = "Downloading update"
        self._render_menu()

        progress_win = UpdateProgressWindow()
        progress_win.show()

        def worker():
            try:
                def _progress(done, total):
                    pct = int(done / total * 100) if total else 0
                    self._status = f"Downloading update… {pct}%"
                    progress_win.update_progress(done, total)
                updater.perform_update(
                    url,
                    on_progress=_progress,
                    is_cancelled=progress_win.is_cancelled,
                )
                log("update", "download complete — quitting for handoff")
                progress_win.close()
                _on_main(rumps.quit_application)
            except updater.UpdateCancelled:
                log("update", "user cancelled download")
                progress_win.close()
                self._status = "Refreshing"
                _on_main(self._render_menu)
            except Exception as exc:
                log.exc("update", "self-update failed", exc)
                progress_win.close()
                _on_main(lambda: subprocess.run(["open", url]))

        threading.Thread(target=worker, daemon=True, name="updater").start()

    # ── Feedback ─────────────────────────────────────────────────────────────
    # The mailto body is tailored to the category the user picked from the
    # Help & Feedback sub-menu. Pure feedback gets a clean email; bug reports
    # get only the debug snippets that are actually relevant to that bug.

    def _course_snapshot(self) -> str:
        """One-line-per-course summary of the current scrape — useful for
        missing-data reports, not for general feedback."""
        lines: list[str] = []
        for c in (self._data or {}).get("courses", []) or []:
            name = c.get("name", "?")
            grades = c.get("grades") or {}
            grade_pct = grades.get("overall_pct")
            grade_str = f"{grade_pct:.1f}%" if grade_pct is not None else "—"
            att_score = c.get("overall_score")
            att_str = f"{att_score:.0f}%" if att_score is not None else "—"
            err_bits = []
            if c.get("error"):
                err_bits.append(f"att:{c['error']}")
            if grades.get("error"):
                err_bits.append(f"grade:{grades['error']}")
            tail = f"  [{', '.join(err_bits)}]" if err_bits else ""
            lines.append(f"  · {name}: grade {grade_str}, attendance {att_str}{tail}")
        return "\n".join(lines) if lines else "  (no scrape data yet)"

    def _compose_with_attachment(self, to: str, subject: str, body: str,
                                  attachment: str) -> bool:
        """Best-effort: ask macOS Mail.app via AppleScript to compose a new
        message with the log file already attached. Returns True on success.

        mailto:// URLs cannot carry attachments (RFC 6068), so for bug-report
        categories that need the log we drive Mail.app directly. Falls back to
        plain mailto if Mail.app is unavailable, the file doesn't exist, or
        the user uses a web mail client (in which case Mail.app may not be
        configured).
        """
        if not Path(attachment).exists():
            return False

        # Use AppleScript variables instead of string interpolation — far safer
        # than escaping quotes / newlines into a heredoc.
        script = '''
on run argv
    set toAddr   to item 1 of argv
    set subj     to item 2 of argv
    set bodyText to item 3 of argv
    set logPath  to POSIX file (item 4 of argv)
    tell application "Mail"
        set newMsg to make new outgoing message with properties {visible:true, subject:subj, content:bodyText}
        tell newMsg
            make new to recipient at end of to recipients with properties {address:toAddr}
            tell content
                make new attachment with properties {file name:logPath} at after the last paragraph
            end tell
        end tell
        activate
    end tell
end run
'''
        try:
            r = subprocess.run(
                ["osascript", "-", to, subject, body, attachment],
                input=script.encode(), capture_output=True, timeout=15,
            )
            if r.returncode == 0:
                log("ui", "Mail.app compose with attachment ok")
                return True
            log("ui", "Mail.app compose failed (rc=%d): %s",
                r.returncode, r.stderr.decode("utf-8", "replace")[:300])
        except Exception as exc:
            log.exc("ui", "osascript compose failed", exc)
        return False

    def _send_feedback(self, category: str) -> None:
        """Open a mailto pre-filled for the given feedback category.

        Categories drive both the subject prefix and which debug context gets
        attached. General feedback stays clean (no grades, no logs); bug
        reports include only the debug bits relevant to that bug — and for
        categories that genuinely need it, we auto-attach menubar.log via
        AppleScript / Mail.app.
        """
        import platform, urllib.parse
        log("ui", "user clicked feedback (%s)", category)

        macos = platform.mac_ver()[0]
        lic_state = (self._license or {}).get("state", "unknown")
        env_block = (
            "\n\n--\n"
            f"App: BB Tracker {APP_VERSION}\n"
            f"macOS: {macos}"
        )

        if category == "general":
            subject = "BB Tracker — Feedback"
            body = (
                "Hi BB Tracker team,\n\n"
                "Your feedback / suggestion:\n\n\n"
                + env_block
            )

        elif category == "missing_data":
            subject = "BB Tracker — Missing or wrong grades / attendance"
            body = (
                "Hi BB Tracker team,\n\n"
                "Course name (exact, as it appears in Blackboard):\n\n"
                "What BB Tracker shows:\n\n"
                "What Blackboard actually shows:\n\n"
                "*** PLEASE ATTACH a screenshot of the Blackboard gradebook\n"
                "    page for this course. Without it we usually can't reproduce. ***\n"
                "(The app's debug log is attached automatically below.)\n"
                "\n--\nWhat the app currently sees per course:\n"
                f"{self._course_snapshot()}"
                + env_block
            )

        elif category == "crash":
            subject = "BB Tracker — Crash / won't launch"
            body = (
                "Hi BB Tracker team,\n\n"
                "When does it happen? (e.g. on launch, mid-refresh, after login)\n\n"
                "Did macOS show \"BB Tracker is damaged\" or block it?  (yes / no)\n\n"
                "What you see (error message / screenshot of dialog if any):\n\n"
                "(menubar.log is attached automatically — if you don't see it,\n"
                " grab it manually via Settings → Advanced → Debug log.)\n"
                + env_block
            )

        elif category == "login":
            subject = "BB Tracker — Login / session issue"
            body = (
                "Hi BB Tracker team,\n\n"
                "How often does it ask you to log in?  (e.g. every launch, once a day)\n\n"
                "Did you click \"Yes\" when Microsoft asked \"Stay signed in?\"  (yes / no)\n\n"
                "What's the last screen you see before it kicks you out?\n\n"
                + env_block
            )

        elif category == "license":
            subject = "BB Tracker — License or payment issue"
            body = (
                "Hi BB Tracker team,\n\n"
                "Your Lemon Squeezy order number (from the receipt email):\n\n"
                "What error does the app show when activating?\n\n"
                "What you've already tried:\n\n"
                f"\n--\nLicense state right now: {lic_state}"
                + env_block
            )

        else:  # "other"
            subject = "BB Tracker — Other"
            body = (
                "Hi BB Tracker team,\n\n"
                "What would you like to share?\n\n\n"
                "\n--\nWhat the app currently sees per course:\n"
                f"{self._course_snapshot()}\n"
                f"License state: {lic_state}"
                + env_block
            )

        # For categories where the log is genuinely useful, try to open Mail.app
        # with the file already attached. Falls through to mailto on failure
        # (e.g. user doesn't use Mail.app) — the body always points to where
        # the log lives so they can attach it manually.
        if category in ("crash", "missing_data"):
            if self._compose_with_attachment(FEEDBACK_EMAIL, subject, body, log.path):
                return

        url = (
            f"mailto:{FEEDBACK_EMAIL}"
            f"?subject={urllib.parse.quote(subject)}"
            f"&body={urllib.parse.quote(body)}"
        )
        subprocess.run(["open", url])

    def _on_refresh(self, _sender) -> None:
        self._start_scrape()

    def _on_open_bb(self, _sender) -> None:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        subprocess.run(["open", cfg["blackboard_base_url"]])

    def _open_folder(self, _sender) -> None:
        subprocess.run(["open", str(BASE_DIR)])


def _acquire_single_instance_lock():
    """Bind a local socket to prevent multiple instances.

    Returns the socket (keep alive for the process lifetime) or exits if
    another instance is already running.
    """
    import socket as _socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", 47291))  # arbitrary fixed port for BB Tracker
        return sock
    except OSError:
        # Port already bound → another instance is running; bring it to focus
        # by sending it a signal, then exit quietly.
        import AppKit
        running = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "com.bbtracker.app"
        )
        for app in running:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        sys.exit(0)


if __name__ == "__main__":
    _lock = _acquire_single_instance_lock()
    BBTrackerApp().run()
