"""Sends pending content to Telegram across 4 channels:

- Reactions     (reaction_queue.json) → paired X+Substack quick takes + Yes/Skip
- Replies       (reply_queue.json)    → tweet context + suggested reply + Yes/Skip
- Daily Ideas   (content_ideas.json)  → idea card with format buttons + Skip
- Long-form     (substack_articles.json, linkedin_posts.json) → notification + dashboard link
"""

import os
import json
import requests
from datetime import datetime, timezone

BOT_TOKEN              = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID                = os.environ["TELEGRAM_CHAT_ID"]
QUEUE_PATH             = "data/reaction_queue.json"
REPLY_QUEUE_PATH       = "data/reply_queue.json"
IDEAS_PATH             = "data/content_ideas.json"
SUBSTACK_PATH          = "data/substack_articles.json"
LINKEDIN_PATH          = "data/linkedin_posts.json"
DASHBOARD_URL          = "https://muneeb-content.streamlit.app"
API                    = f"https://api.telegram.org/bot{BOT_TOKEN}"


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


# ── DAILY IDEAS (new) ────────────────────────────────────────────────────────

def _send_idea(idea: dict) -> int | None:
    """Send a Daily Idea card. The X post button is a direct URL — one tap opens
    X composer with the text pre-loaded, no server round-trip. Same pattern as
    the F&R distribution bot.
    """
    import urllib.parse

    title    = idea.get("title", "")
    angle    = idea.get("angle", "")
    urgency  = idea.get("urgency", "timely")
    pillar   = idea.get("pillar", "")

    icon = {"breaking": "🔴", "timely": "🟡", "evergreen": "🟢"}.get(urgency, "🟡")

    pack       = idea.get("content_pack", {})
    x_text     = pack.get("x_longform", "")
    thread     = pack.get("x_thread", []) or []
    has_x      = bool(x_text)
    has_thread = bool(thread)
    has_sub    = bool(pack.get("substack_draft"))

    # Build the card body — show the X text inline so user can review before tapping
    body_parts = [
        f"💡 <b>Daily Idea</b> {icon}",
        "",
        f"<b>{_esc(title)}</b>",
        "",
        f"<i>Angle:</i> {_esc(angle)}",
        f"<i>Pillar:</i> {_esc(pillar)}  ·  <i>Urgency:</i> {_esc(urgency)}",
    ]
    if has_x:
        body_parts += ["", "<b>🐦 X post:</b>", f"<pre>{_esc(x_text)}</pre>"]
    if has_thread:
        body_parts += ["", f"<b>🧵 Thread ({len(thread)} tweets) — tap below to start.</b>"]
    text = "\n".join(body_parts)

    # Direct URL buttons — instant, no callback hop
    rows = []
    if has_x:
        intent_url = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(x_text)}"
        rows.append([{"text": "🚀 Post on X", "url": intent_url}])
    if has_thread:
        first_intent = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(thread[0])}"
        rows.append([{"text": "🧵 Post first thread tweet", "url": first_intent}])
    if has_sub:
        rows.append([{"text": "📧 Open Substack draft", "url": DASHBOARD_URL}])
    rows.append([{"text": "❌ Skip", "callback_data": "skip"}])

    keyboard = {"inline_keyboard": rows}

    resp = requests.post(f"{API}/sendMessage", json={
        "chat_id":     CHAT_ID,
        "text":        text,
        "parse_mode":  "HTML",
        "reply_markup": keyboard,
        "disable_web_page_preview": True,
    }, timeout=15)
    if resp.status_code == 200:
        return resp.json()["result"]["message_id"]
    print(f"sendMessage(idea) failed [{resp.status_code}]: {resp.text}")
    return None


# ── LONG-FORM NOTIFICATIONS (new) ────────────────────────────────────────────

def _send_long_form_notification(kind: str, title: str, dashboard_anchor: str) -> int | None:
    """Send a 'X is ready, review on dashboard' notification."""
    icon, label = {
        "substack_article": ("📚", "Substack article"),
        "linkedin_post":    ("💼", "LinkedIn post"),
    }.get(kind, ("📝", "Long-form post"))

    text = (
        f"{icon} <b>{label} ready</b>\n\n"
        f"<b>{_esc(title)}</b>\n\n"
        f"Review the full draft and copy from the dashboard:\n"
        f"<a href=\"{DASHBOARD_URL}\">{DASHBOARD_URL}</a>"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": f"{icon} Open dashboard", "url": DASHBOARD_URL},
        ]]
    }

    resp = requests.post(f"{API}/sendMessage", json={
        "chat_id":     CHAT_ID,
        "text":        text,
        "parse_mode":  "HTML",
        "reply_markup": keyboard,
        "disable_web_page_preview": True,
    }, timeout=15)
    if resp.status_code == 200:
        return resp.json()["result"]["message_id"]
    print(f"sendMessage(long-form) failed [{resp.status_code}]: {resp.text}")
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

    # Daily Ideas
    idea_sent = 0
    try:
        with open(IDEAS_PATH) as f:
            ideas_data = json.load(f)
        for idea in ideas_data.get("ideas", []):
            if idea.get("status") == "skipped" or idea.get("telegram_message_id"):
                continue
            mid = _send_idea(idea)
            if mid:
                idea["telegram_message_id"] = mid
                idea["sent_to_telegram_at"] = datetime.now(timezone.utc).isoformat()
                idea.setdefault("status", "pending")
                idea_sent += 1
        if idea_sent:
            ideas_data["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(IDEAS_PATH, "w") as f:
                json.dump(ideas_data, f, indent=2)
    except FileNotFoundError:
        print(f"{IDEAS_PATH} not found")

    # Substack articles
    article_sent = 0
    try:
        with open(SUBSTACK_PATH) as f:
            arts = json.load(f)
        for art in arts.get("articles", []):
            if art.get("telegram_notified_at"):
                continue
            mid = _send_long_form_notification("substack_article", art.get("title", ""), "substack")
            if mid:
                art["telegram_notified_at"] = datetime.now(timezone.utc).isoformat()
                art["telegram_message_id"]  = mid
                article_sent += 1
        if article_sent:
            arts["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(SUBSTACK_PATH, "w") as f:
                json.dump(arts, f, indent=2)
    except FileNotFoundError:
        print(f"{SUBSTACK_PATH} not found")

    # LinkedIn posts
    li_sent = 0
    try:
        with open(LINKEDIN_PATH) as f:
            lis = json.load(f)
        for li in lis.get("posts", []):
            if li.get("telegram_notified_at"):
                continue
            mid = _send_long_form_notification("linkedin_post", li.get("title", ""), "linkedin")
            if mid:
                li["telegram_notified_at"] = datetime.now(timezone.utc).isoformat()
                li["telegram_message_id"]  = mid
                li_sent += 1
        if li_sent:
            lis["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(LINKEDIN_PATH, "w") as f:
                json.dump(lis, f, indent=2)
    except FileNotFoundError:
        print(f"{LINKEDIN_PATH} not found")

    print(
        f"Sent {sent_total} reactions + {reply_sent} replies + {idea_sent} ideas + "
        f"{article_sent} articles + {li_sent} LinkedIn posts"
    )


if __name__ == "__main__":
    notify_pending()
