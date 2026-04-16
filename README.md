# 📰 Medium Agent

**A Telegram bot that summarizes Medium articles on demand and delivers daily digests to your email — fully local, fully free.**

Send it any Medium link → get an instant AI summary. Members-only article? No problem — it reads it through [Freedium](https://freedium.cfd) and gives you the free link too.

No cloud APIs. No subscription. No data leaves your machine.

---

## What It Does

| Feature | How to Use | What Happens |
|---------|-----------|--------------|
| **Article Summary** | Send any Medium URL to the bot | Bot fetches article → detects paywall → uses Freedium if needed → summarizes → replies with summary + free link |
| **Email Digest** | Send `/mail` command | Bot scrapes your Medium feed → picks 10 new posts → summarizes all → sends HTML newsletter to your Gmail |

---

## How It Works

```
┌───────────────────────────────────────────────────────────────────────────┐
│                           YOUR PC (fully local)                          │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────┐        │
│  │                    Telegram Bot (bot.py)                      │        │
│  │                                                               │        │
│  │  📎 Medium Link received          📧 /mail command received   │        │
│  │       │                                  │                    │        │
│  │       ▼                                  ▼                    │        │
│  │  ┌──────────┐                    ┌──────────────┐             │        │
│  │  │ Scraper  │                    │   Scraper    │             │        │
│  │  │ (single) │                    │ (feed scan)  │             │        │
│  │  └────┬─────┘                    └──────┬───────┘             │        │
│  │       │                                  │                    │        │
│  │       ▼                                  ▼                    │        │
│  │  Paywall? ──Yes──▶ Freedium     Filter seen posts             │        │
│  │     │                  │         Pick top 10 new              │        │
│  │     No                 │              │                       │        │
│  │     │                  │              ▼                       │        │
│  │     ▼                  ▼         Paywall? → Freedium          │        │
│  │  ┌──────────────────────┐             │                       │        │
│  │  │  Ollama Summarizer   │◀────────────┘                       │        │
│  │  │  (local LLM)         │                                     │        │
│  │  └──────────┬───────────┘                                     │        │
│  │             │                                                  │        │
│  │       ┌─────┴─────┐                                           │        │
│  │       ▼           ▼                                           │        │
│  │  Reply on     Send Gmail                                     │        │
│  │  Telegram     Newsletter                                     │        │
│  └──────────────────────────────────────────────────────────────┘        │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────┐        │
│  │         Dedup Cache (seen_feed.json) — prevents re-emailing  │        │
│  └──────────────────────────────────────────────────────────────┘        │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message and usage instructions |
| `/mail` | Scrape Medium feed → summarize 10 new posts → email digest |

### Sending a Link

Just paste any Medium article URL into the chat. The bot will:

```
You:   https://medium.com/@author/some-article-abc123

Bot:   📌 Article Summary

       How I Built a Side Project That Pays My Rent
       by Sarah Chen
       🔓 Members-only — free link below

       This article covers the author's journey building a SaaS
       product from scratch. Key takeaways include focusing on a
       niche market and the importance of early user feedback.

       🏷 #SaaS · #indie_hacking · #startup

       🔗 Read on Medium → https://medium.com/...
       🆓 Read free (Freedium) → https://freedium.cfd/...
```

### The `/mail` Flow

```
You:   /mail

Bot:   ⏳ Scanning your Medium feed...
Bot:   📰 Found 10 new posts. Fetching articles & summarizing...
Bot:   ✅ Digest sent to your email!

       📬 10 posts summarized:
         1. How I Built a Side Project ⭐
         2. Understanding Transformers
         3. The Future of Web Assembly ⭐
         ...
```

---

## Workflow — Article Link

```
User sends Medium URL
  │
  ▼
Bot validates it's a Medium link
  │
  ▼
Open headless browser → Login to Medium
  │
  ▼
Navigate to article → Extract title & author
  │
  ▼
Check: is it members-only?
  │
  ├──Yes──▶ Fetch full text from Freedium
  │              (https://freedium.cfd/<url>)
  │
  └──No───▶ Extract text directly from page
  │
  ▼
Summarize with Ollama (local LLM)
  │   Returns: { summary, tags }
  ▼
Reply to user:
  • Summary + tags
  • Medium link
  • Freedium link (if members-only)
```

## Workflow — `/mail` Command

```
User sends /mail
  │
  ▼
Load seen_feed.json (already-emailed posts)
  │
  ▼
Open headless browser → Login to Medium
  │
  ▼
Scrape home feed (up to 30 posts)
  │
  ▼
Filter out already-seen → pick top 10 new
  │
  ▼
No new posts? → Reply "all caught up!" → Exit
  │
  ▼
For each post:
  ├─ Check if members-only
  ├─ If yes → fetch from Freedium
  └─ If no  → fetch directly
  │
  ▼
Summarize all with Ollama
  │
  ▼
Build HTML newsletter email
  │   (includes Freedium links for paywalled posts)
  ▼
Send via Gmail SMTP
  │
  ▼
Update seen_feed.json
  │
  ▼
Reply: "✅ Digest sent!"
```

---

## Project Structure

```
mediumAgent/
├── bot.py                  # ⭐ Main entry — Telegram bot (long-running)
├── login.py                # One-time manual login → saves session.json
├── config.py               # Shared settings, env loading, helpers
├── scraper.py              # Playwright scraper + Freedium integration
├── summarizer.py           # Local Ollama summarization engine
├── gmail_sender.py         # HTML email builder + Gmail SMTP sender
├── telegram_sender.py      # Low-level Telegram message formatting helpers
├── requirements.txt        # Python dependencies
├── env.example             # Environment variable template
├── SETUP.md                # ⬅ Step-by-step setup instructions
└── README.md               # This file
```

---

## Module Breakdown

| Module | Role | Input | Output |
|--------|------|-------|--------|
| `bot.py` | Telegram bot — link handler + `/mail` | User messages | Summaries on Telegram + email |
| `login.py` | One-time manual Medium login | Browser interaction | `session.json` (saved cookies) |
| `config.py` | Central configuration | `.env` file | Settings, paths, logger, helpers |
| `scraper.py` | Web scraper + Freedium bypass | Medium URL + `session.json` | `{ title, url, author, body, is_member_only, freedium_url }` |
| `summarizer.py` | AI summarizer (local Ollama) | Post with `body` | Post + `{ summary, tags }` |
| `gmail_sender.py` | Email sender | List of summarized posts | HTML newsletter with Freedium links |
| `telegram_sender.py` | Message formatting helpers | Post data | Formatted Telegram message |

---

## Technology Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Language | Python 3.10+ | Simple, rich ecosystem |
| Bot Framework | python-telegram-bot v21+ | Modern async Telegram bot library |
| Web Scraping | Playwright (headless Chromium) | Handles JS-heavy Medium pages |
| Paywall Bypass | Freedium (`freedium.cfd`) | Reads members-only articles for free |
| LLM Engine | Ollama (local) | Free, private, no API key needed |
| Default Model | `phi3.5` | Fast, 2 GB, great at structured JSON output |
| Email | Gmail SMTP (TLS) | Free, reliable |
| Deduplication | JSON file cache | Simple, no database needed |

---

## Cost Breakdown

| Item | Cost |
|------|:----:|
| Ollama (local LLM) | **Free** |
| Telegram Bot API | **Free** |
| Freedium | **Free** |
| Gmail SMTP (up to 500/day) | **Free** |
| Playwright / Python | **Free** |
| **Total** | **$0 / month** |

---

## Environment Variables

| Variable | Required | Description |
|----------|:--------:|-------------|
| `OLLAMA_MODEL` | ❌ | Ollama model name (default: `phi3.5`) |
| `OLLAMA_BASE_URL` | ❌ | Ollama API URL (default: `http://localhost:11434`) |
| `FREEDIUM_BASE_URL` | ❌ | Freedium URL (default: `https://freedium.cfd`) |
| `GMAIL_USER` | ✅ | Your Gmail address |
| `GMAIL_APP_PASSWORD` | ✅ | Gmail app password (16 chars) |
| `GMAIL_TO` | ❌ | Recipient email (defaults to `GMAIL_USER`) |
| `TELEGRAM_BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your Telegram chat ID |

> **Note:** Medium login is handled via `python login.py` (browser-based, no credentials in `.env`).

---

## Security

The bot **only responds to messages from your `TELEGRAM_CHAT_ID`**. All other users are silently ignored. No passwords are stored — Medium login uses a saved browser session (`session.json`). Your article data never leaves your machine.

---

## Getting Started

👉 **Follow the complete setup guide:** [**SETUP.md**](./SETUP.md)

The setup guide covers:
- Installing Python and Ollama
- Pulling the right LLM model for your hardware
- Configuring all credentials
- Running the bot
- Keeping it running in the background

---

## License

This project is for personal use. Feel free to fork and modify.
