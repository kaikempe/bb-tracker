"""Interactive helper to fill in course_id values in config.json.

For each course without an ID, prompts you to paste its Blackboard URL.
Extracts the `_12345_1` chunk automatically and saves the config.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"
COURSE_ID_RE = re.compile(r"_\d+_\d+")


def main() -> int:
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    pending = [c for c in config["courses"] if not c.get("course_id")]
    if not pending:
        print("All courses already have a course_id. Nothing to do.")
        return 0

    print("=" * 60)
    print("  Manual course ID entry")
    print("=" * 60)
    print()
    print("For each course, do this in Chrome:")
    print("  1. Go to https://blackboard.ie.edu and log in")
    print("  2. Click the course card")
    print("  3. Copy the URL from the address bar")
    print("  4. Paste it here when prompted")
    print()
    print("The URL will look something like:")
    print("  https://blackboard.ie.edu/ultra/courses/_58472_1/cl/outline")
    print()
    print("You can paste the whole URL — I'll pull out the _58472_1 part.")
    print("Press Enter with nothing to skip a course.")
    print()

    for course in pending:
        while True:
            raw = input(f"  {course['name']}:  ").strip()
            if not raw:
                print("    (skipped)")
                break
            m = COURSE_ID_RE.search(raw)
            if m:
                course["course_id"] = m.group(0)
                print(f"    ✓ {m.group(0)}")
                break
            print(f"    ✗ Couldn't find a course ID pattern like _12345_1 in that. Try again.")

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    print()
    print("✓ Saved. Now run:  python3 scraper.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
