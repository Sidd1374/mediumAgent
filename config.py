"""
config.py — shared settings and helpers for the Medium Agent bot.
Copy .env.example to .env and fill in your values before running.
"""

import os
import json
import hashlib
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Medium (session-based auth) ───────────────────────────────────────────────
# No credentials needed here — run `python login.py` to save your session.

# ── Ollama (local LLM) ────────────────────────────────────────────────────────

OLLAMA_MODEL        = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_BASE_URL     = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# ── Freedium (paywall bypass) ─────────────────────────────────────────────────

FREEDIUM_BASE_URL   = os.environ.get("FREEDIUM_BASE_URL", "https://freedium.cfd")

# ── Gmail ──────────────────────────────────────────────────────────────────────

GMAIL_USER          = os.environ["GMAIL_USER"]           # e.g. you@gmail.com
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]   # Gmail app-password (16 chars)
GMAIL_TO            = os.environ.get("GMAIL_TO", GMAIL_USER)

# ── Telegram ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

# ── Paths ──────────────────────────────────────────────────────────────────────

BASE_DIR            = Path(__file__).parent
SEEN_FEED_FILE      = BASE_DIR / "seen_feed.json"          # tracks feed posts already digested

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("medium_agent")

# ── Seen-post cache helpers ────────────────────────────────────────────────────

def load_seen(path: Path) -> set:
    if path.exists():
        return set(json.loads(path.read_text()))
    return set()

def save_seen(path: Path, seen: set):
    path.write_text(json.dumps(sorted(seen), indent=2))

def post_key(url: str) -> str:
    """Stable ID from a URL — used to deduplicate across runs."""
    return hashlib.md5(url.encode()).hexdigest()

# ── Debugging & Performance ────────────────────────────────────────────────────
# Set to 'true' in .env to enable screenshots and extra logging
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"
# Set to 'true' in .env to run Chrome in the background
HEADLESS_MODE = os.environ.get("HEADLESS_MODE", "false").lower() == "true"
