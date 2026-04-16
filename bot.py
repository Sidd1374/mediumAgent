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
)
from scraper import fetch_single_article, get_feed_posts, fetch_multiple_articles
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
        "👋 <b>Welcome to Medium Agent!</b>\n\n"
        "Here's what I can do:\n\n"
        "📌 <b>Send me any Medium link</b>\n"
        "   → I'll summarize the article and give you a free link "
        "if it's members-only.\n\n"
        "📧 <b>/mail</b>\n"
        "   → I'll scan your Medium feed, summarize the top 10 new posts, "
        "and send them to your email as a newsletter.\n\n"
        "🤖 All summaries are generated <b>locally</b> using Ollama — "
        "your data never leaves your PC."
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

        # Step 3: Scrape feed
        all_posts = await get_feed_posts(max_posts=MAX_FEED_POSTS * 3)

        if not all_posts:
            await status_msg.edit_text("😕 No posts found in your feed. Try again later.")
            return

        # Step 4: Filter to unseen
        new_posts = [p for p in all_posts if post_key(p["url"]) not in seen]
        new_posts = new_posts[:count]

        if not new_posts:
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

        # Step 5: Fetch article text (with Freedium for paywalled ones)
        new_posts = await fetch_multiple_articles(new_posts)

        # Step 6: Summarize
        new_posts = summarize_all(new_posts)

        # Step 7: Send email
        ok = send_feed_digest(new_posts)

        # Step 8: Mark as seen
        for p in new_posts:
            seen.add(post_key(p["url"]))
        save_seen(SEEN_FEED_FILE, seen)

        # Step 9: Report
        if ok:
            # Build a quick summary of what was sent
            post_list = ""
            for i, p in enumerate(new_posts, 1):
                member_tag = " ⭐" if p.get("is_member_only") else ""
                post_list += f"  {i}. {_esc(p['title'][:50])}{member_tag}\n"

            await status_msg.edit_text(
                f"✅ <b>Digest sent to your email!</b>\n\n"
                f"📬 {len(new_posts)} posts summarized:\n"
                f"{post_list}",
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                "❌ Email send failed — check your Gmail credentials in .env"
            )

    except Exception as e:
        log.error(f"/mail failed: {e}")
        await status_msg.edit_text(f"❌ Error: {_esc(str(e))}", parse_mode="HTML")


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

            # Header
            member_badge = " ⭐" if article.get("is_member_only") else ""
            header = (
                f"📌 <b>{_esc(article.get('title', 'Untitled'))}</b>{member_badge}\n"
                f"✍️ <i>{_esc(article.get('author', 'Unknown'))}</i>"
            )

            # Reading time estimate
            word_count = len(article.get("body", "").split())
            read_min = max(1, word_count // 200)

            meta_line = f"\n⏱ {read_min} min read"
            if article.get("is_member_only"):
                meta_line += "  •  🔒 Members-only"

            # TL;DR
            tldr = article.get("tldr", "")
            tldr_section = f"\n\n💬 <b>TL;DR</b>\n{_esc(tldr)}" if tldr else ""

            # Key points
            key_points = article.get("key_points", [])
            if key_points:
                points = "\n".join(f"  → {_esc(p)}" for p in key_points)
                points_section = f"\n\n🔍 <b>Key Insights</b>\n{points}"
            else:
                points_section = ""

            # Takeaway
            takeaway = article.get("takeaway", "")
            takeaway_section = f"\n\n💡 <b>Takeaway</b>\n{_esc(takeaway)}" if takeaway else ""

            # Tags
            tags = article.get("tags", [])
            tags_line = ""
            if tags:
                tags_line = "\n\n🏷 " + " · ".join(
                    f"#{_esc(t.replace(' ', '_'))}" for t in tags
                )

            # Links
            links_section = f'\n\n🔗 <a href="{url}">Read on Medium →</a>'
            if article.get("freedium_url"):
                links_section += f'\n🆓 <a href="{article["freedium_url"]}">Read free (Freedium) →</a>'

            # Separator
            response = (
                f"{header}"
                f"{meta_line}"
                f"\n{'─' * 28}"
                f"{tldr_section}"
                f"{points_section}"
                f"{takeaway_section}"
                f"{tags_line}"
                f"\n{'─' * 28}"
                f"{links_section}"
            )

        elif body_len < 100:
            # ── Extraction failed ──
            response = (
                f"📌 <b>{_esc(article.get('title', 'Untitled'))}</b>\n"
                f"by {_esc(article.get('author', 'Unknown'))}\n\n"
                "⚠️ Could not extract article text.\n\n"
                "<i>Possible causes:</i>\n"
                "• Cloudflare blocked the page\n"
                "• Freedium is down\n"
                "• Session expired — run <code>python login.py</code>\n\n"
                f'🔗 <a href="{url}">Read on Medium →</a>'
            )
            if article.get("freedium_url"):
                response += f'\n🆓 <a href="{article["freedium_url"]}">Try Freedium →</a>'
        else:
            # ── Ollama failed ──
            response = (
                f"📌 <b>{_esc(article.get('title', 'Untitled'))}</b>\n"
                f"by {_esc(article.get('author', 'Unknown'))}\n\n"
                "⚠️ Could not generate summary.\n\n"
                "<i>Check that Ollama is running:</i>\n"
                "<code>ollama serve</code>\n"
                "<code>ollama list</code>  (should show your model)\n\n"
                f'🔗 <a href="{url}">Read on Medium →</a>'
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
