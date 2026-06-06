"""Reply Radar for @MuneebNaseem — generate 3 reply suggestions per run from
the 189 X accounts in data/reaction_sources.json.

Pipeline (mirrors F&R Reply Radar):
  1. Load source pool from reaction_sources.json (189 X accounts).
  2. Load 14-day per-author cooldown from data/.recent_reply_authors.json.
  3. Shuffle pool, pick 3 fresh authors (not in cooldown). Fall back to
     recent authors if pool runs dry.
  4. Fetch 1 recent post per fresh author via Nitter profile RSS.
  5. Random reply style per suggestion (5 styles, sample without replacement
     when pool size allows).
  6. Generate reply via Anthropic SDK.
  7. Send 2 Telegram messages per suggestion (context + raw reply text).
     Fire-and-forget, no buttons.
  8. Append to data/reply_queue.json with status="auto_sent".
  9. Save updated cooldown file.

Banned LLM signatures rule applies (see prompts/reply_radar_prompt.txt).
Independent 14-day per-author cooldown — separate from Reaction Radar's
5-day cooldown so the same handle can be QT'd and replied-to on different
cycles.

Usage:
  python -m scripts.content.reply_radar             # full run
  python -m scripts.content.reply_radar --dry-run   # fetch + style assignment only
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.content.sources import fetch_nitter_profile  # noqa: E402
from scripts.content.jury import judge, format_verdict_card  # noqa: E402


# ── Paths ────────────────────────────────────────────────────────────────────

SOURCES_PATH       = REPO_ROOT / "data" / "reaction_sources.json"
QUEUE_PATH         = REPO_ROOT / "data" / "reply_queue.json"
LESSONS_PATH       = REPO_ROOT / "data" / "lessons_learned.md"
PROMPT_PATH        = REPO_ROOT / "prompts" / "reply_radar_prompt.txt"
COOLDOWN_PATH      = REPO_ROOT / "data" / ".recent_reply_authors.json"
JURY_PATH          = REPO_ROOT / "prompts" / "jury_reply.txt"

COOLDOWN_DAYS      = 14
SUGGESTIONS_PER_RUN = 3
MIN_POST_CHARS     = 80
MAX_POSTS_PER_AUTHOR = 1


# ── Reply styles ─────────────────────────────────────────────────────────────

REPLY_STYLES = [
    {
        "name": "specific_number",
        "instructions": (
            "Drop ONE jaw-dropping specific number that recontextualises the "
            "original post. Just the fact, no setup. Add one short clause of "
            "why it matters. Ends without a question."
        ),
        "example": "Bondholders made $47B the last time this happened in 2020. The same desks are already positioned.",
    },
    {
        "name": "historical_parallel",
        "instructions": (
            "Drop a specific historical parallel from POST-2000 finance/markets/macro. "
            "Recent events only — SVB, Credit Suisse, Bear Stearns, Lehman, "
            "2008 GFC, Argentina default, Greek crisis, COVID-era stimulus, "
            "2022 stablecoin depeg, FTX collapse, Silicon Valley Bank, Signature, "
            "First Republic. Name a year/quarter, name the actor, name the amount. "
            "Present tense. Skip 19th/early-20th century examples — that's F&R's lane, "
            "not Muneeb's. End on a flat statement that lands."
        ),
        "example": "March 2023. SVB deposit run takes 36 hours. Credit Suisse rolled into UBS the same weekend. The era of orderly resolutions ended that month.",
    },
    {
        "name": "reframe",
        "instructions": (
            "Take the post's framing and flip it. State what the situation "
            "looks like from the OTHER side of the trade — the BIS, the "
            "central bank, the bondholder, the winner nobody is naming. "
            "Sounds like someone who has read the minutes."
        ),
        "example": "From the BIS side this looks fine. They wanted exactly this duration mismatch in the periphery.",
    },
    {
        "name": "sharper_question",
        "instructions": (
            "Pose ONE precise question that opens a dimension the original "
            "post did not address. Not a vague 'what do you think' — a "
            "specific question with named actors or mechanisms. "
            "Conversational, not interrogative."
        ),
        "example": "If Saudi tightens the spigot in Q3, does ADNOC follow at the new quota or hold capacity for the next inflection?",
    },
    {
        "name": "hot_take",
        "instructions": (
            "Punchy contrarian read on what the post implies. Take a side. "
            "No hedging, no 'arguably', no 'perhaps'. Sound confident without "
            "being smug. One or two short sentences. The kind of reply a "
            "smart finance person fires off between meetings."
        ),
        "example": "This is the Fed setting up another EM crisis. Same playbook as '97. Spread shoe leather, blame the foreigners.",
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def load_recent_authors() -> dict[str, str]:
    """{author_lower: ISO date last replied} — drops anything older than window."""
    raw = _load_json(COOLDOWN_PATH, {})
    cutoff = datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS)
    fresh = {}
    for a, iso in raw.items():
        try:
            if datetime.fromisoformat(iso.replace("Z", "+00:00")) >= cutoff:
                fresh[a.lower()] = iso
        except Exception:
            continue
    return fresh


def save_recent_authors(recent: dict[str, str], new_authors: list[str]):
    now_iso = datetime.now(timezone.utc).isoformat()
    for a in new_authors:
        recent[a.lower()] = now_iso
    _save_json(COOLDOWN_PATH, recent)


def load_lessons() -> str:
    if not LESSONS_PATH.exists():
        return "(no lessons yet)"
    try:
        return LESSONS_PATH.read_text(encoding="utf-8")
    except Exception:
        return "(no lessons yet)"


# ── Telegram (no buttons, F&R Reply Radar pattern) ───────────────────────────

def _tg_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _tg_chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _tg_send(text: str) -> int | None:
    body = {
        "chat_id": _tg_chat_id(),
        "text": text,
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_tg_token()}/sendMessage",
            json=body,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()["result"]["message_id"]
        print(f"[reply-radar] Telegram send failed [{resp.status_code}]: {resp.text}")
    except Exception as e:
        print(f"[reply-radar] Telegram error: {e}")
    return None


def send_suggestion_to_telegram(
    post: dict, reply_text: str, style_name: str, idx: int, total: int,
    jury_card: str = "",
) -> int | None:
    """Send 2 messages per suggestion:
      1. Context: index + author + style + tweet text + URL (+ jury card)
      2. Raw reply text (long-press to copy on mobile, paste into X reply box)
    Returns the message_id of message 1 (used for feedback attribution).
    """
    author = post.get("handle", "")
    text   = post.get("text", "")[:280]
    url    = post.get("url", "")

    ctx_lines = [
        f"💬 Reply {idx}/{total} — @{author} · {style_name}",
        "",
        f'"{text}"',
        "",
        f"🔗 {url}",
    ]
    if jury_card:
        ctx_lines += ["", jury_card]
    ctx_lines += ["", "💡 Reply with feedback to update lessons_learned.md."]
    ctx_id = _tg_send("\n".join(ctx_lines))

    if reply_text:
        _tg_send(reply_text)

    return ctx_id


# ── LLM ──────────────────────────────────────────────────────────────────────
# Unified provider chain lives in llm.py. See chain order + quotas there.
from .llm import call_llm as _call_llm  # noqa: E402


def call_claude_for_reply(prompt: str) -> str:
    """Reply Radar wrapper. Strips wrapping quotes models sometimes add.

    max_tokens=2000: the prompt has explicit 'think to yourself' / 'self-review'
    steps that trigger CoT on gpt-oss-120b. 1024 was tight — reasoning ate the
    budget and content came back empty. llm.py raises on empty so chain falls
    through, but we want primary to succeed."""
    return _call_llm(prompt, max_tokens=2000, temperature=0.8).strip().strip('"')


def generate_reply(original_post: str, author: str, style: dict, lessons: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        author=author,
        original_post=original_post,
        style_name=style["name"],
        style_instructions=style["instructions"],
        style_example=style["example"],
        lessons_learned=lessons,
    )
    return call_claude_for_reply(prompt)


# ── Main ─────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    sources = _load_json(SOURCES_PATH, {})
    cfg = sources.get("config", {})
    nitter = cfg.get("nitter_instances", ["nitter.poast.org"])
    accounts = sources.get("x_accounts", [])
    if not accounts:
        print("[reply-radar] No accounts in source list; exiting.")
        return

    pool = [a["handle"] for a in accounts if a.get("handle")]
    print(f"[reply-radar] Source pool: {len(pool)} accounts")

    recent = load_recent_authors()
    print(f"[reply-radar] {len(recent)} authors in {COOLDOWN_DAYS}-day cooldown")

    random.shuffle(pool)

    # Pass 1: only fresh authors (not in cooldown)
    selected: list[dict] = []
    seen: set[str] = set()
    for handle in pool:
        if len(selected) >= SUGGESTIONS_PER_RUN:
            break
        if handle.lower() in recent or handle.lower() in seen:
            continue
        posts = fetch_nitter_profile(
            handle,
            nitter_instances=nitter,
            max_posts=MAX_POSTS_PER_AUTHOR,
            min_chars=MIN_POST_CHARS,
            lookback_hours=24,
        )
        if posts:
            selected.append(posts[0])
            seen.add(handle.lower())

    # Pass 2 (fallback): allow already-recent authors if pass 1 came up short
    if len(selected) < SUGGESTIONS_PER_RUN:
        print(f"[reply-radar] Only {len(selected)} fresh authors; falling back to recent...")
        for handle in pool:
            if len(selected) >= SUGGESTIONS_PER_RUN:
                break
            if handle.lower() in seen:
                continue
            posts = fetch_nitter_profile(
                handle,
                nitter_instances=nitter,
                max_posts=MAX_POSTS_PER_AUTHOR,
                min_chars=MIN_POST_CHARS,
                lookback_hours=24,
            )
            if posts:
                selected.append(posts[0])
                seen.add(handle.lower())

    if not selected:
        print("[reply-radar] No posts fetched from any target account; exiting.")
        return

    selected = selected[:SUGGESTIONS_PER_RUN]
    print(f"[reply-radar] Selected {len(selected)} target posts.")

    # Random style per suggestion (no repeats within batch when possible)
    if len(REPLY_STYLES) >= len(selected):
        styles = random.sample(REPLY_STYLES, len(selected))
    else:
        styles = random.choices(REPLY_STYLES, k=len(selected))

    if dry_run:
        print("[reply-radar] --dry-run → skipping LLM + Telegram + queue write")
        for i, (post, style) in enumerate(zip(selected, styles), 1):
            print(f"  [{i}] @{post['handle']} ({style['name']}): {post['text'][:80]}")
        return

    lessons = load_lessons()

    # Header for the batch
    uae = datetime.now(timezone.utc) + timedelta(hours=4)
    _tg_send(
        f"💬 Reply Radar — {uae.strftime('%d %b %H:%M UAE')}\n"
        f"{len(selected)} suggestions, mixed styles"
    )

    queue = _load_json(QUEUE_PATH, {"replies": [], "last_updated": ""})
    new_authors: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    sent_count = 0
    skipped_count = 0
    for i, (post, style) in enumerate(zip(selected, styles), 1):
        print(f"[reply-radar] {i}/{len(selected)} @{post['handle']} · {style['name']}")
        try:
            reply_text = generate_reply(
                post["text"], post["handle"], style, lessons,
            )
        except Exception as e:
            print(f"[reply-radar] LLM error: {e}")
            continue

        # SKIP gate — agent declined topic/style mismatch
        if reply_text.strip().upper().startswith("REPLY: SKIP") or reply_text.strip().upper().startswith("SKIP"):
            skipped_count += 1
            reason = ""
            for line in reply_text.splitlines():
                low = line.strip().lower()
                if low.startswith("reason:"):
                    reason = line.split(":", 1)[1].strip()
                    break
            print(f"  ⊘ Skipped: {reason or '(no reason)'}")
            # Do NOT cooldown skipped authors — give them a chance on a fresher post
            continue

        # Editorial jury — score relevance / voice / compliance
        verdict = judge(
            JURY_PATH,
            tweet_author=post.get("handle", ""),
            tweet_url=post.get("url", ""),
            tweet_text=post.get("text", ""),
            style_name=style["name"],
            style_instructions=style.get("instructions", ""),
            generated_content=reply_text,
        )
        print(f"  Jury: {verdict.get('verdict')} ({verdict.get('verdict_reason','')[:100]})")

        if verdict.get("verdict") == "REJECT":
            skipped_count += 1
            print(f"  ⊘ Jury REJECT — violations: {verdict.get('violations')}")
            continue

        # Sent path
        sent_count += 1
        msg_id = send_suggestion_to_telegram(
            post, reply_text, style["name"], sent_count, SUGGESTIONS_PER_RUN,
            jury_card=format_verdict_card(verdict),
        )

        # Extract tweet_id from URL for queue entry
        import re
        tid_match = re.search(r"/status/(\d+)", post.get("url", ""))
        tweet_id = tid_match.group(1) if tid_match else ""

        queue.setdefault("replies", []).insert(0, {
            "tweet_id":            tweet_id,
            "tweet_url":           post.get("url", ""),
            "tweet_author":        post.get("handle", ""),
            "tweet_text":          post.get("text", "")[:500],
            "reply_text":          reply_text,
            "reply_style":         style["name"],
            "generated_at":        now_iso,
            "status":              "auto_sent",
            "jury":                verdict,
            "telegram_message_id": msg_id,
            "sent_to_telegram_at": now_iso,
        })
        new_authors.append(post["handle"])

    queue["last_updated"] = now_iso
    _save_json(QUEUE_PATH, queue)
    save_recent_authors(recent, new_authors)
    print(f"[reply-radar] Done. Sent {sent_count}, skipped {skipped_count} on quality gates.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + style assignment only. No LLM, no Telegram, no queue write.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
