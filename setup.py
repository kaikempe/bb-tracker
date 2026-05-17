"""First-run login — robust version.

Opens a Chromium window, navigates to Blackboard, and waits until you
ACTUALLY land on the courses page (not just until you close the window).
Then waits a few extra seconds to make sure all cookies — especially the
Microsoft 'Stay signed in?' persistent token — are written to disk before
closing the browser cleanly via Playwright's API.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
BROWSER_DIR = BASE_DIR / "browser_data"

BANNER = """
============================================================
  Blackboard Tracker — Microsoft login
============================================================

A Chromium window will open. Please:
  1. Sign in with your IE account
  2. Approve the Microsoft Authenticator prompt
  3. When asked "Stay signed in?" — click YES  (this matters!)
  4. Wait until you see your Blackboard courses page
  5. DO NOT close the window — this script will close it for you

============================================================
"""


async def main() -> int:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    base_url = config["blackboard_base_url"].rstrip("/")
    expected_host = urlparse(base_url).netloc

    print(BANNER)
    input("Press Enter to open the browser…")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DIR),
            headless=False,
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"{base_url}/ultra/institution-page")

        print(f"\nWaiting for you to log in and reach {expected_host}…")
        print("(Don't close the window — the script will do that.)\n")

        # Poll until we're back on Blackboard's host with /ultra/ in path
        reached = False
        for _ in range(600):  # up to 10 minutes
            await asyncio.sleep(1)
            try:
                current = page.url
            except Exception:
                print("\n✗ Browser window was closed before login completed.")
                print("  Please re-run setup.py and let the script close the window.")
                return 1

            host = urlparse(current).netloc
            if host == expected_host and "/ultra/" in current:
                print(f"✓ Reached: {current}")
                reached = True
                break

        if not reached:
            print("\n✗ Timed out waiting for login.")
            await context.close()
            return 1

        print("  Waiting 8 seconds for cookies to settle…")
        await asyncio.sleep(8)

        print("  Closing browser cleanly…")
        await context.close()

    print("\n✓ Setup complete — cookies saved.")
    print("  Now run:  python3 menubar.py")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
