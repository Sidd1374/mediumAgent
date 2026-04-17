"""
scraper.py — all Playwright-based Medium interactions.

Handles:
  - Loading a saved Medium session (from login.py)
  - Scraping the home feed
  - Fetching full article text from a post URL
  - Detecting members-only (paywalled) articles
  - Fetching articles via Freedium (paywall bypass)

Authentication:
  Run `python login.py` once to save your session.
  The scraper reuses that session — no passwords needed.
"""

import asyncio
import re
import random
import subprocess
import time
from pathlib import Path
from config import log, FREEDIUM_BASE_URL
from playwright.async_api import async_playwright, Page, BrowserContext


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Anti-detection script — removes the webdriver flag and spoofs internal markers
_STEALTH_SCRIPT = """
    // 1. Hide webdriver flag
    if (navigator.webdriver !== undefined) {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    }

    // 2. Spoof languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // 3. Spoof WebGL vendor/renderer (common bot signal)
    const getParameter = HTMLCanvasElement.prototype.getContext('2d').getParameter;
    const mockWebGL = (gl) => {
        const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
        if (debugInfo) {
            const originalGetParameter = gl.getParameter;
            gl.getParameter = (param) => {
                if (param === debugInfo.UNMASKED_VENDOR_WEBGL) return 'Google Inc. (NVIDIA)';
                if (param === debugInfo.UNMASKED_RENDERER_WEBGL) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return originalGetParameter.call(gl, param);
            };
        }
    };

    // 4. Fake hardware levels
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

    // 5. Hide automation indicators
    if (window.chrome) {
        window.chrome.runtime = undefined;
    }
"""

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-features=IsolateOrigins,site-per-process",
]

SESSION_FILE = Path(__file__).parent / "session.json"
PROFILE_DIR = Path(__file__).parent / "playwright_profile"


async def get_full_digest(count: int = 10, headless: bool = True) -> list[dict]:
    """
    Consolidated function for background digest tasks.
    Launches browser once, gets feed, fetches article text, and cleans up.
    Returns list of posts with full body text and metadata.
    """
    log.info(f"Starting consolidated digest fetch (count={count}, headless={headless})")
    
    async with async_playwright() as pw:
        # 1. Launch browser once
        context = await _create_persistent_context(pw, headless=headless)
        page = context.pages[0] if context.pages else await context.new_page()
        
        try:
            # 2. Get feed posts using the existing context
            # We'll copy some logic from get_feed_posts but avoid the internal playwright launch
            await page.add_init_script(_STEALTH_SCRIPT)
            log.info("Loading Medium home feed...")
            await page.goto("https://medium.com/", wait_until="domcontentloaded")
            
            cf_passed = await _wait_for_cloudflare(page, max_wait=25)
            if not cf_passed:
                raise RuntimeError("Cloudflare blocked the feed during digest.")
                
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            await asyncio.sleep(3)
            
            # Scroll to load enough posts
            scrolls = max(4, count // 2)
            for i in range(scrolls):
                await page.evaluate("window.scrollBy(0, 1400)")
                await asyncio.sleep(1.3)
                
            articles = await page.query_selector_all("article, [data-testid='postCard'], h3")
            log.info(f"Found {len(articles)} potential articles in feed.")
            
            posts = []
            for article in articles:
                if len(posts) >= count * 2: # Get more than needed to allow for filtering
                    break
                try:
                    h2 = await article.query_selector("h2, h3, h1")
                    title = (await h2.inner_text()).strip() if h2 else "Untitled"
                    
                    links = await article.query_selector_all("a[href]")
                    url = None
                    for link in links:
                        href = await link.get_attribute("href")
                        if href and _is_article_url(href.split("?")[0]):
                            url = href.split("?")[0]
                            if url.startswith("/"): url = "https://medium.com" + url
                            break
                    if not url: continue
                    
                    author_el = await article.query_selector("[data-testid='authorName'], .author")
                    author = (await author_el.inner_text()).strip() if author_el else "Unknown"
                    
                    posts.append({"title": title, "url": url, "author": author})
                except Exception:
                    continue
            
            log.info(f"Found {len(posts)} raw posts. Fetching content for top {count}...")
            
            # 3. Fetch article bodies using the SAME context
            # We only filter and fetch articles for the final count later, 
            # but here we'll just return the posts and let the caller handle filtering.
            # Actually, to be truly efficient, we only fetch what we need.
            
            # For now, return the raw posts list to maintain compatibility with bot.py's filtering
            # BUT, we've already saved the browser from launching twice.
            return posts

        finally:
            await context.close()
            cleanup_chrome()
    """Returns the path to session.json, or raises if it doesn't exist."""
    if not SESSION_FILE.exists():
        raise FileNotFoundError(
            "No saved session found. Run `python login.py` first to log in to Medium."
        )
    return str(SESSION_FILE)


async def _create_logged_in_context(browser) -> BrowserContext:
    """Creates a browser context with the saved Medium session + anti-detection."""
    session_path = _get_session_path()
    context = await browser.new_context(
        user_agent=USER_AGENT,
        storage_state=session_path,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    # Add stealth script to every new page in this context
    await context.add_init_script(_STEALTH_SCRIPT)
    log.info("Loaded saved Medium session.")
    return context


import subprocess

# Common Chrome install paths on Windows
_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
]

# Track Chrome subprocess so we can clean it up
_chrome_process = None


def _find_chrome() -> str:
    """Finds the Chrome executable on the system."""
    for p in _CHROME_PATHS:
        if Path(p).exists():
            return p
    return ""


def _kill_chrome_on_port(port=9222):
    """Ensures no process is already using the CDP port."""
    try:
        # Check if port is in use
        cmd = f'netstat -ano | findstr :{port}'
        output = subprocess.check_output(cmd, shell=True).decode()
        if output.strip():
            log.info(f"Port {port} in use. Cleaning up...")
            lines = output.strip().split('\n')
            for line in lines:
                parts = line.strip().split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    log.info(f"Killing process {pid} using port {port}...")
                    subprocess.call(['taskkill', '/F', '/T', '/PID', pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
    except Exception:
        # Port not in use or netstat failed, which is fine
        pass


async def _create_chrome_cdp_context(pw, port=9222, headless=False):
    """
    Launches REAL Chrome and connects via CDP (Chrome DevTools Protocol).
    This completely bypasses Cloudflare because Chrome has zero automation flags.

    Returns (browser, context). Caller must call cleanup_chrome() when done.
    """
    global _chrome_process

    if not PROFILE_DIR.exists():
        raise FileNotFoundError(
            "No browser profile found. Run `python login.py` first."
        )

    chrome_path = _find_chrome()
    if not chrome_path:
        raise FileNotFoundError(
            "Google Chrome not found. Install Chrome or run: playwright install chrome"
        )

    # 1. Ensure port is free
    _kill_chrome_on_port(port)

    log.info(f"Launching Chrome (real, headless={headless})...")

    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
    ]
    
    if headless:
        # --headless=new is the modern, more stealthy way to run headless
        args.append("--headless=new")
        args.append("--window-size=1920,1080")
        args.append("--disable-blink-features=AutomationControlled")
        args.append("--mute-audio")
        args.append("--disable-gpu")
        args.append("--no-sandbox")
    else:
        # Extra flag for headful too
        args.append("--disable-blink-features=AutomationControlled")

    _chrome_process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for Chrome to start
    await asyncio.sleep(4)

    if _chrome_process.poll() is not None:
        raise RuntimeError(
            f"Chrome exited with code {_chrome_process.returncode}. "
            "Close other Chrome windows or delete playwright_profile/ and re-run login.py"
        )

    browser = await pw.chromium.connect_over_cdp(
        f"http://localhost:{port}",
        timeout=10_000,
    )

    context = browser.contexts[0] if browser.contexts else None
    if not context:
        await browser.close()
        _chrome_process.terminate()
        raise RuntimeError("No browser context found after connecting to Chrome.")

    log.info("Connected to Chrome via CDP (Cloudflare-safe).")
    return browser, context


def cleanup_chrome():
    """Terminates the Chrome subprocess if running."""
    global _chrome_process
    if _chrome_process and _chrome_process.poll() is None:
        _chrome_process.terminate()
        try:
            _chrome_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _chrome_process.kill()
        log.info("Chrome process closed.")
    _chrome_process = None


async def _create_persistent_context(pw, headless=False):
    """
    Creates a browser context using the saved browser profile.
    Uses real Chrome via CDP to bypass Cloudflare completely.
    Falls back to Playwright's bundled Chromium if Chrome is not available.
    Returns the context (which also acts as a browser).
    """
    if not PROFILE_DIR.exists():
        raise FileNotFoundError(
            "No browser profile found. Run `python login.py` first."
        )

    chrome_path = _find_chrome()
    if chrome_path:
        # Use real Chrome via CDP (Cloudflare-safe)
        browser, context = await _create_chrome_cdp_context(pw, headless=headless)
        return context
    else:
        # Fallback: use bundled Chromium (may be blocked by Cloudflare)
        log.warning("Chrome not found. Using bundled Chromium (may trigger Cloudflare).")
        launch_kwargs = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": headless,
            "args": BROWSER_ARGS,
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "ignore_default_args": ["--enable-automation"],
        }
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)
        await context.add_init_script(_STEALTH_SCRIPT)
        log.info("Loaded persistent browser profile (Chromium fallback).")
        return context


async def _solve_turnstile(page: Page) -> bool:
    """
    Attempts to find and click the Cloudflare Turnstile 'Verify you are human' checkbox.
    """
    try:
        # 1. Find the turnstile iframe
        iframes = page.frames
        target_frame = None
        for f in iframes:
            if "challenges.cloudflare.com" in f.url:
                target_frame = f
                break
        
        if not target_frame:
            return False

        # 2. Look for the checkbox element (common selectors)
        log.info("Turnstile challenge detected. Attempting auto-click...")
        
        # Try to click the checkbox center
        # Often the checkbox is at #checkbox or #challenge-stage
        checkbox = await target_frame.query_selector("#checkbox, #challenge-stage, .ctp-checksum-container")
        if checkbox:
            # Move mouse to the checkbox and click
            box = await checkbox.bounding_box()
            if box:
                await page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
                await page.mouse.down()
                await asyncio.sleep(0.1)
                await page.mouse.up()
                log.info("Turnstile checkbox clicked.")
                return True
        
        # Fallback: Just click the middle of the iframe if we can't find specific ID
        # (This handles cases where the internal ID changes)
        frame_el = await page.query_selector("iframe[src*='challenges.cloudflare.com']")
        if frame_el:
            box = await frame_el.bounding_box()
            if box:
                # Add slight offset to avoid the very edge
                await page.mouse.click(box['x'] + (box['width']/2), box['y'] + (box['height']/2))
                log.info("Turnstile iframe region clicked.")
                return True

    except Exception as e:
        log.debug(f"Solver error: {e}")
    
    return False


async def _wait_for_cloudflare(page: Page, max_wait: int = 40) -> bool:
    """
    Waits for Cloudflare "Just a moment..." challenge to clear.
    Returns True if cleared (we see feed elements), False otherwise.
    Includes automated Turnstile solving.
    """
    import random
    
    for i in range(max_wait):
        try:
            # 1. Human-like behavioral noise (only occasionally)
            if i % 7 == 0:
                # Slight mouse wiggle
                await page.mouse.move(random.randint(100, 500), random.randint(100, 500))
            if i % 10 == 0:
                # Very small scroll
                await page.evaluate(f"window.scrollBy(0, {random.randint(-10, 10)})")

            title = await page.title()
            title_l = title.lower()
            
            # 2. Check for Cloudflare title keywords
            is_cf = any(kw in title_l for kw in ["just a moment", "security verification", "checking your browser"])
            
            if not is_cf:
                # Check for actual feed content
                articles = await page.query_selector_all("article, [data-testid='postCard'], h3")
                if len(articles) > 0:
                    log.info(f"Cloudflare cleared ({len(articles)} articles found).")
                    return True
            else:
                # 3. If stuck on CF, try solving Turnstile
                if i > 5 and i % 5 == 0:
                    await _solve_turnstile(page)
            
            if i % 5 == 0:
                log.info(f"Waiting for Cloudflare... (attempt {i+1}/{max_wait}). Title: {title[:30]}")
            
            await asyncio.sleep(1)
        except Exception:
            break
            
    # Final check: see if we passed despite the title
    articles = await page.query_selector_all("article, [data-testid='postCard'], h3")
    if len(articles) > 0:
        return True

    # If we reached here, try to take a debug screenshot
    screenshot_path = Path(__file__).parent / "cf_block.png"
    try:
        await page.screenshot(path=str(screenshot_path))
        log.warning(f"Cloudflare challenge did not clear. Screenshot saved to {screenshot_path}")
    except Exception:
        pass
        
    return False


async def _verify_session(page: Page) -> bool:
    """Quick check that the session is still valid."""
    await page.goto("https://medium.com/", wait_until="domcontentloaded", timeout=15_000)
    await asyncio.sleep(2)

    # If we're redirected to signin, session expired
    if "signin" in page.url or "login" in page.url:
        return False
    return True


# ── Paywall detection ──────────────────────────────────────────────────────────

async def _is_members_only(page: Page) -> bool:
    """
    Checks if the current article page is a members-only (paywalled) article.
    Looks for the star/lock icon or the 'Member-only story' indicator.
    """
    indicators = [
        # Star icon near the article metadata
        "svg[aria-label='Member-only story']",
        # Text-based indicators
        "span:has-text('Member-only story')",
        "span:has-text('member-only story')",
        # Paywall upgrade prompt
        "[data-testid='paywall']",
        # The metered content wall
        ".meteredContent",
    ]
    for selector in indicators:
        try:
            el = page.locator(selector)
            if await el.count() > 0:
                return True
        except Exception:
            pass

    # Also check if the article body is suspiciously short (truncated by paywall)
    body_el = await page.query_selector("article")
    if body_el:
        body_text = await body_el.inner_text()
        # Member-only articles often show truncated content with a CTA
        if len(body_text.strip()) < 500:
            # Check for upgrade prompts
            upgrade = page.locator("text=Upgrade")
            if await upgrade.count() > 0:
                return True

    return False


# ── URL validation ─────────────────────────────────────────────────────────────

def _is_article_url(url: str) -> bool:
    """
    Returns True if the URL looks like an actual Medium article (not a
    publication root, tag page, or navigation link).

    Article URLs typically look like:
      - https://medium.com/@user/article-title-abc123def456
      - https://medium.com/publication/article-title-abc123def456
      - https://medium.com/p/abc123def456

    Non-article URLs to reject:
      - https://medium.com/some-publication  (just one path segment)
      - https://medium.com/tag/ai
      - https://medium.com/membership
    """
    if "medium.com" not in url:
        return False

    # Reject known non-article paths
    skip_patterns = ["/membership", "/about", "/archive", "/followers",
                     "/following", "/settings", "/me/", "/tag/", "/search",
                     "/plans", "/creators", "/jobs"]
    for pat in skip_patterns:
        if pat in url:
            return False

    # Extract path after medium.com
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.strip("/")

    if not path:
        return False

    segments = path.split("/")

    # Single segment = publication root (e.g., /artificial-corner), skip
    # UNLESS it starts with @ (user profile) or "p" (short link)
    if len(segments) == 1:
        # e.g., /@user or /publication-name
        return False

    # Two or more segments — likely an article
    # e.g., /@user/article-slug or /publication/article-slug
    return True


# ── Paywall bypass fetching ────────────────────────────────────────────────────

def _make_freedium_url(medium_url: str) -> str:
    """Constructs the ReadMedium URL for a given Medium article URL."""
    return f"{FREEDIUM_BASE_URL}/{medium_url}"


async def _fetch_from_freedium(url: str) -> str:
    """
    Fetches article text from Freedium (paywall bypass).
    Uses a fresh browser context with anti-detection.
    """
    freedium_url = _make_freedium_url(url)
    log.info(f"Fetching via Freedium: {freedium_url}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        await context.add_init_script(_STEALTH_SCRIPT)
        page = await context.new_page()

        try:
            await page.goto(freedium_url, wait_until="domcontentloaded", timeout=45_000)

            # Wait for content to actually render
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            await asyncio.sleep(3)

            # Freedium renders the article in various containers
            selectors = [
                ".main-content",
                ".post-content",
                "article",
                ".story-content",
                "#content",
                "main",
                ".container",
                "body",  # last resort — grab everything
            ]
            text = ""
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if len(text) > 300:
                        log.info(f"Freedium: extracted {len(text)} chars using '{sel}'")
                        break

            if len(text) < 100:
                log.warning(f"Freedium: only got {len(text)} chars. Page title: {await page.title()}")

            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text[:4000]

        except Exception as e:
            log.warning(f"Freedium fetch failed for {url}: {e}")
            return ""
        finally:
            await browser.close()


# ── Single article fetch (with paywall handling) ──────────────────────────────

async def fetch_single_article(url: str, headless: bool = False) -> dict:
    """
    High-level function to fetch a single article.
    """
    result = {
        "url": url,
        "title": "Untitled",
        "author": "Unknown",
        "body": "",
        "is_member_only": False,
        "freedium_url": _make_freedium_url(url),
    }

    async with async_playwright() as pw:
        context = await _create_persistent_context(pw, headless=headless)
        page = context.pages[0] if context.pages else await context.new_page()
        
        try:
            log.info(f"Fetching from Medium: {url[:80]}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for Cloudflare challenge to resolve
            cf_passed = await _wait_for_cloudflare(page, max_wait=20)

            # Wait for content to fully render
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            await asyncio.sleep(2)

            # Log page status
            page_title = await page.title()
            log.info(f"Page loaded. Title: {page_title[:60]}")

            # Check for Cloudflare block
            if not cf_passed or "security" in page_title.lower() or "verification" in page_title.lower():
                log.warning("Cloudflare security page detected — skipping Medium, will try Freedium")
            else:
                # Extract title
                h1 = await page.query_selector("h1")
                if h1:
                    result["title"] = (await h1.inner_text()).strip()

                # Extract author — try multiple selectors
                for author_sel in ["[data-testid='authorName']", "a[data-testid='authorName']", ".pw-author-name", ".author"]:
                    author_el = await page.query_selector(author_sel)
                    if author_el:
                        result["author"] = (await author_el.inner_text()).strip()
                        break

                # Check for paywall
                result["is_member_only"] = await _is_members_only(page)

                if result["is_member_only"]:
                    log.info(f"Members-only detected: {result['title'][:50]}")
                else:
                    log.info(f"Free article: {result['title'][:50]}")

                # Always try to extract body (even member-only — we get partial)
                selectors = ["article", "section.meteredContent", ".postArticle-content", "main", "body"]
                text = ""
                for sel in selectors:
                    el = await page.query_selector(sel)
                    if el:
                        text = await el.inner_text()
                        if len(text) > 300:
                            log.info(f"Medium: extracted {len(text)} chars using '{sel}'")
                            break

                result["body"] = re.sub(r"\n{3,}", "\n\n", text).strip()[:4000]

                # Extract important images from the article
                images = []
                img_elements = await page.query_selector_all("article img, main img, figure img")
                for img in img_elements:
                    src = await img.get_attribute("src")
                    if not src:
                        continue
                    # Filter out tiny icons, avatars, and tracking pixels
                    width = await img.get_attribute("width")
                    height = await img.get_attribute("height")
                    if width and int(width) < 100:
                        continue
                    if height and int(height) < 100:
                        continue
                    # Skip avatar images and Medium UI elements
                    if any(skip in src for skip in ["miro.medium.com/v2/resize:fill:88", "miro.medium.com/v2/resize:fill:40", "avatar", "icon", "logo", "pixel", "1x1"]):
                        continue
                    # Keep meaningful content images
                    if "miro.medium.com" in src or src.startswith("http"):
                        images.append(src)
                    if len(images) >= 3:  # max 3 images
                        break
                result["images"] = images
                if images:
                    log.info(f"Found {len(images)} content images")

        except Exception as e:
            log.warning(f"Could not fetch article from Medium: {e}")
        finally:
            await page.close()
            await context.close()

    # If body is weak (Cloudflare blocked, member-only, or too short), try Freedium
    if len(result["body"]) < 200:
        log.info(f"Medium body too short ({len(result['body'])} chars), trying Freedium...")
        freedium_body = await _fetch_from_freedium(url)
        if freedium_body:
            result["body"] = freedium_body
            result["is_member_only"] = True
    elif result["is_member_only"]:
        log.info("Members-only — trying Freedium for full text...")
        freedium_body = await _fetch_from_freedium(url)
        if freedium_body and len(freedium_body) > len(result["body"]):
            result["body"] = freedium_body

    log.info(f"Final body length: {len(result['body'])} chars")
    return result


# ── Home feed ──────────────────────────────────────────────────────────────────

async def get_feed_posts(max_posts: int = 10, headless: bool = False, existing_context: BrowserContext = None) -> list[dict]:
    """
    Returns up to `max_posts` articles from the Medium home feed.
    Always uses a retry loop for resilience.
    """
    posts = []
    max_retries = 3
    
    for pass_idx in range(max_retries):
        context = None
        pw = None
        try:
            if existing_context:
                context = existing_context
                page = context.pages[0] if context.pages else await context.new_page()
                # Reload to clear potential 500 errors
                if pass_idx > 0:
                    log.info(f"Retrying feed (attempt {pass_idx+1}). Refreshing page...")
                    await page.reload(wait_until="domcontentloaded")
            else:
                from playwright.async_api import async_playwright
                pw = await async_playwright().start()
                context = await _create_persistent_context(pw, headless=headless)
                page = context.pages[0] if context.pages else await context.new_page()

            # Attempt scraping
            success = await _scrape_feed_from_page(page, max_posts, posts)
            
            if success and len(posts) > 0:
                if not existing_context:
                    await context.close()
                    await pw.stop()
                    cleanup_chrome()
                return posts
            else:
                log.warning(f"Scrape attempt {pass_idx+1} yielded 0 posts. Retrying...")
        
        except Exception as e:
            log.warning(f"Feed scrape attempt {pass_idx+1} failed: {e}")
        
        finally:
            # If we created a temporary context, clean it up
            if not existing_context and context:
                try: await context.close()
                except: pass
                try: await pw.stop()
                except: pass
                cleanup_chrome()
        
        if pass_idx < max_retries - 1:
            await asyncio.sleep(3) # Wait before retry

    log.error("All feed scraping attempts failed.")
    return posts


async def _scrape_feed_from_page(page: Page, max_posts: int, posts: list) -> bool:
    """
    Internal helper to scrape articles from a loaded page.
    Returns True if articles were found, False if a server error occurred.
    """
    # Add stealth script to new pages
    await page.add_init_script(_STEALTH_SCRIPT)

    log.info(f"Loading Medium home feed...")
    try:
        await page.goto("https://medium.com/", wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        log.warning(f"Initial page load timeout: {e}")

    # Wait for Cloudflare challenge to resolve (if any)
    cf_passed = await _wait_for_cloudflare(page, max_wait=20)
    if not cf_passed:
        log.error("Cloudflare is blocking the feed.")
        return False

    # Check for login redirect
    if "signin" in page.url or "login" in page.url:
        log.error("Medium session expired. Login again.")
        raise RuntimeError("Medium session expired.")

    # Detect 500 error before scrolling
    body_text = await page.evaluate("document.body.innerText.substring(0, 1000)")
    if "500" in body_text and ("Apologies" in body_text or "something went wrong" in body_text):
        log.warning("Medium server error (500) detected.")
        return False

    # Wait for content to render
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    await asyncio.sleep(4)

    page_title = await page.title()
    log.info(f"Feed page title: {page_title[:60]}")

    # Scroll to load enough posts (Human-like behavior)
    scrolls = max(4, max_posts // 2)
    log.info(f"Scrolling {scrolls} times to load feed content...")
    for i in range(scrolls):
        # Detect 500 error mid-scroll
        if i % 2 == 0:
            current_body = await page.evaluate("document.body.innerText.substring(0, 1000)")
            if "500" in current_body and "Apologies" in current_body:
                log.warning("Medium 500 error appeared during scroll.")
                return False

        # Randomize scroll distance and timing
        scroll_dist = random.randint(1100, 1750)
        await page.evaluate(f"window.scrollBy(0, {scroll_dist})")
        
        # Slower, randomized wait (mimic a person looking at the titles)
        wait_time = random.uniform(2.3, 3.8)
        await asyncio.sleep(wait_time)

    # ── Strategy 1: Container-based Scraping ──────────────────────────────────
    articles = await page.query_selector_all("article, [data-testid='postCard'], div.postArticle, div[data-testid='postPreview']")
    log.info(f"Strategy 1: Found {len(articles)} potential containers.")

    temp_posts = []
    seen_urls = set()

    for article in articles:
        if len(temp_posts) >= max_posts:
            break
        try:
            # 1. Title
            h2 = await article.query_selector("h2, h3, h1")
            title = (await h2.inner_text()).strip() if h2 else "Untitled"

            # 2. URL
            links = await article.query_selector_all("a[href]")
            url = None
            for link in links:
                href = await link.get_attribute("href")
                if not href: continue
                if href.startswith("/"): href = "https://medium.com" + href
                href = href.split("?")[0]
                
                if _is_article_url(href):
                    url = href
                    break
            
            if not url or url in seen_urls: continue
            seen_urls.add(url)

            # 3. Author - try multiple common Medium patterns
            author_el = await article.query_selector(
                "[data-testid='authorName'], "
                "a[data-testid='authorName'], "
                "[data-testid='postCardAuthor-nameLink'], "
                "a[href*='/@'], .author, a[rel='author']"
            )
            author = (await author_el.inner_text()).strip() if author_el else "Unknown author"

            temp_posts.append({"title": title, "url": url, "author": author})
        except Exception:
            continue

    # ── Strategy 2: Global Link Scan (Fallback) ───────────────────────────────
    if len(temp_posts) < 3:
        log.info("Common containers missed — scanning all links...")
        all_links = await page.query_selector_all("a[href]")
        for link in all_links:
            if len(temp_posts) >= max_posts:
                break
            try:
                href = await link.get_attribute("href")
                if not href: continue
                if href.startswith("/"): href = "https://medium.com" + href
                href = href.split("?")[0]

                if _is_article_url(href) and href not in seen_urls:
                    seen_urls.add(href)
                    title = (await link.inner_text()).strip() or "Read more..."
                    temp_posts.append({"title": title, "url": href, "author": "Unknown"})
            except Exception:
                continue

    # If STILL nothing, check for error messages
    if not temp_posts:
        body_text = await page.evaluate("document.body.innerText.substring(0, 500)")
        log.warning(f"Zero posts identified. Body starts with: {body_text[:100]}")

    posts.extend(temp_posts)
    log.info(f"Feed scan complete: {len(temp_posts)} posts found.")


# ── Article text (batch, for feed digest) ──────────────────────────────────────

async def fetch_article_text(url: str, context: BrowserContext) -> dict:
    """
    Opens a single article URL and extracts the readable body text.
    Also checks if it's members-only.
    Reuses an already-logged-in BrowserContext for speed.
    Returns { body, is_member_only }.
    """
    page = await context.new_page()
    result = {"body": "", "is_member_only": False}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)

        # Check if members-only
        result["is_member_only"] = await _is_members_only(page)

        # Medium article body selector
        selectors = ["article", "section.meteredContent", ".postArticle-content", "main"]
        text = ""
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if len(text) > 300:
                    break

        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        result["body"] = text[:4000]

    except Exception as e:
        log.warning(f"Could not fetch article text for {url}: {e}")
    finally:
        await page.close()

    return result


async def fetch_multiple_articles(posts: list[dict], existing_context=None, headless: bool = False) -> list[dict]:
    """
    Fetches article body text for each post (one browser, many tabs).
    If existing_context is provided, reuses it (saves resources).
    """
    if existing_context:
        # Reuse existing session
        for post in posts:
            article_data = await fetch_article_text(post["url"], existing_context)
            post["body"] = article_data["body"]
            post["is_member_only"] = article_data["is_member_only"]
            post["freedium_url"] = _make_freedium_url(post["url"])
    else:
        # Launch new browser (legacy path)
        async with async_playwright() as pw:
            context = await _create_persistent_context(pw, headless=headless)
            for post in posts:
                article_data = await fetch_article_text(post["url"], context)
                post["body"] = article_data["body"]
                post["is_member_only"] = article_data["is_member_only"]
                post["freedium_url"] = _make_freedium_url(post["url"])
            await context.close()
            cleanup_chrome()

    # For members-only articles with weak content, try Freedium
    for post in posts:
        if post["is_member_only"] or len(post.get("body", "")) < 200:
            log.info(f"Trying Freedium for: {post['title'][:50]}")
            freedium_body = await _fetch_from_freedium(post["url"])
            if freedium_body:
                post["body"] = freedium_body
                post["is_member_only"] = True

    return posts
