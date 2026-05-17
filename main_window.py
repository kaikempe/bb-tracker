"""Main app window for BB Tracker — sidebar navigation, WKWebView-based."""

from __future__ import annotations

import json
import subprocess
import threading

import objc
from AppKit import (
    NSBackingStoreBuffered, NSColor, NSMakeRect, NSObject,
    NSScreen, NSWindow,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
)
from WebKit import (
    WKUserContentController, WKUserScript,
    WKWebView, WKWebViewConfiguration,
)

from scraper import (
    CONFIG_PATH, DATA_PATH, COURSES_PATH,
    build_grade_categories, bundled_weights_for,
)
import license as _lic
import login_item

_TITLE_LOWER = {"for", "of", "the", "and", "in", "a", "an", "to", "at", "by", "with"}
_ACRONYMS    = {"IE", "CS", "PC", "IT", "AI", "ML", "UI", "UX", "HR", "PR", "NLP"}


def _smart_title(s: str) -> str:
    out = []
    for i, w in enumerate(s.split()):
        wu = w.upper()
        if wu in _ACRONYMS:
            out.append(wu)
        elif i > 0 and w.lower() in _TITLE_LOWER:
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)


class _AppMessageHandler(NSObject):
    _callback = None

    def initWithCallback_(self, callback):
        self = objc.super(_AppMessageHandler, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        if self._callback:
            self._callback(message.body())


# ---------------------------------------------------------------------------
# HTML / CSS / JS
# ---------------------------------------------------------------------------

MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  :root {
    --bg:      #0a0a0f;
    --surface: #13131a;
    --border:  #1e1e2e;
    --text:    #e8e8f0;
    --muted:   #6b6b85;
    --accent:  #7c6fff;
    --accent2: #a78bfa;
    --green:   #4ade80;
    --yellow:  #fbbf24;
    --red:     #f87171;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    user-select: none;
    overflow: hidden;
  }

  /* ── App layout ── */
  #app { display: flex; height: 100%; }

  /* ── Nav sidebar ── */
  #nav {
    width: 160px;
    flex-shrink: 0;
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    padding: 16px 8px 12px;
  }

  .nav-logo {
    font-size: 14px;
    font-weight: 700;
    color: var(--text);
    padding: 4px 8px 16px;
    letter-spacing: -.01em;
  }

  .nav-items { flex: 1; display: flex; flex-direction: column; gap: 2px; }

  .nav-item {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 8px 10px;
    border: none;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    width: 100%;
    text-align: left;
    font-size: 13px;
    font-family: inherit;
    border-radius: 8px;
    transition: background .12s, color .12s;
  }
  .nav-item:hover  { background: rgba(255,255,255,.05); color: var(--text); }
  .nav-item.active { background: rgba(124,111,255,.15); color: var(--accent2); }
  .nav-icon { font-size: 14px; width: 18px; text-align: center; flex-shrink: 0; }

  .nav-footer {
    border-top: 1px solid var(--border);
    padding-top: 12px;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }

  #last-updated {
    font-size: 11px;
    color: var(--muted);
    text-align: center;
    line-height: 1.4;
  }

  #refresh-btn {
    width: 100%;
    padding: 7px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--text);
    font-size: 12px;
    font-family: inherit;
    cursor: pointer;
    transition: background .12s;
  }
  #refresh-btn:hover    { background: rgba(255,255,255,.07); }
  #refresh-btn:disabled { opacity: .5; cursor: default; }

  /* ── Content ── */
  #content { flex: 1; overflow: hidden; display: flex; flex-direction: column; }

  .tab-pane { display: none; height: 100%; flex-direction: column; overflow: hidden; }
  .tab-pane.active { display: flex; }

  .scroll {
    flex: 1; overflow-y: auto; padding: 20px;
  }
  .scroll::-webkit-scrollbar { width: 6px; }
  .scroll::-webkit-scrollbar-track { background: transparent; }
  .scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* ── Dashboard ── */
  .course-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(185px, 1fr));
    gap: 12px;
    padding: 20px;
  }

  .course-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-left-width: 3px;
    border-radius: 12px;
    padding: 14px 14px 12px;
    cursor: pointer;
    transition: border-color .15s, transform .1s;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .course-card:hover { transform: translateY(-1px); }
  .course-card:hover .card-name { color: var(--text); }
  .course-card.green  { border-left-color: var(--green); }
  .course-card.yellow { border-left-color: var(--yellow); }
  .course-card.red    { border-left-color: var(--red); }
  .course-card.grey   { border-left-color: var(--muted); }

  .card-name {
    font-size: 11.5px;
    color: var(--muted);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition: color .12s;
  }
  .card-grade {
    font-size: 30px;
    font-weight: 800;
    letter-spacing: -.03em;
    line-height: 1.1;
  }
  .card-grade.green  { color: var(--green); }
  .card-grade.yellow { color: var(--yellow); }
  .card-grade.red    { color: var(--red); }
  .card-grade.grey   { color: var(--muted); }
  .card-partial { font-size: 11px; color: var(--muted); }
  .card-att { font-size: 12px; color: var(--muted); margin-top: 2px; }

  /* ── Courses tab ── */
  #tab-courses { flex-direction: row !important; }

  #courses-list {
    width: 220px;
    flex-shrink: 0;
    border-right: 1px solid var(--border);
    overflow-y: auto;
    padding: 10px 6px;
  }
  #courses-list::-webkit-scrollbar { width: 4px; }
  #courses-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .cli {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 8px 10px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    color: var(--muted);
    transition: background .1s, color .1s;
  }
  .cli:hover  { background: rgba(255,255,255,.04); color: var(--text); }
  .cli.active { background: rgba(124,111,255,.12); color: var(--text); }
  .cli-name   { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cli-dot    { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
  .cli-dot.green  { background: var(--green); }
  .cli-dot.yellow { background: var(--yellow); }
  .cli-dot.red    { background: var(--red); }
  .cli-dot.grey   { background: var(--muted); }

  #course-detail { flex: 1; overflow-y: auto; padding: 24px; }
  #course-detail::-webkit-scrollbar { width: 6px; }
  #course-detail::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .detail-header {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 22px;
    flex-wrap: wrap;
  }
  .detail-title  { font-size: 20px; font-weight: 700; letter-spacing: -.02em; }
  .detail-grade  { font-size: 20px; font-weight: 700; }
  .detail-partial { font-size: 12px; color: var(--muted); align-self: center; }

  .dsec { margin-bottom: 20px; }
  .dsec-title {
    font-size: 10.5px; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted); margin-bottom: 8px;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }
  .row {
    display: flex; align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    gap: 12px;
  }
  .row:last-child { border-bottom: none; }
  .row-left  { flex: 1; min-width: 0; }
  .row-label { font-size: 13.5px; }
  .row-sub   { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
  .row-score { font-size: 13px; font-weight: 600; flex-shrink: 0; }
  .row-score.green  { color: var(--green); }
  .row-score.yellow { color: var(--yellow); }
  .row-score.red    { color: var(--red); }
  .row-score.grey   { color: var(--muted); }

  .coverage-info {
    font-size: 11.5px; color: var(--muted);
    margin-bottom: 6px;
  }
  .coverage-bar {
    height: 3px;
    background: var(--border);
    border-radius: 2px;
    margin-bottom: 10px;
    overflow: hidden;
  }
  .coverage-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 2px;
  }

  .empty-state {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    height: 100%; gap: 10px;
    color: var(--muted); font-size: 13px; text-align: center;
  }
  .empty-icon { font-size: 36px; opacity: .3; }

  /* ── Calendar placeholder ── */
  .placeholder-wrap {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    height: 100%; gap: 14px;
    padding: 40px; text-align: center;
  }
  .placeholder-icon  { font-size: 52px; opacity: .4; }
  .placeholder-title { font-size: 18px; font-weight: 700; }
  .placeholder-sub   { color: var(--muted); font-size: 13px; max-width: 340px; line-height: 1.65; }

  /* ── Announcements tab ── */
  .ann-card {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    cursor: default;
    transition: background .1s;
  }
  .ann-card:last-child { border-bottom: none; }
  .ann-card:hover { background: rgba(255,255,255,.025); }

  .ann-header {
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 5px; flex-wrap: wrap;
  }
  .ann-course {
    font-size: 10.5px; font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; color: var(--accent2);
    background: rgba(124,111,255,.1); border: 1px solid rgba(124,111,255,.2);
    padding: 2px 8px; border-radius: 100px; flex-shrink: 0;
  }
  .ann-action-badge {
    font-size: 10px; font-weight: 600; letter-spacing: .04em;
    text-transform: uppercase; color: var(--yellow);
    background: rgba(251,191,36,.1); border: 1px solid rgba(251,191,36,.25);
    padding: 2px 7px; border-radius: 100px; flex-shrink: 0;
  }
  .ann-date {
    font-size: 11px; color: var(--muted); margin-left: auto;
  }
  .ann-title {
    font-size: 13.5px; font-weight: 600; margin-bottom: 4px;
    color: var(--text); line-height: 1.4;
  }
  .ann-body {
    font-size: 12px; color: var(--muted); line-height: 1.55;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .ann-author {
    font-size: 11px; color: var(--muted); margin-top: 6px;
  }

  /* ── Settings tab ── */
  .settings-nav {
    display: flex; gap: 2px;
    padding: 14px 16px 0;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .stab {
    padding: 8px 18px; border-radius: 8px 8px 0 0;
    cursor: pointer; font-size: 13px; font-weight: 500;
    color: var(--muted); background: transparent; border: none;
    font-family: inherit;
    transition: color .15s, background .15s;
  }
  .stab:hover  { color: var(--text); background: rgba(255,255,255,.05); }
  .stab.active { color: var(--accent2); background: var(--bg); }

  .spane { display: none; }
  .spane.active { display: block; }

  /* ── Settings shared components ── */
  .section { margin-bottom: 20px; }
  .section-title {
    font-size: 10.5px; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted); margin-bottom: 9px;
  }

  .toggle { position: relative; width: 38px; height: 22px; flex-shrink: 0; cursor: pointer; }
  .toggle input { opacity: 0; width: 0; height: 0; position: absolute; }
  .slider {
    position: absolute; inset: 0; background: rgba(255,255,255,.12);
    border-radius: 22px; transition: background .2s;
  }
  .slider::before {
    content: ""; position: absolute;
    width: 16px; height: 16px; left: 3px; top: 3px;
    background: #fff; border-radius: 50%; transition: transform .2s;
    box-shadow: 0 1px 3px rgba(0,0,0,.4);
  }
  input:checked + .slider { background: var(--accent); }
  input:checked + .slider::before { transform: translateX(16px); }

  select {
    background: rgba(255,255,255,.08); color: var(--text);
    border: 1px solid rgba(255,255,255,.1); border-radius: 8px;
    padding: 5px 28px 5px 10px; font-size: 13px; cursor: pointer;
    outline: none; -webkit-appearance: none; appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' fill='%236b6b85'%3E%3Cpath d='M0 0l5 6 5-6z'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 9px center;
    background-size: 10px 6px; font-family: inherit;
  }
  select option { background: #1e1e2e; }

  .text-input {
    background: rgba(255,255,255,.06); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 12px; font-size: 13px; font-family: "SF Mono", monospace;
    letter-spacing: .03em; width: 100%; outline: none; margin-top: 8px;
    transition: border-color .15s;
  }
  .text-input:focus { border-color: var(--accent); }
  .text-input::placeholder { color: var(--muted); }

  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 16px; border-radius: 8px;
    font-size: 13px; font-weight: 600; cursor: pointer; font-family: inherit;
    border: none; transition: opacity .15s, transform .1s;
  }
  .btn:hover   { opacity: .85; }
  .btn:active  { transform: scale(.97); }
  .btn:disabled { opacity: .5; cursor: default; transform: none; }
  .btn:disabled:hover { opacity: .5; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-outline { background: transparent; color: var(--text); border: 1px solid var(--border); }
  .btn-sm      { padding: 5px 13px; font-size: 12px; }

  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 10px; border-radius: 100px;
    font-size: 12px; font-weight: 600;
  }
  .badge.active  { background: rgba(74,222,128,.12);  color: var(--green);  border: 1px solid rgba(74,222,128,.3); }
  .badge.trial   { background: rgba(251,191,36,.12);  color: var(--yellow); border: 1px solid rgba(251,191,36,.3); }
  .badge.expired { background: rgba(248,113,113,.12); color: var(--red);    border: 1px solid rgba(248,113,113,.3); }

  .course-row {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 16px; border-bottom: 1px solid var(--border);
  }
  .course-row:last-child { border-bottom: none; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .dot.green  { background: var(--green); }
  .dot.yellow { background: var(--yellow); }
  .dot.red    { background: var(--red); }
  .dot.grey   { background: var(--muted); }
  .course-name { flex: 1; font-size: 13.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .seg { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .seg button {
    padding: 3px 7px; font-size: 11px; border: none;
    background: transparent; color: var(--muted); cursor: pointer; font-family: inherit;
    transition: background .15s, color .15s;
  }
  .seg button:not(:last-child) { border-right: 1px solid var(--border); }
  .seg button.active { background: var(--accent); color: #fff; }

  /* ── Expandable category rows ── */
  .cat-row.expandable { cursor: pointer; }
  .cat-row.expandable:hover > .row { background: rgba(255,255,255,.03); }
  .expand-icon {
    font-size: 11px; color: var(--muted); line-height: 1;
    display: inline-block; transition: transform .15s;
  }
  .cat-row.open .expand-icon { transform: rotate(180deg); }
  .cat-items {
    display: none;
    padding: 4px 14px 10px;
    border-top: 1px solid var(--border);
  }
  .cat-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 5px 0; font-size: 12px; color: var(--muted);
    border-bottom: 1px solid rgba(30,30,46,.5); gap: 10px;
  }
  .cat-item:last-child { border-bottom: none; }
  .cat-item-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cat-item-score { font-weight: 600; flex-shrink: 0; font-size: 12px; }

  #toast {
    position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%);
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); padding: 7px 16px; border-radius: 8px;
    font-size: 12px; opacity: 0; transition: opacity .2s; pointer-events: none;
    white-space: nowrap;
  }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
<div id="app">

  <!-- ── Navigation ── -->
  <nav id="nav">
    <div class="nav-logo">BB Tracker</div>
    <div class="nav-items">
      <button class="nav-item active" data-tab="dashboard" onclick="showTab('dashboard')">
        <span class="nav-icon">⊞</span> Dashboard
      </button>
      <button class="nav-item" data-tab="courses" onclick="showTab('courses')">
        <span class="nav-icon">◧</span> Courses
      </button>
      <button class="nav-item" data-tab="announcements" onclick="showTab('announcements')">
        <span class="nav-icon">📢</span> <span id="ann-nav-label">Announcements</span>
      </button>
      <button class="nav-item" data-tab="settings" onclick="showTab('settings')">
        <span class="nav-icon">⊙</span> Settings
      </button>
    </div>
    <div class="nav-footer">
      <div id="last-updated">—</div>
      <button id="refresh-btn" onclick="send('refresh')">↻ Refresh</button>
    </div>
  </nav>

  <!-- ── Content ── -->
  <div id="content">

    <!-- Dashboard -->
    <div id="tab-dashboard" class="tab-pane active">
      <div id="dashboard-grid" class="course-grid"></div>
    </div>

    <!-- Courses -->
    <div id="tab-courses" class="tab-pane">
      <div id="courses-list"></div>
      <div id="course-detail">
        <div class="empty-state">
          <div class="empty-icon">◧</div>
          <div>Select a course</div>
        </div>
      </div>
    </div>

    <!-- Announcements -->
    <div id="tab-announcements" class="tab-pane" style="flex-direction:column">
      <!-- Filter bar -->
      <div id="ann-filter-bar" style="
        display:flex; align-items:center; gap:8px; padding:12px 16px;
        border-bottom:1px solid var(--border); flex-shrink:0; flex-wrap:wrap;">
        <div class="seg" id="ann-filter-seg">
          <button class="active" onclick="setAnnFilter('all')">All</button>
          <button onclick="setAnnFilter('action')">Action Required</button>
          <button onclick="setAnnFilter('unread')">Unread</button>
        </div>
        <select id="ann-course-filter" onchange="buildAnnouncements()"
          style="margin-left:auto;font-size:12px;padding:4px 24px 4px 8px">
          <option value="">All courses</option>
        </select>
      </div>
      <!-- List -->
      <div id="ann-list" class="scroll" style="padding:0"></div>
    </div>

    <!-- Settings -->
    <div id="tab-settings" class="tab-pane">
      <div class="settings-nav">
        <button class="stab active" onclick="showSettingsTab('general')">Settings</button>
        <button class="stab" onclick="showSettingsTab('license')">License</button>
      </div>
      <div class="scroll">

        <!-- General -->
        <div id="spane-general" class="spane active">

          <div class="section">
            <div class="section-title">Courses</div>
            <div style="font-size:11.5px;color:var(--muted);margin-bottom:8px">
              Toggle visibility and choose what appears inline next to each course in the menu bar.
            </div>
            <div class="card" id="settings-course-list">
              <div class="row" style="color:var(--muted);font-size:13px;justify-content:center">
                No courses synced yet
              </div>
            </div>
          </div>

          <div class="section">
            <div class="section-title">Auto-refresh</div>
            <div class="card">
              <div class="row">
                <div class="row-label">Refresh interval</div>
                <select id="refresh_interval_minutes"
                  onchange="setSetting('refresh_interval_minutes', parseInt(this.value))">
                  <option value="30">Every 30 min</option>
                  <option value="60">Every 1 hr</option>
                  <option value="120">Every 2 hrs</option>
                  <option value="240">Every 4 hrs</option>
                  <option value="480">Every 8 hrs</option>
                </select>
              </div>
            </div>
          </div>

          <div class="section">
            <div class="section-title">Attendance rules</div>
            <div class="card">
              <div class="row">
                <div class="row-left">
                  <div class="row-label">Late marks weight</div>
                  <div class="row-sub">How much a "late" counts against your limit</div>
                </div>
                <select id="late_absence_weight"
                  onchange="setSetting('late_absence_weight', parseFloat(this.value))">
                  <option value="0">Not counted</option>
                  <option value="0.5">Half absence</option>
                  <option value="1">Full absence</option>
                </select>
              </div>
            </div>
          </div>

          <div class="section">
            <div class="section-title">Notifications</div>
            <div style="font-size:11.5px;color:var(--muted);margin-bottom:8px">
              macOS alerts — appear in the top-right corner of your screen.
            </div>
            <div class="card">
              <div class="row">
                <div class="row-left">
                  <div class="row-label">New or updated grade</div>
                  <div class="row-sub">Alert when a grade is posted or changed</div>
                </div>
                <label class="toggle">
                  <input type="checkbox" id="notif-new-grade"
                    onchange="setNotifPref('new_grade', this.checked); setNotifPref('grade_changed', this.checked)">
                  <span class="slider"></span>
                </label>
              </div>
              <div class="row">
                <div class="row-left">
                  <div class="row-label">New announcement</div>
                  <div class="row-sub">Alert when a professor posts an announcement</div>
                </div>
                <label class="toggle">
                  <input type="checkbox" id="notif-announcement"
                    onchange="setNotifPref('new_announcement', this.checked)">
                  <span class="slider"></span>
                </label>
              </div>
              <div class="row" id="notif-filter-row">
                <div class="row-left">
                  <div class="row-label">Action items only</div>
                  <div class="row-sub">Only notify for announcements with deadlines, exams, or submissions</div>
                </div>
                <label class="toggle">
                  <input type="checkbox" id="notif-ann-filter"
                    onchange="setNotifPref('announcement_filter', this.checked)">
                  <span class="slider"></span>
                </label>
              </div>
              <div class="row">
                <div class="row-left">
                  <div class="row-label">Attendance warning</div>
                  <div class="row-sub">Alert when you're close to the absence limit</div>
                </div>
                <label class="toggle">
                  <input type="checkbox" id="notif-attendance"
                    onchange="setNotifPref('attendance_warning', this.checked)">
                  <span class="slider"></span>
                </label>
              </div>
              <div class="row" id="notif-threshold-row">
                <div class="row-left">
                  <div class="row-label">Warn when absences left ≤</div>
                </div>
                <select id="notif-threshold"
                  onchange="setNotifPref('attendance_threshold', parseInt(this.value))">
                  <option value="1">1</option>
                  <option value="2">2</option>
                  <option value="3">3</option>
                </select>
              </div>
            </div>
          </div>

          <div class="section">
            <div class="section-title">Startup</div>
            <div class="card">
              <div class="row">
                <div class="row-left">
                  <div class="row-label">Open at login</div>
                  <div class="row-sub">Automatically launch BB Tracker when you log in to your Mac</div>
                </div>
                <select id="login_item"
                  onchange="send('set_login_item', { enabled: this.value === 'on' })">
                  <option value="on">On</option>
                  <option value="off">Off</option>
                </select>
              </div>
            </div>
          </div>

          <div class="section">
            <div class="section-title">Updates</div>
            <div class="card">
              <div class="row">
                <div class="row-left">
                  <div class="row-label">BB Tracker <span id="current-version" style="color:var(--muted);font-size:12px"></span></div>
                  <div class="row-sub" id="update-status">Checks for updates automatically every day</div>
                </div>
                <button class="btn btn-outline btn-sm" onclick="checkForUpdates()">Check now</button>
              </div>
            </div>
          </div>

          <div class="section">
            <div class="section-title">Advanced</div>
            <div class="card">
              <div class="row">
                <div class="row-left">
                  <div class="row-label">Re-discover courses</div>
                  <div class="row-sub">Pull a fresh course list and syllabi from Blackboard. Use this if a course is missing or you've enrolled in a new one.</div>
                </div>
                <button id="sync-btn" class="btn btn-outline btn-sm" onclick="syncCourses()">Sync now</button>
              </div>
              <div class="row">
                <div class="row-left">
                  <div class="row-label">Debug log</div>
                  <div class="row-sub">Reveals <code style="background:rgba(255,255,255,.06);padding:1px 5px;border-radius:4px">menubar.log</code> in Finder. Drag it into a feedback email if reporting an issue.</div>
                </div>
                <button class="btn btn-outline btn-sm" onclick="send('reveal_log')">Open in Finder</button>
              </div>
            </div>
          </div>

        </div><!-- /spane-general -->

        <!-- License -->
        <div id="spane-license" class="spane">

          <div class="section">
            <div class="section-title">Status</div>
            <div class="card">
              <div class="row">
                <div class="row-label">License</div>
                <span id="lic-badge" class="badge trial">—</span>
              </div>
              <div id="lic-detail-row" class="row" style="display:none">
                <div id="lic-detail" style="font-size:12px;color:var(--muted)"></div>
              </div>
            </div>
          </div>

          <div class="section" id="activate-section">
            <div class="section-title">Activate license</div>
            <div class="card">
              <div style="padding:16px">
                <div style="font-size:13px;color:var(--muted)">Paste the key from your confirmation email:</div>
                <input class="text-input" id="lic-key" type="text" placeholder="XXXX-XXXX-XXXX-XXXX" spellcheck="false">
                <div style="display:flex;gap:8px;margin-top:10px;align-items:center">
                  <button class="btn btn-outline btn-sm" onclick="pasteKey()">Paste from clipboard</button>
                  <button class="btn btn-primary btn-sm" onclick="activateKey()">Activate →</button>
                </div>
                <div id="lic-result" style="margin-top:9px;font-size:12px;min-height:16px"></div>
              </div>
            </div>
          </div>

          <div class="section" id="deactivate-section" style="display:none">
            <div class="section-title">Deactivate</div>
            <div class="card">
              <div style="padding:16px">
                <div style="font-size:13px;color:var(--muted);margin-bottom:12px">
                  Remove the license from this Mac — useful if you're selling or retiring it.
                  Your license key stays valid and works on any other Mac.
                </div>
                <button class="btn btn-outline btn-sm" onclick="deactivateLicense()">Deactivate on this Mac</button>
                <div id="deac-result" style="margin-top:9px;font-size:12px;min-height:16px"></div>
              </div>
            </div>
          </div>

          <div class="section" id="get-license-section">
            <div class="section-title">Get a license</div>
            <div class="card">
              <div class="row" style="flex-wrap:wrap;gap:10px">
                <button class="btn btn-primary btn-sm" onclick="send('open_buy')">Buy BB Tracker →</button>
                <span style="font-size:12px;color:var(--muted)">€3/mo &nbsp;·&nbsp; €30/yr &nbsp;·&nbsp; €100 lifetime</span>
              </div>
            </div>
          </div>

        </div><!-- /spane-license -->

      </div><!-- /scroll -->
    </div><!-- /tab-settings -->

  </div><!-- /#content -->
</div><!-- /#app -->

<div id="toast"></div>

<script>
// ── Globals (injected by Python at document start) ──────────────────────────
let cfg           = window.__config        || {};
let dataObj       = window.__data          || {};
let courses       = window.__courses       || [];
let computed      = window.__computed      || {};
let licData       = window.__license       || {};
let displayModes  = window.__displayModes  || {};
let announcements = window.__announcements || [];

let _annFilter = 'all';   // 'all' | 'action' | 'unread'

// ── Bridge ──────────────────────────────────────────────────────────────────
function send(action, payload) {
  window.webkit.messageHandlers.app.postMessage(
    JSON.stringify({ action, ...(payload || {}) })
  );
}

function setSetting(key, value) {
  send('set', { key, value });
  showToast('Saved');
}

// ── Toast ────────────────────────────────────────────────────────────────────
let _toastTimer;
function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}

// ── Helpers ──────────────────────────────────────────────────────────────────
const TITLE_LOWER = new Set(['for','of','the','and','in','a','an','to','at','by','with']);
const ACRONYMS    = new Set(['IE','CS','PC','IT','AI','ML','UI','UX','HR','PR','NLP']);

function smartTitle(s) {
  if (!s) return s || '';
  return s.split(' ').map((w, i) => {
    if (ACRONYMS.has(w.toUpperCase())) return w.toUpperCase();
    if (i > 0 && TITLE_LOWER.has(w.toLowerCase())) return w.toLowerCase();
    return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
  }).join(' ');
}

function gradeColor(pct) {
  if (pct === null || pct === undefined) return 'grey';
  if (pct >= 85) return 'green';
  if (pct >= 70) return 'yellow';
  return 'red';
}

function fmtScore(score, possible) {
  if (score === null || score === undefined) return '—';
  if (possible === null || possible === undefined) return score.toFixed(1);
  if (possible === 100) return score.toFixed(0) + '%';
  const s = parseFloat(score.toFixed(1));
  const p = parseFloat(possible.toFixed(1));
  return `${s}/${p}`;
}

function formatTs(ts) {
  if (!ts) return 'never';
  const diff = (Date.now() - new Date(ts).getTime()) / 1000;
  if (diff < 120)   return 'just now';
  if (diff < 3600)  return Math.floor(diff / 60) + ' min ago';
  if (diff < 86400) return Math.floor(diff / 3600) + ' hr ago';
  return Math.floor(diff / 86400) + 'd ago';
}

function setSelectVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = String(val);
}

// ── Tab navigation ────────────────────────────────────────────────────────────
function showTab(id) {
  document.querySelectorAll('.nav-item').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tab === id)
  );
  document.querySelectorAll('.tab-pane').forEach(p =>
    p.classList.toggle('active', p.id === 'tab-' + id)
  );
  if (id === 'dashboard')     buildDashboard();
  if (id === 'courses')       buildCoursesList();
  if (id === 'announcements') buildAnnouncements();
  if (id === 'settings')  {
    document.getElementById('current-version').textContent =
      'v' + (window.__appVersion || '');
  }
}

function showSettingsTab(id) {
  document.querySelectorAll('.stab').forEach((btn, i) =>
    btn.classList.toggle('active', ['general','license'][i] === id)
  );
  document.querySelectorAll('.spane').forEach(p => p.classList.remove('active'));
  document.getElementById('spane-' + id).classList.add('active');
  if (id === 'license' && !document.getElementById('lic-key').value.trim()) {
    send('paste_key_silent');
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
function init() {
  cfg           = window.__config        || {};
  dataObj       = window.__data          || {};
  courses       = window.__courses       || [];
  computed      = window.__computed      || {};
  licData       = window.__license       || {};
  displayModes  = window.__displayModes  || {};
  announcements = window.__announcements || [];

  const ts  = dataObj.timestamp;
  const err = dataObj.error;
  const lastEl = document.getElementById('last-updated');
  if (lastEl) {
    if (err === 'auth_required') lastEl.textContent = '🔑 Session expired';
    else lastEl.textContent = 'Updated ' + formatTs(ts);
  }

  setSelectVal('refresh_interval_minutes', cfg.refresh_interval_minutes ?? 120);
  setSelectVal('late_absence_weight',      cfg.late_absence_weight      ?? 0.5);
  setSelectVal('login_item',
    (window.__loginItemEnabled ?? true) ? 'on' : 'off');

  // Notification toggles
  const notifPrefs = cfg.notifications || {};
  const setChk = (id, key, def) => {
    const el = document.getElementById(id);
    if (el) el.checked = notifPrefs[key] !== undefined ? notifPrefs[key] : def;
  };
  setChk('notif-new-grade',    'new_grade',            true);
  setChk('notif-announcement', 'new_announcement',     true);
  setChk('notif-ann-filter',   'announcement_filter',  true);
  setChk('notif-attendance',   'attendance_warning',   true);
  setSelectVal('notif-threshold', notifPrefs.attendance_threshold ?? 2);

  // Unread badge on Announcements nav item
  _updateAnnBadge();

  buildSettingsCourseList();
  initLicense();

  const activeTab = document.querySelector('.nav-item.active')?.dataset?.tab || 'dashboard';
  if (activeTab === 'dashboard')     buildDashboard();
  if (activeTab === 'courses')       buildCoursesList();
  if (activeTab === 'announcements') buildAnnouncements();
}

// Called by Python to refresh data without rebuilding the window
function refreshData(tab) {
  cfg           = window.__config        || {};
  dataObj       = window.__data          || {};
  courses       = window.__courses       || [];
  computed      = window.__computed      || {};
  licData       = window.__license       || {};
  displayModes  = window.__displayModes  || {};
  announcements = window.__announcements || [];
  init();
  if (tab) showTab(tab);
}

function setSyncState(active) {
  const btn     = document.getElementById('refresh-btn');
  const syncBtn = document.getElementById('sync-btn');
  if (btn) { btn.disabled = active; btn.textContent = active ? '↻ Refreshing…' : '↻ Refresh'; }
  if (syncBtn) {
    syncBtn.disabled   = active;
    syncBtn.textContent = active ? 'Syncing…' : 'Sync now';
  }
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
function buildDashboard() {
  const grid = document.getElementById('dashboard-grid');
  if (!grid) return;

  const all    = dataObj.courses || [];
  const hidden = cfg.hidden_courses || [];
  const vis    = all.filter(c => !hidden.includes(c.name));

  if (!vis.length) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">⊞</div><div>No data yet — click Refresh</div></div>';
    return;
  }

  grid.innerHTML = '';
  vis.forEach(c => {
    const comp  = computed[c.name] || {};
    const grade = comp.weighted_overall !== undefined && comp.weighted_overall !== null
      ? comp.weighted_overall
      : (c.grades?.overall_pct ?? null);
    const color = gradeColor(grade);
    const cov   = comp.coverage || {};
    const partial = cov.weight_total > 0 && cov.weight_graded < cov.weight_total * 0.95;

    let gradeStr = grade !== null && grade !== undefined
      ? grade.toFixed(1) + '%' : '—';

    // Attendance line
    const rem = c.absences_remaining;
    let attStr = '';
    if (rem !== null && rem !== undefined) {
      attStr = rem <= 0 ? 'At absence limit' : `${rem} absence${rem === 1 ? '' : 's'} left`;
    } else if (c.overall_score !== null && c.overall_score !== undefined) {
      attStr = `${c.overall_score.toFixed(0)}% attendance`;
    } else if (c.error === 'no_attendance_tracking') {
      attStr = 'Not tracked';
    }

    const card = document.createElement('div');
    card.className = `course-card ${color}`;
    card.innerHTML = `
      <div class="card-name">${smartTitle(c.name)}</div>
      <div class="card-grade ${color}">${gradeStr}</div>
      ${partial && grade !== null ? `<div class="card-partial">so far</div>` : ''}
      ${attStr ? `<div class="card-att">${attStr}</div>` : ''}
    `;
    card.onclick = () => { showTab('courses'); selectCourse(c.name); };
    grid.appendChild(card);
  });
}

// ── Courses ───────────────────────────────────────────────────────────────────
let _selectedCourse = null;

function buildCoursesList() {
  const list   = document.getElementById('courses-list');
  if (!list) return;

  const all    = dataObj.courses || [];
  const hidden = cfg.hidden_courses || [];
  const vis    = all.filter(c => !hidden.includes(c.name));

  list.innerHTML = '';
  vis.forEach(c => {
    const comp  = computed[c.name] || {};
    const grade = comp.weighted_overall !== undefined && comp.weighted_overall !== null
      ? comp.weighted_overall : (c.grades?.overall_pct ?? null);
    const color = gradeColor(grade);

    const item = document.createElement('div');
    item.className = 'cli' + (c.name === _selectedCourse ? ' active' : '');
    item.dataset.course = c.name;
    item.innerHTML = `
      <div class="cli-dot ${color}"></div>
      <span class="cli-name" title="${smartTitle(c.name)}">${smartTitle(c.name)}</span>
    `;
    item.onclick = () => selectCourse(c.name);
    list.appendChild(item);
  });

  if (_selectedCourse && vis.find(c => c.name === _selectedCourse)) {
    renderCourseDetail(vis.find(c => c.name === _selectedCourse));
  } else if (vis.length) {
    selectCourse(vis[0].name);
  }
}

function selectCourse(name) {
  _selectedCourse = name;
  document.querySelectorAll('.cli').forEach(el =>
    el.classList.toggle('active', el.dataset.course === name)
  );
  const course = (dataObj.courses || []).find(c => c.name === name);
  if (course) renderCourseDetail(course);
}

function renderCourseDetail(c) {
  const detail = document.getElementById('course-detail');
  if (!detail) return;

  const comp  = computed[c.name] || {};
  const grade = comp.weighted_overall !== undefined && comp.weighted_overall !== null
    ? comp.weighted_overall : (c.grades?.overall_pct ?? null);
  const color = gradeColor(grade);
  const cov   = comp.coverage || {};
  const cats  = comp.categories || [];
  const grades = c.grades || {};
  const assigns = grades.assignments || [];

  const wtGraded = cov.weight_graded || 0;
  const wtTotal  = cov.weight_total  || 0;
  const partial  = wtTotal > 0 && wtGraded < wtTotal * 0.95;

  let html = '';

  // Header
  const gradeLabel = grade !== null && grade !== undefined ? grade.toFixed(1) + '%' : '—';
  const gradeStyle = color === 'grey' ? 'var(--muted)' : `var(--${color})`;
  html += `
    <div class="detail-header">
      <div class="detail-title">${smartTitle(c.name)}</div>
      <div class="detail-grade" style="color:${gradeStyle}">${gradeLabel}</div>
      ${partial && grade !== null ? `<div class="detail-partial">so far</div>` : ''}
    </div>
  `;

  // ── Grade section ──
  html += `<div class="dsec"><div class="dsec-title">Grades</div>`;

  if (cats.length) {
    if (partial) {
      const pct = wtTotal > 0 ? Math.round((wtGraded / wtTotal) * 100) : 0;
      html += `<div class="coverage-info">Based on ${wtGraded.toFixed(0)}% of course graded`;
      if (cov.if_zero_pct !== null && cov.if_zero_pct !== undefined && (grade - cov.if_zero_pct) >= 1) {
        html += ` · Worst case: ${cov.if_zero_pct.toFixed(1)}%`;
      }
      html += `</div><div class="coverage-bar"><div class="coverage-fill" style="width:${pct}%"></div></div>`;
    }
    html += `<div class="card">`;
    cats.forEach(cat => {
      const sc    = cat.score;
      const cc    = sc !== null && sc !== undefined ? gradeColor(sc) : 'grey';
      const items = cat.items || [];
      const exp   = items.length > 0;
      let itemsHtml = '';
      if (exp) {
        itemsHtml = items.map(item => {
          const iac = item.score !== null && item.score !== undefined && item.possible
            ? gradeColor(item.score / item.possible * 100) : 'grey';
          return `<div class="cat-item">
            <span class="cat-item-name">${smartTitle(item.name)}</span>
            <span class="cat-item-score ${iac}">${fmtScore(item.score, item.possible)}</span>
          </div>`;
        }).join('');
      }
      html += `<div class="cat-row${exp ? ' expandable' : ''}" ${exp ? 'onclick="toggleCat(this)"' : ''}>
        <div class="row">
          <div class="row-left">
            <div class="row-label">${smartTitle(cat.name)}</div>
            <div class="row-sub">${cat.weight}% of grade${cat.count ? ` · ${cat.count} item${cat.count !== 1 ? 's' : ''}` : ''}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            ${cat.pearson_injected ? '<span style="font-size:10px;color:var(--muted);margin-right:2px">MyLab</span>' : ''}
            <div class="row-score ${cc}">${sc !== null && sc !== undefined ? sc.toFixed(1) + '%' : 'Not yet'}</div>
            ${exp ? '<span class="expand-icon">▾</span>' : ''}
          </div>
        </div>
        ${exp ? `<div class="cat-items">${itemsHtml}</div>` : ''}
      </div>`;
    });
    html += `</div>`;
    const unmatched = cov.unmatched || [];
    if (unmatched.length) {
      html += `<div style="font-size:11.5px;color:var(--muted);margin:10px 0 6px">Not counted in weighted total:</div><div class="card">`;
      unmatched.slice(0, 5).forEach(a => {
        html += `<div class="row"><div class="row-label">${smartTitle(a.name)}</div>
          <div class="row-score grey">${fmtScore(a.score, a.possible)}</div></div>`;
      });
      html += `</div>`;
    }
  } else if (assigns.length) {
    html += `<div class="card">`;
    assigns.slice(0, 14).forEach(a => {
      const sc = fmtScore(a.score, a.possible);
      const ac = a.score !== null && a.possible ? gradeColor(a.score / a.possible * 100) : 'grey';
      html += `<div class="row"><div class="row-label">${smartTitle(a.name)}</div>
        <div class="row-score ${ac}">${sc}</div></div>`;
    });
    if (!grades.overall_pct) {
      html += `<div class="row" style="color:var(--muted);font-size:12px;justify-content:center">
        (from Blackboard — no syllabus weights)</div>`;
    }
    html += `</div>`;
  } else {
    html += `<div class="card"><div class="row" style="color:var(--muted);justify-content:center">
      ${grades.error ? grades.error : 'No grades posted yet'}</div></div>`;
  }
  html += `</div>`;

  // ── Attendance section ──
  html += `<div class="dsec"><div class="dsec-title">Attendance</div><div class="card">`;
  const attErr = c.error;
  if (attErr === 'no_attendance_tracking') {
    html += `<div class="row" style="color:var(--muted)">Not tracked for this course</div>`;
  } else if (c.overall_score !== null && c.overall_score !== undefined && c.absent === null) {
    const ac = gradeColor(c.overall_score);
    html += `<div class="row"><div class="row-label">Attendance score</div>
      <div class="row-score ${ac}">${c.overall_score.toFixed(1)}%</div></div>`;
    if (c.source && c.source.includes(':')) {
      const src = c.source.split(':')[1];
      html += `<div class="row" style="color:var(--muted);font-size:12px">from gradebook column "${src}"</div>`;
    }
  } else if (c.present !== null && c.present !== undefined) {
    const lim = c.max_absences;
    const rem = c.absences_remaining;
    const ac  = rem !== null ? (rem <= 0 ? 'red' : rem <= 2 ? 'yellow' : 'green') : 'grey';
    html += `<div class="row"><div class="row-label">Present</div>
      <div class="row-score green">${c.present}</div></div>`;
    if (c.late) {
      html += `<div class="row"><div class="row-label">Late</div>
        <div class="row-score yellow">${c.late}</div></div>`;
    }
    html += `<div class="row"><div class="row-label">Absent</div>
      <div class="row-score ${ac}">${c.absent}</div></div>`;
    if (lim !== null && lim !== undefined) {
      const eff = c.effective_absent ?? c.absent ?? 0;
      html += `<div class="row"><div class="row-left"><div class="row-label">Absences remaining</div>
        ${c.late ? `<div class="row-sub">Effective: ${eff} / ${lim}</div>` : ''}</div>
        <div class="row-score ${ac}">${rem !== null ? (rem >= 0 ? rem : 0) : '—'} left</div></div>`;
    } else {
      const rec = (c.present || 0) + (c.late || 0) + (c.absent || 0) + (c.excused || 0);
      if (rec > 0) {
        const pct = ((c.present + (c.late || 0)) / rec * 100).toFixed(0);
        html += `<div class="row"><div class="row-label">Attendance rate</div>
          <div class="row-score ${gradeColor(parseFloat(pct))}">${pct}%</div></div>
          <div class="row" style="color:var(--muted);font-size:12px">No session count in syllabus</div>`;
      }
    }
  } else {
    html += `<div class="row" style="color:var(--muted)">No attendance data</div>`;
  }
  html += `</div></div>`;

  // ── Pearson section ──
  const pearson = c.pearson;
  if (pearson) {
    html += `<div class="dsec"><div class="dsec-title">MyLab / Pearson</div><div class="card">`;
    if (pearson.avg !== null && pearson.avg !== undefined) {
      html += `<div class="row"><div class="row-label">Average</div>
        <div class="row-score ${gradeColor(pearson.avg)}">${pearson.avg.toFixed(1)}%</div></div>
        <div class="row" style="color:var(--muted);font-size:12px">
          ${pearson.graded_count} of ${pearson.total_count} graded</div>`;
    } else {
      html += `<div class="row" style="color:var(--muted)">No grades yet</div>`;
    }
    (pearson.assignments || []).forEach(a => {
      const sc = a.score !== null && a.score !== undefined
        ? a.score.toFixed(1) + '%' : 'submitted';
      const ac     = a.score !== null && a.score !== undefined ? gradeColor(a.score) : 'grey';
      const faded  = !a.counted && a.score !== null;
      html += `<div class="row" style="${faded ? 'opacity:.45' : ''}">
        <div class="row-label">${smartTitle(a.name)}${faded ? ' (replaced)' : ''}</div>
        <div class="row-score ${ac}">${sc}</div></div>`;
    });
    html += `</div></div>`;
  }

  // ── All assignments ──
  const allAssigns = grades.assignments || [];
  if (allAssigns.length) {
    html += `<div class="dsec"><div class="dsec-title">All Assignments</div><div class="card">`;
    allAssigns.forEach(a => {
      const sc = fmtScore(a.score, a.possible);
      const ac = a.score !== null && a.score !== undefined && a.possible
        ? gradeColor(a.score / a.possible * 100) : 'grey';
      html += `<div class="row">
        <div class="row-label">${smartTitle(a.name)}</div>
        <div class="row-score ${ac}">${sc}</div>
      </div>`;
    });
    html += `</div></div>`;
  }

  detail.innerHTML = html;
}

function toggleCat(row) {
  const items = row.querySelector('.cat-items');
  if (!items) return;
  const open = row.classList.toggle('open');
  items.style.display = open ? 'block' : 'none';
}

// ── Announcements ─────────────────────────────────────────────────────────────

function setAnnFilter(f) {
  _annFilter = f;
  document.querySelectorAll('#ann-filter-seg button').forEach((btn, i) => {
    btn.classList.toggle('active', ['all','action','unread'][i] === f);
  });
  buildAnnouncements();
}

function _updateAnnBadge() {
  const actionCount = announcements.filter(a => a.action_required).length;
  const label = document.getElementById('ann-nav-label');
  if (label) {
    label.textContent = actionCount > 0
      ? `Announcements (${actionCount})`
      : 'Announcements';
  }
}

function _fmtAnnDate(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffDays = Math.floor((now - d) / 86400000);
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7)  return d.toLocaleDateString('en', { weekday: 'short' });
    return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
  } catch { return iso.slice(0, 10); }
}

function buildAnnouncements() {
  const list = document.getElementById('ann-list');
  if (!list) return;

  // Populate course filter dropdown
  const sel = document.getElementById('ann-course-filter');
  if (sel) {
    const chosen = sel.value;
    const courseNames = [...new Set(announcements.map(a => a.course_name).filter(Boolean))].sort();
    sel.innerHTML = '<option value="">All courses</option>' +
      courseNames.map(n => `<option value="${n}"${n===chosen?' selected':''}>${smartTitle(n)}</option>`).join('');
  }

  const courseFilter = sel ? sel.value : '';

  let filtered = announcements.filter(a => {
    if (courseFilter && a.course_name !== courseFilter) return false;
    if (_annFilter === 'action') return a.action_required;
    return true;
  });

  if (!filtered.length) {
    list.innerHTML = `<div class="empty-state" style="height:100%;padding:40px">
      <div class="empty-icon">📢</div>
      <div>${announcements.length ? 'No announcements match this filter' : 'No announcements yet — click Refresh'}</div>
    </div>`;
    return;
  }

  list.innerHTML = filtered.map(a => {
    const courseBadge = a.course_name
      ? `<span class="ann-course">${smartTitle(a.course_name)}</span>`
      : '';
    const actionBadge = a.action_required
      ? `<span class="ann-action-badge">⚡ Action required</span>`
      : '';
    const dateStr = _fmtAnnDate(a.created);
    const author  = a.author ? `<div class="ann-author">— ${a.author}</div>` : '';
    return `<div class="ann-card">
      <div class="ann-header">
        ${courseBadge}
        ${actionBadge}
        <span class="ann-date">${dateStr}</span>
      </div>
      <div class="ann-title">${a.title || ''}</div>
      ${a.body ? `<div class="ann-body">${a.body}</div>` : ''}
      ${author}
    </div>`;
  }).join('');
}

// ── Notification preferences ──────────────────────────────────────────────────

function setNotifPref(key, value) {
  send('set_notif', { key, value });
  showToast('Saved');
  // Dim the "action items only" row when announcements notifications are off
  if (key === 'new_announcement') {
    const row = document.getElementById('notif-filter-row');
    if (row) row.style.opacity = value ? '1' : '0.4';
  }
  if (key === 'attendance_warning') {
    const row = document.getElementById('notif-threshold-row');
    if (row) row.style.opacity = value ? '1' : '0.4';
  }
}

// ── Settings ──────────────────────────────────────────────────────────────────
const MODE_LABELS = { both: 'Both', grade: 'Grade', attendance: 'Att.', none: 'None' };
const MODE_KEYS   = ['both', 'grade', 'attendance', 'none'];

function buildSettingsCourseList() {
  const list = document.getElementById('settings-course-list');
  if (!list || !courses.length) return;
  list.innerHTML = '';
  const hidden = cfg.hidden_courses || [];
  courses.forEach(c => {
    const row = document.createElement('div');
    row.className = 'course-row';
    const checked = !hidden.includes(c.raw);
    const curMode = displayModes[c.raw] || 'both';

    const seg = document.createElement('div');
    seg.className = 'seg';
    seg.addEventListener('click', e => e.stopPropagation());
    MODE_KEYS.forEach(m => {
      const btn = document.createElement('button');
      btn.textContent = MODE_LABELS[m];
      if (m === curMode) btn.classList.add('active');
      btn.addEventListener('click', () => setDisplayMode(c.raw, m, btn));
      seg.appendChild(btn);
    });

    const toggle = document.createElement('label');
    toggle.className = 'toggle';
    toggle.addEventListener('click', e => e.stopPropagation());
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = checked;
    cb.addEventListener('change', () => toggleCourse(c.raw, cb.checked));
    toggle.appendChild(cb);
    const slider = document.createElement('span');
    slider.className = 'slider';
    toggle.appendChild(slider);

    const dot = document.createElement('div');
    dot.className = `dot ${c.status}`;
    const nameEl = document.createElement('span');
    nameEl.className = 'course-name';
    nameEl.title = c.display;
    nameEl.textContent = c.display;

    row.appendChild(dot);
    row.appendChild(nameEl);
    row.appendChild(seg);
    row.appendChild(toggle);
    list.appendChild(row);
  });
}

function setDisplayMode(raw, mode, btn) {
  btn.closest('.seg').querySelectorAll('button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  displayModes[raw] = mode;
  send('set_course_display', { name: raw, mode });
  showToast('Saved');
}

function toggleCourse(raw, visible) {
  send('toggle_course', { name: raw, visible });
  showToast(visible ? 'Course shown' : 'Course hidden');
}

function checkForUpdates() {
  const el = document.getElementById('update-status');
  if (el) el.textContent = 'Checking…';
  send('check_update');
}

function syncCourses() {
  setSyncState(true);
  send('sync_courses');
}

function setUpdateResult(msg) {
  const el = document.getElementById('update-status');
  if (el) el.textContent = msg;
}

// ── License ───────────────────────────────────────────────────────────────────
function initLicense() {
  const badge     = document.getElementById('lic-badge');
  const detailRow = document.getElementById('lic-detail-row');
  const detail    = document.getElementById('lic-detail');
  const actSect   = document.getElementById('activate-section');
  const deacSect  = document.getElementById('deactivate-section');
  const buySect   = document.getElementById('get-license-section');
  if (!badge) return;

  const state = licData.state || 'no_license';
  const map = {
    active:     ['● Active',                                      'active'],
    trial:      [`Free trial — ${licData.days_left ?? '?'} days left`, 'trial'],
    grace:      ['Grace period',                                  'trial'],
    expired:    ['Expired',                                       'expired'],
    no_license: ['No license',                                    'expired'],
  };
  const [text, cls] = map[state] || ['Unknown', 'expired'];
  badge.textContent = text;
  badge.className   = 'badge ' + cls;

  if (licData.email) {
    detail.textContent      = licData.email;
    detailRow.style.display = '';
  }

  if (state === 'active') {
    actSect.style.display  = 'none';
    deacSect.style.display = '';
    buySect.style.display  = 'none';
  } else {
    deacSect.style.display = 'none';
    buySect.style.display  = '';
  }
}

function setKeyInput(val) {
  const el = document.getElementById('lic-key');
  if (el) el.value = val;
}

function pasteKey() { send('paste_key'); }

function activateKey() {
  const key = (document.getElementById('lic-key')?.value || '').trim();
  if (!key) return;
  setLicResult('Activating…', '');
  send('activate_key', { key });
}

function deactivateLicense() {
  const el = document.getElementById('deac-result');
  if (el) el.textContent = 'Deactivating…';
  send('deactivate_key');
}

function setLicResult(msg, color) {
  const el = document.getElementById('lic-result');
  if (el) { el.textContent = msg; el.style.color = color || 'var(--text)'; }
}

function setDeacResult(msg, color) {
  const el = document.getElementById('deac-result');
  if (el) { el.textContent = msg; el.style.color = color || 'var(--text)'; }
}

init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow:
    """Main app window with sidebar navigation. Singleton-per-instance — create
    once in BBTrackerApp.__init__ and call show(tab=...) from anywhere."""

    def __init__(self, on_change=None, on_refresh=None, on_sync=None,
                 on_reveal_log=None, on_self_update=None):
        self._on_change      = on_change
        self._on_refresh     = on_refresh
        self._on_sync        = on_sync
        self._on_reveal_log  = on_reveal_log
        self._on_self_update = on_self_update
        self._window  = None
        self._webview = None
        self._handler = None

    # ── Public ────────────────────────────────────────────────────────────────

    def show(self, tab: str = "dashboard", subtab: str | None = None) -> None:
        """Open or focus the window. subtab only applies when tab='settings'."""
        from AppKit import NSApp
        if self._window is not None:
            try:
                visible = self._window.isVisible()
            except Exception:
                visible = False
                self._window = None
                self._webview = None

            if visible:
                js = self._make_init_js()
                js += f"\nrefreshData({json.dumps(tab)});"
                if subtab:
                    js += f"\nshowSettingsTab({json.dumps(subtab)});"
                self._webview.evaluateJavaScript_completionHandler_(js, None)
                self._window.makeKeyAndOrderFront_(None)
                NSApp.activateIgnoringOtherApps_(True)
                return

        self._build(initial_tab=tab, initial_subtab=subtab)
        self._window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def set_sync_state(self, active: bool) -> None:
        """Reflect scrape-in-progress state in the UI. Safe to call when closed."""
        if self._webview is None:
            return
        js = "setSyncState(true)" if active else "setSyncState(false)"
        try:
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self, initial_tab: str = "dashboard", initial_subtab: str | None = None) -> None:
        W, H = 1000, 660
        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width  - W) / 2
        y = (screen.size.height - H) / 2

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskResizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H), style, NSBackingStoreBuffered, False
        )
        win.setReleasedWhenClosed_(False)
        win.setTitle_("BB Tracker")
        win.setMinSize_((800.0, 550.0))
        win.setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.039, 0.039, 0.059, 1.0)
        )

        ucc = WKUserContentController.alloc().init()

        init_js = self._make_init_js()
        nav_js = ""
        if initial_tab != "dashboard":
            nav_js += f"showTab({json.dumps(initial_tab)});"
        if initial_subtab:
            nav_js += f"showSettingsTab({json.dumps(initial_subtab)});"
        if nav_js:
            init_js += f"\ndocument.addEventListener('DOMContentLoaded',()=>{{ {nav_js} }});"

        script = WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            init_js, 0, True  # WKUserScriptInjectionTimeAtDocumentStart
        )
        ucc.addUserScript_(script)

        self._handler = _AppMessageHandler.alloc().initWithCallback_(self._on_message)
        ucc.addScriptMessageHandler_name_(self._handler, "app")

        wk_cfg = WKWebViewConfiguration.alloc().init()
        wk_cfg.setUserContentController_(ucc)

        bounds = win.contentView().bounds()
        wv = WKWebView.alloc().initWithFrame_configuration_(bounds, wk_cfg)
        wv.setAutoresizingMask_(18)  # width + height sizable
        wv.loadHTMLString_baseURL_(MAIN_HTML, None)
        win.contentView().addSubview_(wv)

        self._window  = win
        self._webview = wv

    def _make_init_js(self) -> str:
        from version import APP_VERSION

        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            cfg = {}

        try:
            data = json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() else {}
        except Exception:
            data = {}

        try:
            all_courses = json.loads(COURSES_PATH.read_text()) if COURSES_PATH.exists() else []
        except Exception:
            all_courses = []

        # Build grade weights map
        weights_map: dict[str, dict] = {}
        for c in all_courses:
            name = c.get("name") or ""
            weights_map[name] = c.get("grade_weights") or bundled_weights_for(name)

        # Pre-compute grade breakdowns so JS doesn't need to re-implement the logic
        computed: dict = {}
        for course in data.get("courses") or []:
            name    = course["name"]
            grades  = course.get("grades") or {}
            assigns = grades.get("assignments") or []
            weights = weights_map.get(name) or {}
            weighted, cats, coverage = build_grade_categories(assigns, weights)

            # Inject Pearson mini-test average into any ungraded mini-test category.
            # Blackboard often posts these scores late; Pearson has them already.
            pearson  = course.get("pearson") or {}
            mt_avg   = pearson.get("mini_tests_avg")
            mt_count = pearson.get("mini_tests_count") or 0
            if mt_avg is not None and cats:
                for cat in cats:
                    n = cat["name"].lower()
                    is_mini = ("mini" in n and "test" in n) or ("mini" in n and "quiz" in n)
                    if is_mini and cat["score"] is None:
                        cat["score"]            = mt_avg
                        cat["count"]            = mt_count
                        cat["pearson_injected"] = True
                        break
                # Recompute weighted overall with the newly-graded category
                graded_cats = [c for c in cats if c["score"] is not None]
                if graded_cats:
                    wt = sum(c["weight"] for c in graded_cats)
                    weighted = round(
                        sum(c["score"] * c["weight"] for c in graded_cats) / wt, 1
                    ) if wt else None

            computed[name] = {
                "weighted_overall": weighted,
                "categories":       cats,
                "coverage":         coverage,
            }

        # Attendance status for settings course list dots
        status_map: dict[str, str] = {}
        for c in data.get("courses") or []:
            rem = c.get("absences_remaining")
            if rem is None:
                status_map[c["name"]] = "grey"
            elif rem <= 0:
                status_map[c["name"]] = "red"
            elif rem <= 2:
                status_map[c["name"]] = "yellow"
            else:
                status_map[c["name"]] = "green"

        # Course list for settings pane (enabled courses from courses.json)
        settings_courses: list[dict] = []
        for c in all_courses:
            if not c.get("enabled", True):
                continue
            name = c["name"]
            settings_courses.append({
                "raw":     name,
                "display": _smart_title(name),
                "status":  status_map.get(name, "grey"),
            })
        if not settings_courses:
            for name, status in status_map.items():
                settings_courses.append({"raw": name, "display": _smart_title(name), "status": status})

        lic          = _lic.get_status()
        display_modes = cfg.get("course_display_modes", {})

        # Load announcements from disk
        try:
            from scraper import load_announcements as _load_anns
            announcements = _load_anns()
        except Exception:
            announcements = []

        return (
            f"window.__config        = {json.dumps(cfg)};\n"
            f"window.__data          = {json.dumps(data)};\n"
            f"window.__courses       = {json.dumps(settings_courses)};\n"
            f"window.__computed      = {json.dumps(computed)};\n"
            f"window.__license       = {json.dumps(lic)};\n"
            f"window.__displayModes  = {json.dumps(display_modes)};\n"
            f"window.__appVersion    = {json.dumps(APP_VERSION)};\n"
            f"window.__loginItemEnabled = {json.dumps(login_item.is_enabled())};\n"
            f"window.__announcements = {json.dumps(announcements)};\n"
        )

    # ── Message handling ──────────────────────────────────────────────────────

    def _on_message(self, body: str) -> None:
        try:
            msg = json.loads(body)
            self._handle_action(msg.get("action"), msg)
        except Exception as e:
            print(f"[main_window] message handler error: {e}")

    def _handle_action(self, action: str, msg: dict) -> None:
        if action == "set":
            self._write_config(msg["key"], msg["value"])

        elif action == "set_notif":
            # Merge one notification preference key into config.notifications
            key   = msg.get("key")
            value = msg.get("value")
            if key is not None:
                try:
                    cfg = json.loads(CONFIG_PATH.read_text())
                    prefs = cfg.get("notifications") or {}
                    prefs[key] = value
                    cfg["notifications"] = prefs
                    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
                    if self._on_change:
                        self._on_change()
                except Exception as e:
                    print(f"[main_window] set_notif failed: {e}")

        elif action == "toggle_course":
            self._toggle_course(msg["name"], msg["visible"])

        elif action == "set_course_display":
            self._set_course_display(msg["name"], msg["mode"])

        elif action in ("paste_key", "paste_key_silent"):
            import re
            result = subprocess.run(["pbpaste"], capture_output=True, text=True)
            key = result.stdout.strip()
            if action == "paste_key_silent" and not re.match(
                r'^[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}$', key
            ):
                key = ""
            if key and self._webview:
                self._webview.evaluateJavaScript_completionHandler_(
                    f"setKeyInput({json.dumps(key)})", None
                )

        elif action == "activate_key":
            key = msg.get("key", "").strip()
            ok, err = _lic.activate(key)
            js = ("setLicResult('✓ License activated — enjoy BB Tracker!', 'var(--green)')"
                  if ok else
                  f"setLicResult({json.dumps(err or 'Activation failed — check the key and try again.')}, 'var(--red)')")
            if self._webview:
                self._webview.evaluateJavaScript_completionHandler_(js, None)
            if self._on_change:
                self._on_change()

        elif action == "deactivate_key":
            _lic.deactivate()
            js = ("setDeacResult('✓ Deactivated — activate on your new Mac with the same key.', 'var(--green)');"
                  "setTimeout(()=>{ window.__license.state='no_license'; initLicense(); }, 1500);")
            if self._webview:
                self._webview.evaluateJavaScript_completionHandler_(js, None)
            if self._on_change:
                self._on_change()

        elif action == "check_update":
            self._check_update_now()

        elif action == "open_update":
            from version import DOWNLOAD_URL
            url     = getattr(self, "_pending_update_url",     DOWNLOAD_URL)
            version = getattr(self, "_pending_update_version", "")
            if self._on_self_update:
                self._on_self_update(url, version)
            else:
                subprocess.run(["open", url])

        elif action == "open_buy":
            from version import STORE_YEARLY
            subprocess.run(["open", STORE_YEARLY])

        elif action == "sync_courses":
            if self._on_sync:
                self._on_sync()

        elif action == "refresh":
            if self._on_refresh:
                self._on_refresh()

        elif action == "reveal_log":
            if self._on_reveal_log:
                self._on_reveal_log()

        elif action == "open_bb":
            try:
                url = json.loads(CONFIG_PATH.read_text()).get("blackboard_base_url", "")
                if url:
                    subprocess.run(["open", url])
            except Exception:
                pass

        elif action == "set_login_item":
            if msg.get("enabled"):
                login_item.enable()
            else:
                login_item.disable()

    def _check_update_now(self) -> None:
        import urllib.request, ssl
        from version import VERSION_URL, APP_VERSION, DOWNLOAD_URL
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
                latest = data.get("version", "0")
                if _v(latest) > _v(APP_VERSION):
                    url = data.get("url") or data.get("download_url") or DOWNLOAD_URL
                    js = (f"setUpdateResult('v{latest} available — ');"
                          f"document.getElementById('update-status').innerHTML="
                          f"'v{latest} available — <a onclick=\"send(\\'open_update\\')\" "
                          f"style=\"color:var(--accent2);cursor:pointer\">Download →</a>';")
                    self._pending_update_url     = url
                    self._pending_update_version = latest
                else:
                    js = f"setUpdateResult('You\\'re on the latest version (v{APP_VERSION})');"
            except Exception:
                js = "setUpdateResult('Could not check — are you connected to the internet?');"
            if self._webview:
                self._webview.evaluateJavaScript_completionHandler_(js, None)

        threading.Thread(target=_fetch, daemon=True).start()

    def _write_config(self, key: str, value) -> None:
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            cfg[key] = value
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            if self._on_change:
                self._on_change()
        except Exception as e:
            print(f"[main_window] config write failed for {key}: {e}")

    def _toggle_course(self, raw: str, visible: bool) -> None:
        try:
            cfg    = json.loads(CONFIG_PATH.read_text())
            hidden = cfg.get("hidden_courses", [])
            if visible:
                hidden = [n for n in hidden if n != raw]
            elif raw not in hidden:
                hidden.append(raw)
            cfg["hidden_courses"] = hidden
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            if self._on_change:
                self._on_change()
        except Exception as e:
            print(f"[main_window] toggle_course failed: {e}")

    def _set_course_display(self, name: str, mode: str) -> None:
        if mode not in ("both", "grade", "attendance", "none"):
            return
        try:
            cfg   = json.loads(CONFIG_PATH.read_text())
            modes = cfg.get("course_display_modes", {})
            modes[name] = mode
            cfg["course_display_modes"] = modes
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            if self._on_change:
                self._on_change()
        except Exception as e:
            print(f"[main_window] set_course_display failed: {e}")
