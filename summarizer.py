"""
summarizer.py — uses a local Ollama model to summarize articles.

Runs entirely on your machine via Ollama (https://ollama.com).
No API keys, no cloud calls, no cost.

Default model: phi3.5 (configurable via OLLAMA_MODEL in .env)
"""

import json
import re
import requests
from config import log, OLLAMA_BASE_URL, OLLAMA_MODEL


SYSTEM_PROMPT = """You are a sharp reading assistant. Given the text of an article,
produce a rich, structured summary in exactly this JSON format (no markdown fences, raw JSON only):

{
  "tldr": "One punchy sentence that captures the core idea.",
  "key_points": [
    "First important insight or fact from the article",
    "Second important insight or fact",
    "Third important insight or fact",
    "Fourth important insight (if applicable)"
  ],
  "takeaway": "The single most actionable or memorable thing the reader should remember.",
  "tags": ["tag1", "tag2", "tag3"]
}

Rules:
- key_points should be 3-5 bullet points, each one a complete, informative sentence.
- Extract specific facts, numbers, quotes, or actionable advice — not vague descriptions.
- tldr should be bold and direct, like a tweet — one sentence max.
- takeaway is the "so what?" — why should someone care?
- Tags should be lowercase topic keywords.
- IMPORTANT: Return ONLY the raw JSON object. No explanation, no markdown, no extra text."""


def _check_ollama_running() -> bool:
    """Verify that Ollama is running and the model is available."""
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        return resp.status_code == 200
    except requests.ConnectionError:
        return False


def _extract_json(raw: str) -> dict:
    """Extracts JSON from model output, handling markdown fences and extra text."""
    # Try to extract from code fences first
    if "```" in raw:
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if json_match:
            raw = json_match.group(1).strip()

    # Try to find JSON object in the text
    brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace_match:
        raw = brace_match.group(0)

    return json.loads(raw)


def summarize_post(post: dict) -> dict:
    """
    Takes a post dict with at least { title, url, author, body }.
    Returns the same dict with structured summary keys added:
      - tldr: one-line summary
      - key_points: list of bullet points
      - takeaway: actionable insight
      - tags: topic tags
      - summary: formatted plain text (for email and fallback)
    Falls back gracefully if Ollama is unavailable.
    """
    body = post.get("body", "").strip()

    if not body or len(body) < 100:
        post["tldr"] = ""
        post["key_points"] = []
        post["takeaway"] = ""
        post["summary"] = post.get("description", "No content could be extracted for this post.")
        post["tags"] = []
        return post

    user_message = (
        f"Title: {post['title']}\n"
        f"Author: {post['author']}\n\n"
        f"Article text:\n{body[:3500]}"
    )

    try:
        if not _check_ollama_running():
            raise ConnectionError(
                "Ollama is not running. Start it with: ollama serve"
            )

        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": user_message,
                "system": SYSTEM_PROMPT,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 600,  # more tokens for richer output
                },
            },
            timeout=180,  # local models can be slower
        )
        response.raise_for_status()

        raw = response.json().get("response", "").strip()
        parsed = _extract_json(raw)

        post["tldr"] = parsed.get("tldr", "")
        post["key_points"] = parsed.get("key_points", [])
        post["takeaway"] = parsed.get("takeaway", "")
        post["tags"] = parsed.get("tags", [])

        # Build a plain-text summary as fallback (for email, etc.)
        points_text = "\n".join(f"• {p}" for p in post["key_points"])
        post["summary"] = f"{post['tldr']}\n\n{points_text}\n\n💡 {post['takeaway']}"

    except json.JSONDecodeError:
        log.warning(f"Model returned non-JSON for '{post['title']}' — using raw text.")
        post["tldr"] = ""
        post["key_points"] = []
        post["takeaway"] = ""
        post["summary"] = raw[:500] if raw else post.get("description", "Summary unavailable.")
        post["tags"] = []

    except Exception as e:
        log.warning(f"Summarization failed for '{post['title']}': {e}")
        post["tldr"] = ""
        post["key_points"] = []
        post["takeaway"] = ""
        post["summary"] = post.get("description", "Summary unavailable.")
        post["tags"] = []

    return post


def summarize_all(posts: list[dict]) -> list[dict]:
    """Summarizes a list of posts one by one (sequential for local model)."""
    results = []
    for i, post in enumerate(posts, 1):
        log.info(f"Summarizing {i}/{len(posts)}: {post['title'][:60]}")
        results.append(summarize_post(post))
    return results
