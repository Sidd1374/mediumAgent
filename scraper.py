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
from pathlib import Path
from config import log, FREEDIUM_BASE_URL
from playwright.async_api import async_playwright, Page, BrowserContext


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Anti-detection script — removes the webdriver flag that Cloudflare looks for
_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined
    });
"""

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
]

SESSION_FILE = Path(__file__).parent / "session.json"
PROFILE_DIR = Path(__file__).parent / "browser_profile"


# ── Session-based auth ─────────────────────────────────────────────────────────

def _get_session_path() -> str:
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


async def _create_persistent_context(pw):
    """
    Creates a persistent browser context using the saved browser profile.
    This includes Cloudflare cf_clearance tokens, bypassing "Just a moment..." pages.
    Returns the context (which also acts as a browser).
    """
    if not PROFILE_DIR.exists():
        raise FileNotFoundError(
            "No browser profile found. Run `python login.py` first."
        )

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=True,
        args=BROWSER_ARGS,
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    # Add stealth script
    for page in context.pages:
        await page.add_init_script(_STEALTH_SCRIPT)
    log.info("Loaded persistent browser profile (with Cloudflare tokens).")
    return context


async def _wait_for_cloudflare(page: Page, max_wait: int = 15) -> bool:
    """Waits for Cloudflare challenge to resolve. Returns True if passed."""
    title = await page.title()
    if "just a moment" not in title.lower() and "security" not in title.lower():
        return True

    log.info("Cloudflare challenge detected — waiting for it to resolve...")
    for i in range(max_wait):
        await asyncio.sleep(1)
        title = await page.title()
        if "just a moment" not in title.lower() and "security" not in title.lower():
            log.info(f"Cloudflare passed after {i+1}s")
            return True

    log.warning(f"Cloudflare did not clear after {max_wait}s")
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
        if segments[0].startswith("@") or segments[0] == "p":
            return False  # just a user profile page, not article
        return False  # just a publication name

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

async def fetch_single_article(url: str) -> dict:
    """
    High-level function to fetch a single article.
    1. Opens the Medium URL using saved session
    2. Checks if it's members-only
    3. Extracts text (always tries, even for member-only)
    4. Falls back to Freedium if text is weak
    5. Returns { body, is_member_only, freedium_url, title, author }
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
        browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        context = await _create_logged_in_context(browser)

        page = await context.new_page()
        try:
            log.info(f"Fetching from Medium: {url[:80]}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

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
            if "security" in page_title.lower() or "verification" in page_title.lower():
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
            await browser.close()

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

async def get_feed_posts(max_posts: int = 10) -> list[dict]:
    """
    Returns up to `max_posts` articles from the Medium home feed.
    Each item: { title, url, author, description }
    """
    posts = []
    async with async_playwright() as pw:
        # Use persistent profile — includes Cloudflare clearance tokens
        context = await _create_persistent_context(pw)
        page = context.pages[0] if context.pages else await context.new_page()

        # Add stealth script to new pages
        await page.add_init_script(_STEALTH_SCRIPT)

        log.info("Loading Medium home feed…")
        await page.goto("https://medium.com/", wait_until="domcontentloaded")

        # Wait for Cloudflare challenge to resolve (if any)
        cf_passed = await _wait_for_cloudflare(page, max_wait=20)

        if not cf_passed:
            log.error("Cloudflare is blocking the feed. Run `python login.py` again.")
            await context.close()
            return []

        # Wait for content to render
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(3)

        page_title = await page.title()
        log.info(f"Feed page title: {page_title[:60]}")

        # Check for login redirect
        if "signin" in page.url or "login" in page.url:
            log.error("Medium session expired. Run `python login.py` to log in again.")
            await context.close()
            raise RuntimeError("Medium session expired. Run `python login.py` to log in again.")

        # Scroll to load enough posts
        scrolls = max(4, max_posts // 2)
        for i in range(scrolls):
            await page.evaluate("window.scrollBy(0, 1400)")
            await asyncio.sleep(1.3)

        # Try multiple selectors — Medium changes their HTML structure
        articles = await page.query_selector_all("article")
        log.info(f"Found {len(articles)} <article> elements in feed.")

        # Fallback: if no <article> tags, try common Medium feed containers
        if not articles:
            log.info("No <article> elements — trying alternative selectors...")
            for alt_selector in [
                "div[data-testid='postPreview']",
                "div.postArticle",
                "div[role='link']",
                "section > div > div > div",
            ]:
                articles = await page.query_selector_all(alt_selector)
                if articles:
                    log.info(f"Found {len(articles)} elements using '{alt_selector}'")
                    break

        # If still nothing, log page content for debugging
        if not articles:
            snippet = await page.evaluate("document.body.innerText.substring(0, 500)")
            log.warning(f"No articles found. Page body starts with: {snippet[:200]}")

        for article in articles:
            if len(posts) >= max_posts:
                break
            try:
                h2 = await article.query_selector("h2, h3, h1")
                title = (await h2.inner_text()).strip() if h2 else "Untitled"

                links = await article.query_selector_all("a[href]")
                url = None
                for link in links:
                    href = await link.get_attribute("href")
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://medium.com" + href
                    href = href.split("?")[0]

                    # Skip non-article links
                    if not _is_article_url(href):
                        continue

                    url = href
                    break

                if not url:
                    continue

                author_el = await article.query_selector("[data-testid='authorName'], .author, a[rel='author']")
                author = (await author_el.inner_text()).strip() if author_el else "Unknown author"

                desc_el = await article.query_selector("h4, p, h3")
                description = (await desc_el.inner_text()).strip() if desc_el else ""

                posts.append({
                    "title": title,
                    "url": url,
                    "author": author,
                    "description": description,
                })
            except Exception as e:
                log.warning(f"Skipped feed article: {e}")

        await context.close()

    seen_urls = set()
    unique = []
    for p in posts:
        if p["url"] not in seen_urls:
            seen_urls.add(p["url"])
            unique.append(p)

    log.info(f"Feed: {len(unique)} unique posts found.")
    return unique


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


async def fetch_multiple_articles(posts: list[dict]) -> list[dict]:
    """
    Fetches article body text for each post (one browser, many tabs).
    For members-only articles, falls back to Freedium.
    Adds 'body', 'is_member_only', and 'freedium_url' keys to each post dict.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        context = await _create_logged_in_context(browser)

        # Fetch articles sequentially to avoid overwhelming the browser
        for post in posts:
            article_data = await fetch_article_text(post["url"], context)
            post["body"] = article_data["body"]
            post["is_member_only"] = article_data["is_member_only"]
            post["freedium_url"] = _make_freedium_url(post["url"])

        await browser.close()

    # For members-only articles with weak content, try Freedium
    for post in posts:
        if post["is_member_only"] or len(post.get("body", "")) < 200:
            log.info(f"Trying Freedium for: {post['title'][:50]}")
            freedium_body = await _fetch_from_freedium(post["url"])
            if freedium_body:
                post["body"] = freedium_body
                post["is_member_only"] = True

    return posts
