# Blackboard Attendance Tracker

Menu-bar app for macOS that shows your IE University Blackboard attendance
and how many absences you have left per course (80% rule — 20% cap).

```
🟢  All good
🟡  One or more courses at ≤2 absences remaining
🔴  At limit or over
🔑  Microsoft session expired — re-run setup.py
```

Click the indicator to see every course with `absent / max · remaining`.

---

## Install (once)

Drop this folder anywhere you like — e.g. `~/bb-tracker`.

```bash
cd ~/bb-tracker
./install.sh
```

This creates a `.venv`, installs Playwright + rumps, and downloads Chromium.

If you don't have Python yet: `brew install python@3.12`

## First-time login (once, and again when session expires)

```bash
source .venv/bin/activate
python3 setup.py
```

A browser window opens. **Important**: when Microsoft asks *"Stay signed in?"*
click **Yes** — this is what lets the tracker run silently afterwards.

Finish your Authenticator prompt, wait until you see the Blackboard courses
page, then close the window. Cookies are saved to `browser_data/`.

You'll likely need to repeat this every 30–90 days when Microsoft rotates
the session token. The menu bar will show 🔑 and the top item will say
*"Session expired — run setup.py"*.

## Run the app

```bash
source .venv/bin/activate
python3 menubar.py
```

An indicator appears in your menu bar. It scrapes immediately, then
re-scrapes every 2 hours (configurable in `config.json`). Closing the
terminal will kill the app — see below for auto-start.

## Auto-start at login

Edit `com.kai.bb-tracker.plist` — replace `YOURNAME` with your macOS
username in all three paths. Then:

```bash
cp com.kai.bb-tracker.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kai.bb-tracker.plist
```

To stop it auto-starting:
```bash
launchctl unload ~/Library/LaunchAgents/com.kai.bb-tracker.plist
```

---

## config.json

```json
{
  "blackboard_base_url": "https://campusonline.ie.edu",
  "max_absence_pct": 0.20,
  "refresh_interval_minutes": 120,
  "count_late_as_absent": false,
  "courses": [ … ]
}
```

- `blackboard_base_url` — the root URL of IE's Blackboard. The default is
  `campusonline.ie.edu`; change it if IE moves.
- `max_absence_pct` — the attendance rule. 0.20 = can miss 20%.
- `refresh_interval_minutes` — how often to scrape in the background.
- `count_late_as_absent` — the syllabus you showed says *"80% attendance"*,
  which normally means *Late still counts as attended*. Set this to `true`
  if your program coordinator tells you otherwise.
- `courses` — course names must match how they appear in Blackboard
  (case-insensitive substring match). `course_id` is filled in
  automatically on first run; leave it `null` when adding new courses.

---

## Troubleshooting

**Scraper keeps showing `auth_required`**
Re-run `python3 setup.py` and make sure you tick *"Stay signed in?"*.

**A course shows "course not found"**
The name in `config.json` doesn't match the Blackboard course card.
Open Blackboard, copy the exact name, paste into `config.json`, delete
the `"course_id": "..."` line for that course, save, and refresh.

**A course shows "not tracked on Blackboard"**
The professor doesn't log attendance in Blackboard for that course.
Only workaround is to track it yourself (ask me and I'll add a manual
tab).

**The menu bar icon is 🔴 but Blackboard says I'm fine**
Hover the course line — the tracker uses *Absent* only by default.
If your program counts Late differently, flip `count_late_as_absent`
to `true` in `config.json`.

**First launch takes a while / seems stuck**
First run has to discover course IDs for all 7 courses (≈45s). After
that, data.json is cached and subsequent scrapes are faster.

---

## Files

| File              | What it does |
|-------------------|--------------|
| `config.json`     | Your courses, session counts, thresholds |
| `setup.py`        | Interactive browser login — run once |
| `scraper.py`      | Headless scrape of Blackboard Ultra |
| `menubar.py`      | The menu bar app |
| `install.sh`      | Set up venv + dependencies |
| `requirements.txt`| Python deps |
| `com.kai.bb-tracker.plist` | launchd config for auto-start |
| `browser_data/`   | Persistent Chromium profile (created on first login) |
| `data.json`       | Latest scrape results (created by scraper) |

Never commit `browser_data/` to git — it contains your session cookies.
