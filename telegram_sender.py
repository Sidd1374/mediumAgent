"""
telegram_sender.py — sends reading list post summaries via Telegram Bot API.

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Start a chat with your bot, then visit:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     Copy the chat_id from the response.
  3. Put both in .env as TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

Cost: Free (Telegram Bot API has no usage fees).
"""

import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, log


TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Sends a single message. Returns True on success."""
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            log.error(f"Telegram error: {data.get('description')}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def send_reading_list_post(post: dict) -> bool:
    """
    Sends a single reading-list post as a Telegram message.
    Called immediately when a new saved post is detected.

    Format:
        📌 New saved post

        <b>Title</b>
        by Author

        Summary text here.

        🏷 tag1 · tag2 · tag3
        🔗 Read →  https://...
    """
    tags_line = ""
    if post.get("tags"):
        tags_line = "\n🏷 " + " · ".join(f"#{t.replace(' ', '_')}" for t in post["tags"])

    text = (
        f"📌 <b>New saved post</b>\n\n"
        f"<b>{_esc(post['title'])}</b>\n"
        f"by {_esc(post.get('author', 'Unknown'))}\n\n"
        f"{_esc(post.get('summary', ''))}"
        f"{tags_line}\n\n"
        f"🔗 <a href=\"{post['url']}\">Read the full article →</a>"
    )

    log.info(f"Sending to Telegram: {post['title'][:60]}")
    return _send_message(text)


def send_reading_list_batch(posts: list[dict]) -> int:
    """
    Sends multiple posts one by one.
    Returns count of successfully sent messages.
    """
    if not posts:
        log.info("No new reading list posts to send via Telegram.")
        return 0

    # Opening message
    _send_message(
        f"📚 <b>You saved {len(posts)} new post{'s' if len(posts) > 1 else ''}</b> — here's the quick summary:"
    )

    import time
    sent = 0
    for post in posts:
        if send_reading_list_post(post):
            sent += 1
        time.sleep(0.5)  # avoid hitting Telegram rate limits

    log.info(f"Telegram: sent {sent}/{len(posts)} posts.")
    return sent


def _esc(text: str) -> str:
    """Escapes HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
