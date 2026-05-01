"""Sends pending paired reactions AND reply opportunities to Telegram.

Reactions (data/reaction_queue.json) → 1 message with X+Substack pair + Yes/Skip
Replies (data/reply_queue.json)      → 2 messages (tweet context + suggested reply with buttons)
"""

import os
import json
import requests
from datetime import datetime, timezone

BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID          = os.environ["TELEGRAM_CHAT_ID"]
QUEUE_PATH       = "data/reaction_queue.json"
REPLY_QUEUE_PATH = "data/reply_queue.json"
API              = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── REACTIONS (existing) ─────────────────────────────────────────────────────

def _format_reaction(post: dict) -> str:
    topic   = post.get("topic", "")
    src_url = post.get("source_url", "")
    src_hd  = post.get("source_headline", "")
    x_post  = post.get("x_post", "")
    sub     = post.get("substack_note", "")
    return (
        f"<b>📡 {_esc(topic)}</b>\n\n"
        f"📰 <a href=\"{src_url}\">{_esc(src_hd)}</a>\n\n"
        f"<b>🐦 X version:</b>\n"
        f"<pre>{_esc(x_post)}</pre>\n\n"
        f"<b>📧 Substack version:</b>\n"
        f"<pre>{_esc(sub)}</pre>"
    )


def _send_reaction(post: dict) -> int | None:
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Yes — post both", "callback_data": "yes"},
            {"text": "❌ Skip",           "callback_data": "skip"},
        ]]
    }
    resp = requests.post(f"{API}/sendMessage", json={
        "chat_id":     CHAT_ID,
        "text":        _format_reaction(post),
        "parse_mode":  "HTML",
        "reply_markup": keyboard,
        "disable_web_page_preview": False,
    }, timeout=15)
    if resp.status_code == 200:
        return resp.json()["result"]["message_id"]
    print(f"sendMessage(reaction) failed [{resp.status_code}]: {resp.text}")
    return None


# ── REPLIES (new) ────────────────────────────────────────────────────────────

def _send_reply(reply: dict) -> int | None:
    """Send reply opportunity as 2 messages. Returns msg_id of the buttons message."""
    tweet_url     = reply.get("tweet_url", "")
    tweet_author  = reply.get("tweet_author", "")
    tweet_text    = (reply.get("tweet_text", "") or "")[:400]
    reply_text    = reply.get("reply_text", "")
    reply_angle   = reply.get("reply_angle", "")

    # Message 1: tweet context (no buttons)
    msg1 = (
        f"💬 <b>Reply opportunity</b> — {_esc(tweet_author)}\n\n"
        f"<i>{_esc(tweet_text)}</i>\n\n"
        f"🔗 <a href=\"{tweet_url}\">Open original tweet</a>"
    )
    requests.post(f"{API}/sendMessage", json={
        "chat_id":     CHAT_ID,
        "text":        msg1,
        "parse_mode":  "HTML",
        "disable_web_page_preview": False,
    }, timeout=15)

    # Message 2: suggested reply + Yes/Skip
    angle_line = f" <i>({_esc(reply_angle)})</i>" if reply_angle else ""
    msg2 = (
        f"🐦 <b>Suggested reply</b>{angle_line}:\n\n"
        f"<pre>{_esc(reply_text)}</pre>"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Yes — reply on X", "callback_data": "yes"},
            {"text": "❌ Skip",            "callback_data": "skip"},
        ]]
    }
    resp = requests.post(f"{API}/sendMessage", json={
        "chat_id":     CHAT_ID,
        "text":        msg2,
        "parse_mode":  "HTML",
        "reply_markup": keyboard,
    }, timeout=15)

    if resp.status_code == 200:
        return resp.json()["result"]["message_id"]
    print(f"sendMessage(reply) failed [{resp.status_code}]: {resp.text}")
    return None


# ── Main ─────────────────────────────────────────────────────────────────────

def notify_pending() -> None:
    sent_total = 0

    # Reactions
    try:
        with open(QUEUE_PATH) as f:
            rq = json.load(f)
        for post in rq.get("posts", []):
            if post.get("status") != "pending" or post.get("telegram_message_id"):
                continue
            mid = _send_reaction(post)
            if mid:
                post["telegram_message_id"] = mid
                post["sent_to_telegram_at"] = datetime.now(timezone.utc).isoformat()
                sent_total += 1
        if sent_total:
            rq["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(QUEUE_PATH, "w") as f:
                json.dump(rq, f, indent=2)
    except FileNotFoundError:
        print(f"{QUEUE_PATH} not found")

    # Replies
    reply_sent = 0
    try:
        with open(REPLY_QUEUE_PATH) as f:
            repq = json.load(f)
        for reply in repq.get("replies", []):
            if reply.get("status") != "pending" or reply.get("telegram_message_id"):
                continue
            mid = _send_reply(reply)
            if mid:
                reply["telegram_message_id"] = mid
                reply["sent_to_telegram_at"] = datetime.now(timezone.utc).isoformat()
                reply_sent += 1
        if reply_sent:
            repq["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(REPLY_QUEUE_PATH, "w") as f:
                json.dump(repq, f, indent=2)
    except FileNotFoundError:
        print(f"{REPLY_QUEUE_PATH} not found")

    print(f"Sent {sent_total} reactions + {reply_sent} reply opportunities")


if __name__ == "__main__":
    notify_pending()
