"""
Pearson/MyLab grade explorer — manual-click edition.

Opens Blackboard in a visible browser, navigates to the Cost Accounting outline,
then waits for YOU to click the MyLab link. While you browse, the script captures
every network response from every frame. When you're done, press Enter in the
terminal and it dumps everything to disk.

Run with:
    .venv/bin/python3 explore_pearson.py
BB Tracker must NOT be running (both share the same Chromium profile lock).
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from scraper import load_config, load_courses

BROWSER_DIR = Path.home() / "Library" / "Application Support" / "BBTracker" / "browser_data"
OUT_DIR     = Path.home() / "Library" / "Application Support" / "BBTracker" / "pearson_explore"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PAGE_TIMEOUT = 30_000


async def main():
    config   = load_config()
    base_url = config["blackboard_base_url"].rstrip("/")
    expected_host = urlparse(base_url).netloc
    courses  = load_courses()

    # Find Cost Accounting course
    cost = next((c for c in courses
                 if "cost account" in c.get("name", "").lower()), None)
    if not cost:
        print("Cost Accounting course not found in courses.json — listing all courses:")
        for c in courses:
            print(f"  {c['name']}")
        sys.exit(1)

    course_id = cost["course_id"]
    print(f"\nBB Tracker Pearson Explorer (manual-click mode)")
    print(f"Course: {cost['name']}  ({course_id})")
    print(f"Output: {OUT_DIR}\n")

    all_responses: list[dict] = []

    async with async_playwright() as p:
        print("Launching browser…")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DIR),
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-focus-on-load", "--no-first-run", "--disable-extensions"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Verify session
        await page.goto(f"{base_url}/ultra/stream",
                        wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(2000)
        if urlparse(page.url).netloc != expected_host:
            print("✗ Not logged in. Open BB Tracker, log in, quit it, then rerun.")
            await context.close()
            sys.exit(1)
        print("✓ Session valid")

        # Context-level response capture — catches ALL frames and iframes
        async def on_response(resp):
            url = resp.url
            ct  = resp.headers.get("content-type", "")
            is_json = "json" in ct
            is_mylab_html = "mylab.pearson.com" in url and ("html" in ct or "text" in ct)

            if not (is_json or is_mylab_html):
                return
            try:
                if is_json:
                    body = await resp.json()
                    text = json.dumps(body)
                else:
                    body = await resp.text()
                    text = body
            except Exception:
                return
            all_responses.append({"url": url, "body": body, "type": "json" if is_json else "html"})
            # Highlight anything that looks grade-related
            if any(k in text.lower() for k in ("grade", "score", "result",
                                                "assignment", "attempt",
                                                "correct", "percent", "points")):
                print(f"  ★ GRADE response ({len(text)} chars): {url[:100]}")
            else:
                print(f"    · {url[:100]}")

        context.on("response", on_response)

        # Navigate to the Cost Accounting outline
        print(f"\nNavigating to Cost Accounting outline…")
        await page.goto(
            f"{base_url}/ultra/courses/{course_id}/outline",
            wait_until="domcontentloaded", timeout=PAGE_TIMEOUT,
        )
        await page.wait_for_timeout(3000)

        print("\n" + "="*60)
        print("READY. In the browser:")
        print("  1. Expand the 'Pearson – My Accounting Lab' folder")
        print("  2. Click 'MyLab and Mastering All Assignments'")
        print("  3. Browse to your grades in Pearson")
        print("  4. Come back here and press ENTER when done")
        print("="*60 + "\n")

        # Block until the user presses Enter (run input in executor so async loop keeps running)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input)

        # Dump all captured responses
        ts = datetime.now().strftime("%H%M%S")

        all_path = OUT_DIR / f"all_responses_{ts}.json"
        all_path.write_text(json.dumps(all_responses, indent=2), encoding="utf-8")
        print(f"\n✓ {len(all_responses)} total JSON responses → {all_path.name}")

        # Separate out anything grade-related
        grade_responses = [
            r for r in all_responses
            if any(k in json.dumps(r["body"]).lower()
                   for k in ("grade", "score", "result", "assignment",
                              "attempt", "correct", "percent", "points"))
        ]
        if grade_responses:
            grade_path = OUT_DIR / f"grade_responses_{ts}.json"
            grade_path.write_text(json.dumps(grade_responses, indent=2), encoding="utf-8")
            print(f"✓ {len(grade_responses)} grade-related responses → {grade_path.name}")
        else:
            print("⚠  No grade-related responses captured — Pearson may not have loaded yet")

        # Print all unique domains seen
        domains = sorted({"/".join(r["url"].split("/")[:3]) for r in all_responses})
        print(f"\nDomains seen:")
        for d in domains:
            print(f"  {d}")

        # Show all current frames
        # Dump HTML of every frame that looks like MyLab
        print(f"\nDumping frame HTML…")
        frame_count = 0
        for cp in context.pages:
            for fr in cp.frames:
                url = fr.url or ""
                if not url or url == "about:blank":
                    continue
                print(f"  frame: {url[:100]}")
                try:
                    html = await fr.content()
                    slug = url.split("//")[-1].split("/")[0].replace(".", "_")[:30]
                    fpath = OUT_DIR / f"frame_{slug}_{ts}.html"
                    fpath.write_text(html, encoding="utf-8")
                    frame_count += 1
                    print(f"    → saved {len(html)} chars to {fpath.name}")
                except Exception as e:
                    print(f"    → could not capture: {e}")
        if frame_count == 0:
            print("  (no frames captured)")

        # Highlight MyLab-specific JSON responses
        mylab = [r for r in all_responses if "mylab.pearson.com" in r["url"]
                 or "pearsoned.com" in r["url"] or "pearson.com/api" in r["url"]]
        if mylab:
            mylab_path = OUT_DIR / f"mylab_responses_{ts}.json"
            mylab_path.write_text(json.dumps(mylab, indent=2), encoding="utf-8")
            print(f"\n✓ {len(mylab)} MyLab-specific responses → {mylab_path.name}")
            for r in mylab:
                print(f"  {r['url']}")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
