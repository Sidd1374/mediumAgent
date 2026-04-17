"""
login.py -- One-time manual login to Medium.

Launches your REAL Chrome browser (not Playwright's bundled Chromium) to
completely bypass Cloudflare detection. Saves your session for the bot.

Usage:
  python login.py

Re-run this if your session expires (typically lasts weeks/months).
"""

import asyncio
import subprocess
import time
from pathlib import Path
from playwright.async_api import async_playwright
from config import log

SESSION_FILE = Path(__file__).parent / "session.json"
PROFILE_DIR = Path(__file__).parent / "playwright_profile"

# Common Chrome install paths on Windows
_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def _find_chrome() -> str:
    """Finds the Chrome executable on the system."""
    for p in _CHROME_PATHS:
        if Path(p).exists():
            return str(p)
    return ""


async def manual_login():
    log.info("=" * 55)
    log.info("Medium Manual Login")
    log.info("=" * 55)
    print()
    print("  Your REAL Chrome browser will open (not Playwright).")
    print("  This bypasses Cloudflare completely.")
    print()
    print("  STEP 1: Log in to Medium using any method")
    print("          (Google, Apple, email link, etc.).")
    print()
    print("  STEP 2: Make sure you can see your Medium feed.")
    print("          Then come back here and press ENTER.")
    print()
    log.info("=" * 55)

    chrome_path = _find_chrome()
    if not chrome_path:
        log.error("Google Chrome not found at standard paths!")
        log.error("Please install Chrome or provide the path manually.")
        return

    log.info(f"Found Chrome: {chrome_path}")

    # Ensure profile directory exists
    PROFILE_DIR.mkdir(exist_ok=True)

    # ---- Launch REAL Chrome as a standalone process ----
    # Key: we do NOT use Playwright's launcher, so Chrome has ZERO automation
    # flags. Cloudflare cannot detect this as automated.
    debug_port = 9222

    log.info(f"Launching Chrome on port {debug_port}...")
    chrome_proc = subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "https://medium.com/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for Chrome to start and load
    log.info("Waiting for Chrome to start...")
    await asyncio.sleep(5)

    # Check Chrome is alive
    if chrome_proc.poll() is not None:
        log.error(f"Chrome exited immediately with code {chrome_proc.returncode}.")
        log.error("Try closing all other Chrome windows first, then run again.")
        return

    log.info("Chrome is running. Connecting via CDP...")

    # ---- Connect Playwright to running Chrome ----
    async with async_playwright() as pw:
        browser = None
        try:
            browser = await pw.chromium.connect_over_cdp(
                f"http://localhost:{debug_port}",
                timeout=10_000,
            )
        except Exception as e:
            log.error(f"Could not connect to Chrome: {e}")
            log.error("Make sure no other process is using port 9222.")
            chrome_proc.terminate()
            return

        contexts = browser.contexts
        if not contexts:
            log.error("No browser context found. Chrome may not have started properly.")
            await browser.close()
            chrome_proc.terminate()
            return

        context = contexts[0]
        pages = context.pages

        if pages:
            page = pages[0]
            title = await page.title()
            log.info(f"Chrome loaded: {title[:60]}")
        else:
            log.info("Chrome opened. Navigate to medium.com in the browser.")

        # ---- Wait for user to log in ----

        input("\n[OK] Press ENTER after you've logged in and can see your Medium feed...\n")

        # ---- Verify login ----

        log.info("Verifying login and clearing any final Cloudflare checks...")
        if pages:
            page = pages[0]

            # Navigate to medium.com to confirm login and force a fresh check
            try:
                await page.goto("https://medium.com/", wait_until="networkidle", timeout=30_000)
            except Exception:
                pass

            # Wait for either the feed OR a sign-in button (to see if we failed)
            # Medium feed often has articles with data-testid="postCard" or specific structure
            success = False
            for _ in range(20):
                title = (await page.title()).lower()
                url = page.url
                
                # Check for Cloudflare stuckness
                if "just a moment" in title or "security verification" in title:
                    log.info("Still seeing Cloudflare... waiting...")
                    await asyncio.sleep(2)
                    continue

                # Check for successful feed load
                # Common Medium feed selectors: article, [data-testid="postCard"], h2
                articles = await page.query_selector_all("article, [data-testid='postCard'], h2")
                if len(articles) > 3 and "signin" not in url and "login" not in url:
                    log.info(f"[OK] Found {len(articles)} articles. Login verified!")
                    success = True
                    break
                
                log.info("Waiting for feed elements to appear...")
                await asyncio.sleep(2)

            if not success:
                log.warning("[!] Could not verify feed elements.")
                log.warning("    If you ARE looking at your feed, you can still save.")
                proceed = input("    Save session anyway? (y/n): ").strip().lower()
                if proceed != "y":
                    log.info("Cancelled. No session saved.")
                    await browser.close()
                    chrome_proc.terminate()
                    return
            else:
                log.info("[OK] Login verified -- you're on your Medium feed!")

        # ---- Save session ----

        try:
            await context.storage_state(path=str(SESSION_FILE))
            log.info(f"[OK] Session saved to {SESSION_FILE}")
            log.info(f"[OK] Browser profile saved to {PROFILE_DIR}/")
        except Exception as e:
            log.error(f"Failed to save session: {e}")

        print()
        print("  Done! The bot will now use this session.")
        print("  Re-run this script if your session expires.")

        # ---- Cleanup ----
        await browser.close()

    # Close Chrome
    chrome_proc.terminate()
    try:
        chrome_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        chrome_proc.kill()

    log.info("Chrome closed. Login complete.")


if __name__ == "__main__":
    asyncio.run(manual_login())
