"""Reaction Radar — generate one Muneeb-voice X reaction per run.

Pipeline:
  1. Load curated source list (data/reaction_sources.json).
  2. Fetch recent posts from ~50 X accounts via Nitter profile RSS (24h lookback).
  3. Fall back to RSS news headlines if X yield is low (6h lookback).
  4. Drop candidates already in reaction_queue.json within dedup window.
  5. LLM scores all candidates 1–5 on (reframe potential, anchor, shelf life).
  6. Top candidate (avg ≥ 4.0) → generate full long-form reaction + Substack Note.
  7. Append to data/reaction_queue.json with status="auto_sent".
  8. Send 2 messages to Telegram: context (source URL + headline), then raw X text.
  9. Substack Note follows as a separate raw text message.

No buttons. No approval flow. Reply to any of the bot's messages with free-form
feedback — scripts/telegram/poller.py picks it up via attribution rule 1
(reply_to_message) and writes to data/lessons_learned.md.

Usage:
  python -m scripts.content.reaction_radar           # full run
  python -m scripts.content.reaction_radar --dry-run # fetch + score only, no LLM gen, no Telegram, no queue write
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.content.sources import (  # noqa: E402
    fetch_x_accounts,
    fetch_rss_news,
    filter_already_reacted,
    filter_recent_authors,
)


# ── Paths ────────────────────────────────────────────────────────────────────

SOURCES_PATH    = REPO_ROOT / "data" / "reaction_sources.json"
QUEUE_PATH      = REPO_ROOT / "data" / "reaction_queue.json"
LESSONS_PATH    = REPO_ROOT / "data" / "lessons_learned.md"
PROMPT_PATH     = REPO_ROOT / "prompts" / "reaction_radar_prompt.txt"


# ── Telegram (no buttons, F&R Reply Radar pattern) ───────────────────────────

def _tg_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _tg_chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _tg_send(text: str, parse_mode: str | None = None) -> int | None:
    """Send a Telegram message and return its message_id (or None on error)."""
    body = {"chat_id": _tg_chat_id(), "text": text, "disable_web_page_preview": False}
    if parse_mode:
        body["parse_mode"] = parse_mode
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_tg_token()}/sendMessage",
            json=body,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()["result"]["message_id"]
        print(f"[reaction-radar] Telegram send failed [{resp.status_code}]: {resp.text}")
    except Exception as e:
        print(f"[reaction-radar] Telegram send error: {e}")
    return None


def send_reaction_to_telegram(post: dict) -> int | None:
    """Send 2 Telegram messages:
      1. Quote-tweet prompt: source URL first, then context (open URL, click QT)
      2. Raw X long-form text (paste into QT composer)
    Returns the message_id of message 1 (used for feedback attribution).
    """
    topic = post.get("topic", "")
    src_url = post.get("source_url", "")
    src_headline = post.get("source_headline", "")
    src_label = post.get("source", "")
    x_text = post.get("x_post", "")

    # Message 1: source URL first (tap once to open, then click QT on X)
    ctx_lines = [
        "🔁 Quote-tweet this:",
        src_url,
        "",
        f"📡 {topic}",
        f"{src_label} · {src_headline}",
        "",
        "💡 Reply here with feedback to update lessons_learned.md.",
    ]
    main_id = _tg_send("\n".join(ctx_lines))

    # Message 2: raw X text — long-press to copy on mobile, paste into QT composer
    if x_text:
        _tg_send(x_text)

    return main_id


# ── LLM ──────────────────────────────────────────────────────────────────────

def _call_groq(prompt: str, max_tokens: int = 4096) -> str:
    """Call Groq Llama 3.3 70B (free tier, 1000 RPD). Raises on failure."""
    api_key = os.environ["GROQ_API_KEY"]
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                },
                timeout=90,
            )
            if resp.status_code == 429 and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"[reaction-radar] Groq 429, retrying in {wait}s (attempt {attempt + 1}/3)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(5)
                continue
    raise last_error or RuntimeError("Groq call failed")


def _call_gemini(prompt: str) -> str:
    """Call Gemini 2.0 Flash. Raises on failure."""
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.5-pro")
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = 60 * (attempt + 1)
                print(f"[reaction-radar] Gemini 429, retrying in {wait}s (attempt {attempt + 1}/3)")
                time.sleep(wait)
                continue
            raise


def call_claude_for_reaction(prompt: str) -> str:
    """Call LLM for scoring + generation. Gemini primary (better quality on these
    nuanced prompts), Groq fallback when Gemini 429s. Returns raw text response."""
    if os.environ.get("GEMINI_API_KEY"):
        try:
            return _call_gemini(prompt)
        except Exception as e:
            print(f"[reaction-radar] Gemini failed: {e}, falling back to Groq")
    return _call_groq(prompt)


def parse_json_response(text: str) -> dict:
    """LLM sometimes wraps JSON in ```json fences. Strip if present."""
    t = text.strip()
    if t.startswith("```"):
        # drop first fence line and trailing fence
        lines = t.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines)
    return json.loads(t)


# ── Pipeline ─────────────────────────────────────────────────────────────────

def load_sources() -> dict:
    with open(SOURCES_PATH) as f:
        return json.load(f)


def load_lessons() -> str:
    try:
        with open(LESSONS_PATH) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def load_queue() -> dict:
    try:
        with open(QUEUE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"posts": [], "last_updated": ""}


def save_queue(queue: dict) -> None:
    queue["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)


def candidates_block(candidates: list[dict]) -> str:
    """Render candidates for the LLM prompt."""
    lines = []
    for i, c in enumerate(candidates):
        kind = c.get("source", "")
        if kind.startswith("x:"):
            lines.append(
                f"[{i}] X · @{c.get('handle','')}\n"
                f"  url: {c.get('url','')}\n"
                f"  text: {c.get('text','')}"
            )
        else:
            lines.append(
                f"[{i}] {kind}\n"
                f"  headline: {c.get('headline','')}\n"
                f"  url: {c.get('url','')}\n"
                f"  summary: {c.get('summary','')}"
            )
    return "\n\n".join(lines) if lines else "(no candidates)"


def normalize_candidate_for_queue(c: dict, llm_topic: str) -> dict:
    """Turn a raw X-post or news-headline candidate into the queue schema."""
    if c.get("source", "").startswith("x:"):
        return {
            "topic": llm_topic,
            "source": c.get("source", ""),
            "source_url": c.get("url", ""),
            "source_headline": c.get("text", "")[:160],
            "source_handle": c.get("handle", ""),
        }
    return {
        "topic": llm_topic,
        "source": c.get("source", ""),
        "source_url": c.get("url", ""),
        "source_headline": c.get("headline", ""),
        "source_handle": "",
    }


def run(dry_run: bool = False) -> None:
    sources = load_sources()
    cfg = sources.get("config", {})
    nitter = cfg.get("nitter_instances", [])

    print("[reaction-radar] Fetching X accounts...")
    x_candidates = fetch_x_accounts(
        sources.get("x_accounts", []),
        nitter_instances=nitter,
        max_posts_per_account=cfg.get("max_posts_per_account", 2),
        min_chars=cfg.get("min_post_chars", 80),
        lookback_hours=cfg.get("x_lookback_hours", 24),
    )
    print(f"[reaction-radar] X candidates: {len(x_candidates)}")

    print("[reaction-radar] Fetching RSS news (parallel signal, not fallback)...")
    rss_candidates = fetch_rss_news(
        sources.get("rss_news", []),
        lookback_hours=cfg.get("rss_lookback_hours", 6),
        max_per_feed=5,
    )
    print(f"[reaction-radar] RSS candidates: {len(rss_candidates)}")

    # Interleave X and RSS so the candidate slice (capped at candidates_to_score)
    # reflects both signal layers, not just whichever came first.
    all_candidates = []
    max_len = max(len(x_candidates), len(rss_candidates))
    for i in range(max_len):
        if i < len(x_candidates):
            all_candidates.append(x_candidates[i])
        if i < len(rss_candidates):
            all_candidates.append(rss_candidates[i])

    queue = load_queue()
    all_candidates = filter_already_reacted(
        all_candidates, queue, dedup_window_days=cfg.get("dedup_window_days", 14)
    )
    print(f"[reaction-radar] After URL dedup: {len(all_candidates)}")

    all_candidates = filter_recent_authors(
        all_candidates, queue, cooldown_days=cfg.get("author_cooldown_days", 5)
    )
    print(f"[reaction-radar] After author cooldown: {len(all_candidates)}")

    if not all_candidates:
        print("[reaction-radar] No candidates after dedup. Exiting.")
        return

    # Cap candidates passed to LLM (cost control)
    limit = cfg.get("candidates_to_score", 25)
    all_candidates = all_candidates[:limit]

    if dry_run:
        print("[reaction-radar] --dry-run → skipping LLM + Telegram + queue write")
        print(f"[reaction-radar] Would score {len(all_candidates)} candidates:")
        for i, c in enumerate(all_candidates):
            label = c.get("handle") or c.get("source", "?")
            preview = (c.get("text") or c.get("headline") or "")[:80]
            print(f"  [{i}] @{label}: {preview}")
        return

    # Build prompt
    with open(PROMPT_PATH) as f:
        template = f.read()
    prompt = template.format(
        lessons_learned=load_lessons() or "(no lessons yet)",
        candidate_count=len(all_candidates),
        candidates_block=candidates_block(all_candidates),
    )

    print("[reaction-radar] Calling Claude for scoring + generation...")
    try:
        raw = call_claude_for_reaction(prompt)
    except Exception as e:
        print(f"[reaction-radar] LLM error: {e}")
        _tg_send(f"🔴 Reaction Radar — LLM error\n{str(e)[:300]}")
        return
    try:
        result = parse_json_response(raw)
    except json.JSONDecodeError as e:
        print(f"[reaction-radar] LLM returned invalid JSON: {e}")
        print(f"[reaction-radar] Raw response (first 500 chars): {raw[:500]}")
        return

    winner_idx = result.get("winning_index", -1)
    if winner_idx < 0 or winner_idx >= len(all_candidates):
        reason = result.get("skip_reason", "no winner")
        print(f"[reaction-radar] Skipping run: {reason}")
        return

    winner = all_candidates[winner_idx]
    queue_entry = normalize_candidate_for_queue(winner, result.get("topic", ""))
    queue_entry.update({
        "x_post": result.get("x_post", ""),
        "soft_ban_flags": result.get("soft_ban_flags", []),
        "self_review_pass": result.get("self_review_pass", ""),
        "scores": result.get("scores", []),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "auto_sent",
    })

    msg_id = send_reaction_to_telegram(queue_entry)
    if msg_id:
        queue_entry["telegram_message_id"] = msg_id
        queue_entry["sent_to_telegram_at"] = datetime.now(timezone.utc).isoformat()

    queue.setdefault("posts", []).insert(0, queue_entry)
    save_queue(queue)

    print(f"[reaction-radar] Done. Topic: {queue_entry.get('topic','')}")
    print(f"[reaction-radar] Source: {queue_entry.get('source_url','')}")
    print(f"[reaction-radar] Telegram message_id: {msg_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + dedup only. No LLM, no Telegram, no queue write.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
