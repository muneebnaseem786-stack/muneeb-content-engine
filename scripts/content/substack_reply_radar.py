"""Substack Reply Radar for @MuneebNaseem

Monitors curated Substack publications (articles via RSS, Notes via unofficial
API where available). Scores candidates, then generates either:
  RESTACK — 1-2 sentence framing comment to add when restacking a Note/article
  REPLY   — 150-250 word comment in Muneeb's conversational Substack voice

Voice: first-person, analytical, conversational. NOT Akash X style.

Pipeline:
  1. Load publication list from data/substack_reply_sources.json
  2. Fetch recent articles via RSS (48h lookback)
  3. Attempt to fetch recent Notes via unofficial Substack API (24h lookback, best-effort)
  4. Dedup against data/substack_reply_queue.json (14-day URL window)
  5. Apply per-publication cooldown (data/.substack_reply_cooldown.json, 5-day default)
  6. Score up to 20 candidates via LLM (reframe, anchor, shelf life — avg ≥ 4.0 to generate)
  7. Generate RESTACK comment or REPLY text for the winning candidate
  8. Send 2 Telegram messages (context + content)
  9. Append to data/substack_reply_queue.json, update cooldown file

Usage:
  python -m scripts.content.substack_reply_radar             # full run
  python -m scripts.content.substack_reply_radar --dry-run   # fetch + score only
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import feedparser

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ── Paths ─────────────────────────────────────────────────────────────────────

SOURCES_PATH  = REPO_ROOT / "data" / "substack_reply_sources.json"
QUEUE_PATH    = REPO_ROOT / "data" / "substack_reply_queue.json"
COOLDOWN_PATH = REPO_ROOT / "data" / ".substack_reply_cooldown.json"
LESSONS_PATH  = REPO_ROOT / "data" / "lessons_learned.md"
PROMPT_PATH   = REPO_ROOT / "prompts" / "substack_reply_radar_prompt.txt"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_lessons() -> str:
    if not LESSONS_PATH.exists():
        return "(no lessons yet)"
    try:
        return LESSONS_PATH.read_text(encoding="utf-8")
    except Exception:
        return "(no lessons yet)"


def _parse_date(entry) -> datetime | None:
    """Try multiple feedparser date fields, return UTC datetime or None."""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


# ── Cooldown ──────────────────────────────────────────────────────────────────

def load_cooldown(cooldown_days: int) -> dict[str, str]:
    """Return {publication_handle_lower: ISO timestamp} for handles still in cooldown."""
    raw = _load_json(COOLDOWN_PATH, {})
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    return {
        k.lower(): v for k, v in raw.items()
        if _parse_iso(v) and _parse_iso(v) >= cutoff
    }


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def save_cooldown(cooldown: dict[str, str], new_handle: str):
    cooldown[new_handle.lower()] = datetime.now(timezone.utc).isoformat()
    _save_json(COOLDOWN_PATH, cooldown)


# ── Dedup ─────────────────────────────────────────────────────────────────────

def load_seen_urls(dedup_days: int) -> set[str]:
    queue = _load_json(QUEUE_PATH, {"items": []})
    cutoff = datetime.now(timezone.utc) - timedelta(days=dedup_days)
    seen = set()
    for item in queue.get("items", []):
        ts = _parse_iso(item.get("generated_at", ""))
        if ts and ts >= cutoff:
            url = item.get("content_url", "")
            if url:
                seen.add(url)
    return seen


# ── Article fetching (RSS) ────────────────────────────────────────────────────

def fetch_articles(pub: dict, lookback_hours: int, min_chars: int) -> list[dict]:
    """Fetch recent articles from a publication's RSS feed."""
    feed_url = pub.get("feed_url", "")
    if not feed_url:
        return []
    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        print(f"  [substack-radar] RSS fetch failed for {pub['name']}: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    results = []
    for entry in feed.entries[:10]:
        pub_date = _parse_date(entry)
        if pub_date and pub_date < cutoff:
            continue
        title = getattr(entry, "title", "") or ""
        link  = getattr(entry, "link", "") or ""
        # Try to get a summary/excerpt
        summary = ""
        for field in ("summary", "description", "content"):
            raw = getattr(entry, field, None)
            if raw:
                if isinstance(raw, list):
                    raw = raw[0].get("value", "") if raw else ""
                # Strip basic HTML tags
                import re
                summary = re.sub(r"<[^>]+>", " ", str(raw)).strip()
                summary = re.sub(r"\s+", " ", summary)[:600]
                break

        text = f"{title}. {summary}".strip()
        if len(text) < min_chars:
            continue

        results.append({
            "publication_name":   pub["name"],
            "publication_handle": pub["handle"],
            "content_type":       "article",
            "content_title":      title,
            "content_url":        link,
            "content_excerpt":    summary[:400],
            "text_for_scoring":   text[:800],
            "published_at":       pub_date.isoformat() if pub_date else "",
            "pillars":            pub.get("pillars", []),
        })
    return results


# ── Notes fetching (unofficial API, best-effort) ──────────────────────────────

def fetch_notes(pub: dict, lookback_hours: int, min_chars: int) -> list[dict]:
    """Attempt to fetch recent Notes via Substack's unofficial API.
    Returns empty list on any failure — notes support is best-effort."""
    handle = pub.get("handle", "")
    if not handle:
        return []

    url = f"https://{handle}.substack.com/api/v1/notes?limit=10&order=new"
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    # Notes API returns different structures depending on the Substack version.
    # Try to handle the most common shapes.
    items = data if isinstance(data, list) else data.get("notes", data.get("items", []))
    if not isinstance(items, list):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    results = []
    for note in items[:10]:
        # Extract fields — structure varies
        body    = note.get("body", "") or note.get("text", "") or note.get("content", "")
        date_s  = note.get("date", "") or note.get("created_at", "") or note.get("timestamp", "")
        note_id = note.get("id", "")
        handle_v = note.get("handle", "") or handle

        if date_s:
            pub_date = _parse_iso(str(date_s))
            if pub_date and pub_date < cutoff:
                continue
        else:
            pub_date = None

        if isinstance(body, dict):
            body = body.get("text", "") or str(body)
        body = str(body).strip()

        if len(body) < min_chars:
            continue

        note_url = f"https://{handle_v}.substack.com/note/{note_id}" if note_id else ""

        results.append({
            "publication_name":   pub["name"],
            "publication_handle": pub["handle"],
            "content_type":       "note",
            "content_title":      body[:80] + "..." if len(body) > 80 else body,
            "content_url":        note_url,
            "content_excerpt":    body[:400],
            "text_for_scoring":   body[:800],
            "published_at":       pub_date.isoformat() if pub_date else "",
            "pillars":            pub.get("pillars", []),
        })
    return results


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _tg_chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_send(text: str, reply_markup: dict | None = None, parse_mode: str = "") -> int | None:
    body = {
        "chat_id": _tg_chat_id(),
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_tg_token()}/sendMessage",
            json=body,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()["result"]["message_id"]
        print(f"[substack-radar] Telegram send failed [{resp.status_code}]: {resp.text}")
    except Exception as e:
        print(f"[substack-radar] Telegram error: {e}")
    return None


def send_suggestion_to_telegram(result: dict, idx: int, total: int) -> int | None:
    """Send 2 Telegram messages per suggestion:
      1. Context: publication, title as clickable link, excerpt, action type
      2. Raw content: restack comment or reply text (long-press to copy) + direct link button
    Returns message_id of message 1."""
    action      = result.get("action", "REPLY")
    pub_name    = result.get("publication_name", "")
    title       = result.get("content_title", "")
    excerpt     = result.get("content_excerpt", "")[:300]
    url         = result.get("content_url", "")
    c_type      = result.get("content_type", "article")
    action_icon = "🔁" if action == "RESTACK" else "💬"
    type_icon   = "📝" if c_type == "article" else "📌"

    # Title as a clickable hyperlink to the specific article/note
    title_link = f'<a href="{url}">{_esc(title)}</a>' if url else _esc(title)

    ctx = (
        f"{action_icon} <b>Substack {action} {idx}/{total}</b> — {_esc(pub_name)}\n\n"
        f"{type_icon} {title_link}\n\n"
        f"<i>{_esc(excerpt)}</i>\n\n"
        f"Reply with feedback to train the radar."
    )
    ctx_id = _tg_send(ctx, parse_mode="HTML")

    # Message 2: raw content (long-press to copy) + button linking directly to the article/note
    if action == "RESTACK":
        content_text = result.get("restack_comment", "")
        label = "🔁 <b>Restack comment</b> (add when you restack):"
    else:
        content_text = result.get("reply_text", "")
        label = "💬 <b>Reply</b> (paste into Substack comments):"

    btn_label = f"{'📝 Read article' if c_type == 'article' else '📌 Read note'} → {_esc(pub_name)}"
    open_btn = {"inline_keyboard": [[{"text": btn_label, "url": url}]]} if url else None
    _tg_send(f"{label}\n\n{_esc(content_text)}", reply_markup=open_btn, parse_mode="HTML")

    return ctx_id


# ── LLM ───────────────────────────────────────────────────────────────────────

def call_claude(prompt: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed.") from e
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def build_candidates_block(candidates: list[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates):
        lines.append(
            f"[{i}] {c['publication_name']} ({c['content_type']}) | {c['content_title']}\n"
            f"    Pillars: {', '.join(c.get('pillars', []))}\n"
            f"    URL: {c['content_url']}\n"
            f"    Text: {c['text_for_scoring'][:500]}\n"
        )
    return "\n".join(lines)


def run_llm(candidates: list[dict], lessons: str) -> dict:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        lessons_learned=lessons,
        candidate_count=len(candidates),
        candidates_block=build_candidates_block(candidates),
    )
    raw = call_claude(prompt)
    # Strip any accidental markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    return json.loads(raw)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    sources_data = _load_json(SOURCES_PATH, {})
    publications = sources_data.get("publications", [])
    cfg          = sources_data.get("config", {})

    article_lookback = cfg.get("article_lookback_hours", 48)
    note_lookback    = cfg.get("note_lookback_hours", 24)
    min_chars        = cfg.get("min_text_chars", 100)
    candidates_cap   = cfg.get("candidates_to_score", 20)
    score_threshold  = cfg.get("score_threshold", 4.0)
    dedup_days       = cfg.get("dedup_window_days", 14)
    cooldown_days    = cfg.get("publication_cooldown_days", 5)
    suggestions_cap  = cfg.get("suggestions_per_run", 2)

    print(f"[substack-radar] {len(publications)} publications in source list")

    seen_urls = load_seen_urls(dedup_days)
    cooldown  = load_cooldown(cooldown_days)
    print(f"[substack-radar] {len(seen_urls)} URLs deduped | {len(cooldown)} pubs in cooldown")

    # Fetch candidates from all publications not in cooldown
    all_candidates: list[dict] = []
    for pub in publications:
        handle = pub.get("handle", "").lower()
        if handle in cooldown:
            print(f"  [substack-radar] Skip (cooldown): {pub['name']}")
            continue

        articles = fetch_articles(pub, article_lookback, min_chars)
        notes    = fetch_notes(pub, note_lookback, min_chars)
        combined = articles + notes

        # Dedup by URL
        fresh = [c for c in combined if c.get("content_url") not in seen_urls and c.get("content_url")]
        if fresh:
            print(f"  [substack-radar] {pub['name']}: {len(fresh)} fresh items ({len(articles)} articles, {len(notes)} notes)")
            all_candidates.extend(fresh)
        else:
            print(f"  [substack-radar] {pub['name']}: nothing fresh")

        time.sleep(0.3)  # gentle rate limiting

    if not all_candidates:
        print("[substack-radar] No candidates after dedup + cooldown. Exiting.")
        return

    # Cap for LLM cost control
    candidates = all_candidates[:candidates_cap]
    print(f"[substack-radar] {len(all_candidates)} total candidates → capped to {len(candidates)} for scoring")

    if dry_run:
        print("[substack-radar] --dry-run → skipping LLM + Telegram + queue write")
        for i, c in enumerate(candidates[:10]):
            print(f"  [{i}] {c['publication_name']} | {c['content_title'][:60]}")
        return

    lessons = load_lessons()

    # Score + generate
    suggestions_sent = 0
    queue = _load_json(QUEUE_PATH, {"items": [], "last_updated": ""})
    now_iso = datetime.now(timezone.utc).isoformat()

    uae = datetime.now(timezone.utc) + timedelta(hours=4)
    _tg_send(
        f"📚 Substack Radar — {uae.strftime('%d %b %H:%M UAE')}\n"
        f"Scoring {len(candidates)} candidates from {len(publications)} publications"
    )

    # Run LLM in a loop until we have enough suggestions or exhaust candidates
    # Each run picks the best candidate from the remaining pool
    tried_urls: set[str] = set()

    while suggestions_sent < suggestions_cap and candidates:
        remaining = [c for c in candidates if c.get("content_url") not in tried_urls]
        if not remaining:
            break

        try:
            result = run_llm(remaining, lessons)
        except Exception as e:
            print(f"[substack-radar] LLM error: {e}")
            break

        winning_index = result.get("winning_index", -1)
        if winning_index == -1:
            print(f"[substack-radar] No candidate met threshold ({score_threshold}). Done.")
            break

        # Map winning_index back to the remaining list
        if winning_index >= len(remaining):
            print(f"[substack-radar] winning_index {winning_index} out of bounds. Stopping.")
            break

        winner = remaining[winning_index]
        tried_urls.add(winner.get("content_url", ""))

        action = result.get("action", "REPLY")
        restack_comment = result.get("restack_comment", "")
        reply_text = result.get("reply_text", "")

        # Merge winner metadata into result for Telegram
        result["publication_name"]   = winner["publication_name"]
        result["publication_handle"] = winner["publication_handle"]
        result["content_title"]      = winner["content_title"]
        result["content_url"]        = winner["content_url"]
        result["content_excerpt"]    = winner["content_excerpt"]
        result["content_type"]       = winner["content_type"]

        suggestions_sent += 1
        msg_id = send_suggestion_to_telegram(result, suggestions_sent, suggestions_cap)

        queue.setdefault("items", []).insert(0, {
            "publication_name":   winner["publication_name"],
            "publication_handle": winner["publication_handle"],
            "content_url":        winner["content_url"],
            "content_title":      winner["content_title"],
            "content_excerpt":    winner["content_excerpt"],
            "content_type":       winner["content_type"],
            "action":             action,
            "restack_comment":    restack_comment,
            "reply_text":         reply_text,
            "action_reason":      result.get("action_reason", ""),
            "self_review_pass":   result.get("self_review_pass", ""),
            "generated_at":       now_iso,
            "status":             "auto_sent",
            "telegram_message_id": msg_id,
            "sent_to_telegram_at": now_iso,
        })

        save_cooldown(cooldown, winner["publication_handle"])
        print(f"[substack-radar] {suggestions_sent}/{suggestions_cap} — {action} for {winner['publication_name']}")

    queue["last_updated"] = now_iso
    _save_json(QUEUE_PATH, queue)
    print(f"[substack-radar] Done. {suggestions_sent} suggestions sent.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + score only. No LLM, no Telegram, no queue write.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
