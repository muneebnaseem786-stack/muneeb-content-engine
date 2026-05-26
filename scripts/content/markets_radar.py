"""Markets Radar — generate up to 3 ready-to-post X market commentaries per run.

Pipeline:
  1. Load markets source list (data/markets_sources.json).
  2. Fetch recent posts from markets X accounts via Nitter profile RSS (12h lookback).
  3. Fetch RSS news from financial feeds (6h lookback).
  4. Interleave X and RSS candidates.
  5. Drop candidates already in markets_queue.json within dedup window.
  6. Drop candidates from authors on cooldown.
  7. LLM scores all candidates on (news value, angle quality, timeliness).
  8. Top 3 candidates with avg ≥ 3.5 → generate X commentary post for each.
  9. Append to data/markets_queue.json with status="auto_sent".
  10. Send 2 Telegram messages per post: context (event + source URL), then raw X text.

Compliance gate is enforced in the prompt: payments network companies are dropped
by the LLM before scoring. No hard filter in code — prompt handles it.

Usage:
  python -m scripts.content.markets_radar           # full run
  python -m scripts.content.markets_radar --dry-run # fetch + dedup only, no LLM, no Telegram
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Windows terminals default to cp1252; X posts contain emojis that crash print().
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.content.sources import (  # noqa: E402
    fetch_x_accounts,
    fetch_rss_news,
    filter_already_reacted,
    filter_recent_authors,
)


# ── Paths ─────────────────────────────────────────────────────────────────────

SOURCES_PATH = REPO_ROOT / "data" / "markets_sources.json"
QUEUE_PATH   = REPO_ROOT / "data" / "markets_queue.json"
PROMPT_PATH  = REPO_ROOT / "prompts" / "markets_radar_prompt.txt"


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _tg_chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _tg_send(text: str, parse_mode: str | None = None) -> int | None:
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
        print(f"[markets-radar] Telegram send failed [{resp.status_code}]: {resp.text}")
    except Exception as e:
        print(f"[markets-radar] Telegram send error: {e}")
    return None


def _tg_send_photo(photo_url: str, caption: str = "") -> int | None:
    """Send a photo via URL. Telegram's servers fetch the image."""
    body = {"chat_id": _tg_chat_id(), "photo": photo_url, "caption": caption}
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_tg_token()}/sendPhoto",
            json=body,
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()["result"]["message_id"]
        print(f"[markets-radar] Telegram photo send failed [{resp.status_code}]: {resp.text[:200]}")
    except Exception as e:
        print(f"[markets-radar] Telegram photo send error: {e}")
    return None


def send_markets_post_to_telegram(post: dict) -> int | None:
    """Send 2 Telegram messages per post:
      1. Context: event label + source URL (open to verify; this is an original post, not a QT)
      2. Raw X text: long-press to copy on mobile, paste into X composer
    Returns message_id of message 1 for feedback attribution.
    """
    topic      = post.get("topic", "")
    src_url    = post.get("source_url", "")
    src_label  = post.get("source", "")
    src_head   = post.get("source_headline", "")
    fmt        = post.get("format", "")
    x_text     = post.get("x_post", "")

    fmt_label = "Long-form" if fmt == "long_form" else "Single tweet"

    ctx_lines = [
        f"📊 Markets ({fmt_label})",
        f"Topic: {topic}",
        "",
        f"Source: {src_label}",
        src_head,
        src_url,
        "",
        "💡 Reply here with feedback.",
    ]
    main_id = _tg_send("\n".join(ctx_lines))

    if x_text:
        _tg_send(x_text)

    image_url = post.get("image_url")
    if image_url:
        _tg_send_photo(image_url, caption="📷 Source chart/image — attach to your post if relevant")

    return main_id


# ── LLM ───────────────────────────────────────────────────────────────────────

def _call_groq(prompt: str, max_tokens: int = 4096) -> str:
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
                    "response_format": {"type": "json_object"},
                },
                timeout=90,
            )
            if resp.status_code == 429 and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"[markets-radar] Groq 429, retrying in {wait}s (attempt {attempt + 1}/3)")
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
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.5-flash")
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = 60 * (attempt + 1)
                print(f"[markets-radar] Gemini 429, retrying in {wait}s (attempt {attempt + 1}/3)")
                time.sleep(wait)
                continue
            raise


def call_llm(prompt: str) -> str:
    """Gemini primary → Groq fallback."""
    if os.environ.get("GEMINI_API_KEY"):
        try:
            return _call_gemini(prompt)
        except Exception as e:
            print(f"[markets-radar] Gemini failed: {e}, falling back to Groq")
    return _call_groq(prompt)


def parse_json_response(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines)
    return json.loads(t)


# ── Queue helpers ─────────────────────────────────────────────────────────────

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


# ── Candidate formatting ──────────────────────────────────────────────────────

def candidates_block(candidates: list[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates):
        kind = c.get("source", "")
        if kind.startswith("x:"):
            lines.append(
                f"[{i}] X · @{c.get('handle', '')}\n"
                f"  url: {c.get('url', '')}\n"
                f"  text: {c.get('text', '')}"
            )
        else:
            lines.append(
                f"[{i}] {kind}\n"
                f"  headline: {c.get('headline', '')}\n"
                f"  url: {c.get('url', '')}\n"
                f"  summary: {c.get('summary', '')}"
            )
    return "\n\n".join(lines) if lines else "(no candidates)"


def normalize_candidate(c: dict) -> dict:
    if c.get("source", "").startswith("x:"):
        return {
            "source": c.get("source", ""),
            "source_url": c.get("url", ""),
            "source_headline": c.get("text", "")[:160],
            "source_handle": c.get("handle", ""),
            "image_url": c.get("image_url"),
        }
    return {
        "source": c.get("source", ""),
        "source_url": c.get("url", ""),
        "source_headline": c.get("headline", ""),
        "source_handle": "",
        "image_url": c.get("image_url"),
    }


# ── Pipeline ──────────────────────────────────────────────────────────────────

def load_sources() -> dict:
    with open(SOURCES_PATH) as f:
        return json.load(f)


def run(dry_run: bool = False) -> None:
    sources = load_sources()
    cfg     = sources.get("config", {})
    nitter  = cfg.get("nitter_instances", [])

    print("[markets-radar] Fetching X accounts...")
    x_candidates = fetch_x_accounts(
        sources.get("x_accounts", []),
        nitter_instances=nitter,
        max_posts_per_account=cfg.get("max_posts_per_account", 2),
        min_chars=cfg.get("min_post_chars", 60),
        lookback_hours=cfg.get("x_lookback_hours", 12),
    )
    print(f"[markets-radar] X candidates: {len(x_candidates)}")

    print("[markets-radar] Fetching RSS news...")
    rss_candidates = fetch_rss_news(
        sources.get("rss_news", []),
        lookback_hours=cfg.get("rss_lookback_hours", 6),
        max_per_feed=5,
    )
    print(f"[markets-radar] RSS candidates: {len(rss_candidates)}")

    # Interleave X and RSS
    all_candidates = []
    max_len = max(len(x_candidates), len(rss_candidates))
    for i in range(max_len):
        if i < len(x_candidates):
            all_candidates.append(x_candidates[i])
        if i < len(rss_candidates):
            all_candidates.append(rss_candidates[i])

    queue = load_queue()
    all_candidates = filter_already_reacted(
        all_candidates, queue, dedup_window_days=cfg.get("dedup_window_days", 7)
    )
    print(f"[markets-radar] After URL dedup: {len(all_candidates)}")

    all_candidates = filter_recent_authors(
        all_candidates, queue, cooldown_days=cfg.get("author_cooldown_days", 3)
    )
    print(f"[markets-radar] After author cooldown: {len(all_candidates)}")

    if not all_candidates:
        print("[markets-radar] No candidates after dedup. Exiting.")
        return

    limit = cfg.get("candidates_to_score", 40)
    all_candidates = all_candidates[:limit]

    if dry_run:
        print("[markets-radar] --dry-run -> skipping LLM + Telegram + queue write")
        print(f"[markets-radar] Would score {len(all_candidates)} candidates:")
        for i, c in enumerate(all_candidates):
            label   = c.get("handle") or c.get("source", "?")
            preview = (c.get("text") or c.get("headline") or "")[:80]
            print(f"  [{i}] {label}: {preview}")
        return

    with open(PROMPT_PATH) as f:
        template = f.read()
    prompt = template.format(
        candidate_count=len(all_candidates),
        candidates_block=candidates_block(all_candidates),
    )

    print("[markets-radar] Calling LLM for scoring + generation...")
    try:
        raw = call_llm(prompt)
    except Exception as e:
        print(f"[markets-radar] LLM error: {e}")
        _tg_send(f"🔴 Markets Radar — LLM error\n{str(e)[:300]}")
        return

    try:
        result = parse_json_response(raw)
    except json.JSONDecodeError as e:
        print(f"[markets-radar] LLM returned invalid JSON: {e}")
        print(f"[markets-radar] Raw response (first 500 chars): {raw[:500]}")
        return

    winners = result.get("winners", [])
    if not winners:
        reason = result.get("skip_reason", "no winners")
        print(f"[markets-radar] No winners this run: {reason}")
        return

    max_winners = cfg.get("max_winners_per_run", 3)
    winners = winners[:max_winners]
    print(f"[markets-radar] {len(winners)} winner(s) to send.")

    for w in winners:
        idx = w.get("candidate_index", -1)
        if idx < 0 or idx >= len(all_candidates):
            print(f"[markets-radar] Invalid candidate_index {idx}, skipping.")
            continue

        source_candidate = all_candidates[idx]
        entry = normalize_candidate(source_candidate)
        entry.update({
            "topic":            w.get("topic", ""),
            "format":           w.get("format", ""),
            "x_post":           w.get("x_post", ""),
            "soft_ban_flags":   w.get("soft_ban_flags", []),
            "self_review_pass": w.get("self_review_pass", ""),
            "scores":           result.get("scores", []),
            "generated_at":     datetime.now(timezone.utc).isoformat(),
            "status":           "auto_sent",
        })

        msg_id = send_markets_post_to_telegram(entry)
        if msg_id:
            entry["telegram_message_id"]  = msg_id
            entry["sent_to_telegram_at"]  = datetime.now(timezone.utc).isoformat()

        queue.setdefault("posts", []).insert(0, entry)

        print(f"[markets-radar] Sent: {entry.get('topic', '')} | {entry.get('format', '')} | msg_id={msg_id}")
        time.sleep(1)  # brief pause between Telegram sends

    save_queue(queue)
    print(f"[markets-radar] Done. {len(winners)} post(s) queued.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + dedup only. No LLM, no Telegram, no queue write.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
