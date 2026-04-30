"""Polls Telegram for callback queries (button taps) and processes them.

On Yes  → posts X version via tweepy, sends Substack text + open link.
On Skip → asks reason, then logs feedback.
"""

import os
import json
import requests
import tweepy
from datetime import datetime, timezone

BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
QUEUE_PATH       = "data/reaction_queue.json"
OFFSET_PATH      = "data/telegram_offset.json"
POSTED_LOG_PATH  = "data/posted_log.json"
FEEDBACK_PATH    = "data/feedback_log.json"
API              = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ── helpers ──────────────────────────────────────────────────────────────────

def _load(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _x_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )


def _post_to_x(text: str) -> str | None:
    try:
        resp     = _x_client().create_tweet(text=text)
        tweet_id = resp.data["id"]
        return f"https://x.com/MuneebNaseem/status/{tweet_id}"
    except Exception as e:
        print(f"X post failed: {e}")
        return None


def _tg(method: str, body: dict) -> dict:
    resp = requests.post(f"{API}/{method}", json=body, timeout=15)
    if resp.status_code != 200:
        print(f"Telegram {method} failed [{resp.status_code}]: {resp.text}")
    return resp.json() if resp.status_code == 200 else {}


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _find_post_by_message_id(queue: dict, message_id: int) -> dict | None:
    """Look up the post that originated a Telegram message (most reliable)."""
    for p in queue.get("posts", []):
        if p.get("telegram_message_id") == message_id:
            return p
    return None


def _log_posted(post: dict, tweet_url: str) -> None:
    log = _load(POSTED_LOG_PATH, {"posted": []})
    log["posted"].append({
        "topic":          post.get("topic"),
        "source_url":     post.get("source_url"),
        "tweet_url":      tweet_url,
        "x_text":         post.get("x_post"),
        "substack_text":  post.get("substack_note"),
        "posted_at":      datetime.now(timezone.utc).isoformat(),
    })
    _save(POSTED_LOG_PATH, log)


def _log_feedback(post: dict, reason: str) -> None:
    fb = _load(FEEDBACK_PATH, {"feedback": []})
    fb["feedback"].append({
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "topic":           post.get("topic"),
        "source_headline": post.get("source_headline"),
        "source_url":      post.get("source_url"),
        "post_preview":    (post.get("x_post", "") + " | " + post.get("substack_note", ""))[:200],
        "reason":          reason,
        "generated_at":    post.get("generated_at"),
        "via":             "telegram",
    })
    _save(FEEDBACK_PATH, fb)


# ── callback handlers ────────────────────────────────────────────────────────

def _handle_yes(callback, post, queue, chat_id, message_id):
    tweet_url = _post_to_x(post["x_post"])

    if not tweet_url:
        _tg("answerCallbackQuery", {
            "callback_query_id": callback["id"],
            "text":              "❌ X post failed — see workflow logs",
            "show_alert":        True,
        })
        return

    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "✅ Posted to X!"})

    # Edit the original message: replace buttons with status
    _tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       (
            f"✅ <b>Posted to X</b>: <a href=\"{tweet_url}\">{tweet_url}</a>\n\n"
            f"📡 <b>{_esc(post.get('topic',''))}</b>\n\n"
            f"📧 Substack note sent below — tap to copy and post."
        ),
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })

    # Follow-up: substack note in copyable code block + open link
    _tg("sendMessage", {
        "chat_id":    chat_id,
        "text":       f"📧 <b>Substack note (tap to copy):</b>\n\n<pre>{_esc(post['substack_note'])}</pre>",
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "📝 Open Substack Notes", "url": "https://substack.com/notes"}
            ]]
        },
    })

    post["status"]      = "posted"
    post["tweet_url"]   = tweet_url
    post["posted_at"]   = datetime.now(timezone.utc).isoformat()
    _log_posted(post, tweet_url)


def _handle_skip(callback, post, chat_id, message_id):
    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Why skip?"})
    _tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       f"❌ Skipping: <b>{_esc(post.get('topic',''))}</b>\n\nWhy?",
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚫 Topic",            "callback_data": "reason:topic"},
                {"text": "✏️ X quality",        "callback_data": "reason:xq"},
                {"text": "✏️ Substack quality", "callback_data": "reason:sq"},
            ]]
        },
    })


def _handle_reason(callback, post, queue, chat_id, message_id, reason):
    label_map = {
        "topic": ("🚫 Topic not for me",       "topic"),
        "xq":    ("✏️ X post quality",         "x_quality"),
        "sq":    ("✏️ Substack note quality",  "substack_quality"),
    }
    label, full_reason = label_map.get(reason, (reason, reason))

    _log_feedback(post, full_reason)
    post["status"]     = f"skipped_{full_reason}"
    post["skipped_at"] = datetime.now(timezone.utc).isoformat()

    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Logged"})
    _tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       f"❌ Skipped: <b>{_esc(post.get('topic',''))}</b>\n\nReason: {label}",
        "parse_mode": "HTML",
    })


# ── main ─────────────────────────────────────────────────────────────────────

def _process_callback(callback) -> None:
    chat_id    = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    data       = callback.get("data", "")

    queue = _load(QUEUE_PATH, {"posts": []})
    post  = _find_post_by_message_id(queue, message_id)

    if not post:
        print(f"No post found for message_id={message_id}")
        _tg("answerCallbackQuery", {
            "callback_query_id": callback["id"],
            "text":              "Post not found in queue",
        })
        return

    if data == "yes":
        _handle_yes(callback, post, queue, chat_id, message_id)

    elif data == "skip":
        _handle_skip(callback, post, chat_id, message_id)

    elif data.startswith("reason:"):
        reason = data.split(":", 1)[1]
        _handle_reason(callback, post, queue, chat_id, message_id, reason)

    _save(QUEUE_PATH, queue)


def poll() -> None:
    offset_data = _load(OFFSET_PATH, {"offset": 0})
    offset      = offset_data.get("offset", 0)

    resp = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=10)
    if resp.status_code != 200:
        print(f"getUpdates failed: {resp.text}")
        return

    updates = resp.json().get("result", [])
    if not updates:
        print("No new updates")
        return

    print(f"Processing {len(updates)} updates...")
    new_offset = offset
    for u in updates:
        new_offset = max(new_offset, u["update_id"] + 1)
        if "callback_query" in u:
            try:
                _process_callback(u["callback_query"])
            except Exception as e:
                print(f"Callback error: {e}")

    _save(OFFSET_PATH, {"offset": new_offset})
    print(f"Advanced offset to {new_offset}")


if __name__ == "__main__":
    poll()
