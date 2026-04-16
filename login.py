"""
login.py — One-time manual login to Medium.

Opens a visible browser window where you log in to Medium using any method
(Google, Apple, email magic link, etc.). Once logged in, your session is
saved in TWO ways:
  1. session.json — cookies for regular article fetching
  2. browser_profile/ — full browser profile that includes Cloudflare tokens

Uses anti-detection settings to bypass Cloudflare bot protection.

Usage:
  python login.py

Re-run this if your session expires (typically lasts weeks/months).
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from config import log

SESSION_FILE = Path(__file__).parent / "session.json"
PROFILE_DIR = Path(__file__).parent / "browser_profile"


async def manual_login():
    log.info("=" * 55)
    log.info("Medium Manual Login")
    log.info("=" * 55)
    log.info("")
    log.info("A browser window will open. Log in to Medium using")
    log.info("any method (Google, Apple, email link, etc.).")
    log.info("")
    log.info("IMPORTANT: After logging in, STAY in the browser.")
    log.info("Navigate to https://medium.com/ and make sure you")
    log.info("can see your feed. Then come back here and press ENTER.")
    log.info("=" * 55)

    async with async_playwright() as pw:
        # Use persistent context — saves full browser state including Cloudflare tokens
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,  # visible browser
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="Asia/Kolkata",
        )

        # Remove the "webdriver" flag
        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        # Navigate to Medium sign-in
        log.info("Opening Medium...")
        await page.goto("https://medium.com/m/signin", wait_until="domcontentloaded")

        # Wait for user to complete login
        input("\n✅ Press ENTER after you've logged in and can see your Medium feed...\n")

        # Navigate to home to verify login (don't rely on signin page URL)
        log.info("Checking login status...")
        await page.goto("https://medium.com/", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Check for Cloudflare first
        title = await page.title()
        if "just a moment" in title.lower():
            log.info("Waiting for Cloudflare to clear...")
            for _ in range(15):
                await asyncio.sleep(1)
                title = await page.title()
                if "just a moment" not in title.lower():
                    break

        # Check if we're on the actual Medium page (not signin)
        current_url = page.url
        log.info(f"Current URL: {current_url}")
        log.info(f"Page title: {title[:60]}")

        if "signin" in current_url or "login" in current_url:
            log.warning("⚠️  It looks like login didn't complete.")
            log.warning("     You may need to log in again in the browser.")
            proceed = input("Save session anyway? (y/n): ").strip().lower()
            if proceed != "y":
                log.info("Cancelled. No session saved.")
                await context.close()
                return
        else:
            log.info("✅ Login verified — you're on Medium!")

        # Save storage_state as session.json
        await context.storage_state(path=str(SESSION_FILE))
        log.info(f"✅ Session saved to {SESSION_FILE}")
        log.info(f"✅ Browser profile saved to {PROFILE_DIR}/")

        log.info("")
        log.info("The bot will now use this session for all scraping.")
        log.info("Re-run this script if your session expires.")

        await context.close()


if __name__ == "__main__":
    asyncio.run(manual_login())
