"""Change detection and macOS notification dispatch for BB Tracker.

Compares a new scrape result against the previous one and fires
rumps.notification() for each meaningful change:
  - new grade posted for any assignment
  - grade score changed (re-grade or correction)
  - new announcement (from announcements.json)
  - attendance warning (absences_remaining drops to threshold)

Notification preferences live in config.json under the key "notifications".
Call process_changes() from the menubar after every successful scrape.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import rumps as _rumps

# Keywords that flag an announcement as "action required".
_ACTION_KEYWORDS = re.compile(
    r"\b(?:deadline|due|submit|submission|mandatory|required|exam|quiz|test|"
    r"homework|hw|assignment|reminder|urgent|important|attention|project|"
    r"presentation|today|tomorrow|this\s+week|by\s+\w+day|hand.?in|graded)\b",
    re.IGNORECASE,
)

_DEFAULT_PREFS: dict = {
    "new_grade":              True,
    "grade_changed":          True,
    "new_announcement":       True,
    "announcement_filter":    True,   # only notify for action-required announcements
    "attendance_warning":     True,
    "attendance_threshold":   2,      # notify when remaining drops to this or below
}


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

def load_prefs(config: dict) -> dict:
    """Merge stored notification prefs with defaults."""
    stored = config.get("notifications") or {}
    return {**_DEFAULT_PREFS, **stored}


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_grade_changes(old_data: dict, new_data: dict) -> list[dict]:
    """Return events for new or changed grades."""
    events: list[dict] = []
    if not old_data or not new_data:
        return events

    old_by_name = {c["name"]: c for c in old_data.get("courses", [])}

    for course in new_data.get("courses", []):
        name = course["name"]
        old_course = old_by_name.get(name) or {}

        old_assigns = {
            a["name"]: a
            for a in (old_course.get("grades") or {}).get("assignments", [])
        }
        new_assigns = (course.get("grades") or {}).get("assignments", [])

        for a in new_assigns:
            aname = a["name"]
            score = a.get("score")
            possible = a.get("possible")

            if aname not in old_assigns:
                if score is not None:
                    events.append({
                        "type": "new_grade",
                        "course": name,
                        "assignment": aname,
                        "score": score,
                        "possible": possible,
                    })
            else:
                old_score = old_assigns[aname].get("score")
                if score is not None and old_score != score:
                    events.append({
                        "type": "grade_changed",
                        "course": name,
                        "assignment": aname,
                        "old_score": old_score,
                        "new_score": score,
                        "possible": possible,
                    })

    return events


def detect_attendance_warnings(old_data: dict, new_data: dict, threshold: int) -> list[dict]:
    """Return events when absences_remaining drops to or below threshold."""
    events: list[dict] = []
    if not new_data:
        return events

    old_by_name = {c["name"]: c for c in (old_data or {}).get("courses", [])}

    for course in new_data.get("courses", []):
        name = course["name"]
        remaining = course.get("absences_remaining")
        if remaining is None:
            continue
        old_remaining = old_by_name.get(name, {}).get("absences_remaining")
        # Fire if: newly at/below threshold, OR went from above to at/below.
        was_ok = old_remaining is None or old_remaining > threshold
        now_warning = remaining <= threshold
        if was_ok and now_warning:
            events.append({
                "type": "attendance_warning",
                "course": name,
                "remaining": remaining,
            })

    return events


def detect_new_announcements(
    old_anns: list[dict], new_anns: list[dict], action_filter: bool
) -> list[dict]:
    """Return events for genuinely new announcements."""
    events: list[dict] = []
    if not new_anns:
        return events

    old_ids = {a.get("id") for a in (old_anns or [])}

    for ann in new_anns:
        ann_id = ann.get("id")
        if ann_id and ann_id in old_ids:
            continue
        title = ann.get("title") or ""
        body  = ann.get("body")  or ""
        if action_filter and not _ACTION_KEYWORDS.search(title + " " + body):
            continue
        events.append({
            "type": "new_announcement",
            "course": ann.get("course_name") or "",
            "title": title,
            "body": body[:200],
        })

    return events


# ---------------------------------------------------------------------------
# Notification dispatch
# ---------------------------------------------------------------------------

def _pct_str(score, possible) -> str:
    if score is None:
        return "—"
    if possible and possible > 0:
        return f"{score / possible * 100:.0f}%"
    return f"{score:.1f}"


def fire_notifications(events: list[dict], prefs: dict) -> None:
    """Fire macOS notifications for the given events, respecting prefs."""
    try:
        import rumps
    except ImportError:
        return

    for ev in events:
        ev_type = ev["type"]

        if ev_type == "new_grade" and prefs.get("new_grade"):
            course = ev["course"].title()
            score_str = _pct_str(ev.get("score"), ev.get("possible"))
            rumps.notification(
                title=f"New grade — {course[:40]}",
                subtitle=ev.get("assignment", "")[:60],
                message=score_str,
            )

        elif ev_type == "grade_changed" and prefs.get("grade_changed"):
            course = ev["course"].title()
            old_s = _pct_str(ev.get("old_score"), ev.get("possible"))
            new_s = _pct_str(ev.get("new_score"), ev.get("possible"))
            rumps.notification(
                title=f"Grade updated — {course[:40]}",
                subtitle=ev.get("assignment", "")[:60],
                message=f"{old_s} → {new_s}",
            )

        elif ev_type == "attendance_warning" and prefs.get("attendance_warning"):
            rem = ev.get("remaining", 0)
            course = ev["course"].title()
            msg = "At the absence limit!" if rem <= 0 else f"{rem} absence{'s' if rem != 1 else ''} left"
            rumps.notification(
                title=f"Attendance alert — {course[:40]}",
                subtitle=msg,
                message="Check your attendance in BB Tracker",
            )

        elif ev_type == "new_announcement" and prefs.get("new_announcement"):
            course = ev.get("course", "").title()
            title  = ev.get("title", "New announcement")
            body   = ev.get("body", "")
            rumps.notification(
                title=course[:50] if course else "New announcement",
                subtitle=title[:80],
                message=body[:120] if body else "",
            )


# ---------------------------------------------------------------------------
# Main entry point (called from menubar after each scrape)
# ---------------------------------------------------------------------------

def process_changes(
    old_data: dict | None,
    new_data: dict | None,
    old_anns: list[dict] | None,
    new_anns: list[dict] | None,
    config: dict,
) -> None:
    """Detect all changes and fire macOS notifications for new events."""
    prefs = load_prefs(config)

    events: list[dict] = []

    events.extend(detect_grade_changes(old_data or {}, new_data or {}))
    events.extend(detect_attendance_warnings(
        old_data or {}, new_data or {},
        int(prefs.get("attendance_threshold", 2)),
    ))
    events.extend(detect_new_announcements(
        old_anns or [], new_anns or [],
        bool(prefs.get("announcement_filter", True)),
    ))

    if events:
        fire_notifications(events, prefs)
