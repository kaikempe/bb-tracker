<div align="center">

# BB Tracker

**Your Blackboard grades and attendance, live in your macOS menu bar.**

A native menu bar app for IE University students that scrapes Blackboard Ultra in the background and shows live grades, weighted averages, and remaining absences at a glance — no more refreshing Blackboard every day.

[![Platform](https://img.shields.io/badge/platform-macOS%2012%2B-lightblue?logo=apple&logoColor=white)](https://bblivetracker.netlify.app)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-45ba4b?logo=playwright&logoColor=white)](https://playwright.dev)
[![PyObjC](https://img.shields.io/badge/PyObjC-Cocoa%20%2F%20WebKit-FF6B00)](https://pyobjc.readthedocs.io)
[![License](https://img.shields.io/badge/license-proprietary-red)](https://bblivetracker.netlify.app)

[**Download for macOS →**](https://bblivetracker.netlify.app) &nbsp;·&nbsp; [Website](https://bblivetracker.netlify.app) &nbsp;·&nbsp; 7-day free trial · No credit card required

> Solo project, shipped end-to-end as a paid macOS product (~5,700 lines of Python). This repository is a public showcase — see [*A note on the source code*](#a-note-on-the-source-code) below.

</div>

---

## Screenshots

<p align="center">
  <img src="screenshots/menu-open.png" alt="Menu bar dropdown" width="380">
  &nbsp;&nbsp;
  <img src="screenshots/settings.png" alt="Settings — courses" width="280">
  &nbsp;&nbsp;
  <img src="screenshots/settings-rules.png" alt="Settings — attendance rules" width="280">
</p>

<p align="center">
  <em>Status indicator in the menu bar · per-course grade + attendance breakdown · settings window rendered in WebKit</em>
</p>

<p align="center">
  <img src="screenshots/website.png" alt="Marketing site" width="760"><br>
  <em>Marketing site — <a href="https://bblivetracker.netlify.app">bblivetracker.netlify.app</a></em>
</p>

---

## What it does

BB Tracker lives in your menu bar and surfaces a single status icon summarizing your attendance standing across all courses. Click the icon for a full per-course breakdown — grades and absences — without opening a browser.

| Indicator | Meaning |
|:---------:|---------|
| 🟢 | All courses within the safe attendance zone |
| 🟡 | At least one course at ≤ 2 absences remaining |
| 🔴 | At or over the 80% attendance limit |
| 🔑 | Microsoft session expired — click to re-login |

The app re-scrapes Blackboard automatically in the background (default every 2 hours) and fires a native macOS notification the moment a grade is posted or changes.

---

## Features

- **Weighted grade calculation** — auto-parses each course's syllabus PDF to extract assessment categories and weights, then computes the real weighted grade rather than a flat average.
- **Attendance engine** — enforces IE's 80% rule with a configurable `max_absence_pct` and `late_absence_weight` (default 0.5, matching IE's policy that a late counts as half an absence).
- **Dual-source grade scraping** — pulls grades from Blackboard's internal grade API (intercepted via Playwright's network layer) plus a text-parsing fallback, with deduplication.
- **Grade-change notifications** — diffs each scrape against the previous result and sends a native macOS notification when a grade is posted or updated.
- **Auto-refresh** in the background on a configurable interval, with single-instance locking.
- **Course filters** — hide courses you don't want to track, or surface only Blackboard "favorites".
- **Settings window** — full HTML/CSS/JS UI rendered in `WKWebView` inside a native Cocoa window, with a JS↔Python message bridge for live config edits and on-demand sync.
- **First-run onboarding** — full-screen WebKit flow that walks new users through Microsoft SSO + Authenticator login, handles browser install progress, and self-heals if interrupted.
- **In-app auto-updater** — checks the marketing site for new versions, downloads the `.dmg` with a live progress window (progress bar, MB counter, cancel), and replaces the installed `.app` bundle in place.
- **Login-item registration** for auto-start at login.

---

## How it works

**Authentication against Microsoft SSO.** The hard part of any Blackboard scraper is surviving Microsoft's MFA + Authenticator flow. BB Tracker uses Playwright with a persistent Chromium profile, so the user logs in once interactively and cookies survive Microsoft's 30–90 day session-token rotations. When a session does expire, the app detects it and surfaces the 🔑 prompt.

**Syllabus-driven grade weighting.** Most trackers either ignore weighting or make the user enter it manually. BB Tracker downloads each course's syllabus PDF, parses it with `pdfplumber`, and uses keyword normalization (singularization + synonyms) to map syllabus categories to gradebook columns. There's a bundled-weights fallback for known IE courses when the syllabus can't be parsed.

**WebKit + Cocoa GUI inside a Python app.** Settings, onboarding, and pricing windows are full HTML/CSS/JS pages rendered in `WKWebView`, embedded in native `NSWindow`s via PyObjC, with `WKScriptMessageHandler` bridging JS calls back to Python. The UI stays flexible (it's a webpage) while still feeling native.

**Anti-tamper trial without a backend.** The 7-day trial start is signed and stored in two places — a config file *and* an `NSUserDefaults` anchor. The app always trusts the **earliest** timestamp, so deleting the config file doesn't reset the trial clock. Paid licenses validate against the Lemon Squeezy API with a grace-period state for offline use.

**Custom URL scheme deep-linking.** The activation page links to `bbtracker://activate?key=…`, handled by the running app via Cocoa's `NSAppleEventManager`. One click on the website activates the license inside the app — no copy-paste.

**Self-updating native app.** The updater downloads the new `.dmg`, mounts it, and replaces the installed `.app` bundle in place — including the running binary — with a clean relaunch handoff.

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Language | Python 3.12 | Core application logic |
| Scraping | Playwright (Chromium) | Blackboard login + grade scraping |
| GUI | PyObjC — Cocoa, WebKit, Foundation | Menu bar shell, native windows, JS bridge |
| PDF parsing | pdfplumber | Syllabus weight extraction |
| Packaging | PyInstaller + signed `.dmg` | macOS app bundle distribution |
| Payments | Lemon Squeezy | License generation + validation API |
| Auto-start | launchd | Login-item registration |
| Website | Hand-rolled HTML/CSS | Marketing + activation site (Netlify) |

---

## Project Scale

| Metric | Value |
|--------|-------|
| Lines of Python | ~5,700 |
| Modules | ~10 (`menubar`, `scraper`, `settings_window`, `license`, `updater`, …) |
| Contributors | 1 (solo, end-to-end) |
| Scope | Code · packaging · signing · marketing site · payments · support |
| Distribution | Versioned `.dmg` releases via in-app updater |
| Business model | Free trial → monthly / yearly / lifetime tiers |

---

## Download

Grab the latest `.dmg` from **[bblivetracker.netlify.app](https://bblivetracker.netlify.app)**.

Requires macOS 12+. 7-day free trial, no credit card required.

---

## A note on the source code

BB Tracker is a commercial product — the source code is **proprietary and not included in this repository**. This repo exists as a public-facing project description for portfolio purposes.

If you're a recruiter or engineer interested in seeing specific parts of the implementation, I'm happy to walk through code in an interview. Reach me at **kai@kempe-family.de**.

---

<div align="center">
<sub>BB Tracker is an independent project and is not affiliated with, endorsed by, or sponsored by IE University or Anthology Inc. (Blackboard). All product names, logos, and trademarks are the property of their respective owners.</sub>
</div>
