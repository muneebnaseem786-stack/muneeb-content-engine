"""Polls Telegram for callback queries (button taps) and processes them.

On Yes  → sends X intent URL (one-tap publish) + Substack text + open link.
On Skip → asks reason, then logs feedback.

No paid X API write access required — uses twitter.com/intent/tweet for posting.
Performance tracker auto-fetches the user's recent tweets afterward.
"""

import os
import json
import requests
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


def _log_approved(post: dict) -> None:
    log = _load(POSTED_LOG_PATH, {"posted": []})
    log["posted"].append({
        "topic":          post.get("topic"),
        "source_url":     post.get("source_url"),
        "x_text":         post.get("x_post"),
        "substack_text":  post.get("substack_note"),
        "approved_at":    datetime.now(timezone.utc).isoformat(),
        "via":            "telegram_intent_url",
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
    import urllib.parse

    x_text        = post.get("x_post", "")
    substack_text = post.get("substack_note", "")
    intent_url    = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(x_text)}"

    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "✅ Approved!"})

    # Edit the original message: confirm approval
    _tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       (
            f"✅ <b>Approved</b>: {_esc(post.get('topic',''))}\n\n"
            f"X tweet and Substack note are below. Tap each to publish."
        ),
        "parse_mode": "HTML",
    })

    # Follow-up 1: X tweet with intent URL button (one-tap publish)
    _tg("sendMessage", {
        "chat_id":    chat_id,
        "text":       f"🐦 <b>X tweet — tap to publish:</b>\n\n<pre>{_esc(x_text)}</pre>",
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Post on X", "url": intent_url}
            ]]
        },
    })

    # Follow-up 2: Substack note in copyable code block + open link
    _tg("sendMessage", {
        "chat_id":    chat_id,
        "text":       f"📧 <b>Substack note — tap to copy, then publish:</b>\n\n<pre>{_esc(substack_text)}</pre>",
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "📝 Open Substack Notes", "url": "https://substack.com/notes"}
            ]]
        },
    })

    post["status"]      = "approved"
    post["approved_at"] = datetime.now(timezone.utc).isoformat()
    _log_approved(post)


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

    # Accept new format ("yes") and legacy format ("yes:abc") — message_id is the real key
    if data == "yes" or data.startswith("yes:"):
        _handle_yes(callback, post, queue, chat_id, message_id)

    elif data == "skip" or data.startswith("skip:"):
        _handle_skip(callback, post, chat_id, message_id)

    elif data.startswith("reason:"):
        # New format: "reason:xq". Legacy format: "reason:xq:cb_id"
        parts  = data.split(":")
        reason = parts[1] if len(parts) >= 2 else ""
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
