"""Sends pending paired reactions to Telegram with Yes/Skip buttons.

Each pending post in data/reaction_queue.json that hasn't been sent yet
becomes one Telegram message with both X version and Substack version
visible, plus a Yes/Skip inline keyboard.
"""

import os
import json
import requests
from datetime import datetime, timezone

BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]
QUEUE_PATH = "data/reaction_queue.json"
API        = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _format_message(post: dict) -> str:
    topic   = post.get("topic", "")
    src_url = post.get("source_url", "")
    src_hd  = post.get("source_headline", "")
    x_post  = post.get("x_post", "")
    sub     = post.get("substack_note", "")

    # Telegram MarkdownV2 escaping is annoying; use HTML instead
    return (
        f"<b>📡 {_esc(topic)}</b>\n\n"
        f"📰 <a href=\"{src_url}\">{_esc(src_hd)}</a>\n\n"
        f"<b>🐦 X version:</b>\n"
        f"<pre>{_esc(x_post)}</pre>\n\n"
        f"<b>📧 Substack version:</b>\n"
        f"<pre>{_esc(sub)}</pre>"
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _send(post: dict) -> int | None:
    callback_id = post.get("generated_at", "").replace(":", "_")[:64]

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Yes — post both",  "callback_data": f"yes:{callback_id}"},
            {"text": "❌ Skip",            "callback_data": f"skip:{callback_id}"},
        ]]
    }

    resp = requests.post(f"{API}/sendMessage", json={
        "chat_id":     CHAT_ID,
        "text":        _format_message(post),
        "parse_mode":  "HTML",
        "reply_markup": keyboard,
        "disable_web_page_preview": False,
    }, timeout=15)

    if resp.status_code == 200:
        return resp.json()["result"]["message_id"]
    print(f"Telegram send failed [{resp.status_code}]: {resp.text}")
    return None


def notify_pending() -> None:
    with open(QUEUE_PATH) as f:
        queue = json.load(f)

    posts = queue.get("posts", [])
    sent  = 0

    for post in posts:
        if post.get("status") != "pending":
            continue
        if post.get("telegram_message_id"):
            continue

        msg_id = _send(post)
        if msg_id:
            post["telegram_message_id"]   = msg_id
            post["sent_to_telegram_at"]   = datetime.now(timezone.utc).isoformat()
            sent += 1

    if sent > 0:
        queue["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(QUEUE_PATH, "w") as f:
            json.dump(queue, f, indent=2)
        print(f"Sent {sent} new posts to Telegram")
    else:
        print("No new posts to notify")


if __name__ == "__main__":
    notify_pending()
