"""Blackboard Ultra attendance + grades scraper for IE University."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# ── Data directory ────────────────────────────────────────────────────────────
# When packaged as a .app (sys.frozen is set by py2app), the bundle's
# Resources folder is read-only, so user data lives in Application Support.
# In development, data sits next to the source files as before.
_IS_PACKAGED = bool(getattr(sys, "frozen", None))

if _IS_PACKAGED:
    DATA_DIR = Path.home() / "Library" / "Application Support" / "BBTracker"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(DATA_DIR / "browsers"))
    # PyInstaller extracts resources to sys._MEIPASS; py2app uses RESOURCEPATH
    if hasattr(sys, "_MEIPASS"):
        BASE_DIR = Path(sys._MEIPASS)
    else:
        BASE_DIR = Path(os.environ.get("RESOURCEPATH", str(Path(__file__).parent)))
else:
    DATA_DIR = Path(__file__).parent
    BASE_DIR = Path(__file__).parent

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

CONFIG_PATH         = DATA_DIR / "config.json"
COURSES_PATH        = DATA_DIR / "courses.json"
DATA_PATH           = DATA_DIR / "data.json"
ANNOUNCEMENTS_PATH  = DATA_DIR / "announcements.json"
DEBUG_DIR           = DATA_DIR / "debug"
BROWSER_DIR         = DATA_DIR / "browser_data"

# On first packaged launch, seed config.json from the bundled default
if _IS_PACKAGED and not CONFIG_PATH.exists():
    _default = BASE_DIR / "config.json"
    if _default.exists():
        import shutil
        shutil.copy(_default, CONFIG_PATH)

PAGE_TIMEOUT_MS = 30_000
BROWSER_LAUNCH_TIMEOUT_S = 45    # cap on launch_persistent_context — was hanging silently

HEADFUL = os.environ.get("BB_TRACKER_HEADFUL") == "1"
VERBOSE = os.environ.get("BB_TRACKER_VERBOSE") == "1" or (__name__ == "__main__")

from log import log as _logger  # shared file-based logger (see log.py)


def _log(msg: str) -> None:
    """Append to the shared log file; also print when running interactively."""
    if VERBOSE:
        print(msg, flush=True)
    _logger("scraper", "%s", msg)


# ---------------------------------------------------------------------------
# Config & courses
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def load_courses() -> list[dict]:
    """Load courses.json. On first run, migrates from config.json courses array."""
    if COURSES_PATH.exists():
        with open(COURSES_PATH) as f:
            return json.load(f)

    # Migration: pull courses out of config.json
    config = load_config()
    today = datetime.now().strftime("%Y-%m-%d")
    courses = [
        {
            "name": c["name"],
            "course_id": c["course_id"],
            "total_sessions": c.get("total_sessions"),
            "enabled": True,
            "favorited": False,
            "last_seen": today,
        }
        for c in config.get("courses", [])
    ]
    if courses:
        save_courses(courses)
    return courses


def save_courses(courses: list[dict]) -> None:
    with open(COURSES_PATH, "w") as f:
        json.dump(courses, f, indent=2)


def merge_discovered(existing: list[dict], discovered: list[dict]) -> list[dict]:
    if not discovered:
        return existing  # discovery failed — keep existing list unchanged
    """Merge newly-discovered courses with the saved list, preserving user settings."""
    by_id = {c["course_id"]: c for c in existing}
    today = datetime.now().strftime("%Y-%m-%d")
    result = []
    seen = set()

    for disc in discovered:
        cid = disc["course_id"]
        seen.add(cid)
        if cid in by_id:
            entry = by_id[cid].copy()
            entry["favorited"] = disc.get("favorited", entry.get("favorited", False))
            if disc.get("name") and disc["name"] != cid:
                entry["name"] = disc["name"]
            entry["last_seen"] = today
            entry["enabled"] = True
        else:
            entry = {
                "name": disc.get("name", cid),
                "course_id": cid,
                "total_sessions": None,
                "enabled": True,
                "favorited": disc.get("favorited", False),
                "last_seen": today,
            }
        result.append(entry)

    # Keep old courses not found this time (mark disabled so they don't clutter)
    for ex in existing:
        if ex["course_id"] not in seen:
            result.append({**ex, "enabled": False})

    return result


# ---------------------------------------------------------------------------
# Attendance parsing
# ---------------------------------------------------------------------------

# BB Ultra's attendance panel renders all four counters together:
#   "11 Present | 0 Late | 0 Absent | 0 Excused"
# Anchoring on the cluster avoids false-matching isolated phrases like
# "2 attempts submitted (2 Late)" that appear on gradebook assignment
# rows — those used to bleed through and inflate the late count when the
# attendance page surfaced gradebook content via iframes.
_ATTENDANCE_CLUSTER = re.compile(
    r"(?P<present>\d+)\s+Present\s*[|·•,/-]?\s*"
    r"(?P<late>\d+)\s+Late\s*[|·•,/-]?\s*"
    r"(?P<absent>\d+)\s+Absent"
    r"(?:\s*[|·•,/-]?\s*(?P<excused>\d+)\s+Excused)?",
    re.IGNORECASE,
)

# Loose per-field fallback for layouts that don't render the cluster
# verbatim (older BB UIs, Spanish localization with re-ordered fields).
# Late is tightened with a paren-exclusion lookbehind/lookahead so
# "(2 Late)" assignment markers can't sneak in if we ever fall back here.
ATTENDANCE_PATTERNS = {
    "present": r"(\d+)\s+Present",
    "late":    r"(?<!\()(\d+)\s+Late(?!\s*\))",
    "absent":  r"(\d+)\s+Absent",
    "excused": r"(\d+)\s+Excused",
}


def parse_attendance(body_text: str) -> dict:
    result = {"present": 0, "late": 0, "absent": 0, "excused": 0, "overall_score": None}
    cluster = _ATTENDANCE_CLUSTER.search(body_text)
    if cluster:
        result["present"] = int(cluster.group("present"))
        result["late"]    = int(cluster.group("late"))
        result["absent"]  = int(cluster.group("absent"))
        result["excused"] = int(cluster.group("excused")) if cluster.group("excused") else 0
    else:
        for key, pat in ATTENDANCE_PATTERNS.items():
            m = re.search(pat, body_text)
            if m:
                result[key] = int(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*100", body_text)
    if m:
        result["overall_score"] = float(m.group(1))
    return result


def attendance_url(base_url: str, course_id: str) -> str:
    return f"{base_url}/ultra/courses/{course_id}/grades/attendanceGrade?courseId={course_id}"


# ---------------------------------------------------------------------------
# Grade parsing
# ---------------------------------------------------------------------------

def _process_grade_api(responses: list[dict]) -> dict:
    """Extract structured grade data from captured API JSON responses."""
    result: dict = {"overall_pct": None, "categories": [], "assignments": [], "source": "api"}

    for item in responses:
        data = item["data"]

        # Flatten: could be a list directly or under 'results'/'columns'
        entries: list = []
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = data.get("results") or data.get("columns") or []

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            name = (entry.get("name") or entry.get("displayName") or
                    entry.get("columnName") or "").strip()
            if not name:
                continue

            weight = (entry.get("weight") or entry.get("weightPercentage") or
                      entry.get("weightedPossible"))

            # Score can be nested or flat
            score_obj = entry.get("score") or {}
            score: float | None = None
            possible: float | None = None
            if isinstance(score_obj, (int, float)):
                score = float(score_obj)
            elif isinstance(score_obj, dict):
                raw_s = score_obj.get("given") or score_obj.get("value")
                raw_p = score_obj.get("possible")
                score = float(raw_s) if raw_s is not None else None
                possible = float(raw_p) if raw_p is not None else None

            grade_obj = entry.get("grade") or entry.get("currentGrade") or {}
            if isinstance(grade_obj, dict):
                if score is None:
                    raw_s = grade_obj.get("score") or grade_obj.get("value")
                    if raw_s is not None:
                        score = float(raw_s)
                if result["overall_pct"] is None:
                    pct = grade_obj.get("percentage") or grade_obj.get("percent")
                    if pct is not None:
                        result["overall_pct"] = float(pct)

            if weight is not None:
                result["categories"].append({
                    "name": name,
                    "weight": float(weight),
                    "score": score,
                    "possible": possible,
                })
            elif score is not None:
                # Skip zero-scored large-possible items (alternative exam slots
                # for other students) — same filter as the text-parser path.
                if score == 0 and possible is not None and possible >= 20:
                    continue
                result["assignments"].append({
                    "name": name,
                    "score": score,
                    "possible": possible,
                })

        # Top-level overall grade
        if result["overall_pct"] is None and isinstance(data, dict):
            for key in ("overallGrade", "calculatedGrade", "grade", "total", "overall"):
                val = data.get(key)
                if val is None:
                    continue
                if isinstance(val, dict):
                    pct = val.get("percentage") or val.get("percent") or val.get("value")
                    if pct is not None:
                        result["overall_pct"] = float(pct)
                elif isinstance(val, (int, float)):
                    result["overall_pct"] = float(val)
                if result["overall_pct"] is not None:
                    break

    return result


_NAME_NOISE = re.compile(
    r"\s*[-–]\s*SUBMIT\s+HERE\s*$"
    r"|\s*\(Content isn't available\)\s*$"
    r"|\s*\(Late\)\s*$",
    re.IGNORECASE,
)


def _clean_name(name: str) -> str:
    return _NAME_NOISE.sub("", name).strip(" -–.")


def _deduplicate(assignments: list[dict]) -> list[dict]:
    """If one entry equals the sum of all others it's a total — collapse sub-items."""
    if len(assignments) <= 2:
        return assignments
    tot_s = sum(a["score"] for a in assignments)
    tot_p = sum(a["possible"] for a in assignments)
    for a in assignments:
        rest_s = tot_s - a["score"]
        rest_p = tot_p - a["possible"]
        if abs(a["score"] - rest_s) < 0.05 and abs(a["possible"] - rest_p) < 0.05:
            return [a]
    return assignments


_SKIP_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"^\d+(?:[.,]\d+)?\s*/\s*\d+(?:[.,]\d+)?$",   # X/Y score line (comma or period)
    r"^\d+(?:[.,]\d+)?\s*%$",                      # X% line (comma or period, optional space)
    r"^\d{1,2}/\d{1,2}/\d{2,4}$",                # date M/D/YY
    r"^(?:View|Graded|Submitted|Not graded|Draft saved|Unopened|Ongoing)$",
    r"^Grade is (?:complete|incomplete)$",
    r"^(?:Attempt \d+.*|No attempts submitted|No participation.*)$",
    r"^First participated.*$",
    r"^\d+ attempt[s]? submitted$",
    r"^Formative.*$",
    r"^(?:Offline submission|\(Late\))$",
    r"^(?:Item Name|Due Date|Status|Grade|Results)$",
    r"^(?:Page \d+.*|of \d+|gradebook\..*)$",
    r"^(?:Skip to main content|Courses|\d+)$",
    r"^(?:Content|Calendar|Announcements|Discussions?|Gradebook|Messages|Groups|Achievements)$",
    r"^(?:OPEN|COURSE STATUS.*)$",
    r"^(?:Blue Connector.*|Smowl.*|Hidden frame.*|Help for current page)$",
    r"^(?:Unlimited attempts possible|KAI [A-Z].*)$",
    r"^gradebook\.table\..*$",
    # Spanish locale ─────────────────────────────────────────────────────────
    r"^(?:Vista|Calificado|Sin calificar|Sin abrir|Continuo|Abierto|Cerrado)$",
    r"^(?:Saltar al contenido principal|Cursos|Contenido|Calendario|Anuncios|Debates)$",
    r"^(?:Libro de calificaciones|Mensajes|Grupos|Logros|Calificaciones)$",
    r"^(?:Nombre del elemento|Fecha de vencimiento|Estado|Calificaci[oó]n|Resultados)$",
    r"^(?:Calificaci[oó]n actual|Calificaci[oó]n global)\s*:?\s*$",
    r"^Calificaci[oó]n (?:actual|global)\s+\d+(?:[.,]\d+)?\s*%$",
    r"^\d+\s+intento[s]?\s+posible[s]?$",
    r"^P[aá]gina(?:\s+\d+\s+de\s+\d+)?$",
    r"^de\s+\d+$",
    r"^Ayuda para la p[aá]gina actual$",
    r"^ESTADO DEL CURSO.*$",
    r"^(?:Reuni[oó]n|Please provide feedback.*|Remind me later)$",
]]


def _is_skip_line(line: str) -> bool:
    return not line or any(p.match(line) for p in _SKIP_PATTERNS)


def _find_name(lines_before: list[str]) -> str | None:
    """Scan backwards through lines to find an assignment name."""
    for line in reversed(lines_before):
        line = line.strip()
        if len(line) < 3 or _is_skip_line(line):
            continue
        # Strip trailing status hints
        line = re.sub(r"\s*\|\s*\(Late\)\s*$", "", line).strip()
        return line or None
    return None


def _parse_grades_text(body: str) -> dict:
    """Parse BB gradebook page text into grade data.

    Handles three real BB Ultra formats:
      - 'Final Grade: X points out of Y points possible'  (English, most courses)
      - standalone 'X%' lines                              (some courses, e.g. IE Humanities)
      - 'Calificación actual' / 'Current Grade' header     (Spanish locale, comma decimals)
    """
    result: dict = {"overall_pct": None, "categories": [], "assignments": [], "source": "text"}
    lines = [l.strip() for l in body.split("\n")]

    FINAL_GRADE = re.compile(
        r"^Final Grade: (\d+(?:\.\d+)?) points out of (\d+(?:\.\d+)?) points possible$"
    )
    PCT_ONLY = re.compile(r"^(\d+(?:\.\d+)?)%$")
    # Localized overall: "Calificación actual" / "Current Grade" / "Overall Grade"
    # followed (within a few lines) by "82,42 %" or "82.42%" — comma OR period decimal.
    OVERALL_HEADER = re.compile(
        r"^(?:Calificaci[oó]n actual|Current Grade|Overall Grade|Calificaci[oó]n global)\s*:?\s*$",
        re.IGNORECASE,
    )
    PCT_LOCALIZED = re.compile(r"^(\d+(?:[.,]\d+)?)\s*%$")
    # Localized "X,Y/Z" or "X.Y/Z" score (e.g. "22,5/25", "89.47/100")
    SCORE_FRACTION = re.compile(r"^(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)$")

    # ── Format 1: "Final Grade: X points out of Y points possible" ──────────
    found_fg = False
    for i, line in enumerate(lines):
        m = FINAL_GRADE.match(line)
        if not m:
            continue
        found_fg = True
        scored, possible = float(m.group(1)), float(m.group(2))

        name = _find_name(lines[max(0, i - 12):i])
        if not name:
            continue
        if "Attendance" in name:
            continue
        # Entries scored 0 with large possible are usually alternative exam slots
        if scored == 0 and possible >= 20:
            continue

        result["assignments"].append({"name": _clean_name(name), "score": scored, "possible": possible})

    if found_fg and result["assignments"]:
        result["assignments"] = _deduplicate(result["assignments"])
        total_s = sum(a["score"] for a in result["assignments"])
        total_p = sum(a["possible"] for a in result["assignments"])
        if total_p > 0:
            result["overall_pct"] = round(total_s / total_p * 100, 1)
        return result

    # ── Format 2: standalone percentage lines (e.g. "62%", "95%") ───────────
    for i, line in enumerate(lines):
        m = PCT_ONLY.match(line)
        if not m:
            continue
        pct = float(m.group(1))
        name = _find_name(lines[max(0, i - 10):i])
        if not name or "Attendance" in name:
            continue
        result["assignments"].append({"name": _clean_name(name), "score": pct, "possible": 100.0})

    if result["assignments"]:
        result["assignments"] = _deduplicate(result["assignments"])
        total = sum(a["score"] for a in result["assignments"])
        result["overall_pct"] = round(total / len(result["assignments"]), 1)
        return result

    # ── Format 3: localized header + score-fraction rows ────────────────────
    # Anchor on "Calificación actual" / "Current Grade" header line, then take
    # the next non-empty line that matches a percentage. Also harvest per-row
    # "X,Y/Z" assignment scores so the menu can show the breakdown.
    for i, line in enumerate(lines):
        if not OVERALL_HEADER.match(line):
            continue
        for j in range(i + 1, min(i + 5, len(lines))):
            m = PCT_LOCALIZED.match(lines[j])
            if not m:
                continue
            try:
                pct = float(m.group(1).replace(",", "."))
            except ValueError:
                continue
            # "Calificación global --" appears on attendance pages with no data;
            # skip suspiciously low values that are just placeholders.
            if 0 < pct <= 100:
                result["overall_pct"] = round(pct, 1)
            break
        if result["overall_pct"] is not None:
            break

    # Harvest assignment-name → "X,Y/Z" pairs (Spanish gradebook layout).
    # The score line typically appears 1-4 lines after the assignment name,
    # with whitespace-only lines between.
    for i, line in enumerate(lines):
        m = SCORE_FRACTION.match(line)
        if not m:
            continue
        try:
            score = float(m.group(1).replace(",", "."))
            possible = float(m.group(2).replace(",", "."))
        except ValueError:
            continue
        if possible <= 0:
            continue
        name = _find_name(lines[max(0, i - 8):i])
        if not name:
            continue
        entry = {"name": _clean_name(name), "score": score, "possible": possible}
        if "asistencia" in name.lower() or "attendance" in name.lower():
            result.setdefault("attendance_entries", []).append(entry)
        else:
            result["assignments"].append(entry)

    if result["assignments"]:
        result["assignments"] = _deduplicate(result["assignments"])

    return result


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def _collect_all_frame_text(page) -> str:
    chunks: list[str] = []
    for frame in page.frames:
        try:
            text = await frame.evaluate("() => document.body ? document.body.innerText : ''")
            if text:
                chunks.append(text)
        except Exception:
            continue
    return "\n\n".join(chunks)


async def _debug_dump(page, label: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"\W+", "_", label).strip("_").lower()
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=True)
    except Exception as e:
        _log(f"    (screenshot failed: {e})")
    try:
        html = await page.content()
        (DEBUG_DIR / f"{safe}.html").write_text(html)
    except Exception as e:
        _log(f"    (html dump failed: {e})")
    try:
        (DEBUG_DIR / f"{safe}.txt").write_text(await _collect_all_frame_text(page))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-course scraping
# ---------------------------------------------------------------------------

async def _scrape_attendance(page, course: dict, base_url: str) -> dict:
    course_id = course.get("course_id")
    if not course_id:
        return {"error": "no_course_id"}

    url = attendance_url(base_url, course_id)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    except PWTimeout:
        return {"error": "timeout"}

    try:
        await page.wait_for_function(
            r"""
            () => {
                const scan = doc => doc?.body?.innerText || '';
                let text = scan(document);
                for (const f of document.querySelectorAll('iframe')) {
                    try { text += '\n' + scan(f.contentDocument); } catch (e) {}
                }
                return /\d+\s+Present|\d+\s+Absent|No attendance|No data|not\s+tracked/i.test(text);
            }
            """,
            timeout=20_000,
        )
    except PWTimeout:
        pass

    await page.wait_for_timeout(2500)
    body = await _collect_all_frame_text(page)

    if "Present" not in body and "Absent" not in body:
        await _debug_dump(page, course["name"])
        return {"error": "no_attendance_tracking"}

    return parse_attendance(body)


async def _scrape_grades(page, course: dict, base_url: str) -> dict:
    """Scrape the BB gradebook for one course."""
    course_id = course.get("course_id")
    if not course_id:
        return {"error": "no_course_id"}

    url = f"{base_url}/ultra/courses/{course_id}/grades"
    captured: list[dict] = []

    async def on_response(response):
        if response.status != 200:
            return
        if "json" not in response.headers.get("content-type", ""):
            return
        rurl = response.url.lower()
        # Skip obviously unrelated endpoints
        if any(k in rurl for k in ["analytics", "telemetry", "logging", "metrics", "track"]):
            return
        try:
            data = await response.json()
            if data:
                captured.append({"url": response.url, "data": data})
        except Exception:
            pass

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    except PWTimeout:
        page.remove_listener("response", on_response)
        return {"error": "timeout"}

    await page.wait_for_timeout(3000)
    page.remove_listener("response", on_response)

    _log(f"    grades: {len(captured)} API responses")

    if captured:
        result = _process_grade_api(captured)
        if result.get("overall_pct") is not None or result.get("categories") or result.get("assignments"):
            return result

    # Fallback: parse page text
    body = await _collect_all_frame_text(page)
    result = _parse_grades_text(body)

    if not result.get("overall_pct") and not result.get("categories") and not result.get("assignments"):
        await _debug_dump(page, f"grades_{course['name']}")
        # Also dump the raw API JSON so we can diagnose accounts where the
        # API returns 50+ responses but the parser drops every entry —
        # e.g. fields under unexpected keys or scores nested deeper than
        # `score.given`/`score.value`. HTML/PNG alone don't show this.
        try:
            safe = re.sub(r"\W+", "_", course["name"]).strip("_").lower()
            DEBUG_DIR.mkdir(exist_ok=True)
            payload = [{"url": c["url"], "data": c["data"]} for c in captured]
            (DEBUG_DIR / f"grades_{safe}_api.json").write_text(
                json.dumps(payload, indent=2, default=str)[:5_000_000]
            )
        except Exception as e:
            _log(f"    (api json dump failed: {e})")
        result["error"] = "no_grade_data"

    return result


# ---------------------------------------------------------------------------
# Syllabus scraping & grade weight matching
# ---------------------------------------------------------------------------

_WORKLOAD_SKIP = {
    "lectures", "discussions", "studying", "preparation", "self-study",
}
_WEIGHT_SKIP = {"total", "criteria", "score", "comments", "objectives", "method"}

# Semantic aliases: word in a category name → extra keywords to match assignments.
# Aliases must be symmetric — if "partial" expands to {"midterm", ...}, then
# "midterm" must expand to {"partial", ...} too. Otherwise a syllabus category
# named "Midterm Evaluation" silently fails to match an assignment named
# "Partial Exam" (and vice versa). Spanish equivalents kept for IE bilingual
# courses (e.g. "Examen Parcial").
_GRADE_ALIASES: dict[str, set[str]] = {
    "final":         {"final"},
    "exam":          {"exam"},
    "intermediate":  {"mid", "midterm", "partial", "parcial", "term", "quiz"},
    "test":          {"test", "exam"},
    "midterm":       {"midterm", "mid", "term", "partial", "parcial"},
    "partial":       {"midterm", "mid", "partial", "parcial"},
    "parcial":       {"midterm", "mid", "partial", "parcial"},
    "work":          {"work", "group", "team", "project"},
    "group":         {"group", "team", "project", "workgroup"},
    "practice":      {"practice", "exercises", "practical"},
    "participation": {"participation", "class"},
    "assignment":    {"assignment", "practice", "homework"},
    "other":         {"quiz", "practice", "exercise", "other"},
}


# Irregular plurals we care about in IE syllabus / Blackboard column names.
_IRREGULAR_SINGULAR = {"quizzes": "quiz", "classes": "class", "analyses": "analysis"}


def _singularize(word: str) -> str:
    """Cheap singular form. Why: a syllabus row "Quizzes 20%" has to match
    a Blackboard assignment "Quiz 1", but {quizzes} & {quiz} is empty under
    exact-word matching. We don't need NLP — just enough to fold the most
    common plural endings (quizzes, tests, exams, labs, classes) so a
    student's quiz score doesn't silently fall out of the weighted total.
    """
    if word in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[word]
    if len(word) >= 5 and word.endswith("es") and word[-3] in "sxz":
        return word[:-2]   # "boxes" → "box"
    if len(word) >= 5 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]   # "tests" → "test", but leave "class"
    return word


def _normalize_words(text: str) -> set[str]:
    """Tokenize + add singular forms so plural/singular variants match."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    return words | {_singularize(w) for w in words}


def _expand_keywords(category_name: str) -> set[str]:
    words = _normalize_words(category_name)
    expanded = set(words)
    for word in words:
        expanded |= _GRADE_ALIASES.get(word, set())
        for key, vals in _GRADE_ALIASES.items():
            if len(key) >= 5 and key in word:
                expanded |= vals
    return expanded


def _parse_weights(text: str) -> dict[str, float]:
    """Extract grade component weights from IE University syllabus text.
    Returns {category_name: weight_as_fraction}.
    """
    section = re.search(
        r"(?:EVALUATION METHOD|EVALUATION CRITERIA|GRADING CRITERIA|ASSESSMENT CRITERIA)"
        r".*?\n(.*?)(?:\nRE-SIT|\nRE-TAKE|\Z)",
        text, re.IGNORECASE | re.DOTALL,
    )
    search = section.group(0) if section else text

    weights: dict[str, float] = {}
    seen: set[str] = set()

    # Matches tab- or space-separated: "Category Name\tNN %" or "Name  NN %"
    # Use [ \t] not \s to avoid matching newlines inside the name
    row = re.compile(
        r"^([A-Za-z][A-Za-z \t/&()\-]{2,45}?)\s+(\d+(?:[,\.]\d+)?)\s*%",
        re.MULTILINE,
    )
    for m in row.finditer(search):
        name = m.group(1).strip()
        pct_str = m.group(2).replace(",", ".")
        rest = m.group(0)          # full matched text — "hours" signals workload row
        nl = name.lower()
        if nl in seen or nl in _WEIGHT_SKIP:
            continue
        # Skip workload/hours rows (e.g. "Lectures\t30 %\t45 hours")
        if re.search(r"\d+\s*hours", rest, re.IGNORECASE):
            continue
        # Skip if any exact word in the name is a known workload term
        name_words = set(nl.split())
        if name_words & _WORKLOAD_SKIP:
            continue
        try:
            pct = float(pct_str)
        except ValueError:
            continue
        if not 1 <= pct <= 70:
            continue
        weights[name] = pct / 100.0
        seen.add(nl)

    if not weights:
        return {}
    total = sum(weights.values())
    return weights if 0.85 <= total <= 1.15 else {}


_SESSION_RANGE = range(4, 61)   # IE bachelor courses: ~10–30 sessions


def _parse_session_count(text: str) -> int | None:
    """Extract total session count from IE syllabus text. None when not
    found — caller leaves total_sessions unknown and the UI shows a
    percentage-only attendance line instead of inventing an "X left".

    IE syllabi vary in layout (English vs Spanish, table vs prose, with
    or without the literal word "sessions"), so we try multiple patterns
    in confidence order. The workload section — typically *before* the
    EVALUATION METHOD heading — is where the session/hour breakdown
    lives, and isolating that region first prevents stray digits in
    week-by-week schedules or office-hours notes from misleading us.
    """
    # The workload section is the prefix of the document up to the
    # evaluation criteria heading. Reduces false positives by keeping
    # us out of "Session 12: Topic" content rows.
    workload = re.split(
        r"EVALUATION\s+(?:METHOD|CRITERIA)|GRADING\s+CRITERIA|"
        r"ASSESSMENT\s+CRITERIA|CRITERIOS\s+DE\s+EVALUACI[OÓ]N",
        text, maxsplit=1, flags=re.IGNORECASE,
    )[0]

    def _ok(n: int) -> bool:
        return n in _SESSION_RANGE

    # 1. Lectures row in the workload table — explicit "sessions" word.
    for pat in (
        r"(?:Lectures?|Lecciones?(?:\s+magistrales?)?|Clases?(?:\s+magistrales?)?)"
        r"[\s\t]+(\d{1,3})\s+(?:sessions?|sesiones?)",
        r"(?:Sessions?|Sesiones?)\s+presenciales?[\s\t:]+(\d{1,3})",
    ):
        m = re.search(pat, workload, re.IGNORECASE)
        if m and _ok(int(m.group(1))):
            return int(m.group(1))

    # 2. Direct mention: "Number of sessions: 30", "30 sessions of 1.5h".
    for pat in (
        r"(?:Number|Total|N[uú]mero|Cantidad)\s+of\s+sessions?:?\s*(\d{1,3})",
        r"N[uú]mero\s+(?:total\s+)?de\s+sesiones?:?\s*(\d{1,3})",
        r"(\d{1,3})\s+sessions?\s+of\s+\d+(?:[,.]\d+)?\s*(?:h|hours?)",
        r"(\d{1,3})\s+sesiones?\s+de\s+\d+(?:[,.]\d+)?\s*(?:h|horas?)",
        r"\b(\d{1,3})\s+(?:total\s+)?(?:weekly\s+)?sessions?\b",
        r"\b(\d{1,3})\s+(?:total\s+)?sesiones?\b",
    ):
        m = re.search(pat, workload, re.IGNORECASE)
        if m and _ok(int(m.group(1))):
            return int(m.group(1))

    # 3. Two-number row in the workload table: "Lectures   30   45 hours"
    #    where (sessions, hours) appears side by side without the literal
    #    word "sessions". IE's standard session length is 1.5h so the
    #    pair is roughly hours ≈ 1.5 × sessions — use that ratio to pick
    #    the smaller as the session count.
    for line in workload.splitlines():
        if "lecture" not in line.lower() and "lección" not in line.lower():
            continue
        nums = [float(n.replace(",", ".")) for n in
                re.findall(r"\b(\d{1,3}(?:[,.]\d+)?)\b", line)]
        if len(nums) >= 2:
            small, large = sorted(nums)[:2]
            if (_ok(int(small)) and large >= small
                    and 1.0 <= large / small <= 2.5):
                return int(small)

    # 4. Session-by-session schedule. Counts unique "Session N" headers;
    #    the max is the total. Require ≥5 distinct numbers AND a
    #    near-contiguous run so a stray "see Session 12" reference
    #    elsewhere can't mislead us.
    nums = {int(g.group(1)) for g in re.finditer(
        r"\b(?:Session|Sesi[oó]n)\s+(\d{1,3})\b",
        text, re.IGNORECASE,
    ) if 1 <= int(g.group(1)) <= 60}
    if len(nums) >= 5:
        peak = max(nums)
        if _ok(peak) and peak >= len(nums) - 2:
            return peak

    # 5. Last resort: total contact hours ÷ 1.5h.
    m = re.search(
        r"(?:TOTAL\s+(?:CONTACT\s+)?HOURS?|HORAS\s+(?:TOTALES?|PRESENCIALES?)|"
        r"PRESENTIAL\s+HOURS?|CONTACT\s+HOURS?)[:\s]*(\d+(?:[,.]\d+)?)",
        workload, re.IGNORECASE,
    )
    if m:
        hours = float(m.group(1).replace(",", "."))
        sessions = round(hours / 1.5)
        if _ok(sessions):
            return sessions

    return None


def build_grade_categories(
    assignments: list[dict], flat_weights: dict[str, float]
) -> tuple[float | None, list[dict], dict]:
    """Match gradebook assignments to syllabus weight categories.

    Returns (weighted_overall_pct | None, [category_dicts], coverage_info).

    `coverage_info` is the transparency payload — the UI uses it to make
    clear that the overall pct is averaged over only the *graded portion*
    of the course, not the whole course. Keys:
      weight_graded: int 0–100, sum of category weights that have any score
      weight_total:  int, sum of all category weights from the syllabus
      unmatched:     [{"name", "score", "possible"}] — assignments that
                     didn't fit any syllabus category (excluded from the
                     weighted average so users can see what's not counted)
      if_zero_pct:   float | None, projected grade if every ungraded
                     category became 0 — the worst-case floor
    """
    coverage = {"weight_graded": 0, "weight_total": 0, "unmatched": [], "if_zero_pct": None}
    if not flat_weights:
        return None, [], coverage
    # Note: empty `assignments` is fine — we still want to render the
    # weighted category list ("Final Exam 40% — not yet", etc.) so the
    # user sees the syllabus structure before anything's been graded.
    assignments = assignments or []

    cat_keywords = {cat: _expand_keywords(cat) for cat in flat_weights}
    cat_assigns: dict[str, list] = {cat: [] for cat in flat_weights}
    matched_ids: set[int] = set()

    _MID_WORDS  = {"mid", "midterm", "partial"}
    _WORK_WORDS = {"project", "group", "team", "workgroup"}
    _MOCK_WORDS = {"mock", "sample"}

    for a in assignments:
        name_words = _normalize_words(a["name"])

        # Mock/practice exams are never included in any weighted category.
        # Mark them handled so the "Other" catch bucket doesn't absorb them.
        if _MOCK_WORDS & name_words:
            matched_ids.add(id(a))
            continue

        best_cat, best_score = None, 0
        for cat, kw in cat_keywords.items():
            # "Final Exam"-type categories must not match midterm- or
            # project/group-named assignments (e.g. "Final Project" → Workgroups).
            cat_words = _normalize_words(cat)
            if "final" in cat_words and (_MID_WORDS | _WORK_WORDS) & name_words:
                continue
            score = len(kw & name_words)
            if score > best_score:
                best_score, best_cat = score, cat
        if best_cat and best_score >= 1:
            cat_assigns[best_cat].append(a)
            matched_ids.add(id(a))

    # Unmatched → only dump into an explicit "Other"/"Practice"/"General"
    # bucket. Falling back to the lowest-weight category (the old behavior)
    # silently poisoned that bucket with random assignments, e.g. a "Partial
    # Exam" landing in "Final Reflection 5%" and inflating it. Better to
    # leave unmatched items out of the weighted average entirely — the user
    # still sees them in the per-assignment list, just not folded into a
    # category they don't belong to.
    unmatched = [a for a in assignments if id(a) not in matched_ids]
    if unmatched:
        catch = next(
            (c for c in flat_weights if any(k in c.lower() for k in ("other", "practice", "general"))),
            None,
        )
        if catch:
            cat_assigns[catch].extend(unmatched)
            unmatched = []

    categories = []
    for cat in sorted(flat_weights, key=lambda c: -flat_weights[c]):
        assigns = cat_assigns.get(cat, [])
        if assigns:
            ts = sum(a["score"] for a in assigns)
            tp = sum(a["possible"] for a in assigns)
            pct: float | None = round(ts / tp * 100, 1) if tp else None
        else:
            pct = None
        categories.append({
            "name": cat,
            "weight": round(flat_weights[cat] * 100),
            "score": pct,
            "count": len(assigns),
            "items": [{"name": a["name"], "score": a.get("score"),
                       "possible": a.get("possible")} for a in assigns],
        })

    graded = [c for c in categories if c["score"] is not None]
    if graded:
        wt = sum(c["weight"] for c in graded)
        overall: float | None = round(sum(c["score"] * c["weight"] for c in graded) / wt, 1) if wt else None
    else:
        overall = None

    coverage["weight_total"] = sum(c["weight"] for c in categories)
    coverage["weight_graded"] = sum(c["weight"] for c in graded)
    coverage["unmatched"] = [
        {"name": a["name"], "score": a.get("score"), "possible": a.get("possible")}
        for a in unmatched
    ]
    # Worst-case floor: ungraded categories treated as 0%, unmatched items
    # ignored (we don't know what category they belong to). Divisor is
    # weight_total — the whole course — so this number is directly
    # comparable to the final transcript grade.
    if graded and coverage["weight_total"]:
        coverage["if_zero_pct"] = round(
            sum(c["score"] * c["weight"] for c in graded) / coverage["weight_total"], 1
        )

    return overall, categories, coverage


# Bundled fallback weights for known IE courses. Why: when syllabus
# discovery fails (e.g. course has no LTI syllabus link, prof renamed
# the menu item, the IE Syllabus tool is hidden in a deeper folder, or
# the PDF is image-only), we still want to give the student a weighted
# grade view. IE uses standardized course structures across sections of
# the same Bachelor program, so these weights apply to all students
# in the same course — they were extracted by a successful syllabus
# parse and are the same numbers any student would see in the syllabus
# PDF. Names mirror what the parser produces (including IE's bilingual
# quirks like "Continuous"/"Workgroups") so the existing keyword
# matcher in build_grade_categories doesn't need special-casing.
_BUNDLED_WEIGHTS: dict[str, dict[str, float]] = {
    "ie humanities": {
        "Class Participation": 0.10, "Intermediate tests": 0.30,
        "Final Exam": 0.35, "Continuous": 0.20, "Final Reflection": 0.05,
    },
    "physics for computer science lab": {
        "Group Presentation": 0.15, "Individual presentation": 0.25,
        "Lab reports": 0.30, "Class Participation": 0.30,
    },
    "microeconomics": {
        "Final Exam": 0.40, "Workgroups": 0.25, "Class Participation": 0.05,
        "Intermediate Tests": 0.20, "Other": 0.10,
    },
    "physics for computer science": {
        "Final Exam": 0.40, "Group Work": 0.20,
        "Class Participation": 0.10, "Intermediate tests": 0.30,
    },
    "marketing fundamentals": {
        "Final Exam": 0.30, "Individual presentation": 0.10,
        "Group Presentation": 0.25, "Class Participation": 0.10,
        "Intermediate tests": 0.15, "Other": 0.10,
    },
    "principles of programming": {
        "Final Exam": 0.50, "Group Project": 0.15,
        "Class Participation": 0.10, "Midterm Exam": 0.25,
    },
    "discrete mathematics": {
        "Final Exam": 0.30, "Workgroups": 0.15, "Quizzes": 0.20,
        "Midterm Exam": 0.20, "Class Participation": 0.15,
    },
    "fundamentals of data analysis": {
        "Class": 0.10, "Problem sets": 0.15, "Midterm exam": 0.20,
        "Python exam": 0.15, "Final Exam": 0.40,
    },
    "management tools & principles": {
        "Final Exam": 0.50, "Midterm Exam": 0.10,
        "Case Study": 0.20, "Class Participation": 0.20,
    },
    "cost accounting": {
        "Topic presentation": 0.10, "Intermediate mini-tests": 0.25,
        "Other": 0.15, "Class Participation / assignments": 0.10,
        "Final Exam": 0.40,
    },
}


def bundled_weights_for(course_name: str) -> dict[str, float]:
    """Return bundled fallback weights for a course, or {} if unknown."""
    return _BUNDLED_WEIGHTS.get((course_name or "").strip().lower(), {})


async def scrape_syllabus_one(page, course: dict, base_url: str) -> dict:
    """Click the Syllabus item in BB Ultra, capture the IE LTI page, and
    parse {weights, total_sessions} from it.

    Returns a dict with two keys:
      weights:        {category_name: weight_fraction} or {}
      total_sessions: int or None (None when the syllabus didn't say)

    Returning a structured dict keeps callers honest — "no weights" and
    "no session count" are independent failures and the UI handles each
    differently. {} keys are still falsy so existing call sites that
    only care about weights stay simple to update.
    """
    course_id = course.get("course_id")
    if not course_id:
        return {}

    _log(f"  Syllabus → {course['name']}…")
    loop = asyncio.get_event_loop()
    lti_future: asyncio.Future = loop.create_future()
    captured_pdf: list[str] = []       # PDF S3 URLs seen at any point

    def _is_pdf_resp(resp) -> bool:
        ct = resp.headers.get("content-type", "")
        url = resp.url
        # Only capture actual PDF bytes (not octet-stream, not doc-viewer API)
        return "application/pdf" in ct or (
            "content.blackboardcdn.com" in url
            and "response-content-type=application%2Fpdf" in url
        )

    async def on_pdf_resp(resp):
        if resp.status == 200 and _is_pdf_resp(resp):
            captured_pdf.append(resp.url)

    # Listen for PDFs on the main page from the very start
    page.on("response", on_pdf_resp)

    async def on_new_page(pg):
        # Also listen for PDFs in any LTI tab that opens
        pg.on("response", on_pdf_resp)
        if not lti_future.done():
            lti_future.set_result(pg)

    page.context.on("page", on_new_page)

    # Intercept content API to load up to 100 items (default is 10)
    async def on_route(route):
        url = route.request.url
        if "contents/ROOT/children" in url:
            url2 = re.sub(r'limit=\d+', 'limit=100', url)
            await route.continue_(url=url2)
        else:
            await route.continue_()

    await page.route("**", on_route)
    try:
        await page.goto(
            f"{base_url}/ultra/courses/{course_id}/outline",
            wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS,
        )
    except PWTimeout:
        await page.unroute("**")
        page.context.remove_listener("page", on_new_page)
        return {}

    await page.wait_for_timeout(5000)
    await page.unroute("**")

    # Dismiss overlay (IE feedback dialog etc.)
    for dismiss in [
        lambda: page.keyboard.press("Escape"),
        lambda: page.get_by_text("Remind me later").first.click(timeout=1500),
    ]:
        try:
            await dismiss()
            await page.wait_for_timeout(400)
        except Exception:
            pass

    # Find syllabus by handle (ie_syllabus_pro / IESyllabus) OR by text.
    # If not found at root level, expand folder items and try again.
    SYLLABUS_HANDLES = {"ie_syllabus_pro", "iesyllabus"}
    SYLLABUS_TEXTS   = {"syllabus", "programme", "course guide", "programa"}
    NAV_TEXTS = {"content", "calendar", "announcements", "discussions",
                 "gradebook", "messages", "groups", "achievements",
                 "skip to main content", "skip to course information",
                 "zoom for blackboard", "roster", "course description",
                 "attendance", "books & tools"}

    find_syl_js = f"""
    () => {{
        const handles = {list(SYLLABUS_HANDLES)};
        const texts   = {list(SYLLABUS_TEXTS)};
        const link = [...document.querySelectorAll('a')].find(a => {{
            const h = (a.dataset.launchHandle || '').toLowerCase();
            const t = a.textContent.trim().toLowerCase();
            return handles.includes(h) || texts.includes(t);
        }});
        if (link) {{ link.click(); return link.textContent.trim(); }}
        return null;
    }}
    """
    expand_folders_js = f"""
    () => {{
        const navs = {list(NAV_TEXTS)};
        // BB Ultra renders folders as <button> elements (Material UI styled)
        [...document.querySelectorAll('button')].forEach(btn => {{
            const t = btn.textContent.trim().toLowerCase();
            if (t.length > 2 && t.length < 60 && !navs.some(n => t.includes(n))) {{
                btn.click();
            }}
        }});
    }}
    """

    # Capture baseline BEFORE any click so we can detect navigation
    text_before = await _collect_all_frame_text(page)
    url_before = page.url

    clicked = await page.evaluate(f"({find_syl_js})()")
    if not clicked:
        # Expand folder buttons and retry
        await page.evaluate(f"({expand_folders_js})()")
        await page.wait_for_timeout(3000)
        url_before = page.url   # refresh baseline after expansion (URL may drift)
        text_before = await _collect_all_frame_text(page)
        clicked = await page.evaluate(f"({find_syl_js})()")

    if not clicked:
        _log(f"    No syllabus link found for {course['name']} (no matching handle/text)")
        page.context.remove_listener("page", on_new_page)
        page.remove_listener("response", on_pdf_resp)
        return {}

    try:
        lti_page = await asyncio.wait_for(lti_future, timeout=10)
    except asyncio.TimeoutError:
        lti_page = None
    finally:
        page.context.remove_listener("page", on_new_page)

    # ── Wait for content to settle ──────────────────────────────────────────
    if lti_page:
        try:
            await lti_page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await lti_page.wait_for_timeout(2000)
    else:
        # Inline navigation: wait for URL change or DOM growth
        for _ in range(12):
            await page.wait_for_timeout(500)
            if page.url != url_before:
                break
        if page.url != url_before:
            await page.wait_for_timeout(3000)

    # ── Also click any visible ".pdf" attachment to trigger PDF load ─────────
    if not captured_pdf:
        await page.evaluate("""
        () => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while (node = walker.nextNode()) {
                if (node.textContent.includes('.pdf')) {
                    node.parentElement.click(); return;
                }
            }
        }
        """)
        await page.wait_for_timeout(5000)

    page.remove_listener("response", on_pdf_resp)

    # ── Extract text: PDF wins over HTML ────────────────────────────────────
    text = ""
    if captured_pdf:
        _log(f"    Downloading PDF from S3…")
        import io, pdfplumber as _pdfplumber
        try:
            r = await page.request.get(captured_pdf[0])
            if r.status == 200:
                with _pdfplumber.open(io.BytesIO(await r.body())) as doc:
                    text = "\n".join(pg.extract_text() or "" for pg in doc.pages)
        except Exception as e:
            _log(f"    PDF parse error: {e}")

    if not text:
        # Fallback: collect page / LTI-tab text
        source = lti_page or page
        if page.url != url_before and not lti_page:
            source = page
        for frame in source.frames:
            try:
                t = await frame.evaluate("() => document.body?.innerText || ''")
                if len(t) > len(text):
                    text = t
            except Exception:
                pass

    try:
        if lti_page:
            await lti_page.close()
    except Exception:
        pass

    if len(text) < 300:
        _log(f"    Syllabus too short ({len(text)} chars) for {course['name']}")
        return {"weights": {}, "total_sessions": None}

    weights = _parse_weights(text)
    if weights:
        _log(f"    {len(weights)} weight categories parsed for {course['name']}: "
             f"{', '.join(f'{k} {v*100:.0f}%' for k, v in weights.items())}")
    else:
        _log(f"    Could not parse weights for {course['name']}")

    sessions = _parse_session_count(text)
    if sessions:
        _log(f"    {sessions} total sessions parsed for {course['name']}")
    else:
        _log(f"    No session count in syllabus for {course['name']} — attendance shown as % only")
        # Save the raw syllabus text so we can refine the parser against
        # an actual IE syllabus shape next iteration. Capped at 200k
        # chars so we never bloat the debug folder; PDFs are typically
        # 5–15k of extracted text, well under that.
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            safe = re.sub(r"\W+", "_", course["name"]).strip("_").lower()
            (DEBUG_DIR / f"syllabus_{safe}.txt").write_text(text[:200_000])
        except Exception:
            pass

    return {"weights": weights, "total_sessions": sessions}


# ---------------------------------------------------------------------------
# Course discovery
# ---------------------------------------------------------------------------

async def discover_courses_from_bb(page, base_url: str) -> list[dict]:
    """Discover enrolled courses by intercepting BB Ultra's own memberships API call.

    BB Ultra calls /learn/api/v1/users/{id}/memberships when loading /ultra/stream.
    We intercept that response — it returns all enrolled courses including ones the
    public API (which IE blocks) would refuse.  Falls back to DOM /outline links.
    """
    _log("  Discovering courses via API interception…")
    captured: dict = {}

    async def _intercept(route, request):
        response = await route.fetch()
        if "memberships" in request.url and "api/v1/users" in request.url:
            try:
                captured["data"] = await response.json()
                captured["url"]  = request.url
            except Exception:
                pass
        await route.fulfill(response=response)

    await page.route("**/learn/api/v1/users/**", _intercept)

    try:
        await page.goto(f"{base_url}/ultra/stream",
                        wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
    except Exception:
        pass  # networkidle can time out on slow connections — continue anyway

    # Give BB a moment to fire its memberships call
    await page.wait_for_timeout(3000)
    await page.unroute("**/learn/api/v1/users/**")

    if captured.get("data"):
        results = captured["data"].get("results") or []
        courses = []
        for m in results:
            course = m.get("course") or {}
            role   = m.get("courseRole") or {}
            cid    = course.get("id")
            name   = course.get("name") or course.get("displayName")

            if not cid or not name:
                continue
            # Skip organizations/admin areas — they have orgName not courseName in the role
            if "courseName" not in role:
                continue

            courses.append({
                "course_id": cid,
                "name": name,
                "favorited": False,
            })

        if courses:
            _log(f"  Found {len(courses)} courses via API interception")
            return courses

    # DOM fallback — /outline links visible on /ultra/stream
    _log("  API interception missed — falling back to DOM /outline links…")
    try:
        dom_courses = await page.evaluate(r"""
        () => {
            const seen = new Set();
            const out  = [];
            document.querySelectorAll('a[href*="/ultra/courses/"]').forEach(a => {
                const m = (a.href || '').match(/\/ultra\/courses\/([^\/\?#]+)\/outline/);
                if (!m || seen.has(m[1])) return;
                seen.add(m[1]);
                const name = a.textContent.trim().replace(/\s+/g, ' ');
                if (name) out.push({course_id: m[1], name});
            });
            return out;
        }
        """)
        if dom_courses:
            _log(f"  Found {len(dom_courses)} courses via DOM fallback")
            return dom_courses
    except Exception as e:
        _log(f"  DOM error: {e}")

    await _debug_dump(page, "discovery_failed")
    _log("  ✗ Could not discover courses — see debug/discovery_failed.*")
    return []


# ---------------------------------------------------------------------------
# Announcements scraping
# ---------------------------------------------------------------------------

def _is_action_required(title: str, body: str) -> bool:
    import re as _re
    text = (title + " " + body).lower()
    return bool(_re.search(
        r"\b(deadline|due|submit|mandatory|required|exam|quiz|test|"
        r"homework|hw|assignment|reminder|urgent|important|attention|"
        r"project|presentation|today|tomorrow|this week|hand.?in)\b",
        text,
    ))


def _parse_announcement_entry(raw: dict, course_id: str = "", course_name: str = "") -> dict | None:
    """Extract a normalised announcement from any BB Ultra JSON shape.

    BB Ultra uses at least three announcement schemas depending on whether
    you're looking at the activity stream, course announcements endpoint,
    or notification feed. We try each in turn.
    """
    # Shape 1: course announcements endpoint
    # {id, title, body, created, restricted}
    ann_id = raw.get("id") or raw.get("announcementId") or ""
    title  = (raw.get("title") or raw.get("name") or "").strip()
    body   = (raw.get("body") or raw.get("description") or raw.get("content") or "").strip()
    # Strip HTML tags from body
    body = re.sub(r"<[^>]+>", " ", body).strip()
    body = re.sub(r"\s{2,}", " ", body)
    created = (
        raw.get("created") or raw.get("createdDate") or
        raw.get("posted") or raw.get("timestamp") or ""
    )
    author = ""
    if isinstance(raw.get("author"), dict):
        a = raw["author"]
        author = a.get("displayName") or a.get("name") or a.get("firstName", "") + " " + a.get("lastName", "")
        author = author.strip()
    elif isinstance(raw.get("postedBy"), dict):
        a = raw["postedBy"]
        author = a.get("displayName") or a.get("name") or ""

    # Shape 2: activity stream item wrapping an inner "announcement" key
    if not title and "announcement" in raw:
        inner = raw["announcement"]
        if isinstance(inner, dict):
            return _parse_announcement_entry(inner, course_id, course_name)

    # Must have at least a title to be useful
    if not title:
        return None

    # Derive course info from nested fields if not passed in
    if not course_id:
        course_obj = raw.get("course") or raw.get("courseId") or {}
        if isinstance(course_obj, dict):
            course_id   = course_obj.get("id") or ""
            course_name = course_obj.get("name") or course_obj.get("displayName") or course_name
        elif isinstance(course_obj, str):
            course_id = course_obj

    return {
        "id":           ann_id,
        "course_id":    course_id,
        "course_name":  course_name,
        "title":        title,
        "body":         body[:800],
        "created":      created,
        "author":       author,
        "action_required": _is_action_required(title, body),
    }


def _parse_announcement_responses(
    captured: list[dict], active_courses: list[dict]
) -> list[dict]:
    """Walk all intercepted JSON responses and extract announcements."""
    course_by_id   = {c["course_id"]: c["name"] for c in active_courses}
    seen_ids: set  = set()
    result: list   = []

    def _walk(obj, cid="", cname=""):
        if isinstance(obj, list):
            for item in obj:
                _walk(item, cid, cname)
        elif isinstance(obj, dict):
            # Try to parse this node as an announcement
            parsed = _parse_announcement_entry(obj, cid, cname)
            if parsed and parsed.get("title"):
                ann_id = parsed["id"] or parsed["title"]
                if ann_id not in seen_ids:
                    seen_ids.add(ann_id)
                    result.append(parsed)
                return  # don't recurse into a successfully-parsed announcement

            # Otherwise recurse into promising sub-keys
            for key in ("results", "items", "announcements", "activities",
                        "entries", "data", "content"):
                if key in obj:
                    # Try to infer course from context
                    ctx_cid   = cid
                    ctx_cname = cname
                    if not ctx_cid and "courseId" in obj:
                        ctx_cid   = obj["courseId"]
                        ctx_cname = course_by_id.get(ctx_cid, "")
                    _walk(obj[key], ctx_cid, ctx_cname)

    for entry in captured:
        url  = entry.get("url", "")
        data = entry.get("data")
        if not data:
            continue

        # Infer course from URL: .../courses/{id}/announcements
        cid_from_url   = ""
        cname_from_url = ""
        m = re.search(r"/courses/([^/\?]+)/", url)
        if m:
            cid_from_url   = m.group(1)
            cname_from_url = course_by_id.get(cid_from_url, "")

        _walk(data, cid_from_url, cname_from_url)

    # Sort newest first (ISO timestamps sort lexicographically)
    result.sort(key=lambda a: a.get("created") or "", reverse=True)
    return result


async def _scrape_course_announcements(
    page, course: dict, base_url: str
) -> list[dict]:
    """Visit one course's announcements page and capture everything.

    Captures all JSON responses (no URL filter — BB Ultra's internal API
    paths vary by instance) and also extracts page text as a fallback.
    Logs each captured URL so we can discover the real endpoint patterns.
    """
    course_id = course.get("course_id")
    if not course_id:
        return []

    captured: list[dict] = []

    async def on_resp(resp):
        if resp.status not in (200, 204):
            return
        if "json" not in resp.headers.get("content-type", ""):
            return
        try:
            data = await resp.json()
            if data:
                captured.append({"url": resp.url, "data": data})
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        await page.goto(
            f"{base_url}/ultra/courses/{course_id}/announcements",
            wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS,
        )
        await page.wait_for_timeout(3000)
    except Exception as exc:
        _log(f"    ann nav error ({course['name']}): {exc}")
    page.remove_listener("response", on_resp)

    if VERBOSE:
        for r in captured:
            _log(f"    ann captured: {r['url'][:120]}")

    # Parse structured data from the JSON responses
    parsed = _parse_announcement_responses(captured, [course])

    # Fallback: if no structured data found, try the DOM text.
    # BB Ultra renders announcement titles and bodies in the DOM even when
    # the API uses an internal path we haven't seen before.
    if not parsed:
        try:
            text = await _collect_all_frame_text(page)
            parsed = _parse_announcements_from_text(text, course)
        except Exception:
            pass

    return parsed


def _parse_announcements_from_text(text: str, course: dict) -> list[dict]:
    """Extract announcements from BB Ultra's rendered page text.

    BB Ultra's announcements page renders each item as a block containing:
      - The announcement title (appears before the date)
      - A date string ("Posted ... ago" or ISO-like)
      - Body paragraphs

    This is a best-effort fallback for when the JSON interceptor finds
    nothing — it won't produce perfectly-structured records but is far
    better than returning nothing.
    """
    import re as _re
    results = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Date patterns that appear after the title on BB Ultra's page
    DATE_PAT = _re.compile(
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
        r"|\d{4}-\d{2}-\d{2}"
        r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
        r"|\d+\s+(?:minute|hour|day|week)s?\s+ago)",
        _re.IGNORECASE,
    )

    # Skip boilerplate navigation lines that aren't announcement content
    SKIP = {"Announcements", "Content", "Calendar", "Messages", "Groups",
            "Gradebook", "Discussions", "Skip to main content", "Courses"}

    i = 0
    while i < len(lines):
        line = lines[i]
        if line in SKIP or len(line) < 5 or DATE_PAT.search(line):
            i += 1
            continue

        # Look ahead: is the NEXT non-empty line a date?
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and DATE_PAT.search(lines[j]):
            title   = line
            date_s  = lines[j]
            body_lines = []
            k = j + 1
            while k < len(lines) and k < j + 6:
                if lines[k] in SKIP or DATE_PAT.search(lines[k]):
                    break
                body_lines.append(lines[k])
                k += 1
            results.append({
                "id":              "",
                "course_id":       course.get("course_id", ""),
                "course_name":     course.get("name", ""),
                "title":           title,
                "body":            " ".join(body_lines)[:800],
                "created":         date_s,
                "author":          "",
                "action_required": _is_action_required(title, " ".join(body_lines)),
            })
            i = k
            continue
        i += 1

    return results


def load_announcements() -> list[dict]:
    """Load previously-saved announcements from disk."""
    try:
        return json.loads(ANNOUNCEMENTS_PATH.read_text())
    except Exception:
        return []


def save_announcements(announcements: list[dict]) -> None:
    ANNOUNCEMENTS_PATH.write_text(json.dumps(announcements, indent=2))


# ---------------------------------------------------------------------------
# Absence calculation
# ---------------------------------------------------------------------------

def _compute_remaining(att: dict, course: dict, config: dict) -> dict:
    """Compute absences_remaining and max_absences for the menu.

    total_sessions is sourced from the syllabus during discovery (see
    `_parse_session_count`). If we have a real count, the menu can show
    "X / Y · Z left". If we don't (no syllabus, hidden tool, image-only
    PDF), every field that depends on knowing the total is left None and
    the menu falls back to a "X% attended" display — better to show no
    "left" claim than a wrong one.
    """
    total = course.get("total_sessions")
    if total:
        max_absences = int(total * config["max_absence_pct"])
    else:
        max_absences = None
    if "error" in att:
        return {"max_absences": max_absences, "absences_remaining": None}
    # Gradebook-only attendance gives us a percentage but no per-session
    # absent count — the menu's gradebook-source path renders % directly.
    if att.get("absent") is None:
        return {"max_absences": max_absences, "absences_remaining": None}
    if max_absences is None:
        # Native attendance counts present/late/absent, but without a
        # known total we can't say "X left". Menu shows percentage path.
        return {"max_absences": None, "absences_remaining": None}
    absent = att.get("absent") or 0
    late = att.get("late") or 0
    weight = config.get("late_absence_weight", 0.5)
    effective = absent + late * weight
    remaining = max_absences - effective
    return {"max_absences": max_absences, "effective_absent": effective, "absences_remaining": remaining}


async def _safe_launch_browser(p, on_status=None):
    """Launch Playwright's persistent Chromium with a hard timeout.

    launch_persistent_context() can hang silently when:
      - another instance still holds the user_data_dir lock
      - the bundled Chromium binary is quarantined / blocked by Gatekeeper
      - the Playwright Node driver subprocess never starts

    Without a timeout the menu just sits at "Starting browser…" forever and
    nothing in the worker thread can recover. Surface a useful error instead.
    """
    if on_status:
        on_status("Launching browser", 0, 0)
    try:
        return await asyncio.wait_for(
            p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_DIR),
                headless=not HEADFUL,
                viewport={"width": 1440, "height": 900},
                args=["--disable-focus-on-load", "--no-first-run", "--disable-extensions"],
            ),
            timeout=BROWSER_LAUNCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _log(f"✗ Browser launch timed out after {BROWSER_LAUNCH_TIMEOUT_S}s — "
             "another BB Tracker instance may still be running, or the Chromium "
             "profile is locked. Try: Quit BB Tracker, then delete "
             "~/Library/Application Support/BBTracker/browser_data and reopen.")
        raise


def _attendance_from_gradebook(grades: dict, course: dict) -> dict | None:
    """Some courses (e.g. Español Básico 2) don't use BB Ultra's native attendance
    tool, but instead post an attendance score as a gradebook column named
    'Asistencia', 'Attendance', 'QWAttendance', etc. Derive an attendance dict
    from that column so the menu can still show useful info.

    Returns None if no attendance column is found.
    """
    if not grades or grades.get("error") and not grades.get("assignments"):
        return None

    candidates = list(grades.get("attendance_entries") or [])
    for a in grades.get("assignments") or []:
        name = (a.get("name") or "").lower()
        if "asistencia" in name or "attendance" in name:
            candidates.append(a)
    for c in grades.get("categories") or []:
        name = (c.get("name") or "").lower()
        if "asistencia" in name or "attendance" in name:
            score = c.get("score")
            possible = c.get("possible")
            if score is not None and possible:
                candidates.append({"name": c.get("name"), "score": score, "possible": possible})

    if not candidates:
        return None

    # Pick the highest-possible one (typically the aggregate, e.g. /100)
    pick = max(candidates, key=lambda a: (a.get("possible") or 0))
    score = pick.get("score")
    possible = pick.get("possible") or 100.0
    if score is None or possible <= 0:
        return None

    pct = score / possible * 100
    # When the gradebook only exposes an aggregate %, we *don't* know:
    #   - how many sessions have happened
    #   - how many were missed
    # So we deliberately leave the session-level fields unset. The menu
    # detects `source` and renders the attendance percentage instead of a
    # bogus "X absences left".
    return {
        "present": None,
        "late": None,
        "absent": None,
        "excused": None,
        "overall_score": round(pct, 2),
        "source": f"gradebook:{pick.get('name')}",
    }


# ── Pearson / MyLab integration ──────────────────────────────────────────────

PEARSON_BASE = "https://mylab.pearson.com"
_MYLAB_ASSIGNMENTS_URL = PEARSON_BASE + "/Student/DoAssignments.aspx"


def _parse_mylab_html(html: str) -> list[dict]:
    # Parse row by row so name, score, and due date come from the same <tr>,
    # avoiding cross-row regex bleed.
    row_re   = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    name_re  = re.compile(r'class="plainlink[^"]*"[^>]*>([^<]+)</a>', re.IGNORECASE)
    score_re = re.compile(r'<span[^>]*tabindex[^>]*>([\d.]+%)</span>', re.IGNORECASE)
    # Due date patterns: "Due: May 20, 2026" or "5/20/2026" or ISO
    due_re   = re.compile(
        r'(?:Due|Due\s+Date|due\s+by)\s*:?\s*'
        r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}'
        r'|\d{1,2}/\d{1,2}/\d{2,4}'
        r'|\d{4}-\d{2}-\d{2})',
        re.IGNORECASE,
    )
    out = []
    for row in row_re.finditer(html):
        cell = row.group(1)
        nm = name_re.search(cell)
        if not nm:
            continue
        name = nm.group(1).strip()
        sc   = score_re.search(cell)
        due  = due_re.search(cell)
        entry: dict = {"name": name, "due_date": due.group(1).strip() if due else None}
        if sc:
            entry["score"]  = float(sc.group(1).rstrip("%"))
            entry["status"] = "graded"
        else:
            entry["score"]  = None
            entry["status"] = "submitted"
        out.append(entry)
    return out


async def _find_pearson_lti(page, course_id: str, base_url: str) -> str | None:
    candidates: list[dict] = []

    async def _on_resp(resp):
        if f"courses/{course_id}/contents" not in resp.url:
            return
        if "json" not in resp.headers.get("content-type", ""):
            return
        try:
            data = await resp.json()
        except Exception:
            return
        for item in data.get("results") or []:
            icon  = (item.get("iconUrl") or "").lower()
            title = (item.get("title")   or "").lower()
            detail = item.get("contentDetail") or {}
            if "resource/x-bb-blti-link" not in detail:
                continue
            if not any(kw in icon + title for kw in ("pearson", "mylab", "mastering")):
                continue
            candidates.append({
                "id": item.get("id"),
                "title": title,
                "prefer": any(w in title for w in ("assign", "course home")),
            })

    page.on("response", _on_resp)
    try:
        await page.goto(
            f"{base_url}/ultra/courses/{course_id}/outline",
            wait_until="networkidle", timeout=PAGE_TIMEOUT_MS,
        )
        await page.wait_for_timeout(3000)
        await page.evaluate("""() => {
            for (const el of document.querySelectorAll('button, [role="button"]')) {
                if (/pearson|mylab|mastering/i.test(el.textContent)) {
                    el.click(); break;
                }
            }
        }""")
        await page.wait_for_timeout(2500)
    except Exception:
        pass
    page.remove_listener("response", _on_resp)

    for c in candidates:
        if c["prefer"]:
            return c["id"]
    return candidates[0]["id"] if candidates else None


async def _mylab_frame_or_page(page, timeout_s: int = 40):
    """Poll until the main page or a frame has reached the MyLab /Student/ area.

    The LTI launch flow goes: interop.pearson.com → socket.pearsoned.com →
    mylab.pearson.com/api/v1/.../launch (JS redirect) → /Student/DoAssignments.
    We wait for the final /Student/ destination so we never hand back the
    intermediate launch-API URL (whose JS hasn't finished redirecting yet).
    Falls back to any mylab.pearson.com frame if /Student/ never appears.
    """
    student_url = lambda u: PEARSON_BASE in (u or "") and "/Student/" in (u or "")

    for _ in range(timeout_s * 2):
        if student_url(page.url):
            return page, False
        for fr in page.frames:
            if student_url(fr.url):
                return fr, True
        await asyncio.sleep(0.5)

    # Fallback: any mylab frame (caller will force-navigate to DoAssignments)
    if PEARSON_BASE in (page.url or ""):
        return page, False
    for fr in page.frames:
        if PEARSON_BASE in (fr.url or ""):
            return fr, True
    return None, False


async def _scrape_pearson(page, course: dict, base_url: str) -> list[dict] | None:
    content_id = course.get("pearson_content_id")
    course_id  = course.get("course_id")

    # 1. Direct navigation — works when Pearson session cookies are still valid
    _log("pearson: trying direct navigation…")
    try:
        await page.goto(_MYLAB_ASSIGNMENTS_URL,
                        wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(3000)
    except Exception as exc:
        _log(f"pearson: direct nav error: {exc}")
    _log(f"pearson: after direct nav → {page.url[:80]}")

    if PEARSON_BASE in page.url and "DoAssignments" in page.url:
        result = _parse_mylab_html(await page.content())
        if result:
            _log(f"pearson: ✓ direct nav → {len(result)} assignments")
            return result

    if not content_id:
        _log("pearson: no content_id, giving up")
        return None

    # 2. Navigate to the Blackboard course outline and click the LTI item.
    #    This is the same flow as a real user — Blackboard handles the LTI POST
    #    and Playwright follows the redirect to Pearson.
    _log("pearson: trying outline click…")
    try:
        await page.goto(
            f"{base_url}/ultra/courses/{course_id}/outline",
            wait_until="networkidle", timeout=PAGE_TIMEOUT_MS,
        )
        await page.wait_for_timeout(5000)
    except Exception as exc:
        _log(f"pearson: outline nav error: {exc}")

    # Expand the Pearson folder (click the folder header button)
    try:
        await page.evaluate("""() => {
            for (const el of document.querySelectorAll('button, [role="button"]')) {
                if (/pearson|mylab|mastering/i.test(el.textContent)) {
                    el.click(); break;
                }
            }
        }""")
        await page.wait_for_timeout(3000)
    except Exception:
        pass

    # Click the "All Assignments" LTI item
    clicked = False
    for selector_text in ("MyLab and Mastering All Assignments", "All Assignments"):
        if clicked:
            break
        try:
            await page.get_by_text(selector_text, exact=False).first.click(timeout=5000)
            clicked = True
            _log(f"pearson: clicked {selector_text!r}")
        except Exception:
            pass

    if not clicked:
        _log("pearson: outline click failed — trying legacy LTI launch URL")
        launch_url = (
            f"{base_url}/webapps/blackboard/execute/blti/launchLink"
            f"?content_id={content_id}&course_id={course_id}&from_bb=true"
        )
        try:
            await page.goto(launch_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_timeout(5000)
        except Exception as exc:
            _log(f"pearson: launch URL error: {exc}")
        _log(f"pearson: after launch URL → {page.url[:80]}")

    # Wait for MyLab DoAssignments to fully load (LTI chain takes several seconds)
    target, is_frame = await _mylab_frame_or_page(page, timeout_s=40)
    if target is None:
        _log(f"pearson: ✗ MyLab never appeared. final url={page.url[:80]} "
             f"frames={[fr.url[:50] for fr in page.frames if fr.url and 'blank' not in fr.url]}")
        return None

    _log(f"pearson: MyLab loaded ({'frame' if is_frame else 'main page'}) → {target.url[:80]}")
    if "DoAssignments" not in target.url:
        # Not yet on the assignments page — navigate there directly
        try:
            await target.goto(_MYLAB_ASSIGNMENTS_URL,
                               wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await target.wait_for_timeout(3000)
        except Exception as exc:
            _log(f"pearson: DoAssignments nav error: {exc}")

    # Scores load via JavaScript WebForm_DoCallback AJAX calls — click every
    # "see score" link so the server returns each grade, then wait for them.
    try:
        n_links = await target.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href*="scoreCallBack"]')];
            links.forEach(l => l.click());
            return links.length;
        }""")
        if n_links:
            _log(f"pearson: triggered {n_links} score callbacks, waiting…")
            # Wait up to 15s for all score divs to be populated
            await target.wait_for_function(
                """() => document.querySelectorAll('a[href*="scoreCallBack"]').length === 0""",
                timeout=15000,
            )
    except Exception as exc:
        _log(f"pearson: score callback wait: {exc}")

    html = await target.content()
    result = _parse_mylab_html(html)
    if result:
        _log(f"pearson: ✓ {len(result)} assignments scraped")
        return result
    _log(f"pearson: ✗ page reached but no assignments parsed (url={target.url[:80]})")
    return None


def _apply_retakes(assignments: list[dict]) -> list[dict]:
    """Mark each assignment with `counted` based on retake rules.

    If a retake is graded → it counts; all originals for that test are suppressed.
    If a retake is not graded → it doesn't count; use the original's score.
    Originals with no score don't count regardless.
    """
    # Build retake map: normalised base name → retake assignment
    retakes: dict[str, dict] = {}
    for a in assignments:
        m = re.match(r'^(.+?)\s*\(retake\b', a["name"], re.IGNORECASE)
        if m:
            base = m.group(1).strip().lower()
            prev = retakes.get(base)
            if prev is None or (a.get("score") is not None and prev.get("score") is None):
                retakes[base] = a

    for a in assignments:
        m = re.match(r'^(.+?)\s*\(retake\b', a["name"], re.IGNORECASE)
        if m:
            a["counted"] = a.get("score") is not None
        else:
            name_lower = a["name"].lower()
            superseded = any(
                name_lower.startswith(base) and rt.get("score") is not None
                for base, rt in retakes.items()
            )
            a["counted"] = not superseded and a.get("score") is not None
    return assignments


# ---------------------------------------------------------------------------
# Main scrape run
# ---------------------------------------------------------------------------

async def run_scrape(include_grades: bool = True, include_announcements: bool = True,
                     on_progress=None, on_status=None, is_cancelled=None) -> dict:
    config = load_config()
    base_url = config["blackboard_base_url"].rstrip("/")
    expected_host = urlparse(base_url).netloc

    courses = load_courses()
    track_favs    = config.get("track_favorites_only", False)
    hidden_names  = set(config.get("hidden_courses", []))
    active = [c for c in courses
              if c.get("enabled", True)
              and c["name"] not in hidden_names
              and (not track_favs or c.get("favorited", True))]

    out: dict = {"timestamp": datetime.now().isoformat(), "error": None, "courses": []}

    if not active:
        out["error"] = "no_courses"
        _write(out)
        return out

    async with async_playwright() as p:
        if on_status:
            on_status("Starting browser", 0, 0)
        _log("→ Launching browser…")
        try:
            context = await _safe_launch_browser(p, on_status)
        except asyncio.TimeoutError:
            out["error"] = "browser_launch_timeout"
            _write(out)
            return out
        page = context.pages[0] if context.pages else await context.new_page()

        if on_status:
            on_status("Connecting to Blackboard", 0, 0)

        try:
            _log("→ Loading Blackboard…")
            # Use /ultra/stream — it's the reliable auth-check URL for this BB instance
            await page.goto(f"{base_url}/ultra/stream",
                            wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        except Exception:
            pass  # continue — check host after

        if on_status:
            on_status("Verifying session", 0, 0)
        await page.wait_for_timeout(2000)

        current_host = urlparse(page.url).netloc
        if current_host != expected_host:
            _log(f"✗ Auth required (redirected to {current_host})")
            out["error"] = "auth_required"
            out["redirect_url"] = page.url
            await context.close()
            _write(out)
            return out

        if on_status:
            on_status("Refreshing", 0, len(active))

        for i, course in enumerate(active, 1):
            if is_cancelled and is_cancelled():
                _log("✗ Scrape cancelled — stopping early for pending op")
                break

            _log(f"→ [{i}/{len(active)}] {course['name']}…")
            # Per-course label so the menu shows live movement, not just a counter.
            short_name = (course.get("name") or "")[:24]
            if on_progress:
                on_progress(i, len(active))
            if on_status:
                on_status(f"Reading {short_name}", i, len(active))

            att = await _scrape_attendance(page, course, base_url)
            if "error" in att:
                _log(f"    attendance: {att['error']}")
            else:
                _log(f"    attendance: {att.get('absent')} absent, "
                     f"{att.get('late')} late, {att.get('present')} present")

            grades: dict = {}
            if include_grades:
                grades = await _scrape_grades(page, course, base_url)

            # Fallback: if BB's native attendance tool has no data but the
            # course tracks attendance via a gradebook column (e.g. QWAttendance),
            # derive attendance from that.
            if att.get("error") == "no_attendance_tracking":
                derived = _attendance_from_gradebook(grades, course)
                if derived:
                    _log(f"    attendance: derived from gradebook ({derived['source']}) "
                         f"→ {derived['overall_score']}%")
                    att = derived

            entry = {
                "name": course["name"],
                "total_sessions": course.get("total_sessions"),
                **att,
                **_compute_remaining(att, course, config),
                "grades": grades,
            }

            if course.get("pearson_content_id") and not (is_cancelled and is_cancelled()):
                if on_status:
                    on_status(f"Reading {short_name} (MyLab)", i, len(active))
                pearson = await _scrape_pearson(page, course, base_url)
                if pearson is not None:
                    pearson = _apply_retakes(pearson)
                    counted = [a for a in pearson if a.get("counted")]
                    avg = round(sum(a["score"] for a in counted) / len(counted), 2) if counted else None
                    # Compute avg for just mini-test assignments (for grade category injection)
                    mt_scores = [a["score"] for a in counted
                                 if re.search(r'mini.?test', a["name"], re.IGNORECASE)]
                    mt_avg = round(sum(mt_scores) / len(mt_scores), 2) if mt_scores else None
                    entry["pearson"] = {
                        "assignments": pearson,
                        "avg": avg,
                        "graded_count": len(counted),
                        "total_count": len(pearson),
                        "mini_tests_avg": mt_avg,
                        "mini_tests_count": len(mt_scores),
                    }
                    _log(f"    pearson: {len(counted)}/{len(pearson)} graded, avg={avg}")

            out["courses"].append(entry)

        # ── Announcements ─────────────────────────────────────────────────────
        # Visit each course's announcements page directly. We don't try to
        # guess the API URL — we let BB fire whatever it fires, capture all
        # JSON, and also fall back to DOM text parsing. Time-gated: skip if
        # we checked less than 25 minutes ago to avoid double-running on a
        # rapid manual refresh.
        if include_announcements and active and not (is_cancelled and is_cancelled()):
            import time as _time
            ann_age_s = None
            if ANNOUNCEMENTS_PATH.exists():
                ann_age_s = _time.time() - ANNOUNCEMENTS_PATH.stat().st_mtime

            if ann_age_s is None or ann_age_s > 25 * 60:
                if on_status:
                    on_status("Reading announcements", 0, len(active))
                all_anns: list[dict] = []
                for ci, course in enumerate(active):
                    if is_cancelled and is_cancelled():
                        break
                    if on_status:
                        short = (course.get("name") or "")[:24]
                        on_status(f"Announcements: {short}", ci + 1, len(active))
                    try:
                        course_anns = await _scrape_course_announcements(
                            page, course, base_url
                        )
                        _log(f"    ann {course['name']}: {len(course_anns)} found")
                        all_anns.extend(course_anns)
                    except Exception as exc:
                        _log(f"    ann {course['name']} error: {exc}")

                # Deduplicate across courses and sort newest-first
                seen: set = set()
                deduped: list[dict] = []
                for a in all_anns:
                    key = a.get("id") or a.get("title", "")
                    if key not in seen:
                        seen.add(key)
                        deduped.append(a)
                deduped.sort(key=lambda a: a.get("created") or "", reverse=True)

                out["announcements"] = deduped
                save_announcements(deduped)
                _log(f"→ Announcements: {len(deduped)} total across {len(active)} courses")
            else:
                _log(f"→ Announcements: skipped (checked {ann_age_s/60:.0f} min ago)")
                out["announcements"] = load_announcements()

        await context.close()

    _write(out)
    _log("✓ Done.")
    return out


# ---------------------------------------------------------------------------
# Discovery run (called from menu bar "Sync courses" action)
# ---------------------------------------------------------------------------

async def run_syllabus_scrape() -> None:
    """Scrape syllabi for all enabled courses and save weights to courses.json.
    Runs independently of course discovery."""
    config = load_config()
    base_url = config["blackboard_base_url"].rstrip("/")
    expected_host = urlparse(base_url).netloc
    courses = load_courses()

    async with async_playwright() as p:
        try:
            context = await _safe_launch_browser(p)
        except asyncio.TimeoutError:
            return
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await page.goto(f"{base_url}/ultra/institution-page",
                            wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except PWTimeout:
            await context.close()
            return
        await page.wait_for_timeout(3000)
        if urlparse(page.url).netloc != expected_host:
            _log("✗ Auth required")
            await context.close()
            return

        changed = False
        for course in courses:
            if not course.get("enabled", True):
                continue
            need_weights  = not course.get("grade_weights")
            need_sessions = not course.get("total_sessions")
            if not need_weights and not need_sessions:
                _log(f"  Skipping {course['name']} (weights and sessions already saved)")
                continue
            parsed = await scrape_syllabus_one(page, course, base_url)
            weights  = parsed.get("weights")  or {}
            sessions = parsed.get("total_sessions")
            if not weights:
                fallback = bundled_weights_for(course["name"])
                if fallback:
                    _log(f"  Using bundled weights for {course['name']} ({len(fallback)} categories)")
                    weights = fallback
            if weights and need_weights:
                course["grade_weights"] = weights
                changed = True
            if sessions and need_sessions:
                course["total_sessions"] = sessions
                changed = True

        await context.close()

    if changed:
        save_courses(courses)
        _log("✓ Syllabus weights saved to courses.json")
    else:
        _log("  No new weights found")


async def run_discover(on_status=None) -> list[dict] | None:
    """Discover all courses and fetch syllabi.

    Navigates directly to /ultra/stream (the only page that works reliably for
    auth check + course discovery on IE's BB).  Returns None on auth failure so
    the caller knows not to overwrite the auth_required error in data.json.
    on_status(label, done, total) is called to report progress.
    """
    config = load_config()
    base_url = config["blackboard_base_url"].rstrip("/")
    expected_host = urlparse(base_url).netloc

    if on_status:
        on_status("Discovering courses", 0, 0)

    async with async_playwright() as p:
        try:
            context = await _safe_launch_browser(p, on_status)
        except asyncio.TimeoutError:
            _write({
                "timestamp": datetime.now().isoformat(),
                "error": "browser_launch_timeout",
                "courses": [],
            })
            return None
        page = context.pages[0] if context.pages else await context.new_page()

        # Intercept BB Ultra's own memberships API call — set up BEFORE navigation
        captured: dict = {}

        async def _intercept(route, request):
            response = await route.fetch()
            if "memberships" in request.url and "api/v1/users" in request.url:
                try:
                    captured["data"] = await response.json()
                except Exception:
                    pass
            await route.fulfill(response=response)

        await page.route("**/learn/api/v1/users/**", _intercept)

        _log("→ Navigating to /ultra/stream for discovery…")
        try:
            await page.goto(f"{base_url}/ultra/stream",
                            wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        except Exception:
            pass  # networkidle can time out on slow connections — continue

        await page.wait_for_timeout(3000)
        await page.unroute("**/learn/api/v1/users/**")

        # Auth check — if BB redirected us away, session is dead
        if urlparse(page.url).netloc != expected_host:
            _log("✗ Auth required")
            await context.close()
            _write({
                "timestamp": datetime.now().isoformat(),
                "error": "auth_required",
                "redirect_url": page.url,
                "courses": [],
            })
            return None

        # Build course list from intercepted API response
        discovered: list[dict] = []
        if captured.get("data"):
            results = captured["data"].get("results") or []
            # Persist the BB user ID so the license system can tie a key to an account
            if results:
                uid = results[0].get("userId", "")
                if uid:
                    try:
                        cfg = load_config()
                        if cfg.get("bb_username") != uid:
                            cfg["bb_username"] = uid
                            save_config(cfg)
                    except Exception:
                        pass
            for m in results:
                course = m.get("course") or {}
                role   = m.get("courseRole") or {}
                cid    = course.get("id")
                name   = course.get("name") or course.get("displayName")
                if cid and name and "courseName" in role:
                    discovered.append({"course_id": cid, "name": name, "favorited": False})
            _log(f"  Found {len(discovered)} courses via API interception")

        if not discovered:
            _log("  API missed — falling back to DOM /outline links")
            dom = await page.evaluate(r"""
            () => {
                const seen = new Set(), out = [];
                document.querySelectorAll('a[href*="/ultra/courses/"]').forEach(a => {
                    const m = (a.href||'').match(/\/ultra\/courses\/([^\/\?#]+)\/outline/);
                    if (!m || seen.has(m[1])) return;
                    seen.add(m[1]);
                    const name = a.textContent.trim().replace(/\s+/g, ' ');
                    if (name) out.push({course_id: m[1], name});
                });
                return out;
            }
            """)
            discovered = [{"course_id": c["course_id"], "name": c["name"], "favorited": False}
                          for c in (dom or [])]

        existing = load_courses()
        merged   = merge_discovered(existing, discovered)

        # Syllabus scraping with per-course progress. Re-fetch any course
        # that's missing either weights OR a session count so existing
        # users picking up this version (whose old courses.json has no
        # session data) populate it on next sync without extra effort.
        active_courses = [c for c in merged if c.get("enabled", True)]
        needs_syllabus = {c["course_id"] for c in active_courses
                          if not c.get("grade_weights") or not c.get("total_sessions")}
        _log(f"→ Fetching syllabus weights/sessions for {len(needs_syllabus)}/{len(active_courses)} courses…")
        for i, course in enumerate(active_courses, 1):
            if on_status:
                on_status("Reading syllabi", i, len(active_courses))
            if course["course_id"] not in needs_syllabus:
                continue
            _log(f"  Syllabus → {course['name']}…")
            parsed = await scrape_syllabus_one(page, course, base_url)
            weights  = parsed.get("weights")  or {}
            sessions = parsed.get("total_sessions")
            if not weights:
                # Fall back to bundled defaults so a single user's failed
                # PDF parse / hidden syllabus tool doesn't leave them with
                # no weighted-grade view at all.
                fallback = bundled_weights_for(course["name"])
                if fallback:
                    _log(f"    Using bundled weights for {course['name']} ({len(fallback)} categories)")
                    weights = fallback
            if weights and not course.get("grade_weights"):
                course["grade_weights"] = weights
            if sessions and not course.get("total_sessions"):
                course["total_sessions"] = sessions

        # Detect Pearson/MyLab LTI items for any course not already tagged
        needs_pearson = [c for c in merged
                         if c.get("enabled", True) and not c.get("pearson_content_id")]
        if needs_pearson:
            _log(f"→ Scanning for Pearson/MyLab in {len(needs_pearson)} courses…")
            for course in needs_pearson:
                lti_id = await _find_pearson_lti(page, course["course_id"], base_url)
                if lti_id:
                    _log(f"  ✓ Pearson: {course['name']} → {lti_id}")
                    course["pearson_content_id"] = lti_id

        save_courses(merged)
        await context.close()

    _log(f"✓ Discovery complete: {len(discovered)} found, {len(merged)} total in courses.json")
    return merged


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def run_login() -> bool:
    """Open a visible browser for the user to log in to Blackboard.
    Returns True if login succeeded, False on timeout or error.
    No terminal interaction required — just opens the window."""
    config = load_config()
    base_url = config["blackboard_base_url"].rstrip("/")
    expected_host = urlparse(base_url).netloc

    async with async_playwright() as p:
        try:
            context = await asyncio.wait_for(
                p.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_DIR),
                    headless=False,
                    viewport={"width": 1440, "height": 900},
                ),
                timeout=BROWSER_LAUNCH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _log("✗ Login browser launch timed out")
            return False
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"{base_url}/ultra/institution-page")

        reached = False
        for _ in range(600):           # up to 10 minutes
            await asyncio.sleep(1)
            try:
                current = page.url
            except Exception:
                break
            host = urlparse(current).netloc
            if host == expected_host and "/ultra/" in current:
                reached = True
                break

        if reached:
            await asyncio.sleep(8)     # let "Stay signed in?" cookie settle
        await context.close()

    return reached


def _write(payload: dict) -> None:
    with open(DATA_PATH, "w") as f:
        json.dump(payload, f, indent=2)


if __name__ == "__main__":
    import sys
    result = asyncio.run(run_scrape())
    print()
    print(json.dumps(result, indent=2))
