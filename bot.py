"""
bot.py — Interactive Telegram bot for Medium Agent.

Commands:
  /start  — Welcome message with usage instructions
  /mail   — Scrape Medium feed → summarize 10 posts → send email digest

Message handlers:
  Any Medium URL — Fetch article → summarize → reply with summary + Freedium link

Usage:
  python bot.py
"""

import re
import asyncio
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import (
    log,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    SEEN_FEED_FILE,
    load_seen,
    save_seen,
    post_key,
    HEADLESS_MODE,
)
from playwright.async_api import async_playwright
from scraper import (
    fetch_single_article, 
    get_feed_posts, 
    fetch_multiple_articles, 
    _create_persistent_context, 
    cleanup_chrome
)
from summarizer import summarize_post, summarize_all
from gmail_sender import send_feed_digest


# ── Access control ─────────────────────────────────────────────────────────────

def _is_authorized(update: Update) -> bool:
    """Only respond to the configured chat ID for security."""
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


# ── Helpers ────────────────────────────────────────────────────────────────────

MEDIUM_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:medium\.com|[a-zA-Z0-9-]+\.medium\.com)/[^\s]+"
)


def _esc(text: str) -> str:
    """Escapes HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# ── /start ─────────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if not _is_authorized(update):
        return

    welcome = (
        "┌─────────────────────────────┐\n"
        "   🤖  <b>Medium Agent Bot</b>\n"
        "└─────────────────────────────┘\n\n"
        "<b>What I can do:</b>\n\n"
        "📌  <b>Send a Medium link</b>\n"
        "      I'll fetch the article, generate a\n"
        "      structured summary with key insights,\n"
        "      and include a free link if it's paywalled.\n\n"
        "📧  <b>/mail</b>  or  <b>/mail 5</b>\n"
        "      Scans your feed, summarizes new posts,\n"
        "      and emails you a curated digest.\n"
        "      Default: 10 articles  •  Max: 10\n\n"
        "─────────────────────────────\n"
        "🔒  <i>All summaries run locally via Ollama</i>\n"
        "🛡  <i>Your data never leaves your PC</i>"
    )
    await update.message.reply_text(welcome, parse_mode="HTML")


# ── /mail ──────────────────────────────────────────────────────────────────────

MAX_FEED_POSTS = 10

async def mail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the /mail command.
    Usage: /mail [count]  — default 10, max 10
    Scrapes Medium feed → summarizes new posts → sends email digest.
    """
    if not _is_authorized(update):
        return

    # Parse optional count argument: /mail 5
    count = MAX_FEED_POSTS
    if context.args:
        try:
            count = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid number. Usage: <code>/mail 5</code>",
                parse_mode="HTML",
            )
            return

        if count > MAX_FEED_POSTS:
            await update.message.reply_text(
                f"⚠️ Maximum is <b>{MAX_FEED_POSTS}</b> articles per digest.\n"
                f"Sending {MAX_FEED_POSTS} instead.",
                parse_mode="HTML",
            )
            count = MAX_FEED_POSTS
        elif count < 1:
            await update.message.reply_text("⚠️ Count must be at least 1.")
            return

    # Step 1: Acknowledge
    status_msg = await update.message.reply_text(
        f"⏳ Scanning your Medium feed for <b>{count}</b> articles...",
        parse_mode="HTML",
    )

    try:
        # Step 2: Load seen posts
        seen = load_seen(SEEN_FEED_FILE)
        log.info(f"/mail — Known feed posts: {len(seen)}")

        # Start Playwright once for the entire digest process
        async with async_playwright() as pw:
            # Step 3: Launch browser (uses HEADLESS_MODE from .env)
            context = await _create_persistent_context(pw, headless=HEADLESS_MODE)
            
            # Step 4: Scrape feed using the shared context
            all_posts = await get_feed_posts(
                max_posts=MAX_FEED_POSTS * 3, 
                existing_context=context
            )

            if not all_posts:
                await context.close()
                cleanup_chrome()
                await status_msg.edit_text("😕 No posts found in your feed. Try again later.")
                return

            # Step 5: Filter to unseen
            new_posts = [p for p in all_posts if post_key(p["url"]) not in seen]
            new_posts = new_posts[:count]

            if not new_posts:
                await context.close()
                cleanup_chrome()
                await status_msg.edit_text(
                    "✅ All feed posts were already sent in a previous digest. "
                    "No new posts to email."
                )
                return

            await status_msg.edit_text(
                f"📰 Found <b>{len(new_posts)}</b> new posts. "
                f"Fetching articles & summarizing...",
                parse_mode="HTML",
            )

            # Step 6: Fetch article text (reusing the SAME context)
            new_posts = await fetch_multiple_articles(new_posts, existing_context=context)
            
            # Close browser as soon as scraping is done
            await context.close()
            cleanup_chrome()

        # Step 7: Summarize (CPU intensive, not browser dependent)
        new_posts = summarize_all(new_posts)

        # Step 8: Send email
        ok = send_feed_digest(new_posts)

        # Step 8: Mark as seen
        for p in new_posts:
            seen.add(post_key(p["url"]))
        save_seen(SEEN_FEED_FILE, seen)

        # Step 9: Report
        if ok:
            # Build a polished summary
            member_count = sum(1 for p in new_posts if p.get("is_member_only"))
            free_count = len(new_posts) - member_count

            post_list = ""
            for i, p in enumerate(new_posts, 1):
                icon = "⭐" if p.get("is_member_only") else "📄"
                post_list += f"\n  {icon}  <b>{i}.</b> {_esc(p['title'][:48])}"
                if p.get('author') and p['author'] != 'Unknown author':
                    post_list += f"\n        <i>by {_esc(p['author'][:30])}</i>"

            await status_msg.edit_text(
                f"┌─────────────────────────────┐\n"
                f"   ✅  <b>Digest Sent!</b>\n"
                f"└─────────────────────────────┘\n\n"
                f"📬  Emailed <b>{len(new_posts)}</b> article summaries\n"
                f"      📄 {free_count} free  •  ⭐ {member_count} members-only\n\n"
                f"<b>Articles included:</b>\n"
                f"{post_list}\n\n"
                f"─────────────────────────────\n"
                f"📧 <i>Check your inbox!</i>",
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                "┌─────────────────────────────┐\n"
                "   ❌  <b>Email Failed</b>\n"
                "└─────────────────────────────┘\n\n"
                "Check your Gmail credentials in <code>.env</code>\n"
                "  • <code>GMAIL_USER</code>\n"
                "  • <code>GMAIL_APP_PASSWORD</code>",
                parse_mode="HTML",
            )

    except Exception as e:
        log.error(f"/mail failed: {e}")
        await status_msg.edit_text(
            f"┌─────────────────────────────┐\n"
            f"   ❌  <b>Error</b>\n"
            f"└─────────────────────────────┘\n\n"
            f"{_esc(str(e))}",
            parse_mode="HTML",
        )


# ── Link handler ───────────────────────────────────────────────────────────────

async def handle_medium_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles any message containing a Medium URL.
    Fetches the article, summarizes it, and replies with the summary + images + Freedium link.
    """
    if not _is_authorized(update):
        return

    text = update.message.text or ""
    urls = MEDIUM_URL_PATTERN.findall(text)

    if not urls:
        return

    url = urls[0].split("?")[0]  # take first URL, strip tracking params

    # Acknowledge
    status_msg = await update.message.reply_text(
        "⏳ Fetching and summarizing...", parse_mode="HTML"
    )

    try:
        # Fetch article (with paywall detection + Freedium fallback)
        article = await fetch_single_article(url)

        # Summarize
        article = summarize_post(article)

        # ── Build the rich response ──

        body_len = len(article.get("body", ""))
        has_summary = bool(article.get("tldr") or article.get("key_points"))

        if has_summary:
            # ── Rich structured summary ──

            title = _esc(article.get('title', 'Untitled'))
            author = _esc(article.get('author', 'Unknown'))

            # Reading time estimate
            word_count = len(article.get("body", "").split())
            read_min = max(1, word_count // 200)

            # Meta badges
            badges = f"⏱ {read_min} min read"
            if article.get("is_member_only"):
                badges += "  •  ⭐ Members-only"

            # TL;DR
            tldr = article.get("tldr", "")
            tldr_block = ""
            if tldr:
                tldr_block = (
                    f"\n\n💬  <b>TL;DR</b>\n"
                    f"<i>{_esc(tldr)}</i>"
                )

            # Key points
            key_points = article.get("key_points", [])
            points_block = ""
            if key_points:
                points_block = "\n\n🔍  <b>Key Insights</b>\n"
                for p in key_points:
                    points_block += f"\n    ▸  {_esc(p)}"

            # Takeaway
            takeaway = article.get("takeaway", "")
            takeaway_block = ""
            if takeaway:
                takeaway_block = (
                    f"\n\n💡  <b>Takeaway</b>\n"
                    f"    {_esc(takeaway)}"
                )

            # Tags
            tags = article.get("tags", [])
            tags_block = ""
            if tags:
                tag_str = "  ".join(f"<code>#{_esc(t.replace(' ', '_'))}</code>" for t in tags)
                tags_block = f"\n\n🏷  {tag_str}"

            # Links
            links_block = f'\n\n🔗  <a href="{url}">Read on Medium →</a>'
            if article.get("freedium_url"):
                links_block += f'\n🆓  <a href="{article["freedium_url"]}">Read free (no paywall) →</a>'

            # Assemble
            response = (
                f"┌─────────────────────────────┐\n"
                f"   📌  <b>{title}</b>\n"
                f"└─────────────────────────────┘\n\n"
                f"✍️  <i>{author}</i>\n"
                f"{badges}"
                f"\n─────────────────────────────"
                f"{tldr_block}"
                f"{points_block}"
                f"{takeaway_block}"
                f"{tags_block}"
                f"\n─────────────────────────────"
                f"{links_block}"
            )

        elif body_len < 100:
            # ── Extraction failed ──
            response = (
                f"┌─────────────────────────────┐\n"
                f"   ⚠️  <b>Extraction Failed</b>\n"
                f"└─────────────────────────────┘\n\n"
                f"📌  {_esc(article.get('title', 'Untitled'))}\n\n"
                f"<b>Possible causes:</b>\n"
                f"    ▸  Cloudflare blocked the page\n"
                f"    ▸  Paywall bypass service is down\n"
                f"    ▸  Session expired\n\n"
                f"<b>Fix:</b>  <code>python login.py</code>\n\n"
                f'🔗  <a href="{url}">Read on Medium →</a>'
            )
            if article.get("freedium_url"):
                response += f'\n🆓  <a href="{article["freedium_url"]}">Try free link →</a>'

        else:
            # ── Ollama failed ──
            response = (
                f"┌─────────────────────────────┐\n"
                f"   ⚠️  <b>Summarizer Offline</b>\n"
                f"└─────────────────────────────┘\n\n"
                f"📌  {_esc(article.get('title', 'Untitled'))}\n\n"
                f"<b>Check Ollama:</b>\n"
                f"    ▸  <code>ollama serve</code>\n"
                f"    ▸  <code>ollama list</code>\n\n"
                f'🔗  <a href="{url}">Read on Medium →</a>'
            )

        # Send the text summary
        await status_msg.edit_text(response, parse_mode="HTML", disable_web_page_preview=True)

        # Send article images (if any)
        images = article.get("images", [])
        if images:
            from telegram import InputMediaPhoto
            try:
                if len(images) == 1:
                    await update.message.reply_photo(
                        photo=images[0],
                        caption="📸 Article image",
                    )
                else:
                    media_group = [
                        InputMediaPhoto(media=img, caption=f"📸 Image {i+1}" if i == 0 else "")
                        for i, img in enumerate(images)
                    ]
                    await update.message.reply_media_group(media=media_group)
            except Exception as img_err:
                log.warning(f"Could not send images: {img_err}")

    except Exception as e:
        log.error(f"Link handler failed for {url}: {e}")
        await status_msg.edit_text(
            f"❌ Failed to process this article.\n\nError: {_esc(str(e))}",
            parse_mode="HTML",
        )


# ── Fallback for non-Medium messages ──────────────────────────────────────────

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds to messages that aren't commands or Medium links."""
    if not _is_authorized(update):
        return

    text = update.message.text or ""

    # Only respond if it looks like the user meant to send something
    if text.strip():
        await update.message.reply_text(
            "🤔 I can only process <b>Medium article links</b> or commands.\n\n"
            "Try:\n"
            "• Send a Medium URL to get a summary\n"
            "• Use /mail to get a feed digest email",
            parse_mode="HTML",
        )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    """Builds and runs the Telegram bot."""
    log.info("=" * 55)
    log.info("Medium Agent Bot — starting")
    log.info("=" * 55)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register handlers (order matters — more specific first)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("mail", mail_command))

    # Medium link handler — matches any message containing a Medium URL
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(MEDIUM_URL_PATTERN),
        handle_medium_link,
    ))

    # Fallback for other text messages
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_unknown,
    ))

    log.info(f"Bot is running. Listening for chat ID: {TELEGRAM_CHAT_ID}")
    log.info("Press Ctrl+C to stop.")

    # Start polling
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
