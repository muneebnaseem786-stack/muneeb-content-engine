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
QUEUE_PATH                  = "data/reaction_queue.json"
REPLY_QUEUE_PATH            = "data/reply_queue.json"
SUBSTACK_REPLY_QUEUE_PATH   = "data/substack_reply_queue.json"
IDEAS_PATH                  = "data/content_ideas.json"
SUBSTACK_PATH               = "data/substack_articles.json"
LINKEDIN_PATH               = "data/linkedin_posts.json"
ARTICLE_JURY_PATH           = "data/article_jury.json"
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


# ── SUBSTACK REPLY RADAR (new) ───────────────────────────────────────────────

def _send_substack_reply(item: dict) -> int | None:
    """Send a Substack Reply Radar suggestion as 2 messages.
      1. Context: publication, title, excerpt, URL, action type
      2. Raw content: restack comment or reply text + Open on Substack button
    Returns msg_id of message 1."""
    action    = item.get("action", "REPLY")
    pub_name  = item.get("publication_name", "")
    title     = item.get("content_title", "")
    excerpt   = (item.get("content_excerpt", "") or "")[:300]
    url       = item.get("content_url", "")
    c_type    = item.get("content_type", "article")
    a_icon    = "🔁" if action == "RESTACK" else "💬"
    t_icon    = "📝" if c_type == "article" else "📌"

    title_link = f'<a href="{url}">{_esc(title)}</a>' if url else _esc(title)
    msg1 = (
        f"{a_icon} <b>Substack {action}</b> — {_esc(pub_name)}\n\n"
        f"{t_icon} {title_link}\n\n"
        f"<i>{_esc(excerpt)}</i>"
    )
    requests.post(f"{API}/sendMessage", json={
        "chat_id":    CHAT_ID,
        "text":       msg1,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=15)

    if action == "RESTACK":
        content = item.get("restack_comment", "")
        label   = "🔁 <b>Restack comment</b> (add when you restack):"
    else:
        content = item.get("reply_text", "")
        label   = "💬 <b>Reply</b> (paste into Substack comments):"

    msg2 = f"{label}\n\n<pre>{_esc(content)}</pre>"
    btn_label = f"{'📝 Read article' if c_type == 'article' else '📌 Read note'} → {pub_name}"
    keyboard = {"inline_keyboard": [[{"text": btn_label, "url": url}]]} if url else None

    payload = {
        "chat_id":    CHAT_ID,
        "text":       msg2,
        "parse_mode": "HTML",
    }
    if keyboard:
        payload["reply_markup"] = keyboard

    resp = requests.post(f"{API}/sendMessage", json=payload, timeout=15)
    if resp.status_code == 200:
        return resp.json()["result"]["message_id"]
    print(f"sendMessage(substack-reply) failed [{resp.status_code}]: {resp.text}")
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
    sub_text   = pack.get("substack_draft", "")
    has_x      = bool(x_text)
    has_thread = bool(thread)
    has_sub    = bool(sub_text)

    # Build the card body — show all content inline so user can review + long-press to copy
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
    if has_sub:
        body_parts += ["", "<b>📧 Substack draft sent below ⬇️</b>"]
    text = "\n".join(body_parts)

    # Main card — direct URL buttons, instant
    rows = []
    if has_x:
        intent_url = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(x_text)}"
        rows.append([{"text": "🚀 Post on X", "url": intent_url}])
    if has_thread:
        first_intent = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(thread[0])}"
        rows.append([{"text": "🧵 Post first thread tweet", "url": first_intent}])
    # No Skip callback button. Reply to this message with feedback text to skip + train.

    resp = requests.post(f"{API}/sendMessage", json={
        "chat_id":     CHAT_ID,
        "text":        text,
        "parse_mode":  "HTML",
        "reply_markup": {"inline_keyboard": rows},
        "disable_web_page_preview": True,
    }, timeout=15)
    if resp.status_code != 200:
        print(f"sendMessage(idea) failed [{resp.status_code}]: {resp.text}")
        return None
    main_msg_id = resp.json()["result"]["message_id"]

    # Follow-up: substack draft as raw text (no header, no HTML) so long-press
    # copies ONLY the draft. Long drafts get split across multiple messages.
    # Last message has a small header line + Open Substack Notes button.
    if has_sub:
        MAX = 4000  # plain text, no HTML overhead
        chunks = [sub_text[i:i+MAX] for i in range(0, len(sub_text), MAX)]
        for idx, chunk in enumerate(chunks):
            payload = {
                "chat_id":    CHAT_ID,
                "text":       chunk,  # raw text — long-press copies just this
                "disable_web_page_preview": True,
                "reply_to_message_id": main_msg_id,
            }
            r = requests.post(f"{API}/sendMessage", json=payload, timeout=15)
            if r.status_code != 200:
                print(f"sendMessage(idea-substack chunk {idx+1}) failed [{r.status_code}]: {r.text}")

        # Footer message with the Open Substack button
        requests.post(f"{API}/sendMessage", json={
            "chat_id":    CHAT_ID,
            "text":       "📧 Long-press the draft above to copy, then tap below to open Substack.",
            "reply_markup": {"inline_keyboard": [[
                {"text": "📝 Open Substack Notes", "url": "https://substack.com/notes"},
            ]]},
            "reply_to_message_id": main_msg_id,
        }, timeout=15)

    return main_msg_id


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


# ── ARTICLE JURY ─────────────────────────────────────────────────────────────

JURY_STAGE_MESSAGES = {
    "ideas_awaiting_pick": (
        "📚 <b>Article jury</b> — 5 ideas ready.\n\n"
        "Pick the strongest one on the dashboard. The next stage runs after you pick."
    ),
    "hooks_awaiting_pick": (
        "📚 <b>Article jury</b> — 5 hooks ready for your picked idea.\n\n"
        "Pick the strongest hook on the dashboard. The full article runs next."
    ),
    "done": (
        "📚 <b>Article jury</b> — final article ready.\n\n"
        "Review and copy from the dashboard."
    ),
}


def _send_jury_stage_notification(stage: str) -> bool:
    body = JURY_STAGE_MESSAGES.get(stage)
    if not body:
        return False
    text = (
        f"{body}\n\n<a href=\"{DASHBOARD_URL}\">{DASHBOARD_URL}</a>"
    )
    resp = requests.post(f"{API}/sendMessage", json={
        "chat_id":     CHAT_ID,
        "text":        text,
        "parse_mode":  "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[
            {"text": "📚 Open dashboard", "url": DASHBOARD_URL},
        ]]},
    }, timeout=15)
    if resp.status_code == 200:
        return True
    print(f"sendMessage(jury) failed [{resp.status_code}]: {resp.text}")
    return False


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

    # Substack Reply Radar
    substack_reply_sent = 0
    try:
        with open(SUBSTACK_REPLY_QUEUE_PATH) as f:
            srq = json.load(f)
        for item in srq.get("items", []):
            if item.get("status") != "pending" or item.get("telegram_message_id"):
                continue
            mid = _send_substack_reply(item)
            if mid:
                item["telegram_message_id"] = mid
                item["sent_to_telegram_at"] = datetime.now(timezone.utc).isoformat()
                substack_reply_sent += 1
        if substack_reply_sent:
            srq["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(SUBSTACK_REPLY_QUEUE_PATH, "w") as f:
                json.dump(srq, f, indent=2)
    except FileNotFoundError:
        print(f"{SUBSTACK_REPLY_QUEUE_PATH} not found")

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

    # Article jury — fire on every state transition into a notify-worthy stage
    jury_sent = 0
    try:
        with open(ARTICLE_JURY_PATH) as f:
            jury = json.load(f)
        cur   = jury.get("current_article", {})
        stage = cur.get("stage")
        last  = cur.get("last_notified_stage")
        if stage in JURY_STAGE_MESSAGES and stage != last:
            if _send_jury_stage_notification(stage):
                cur["last_notified_stage"] = stage
                jury["current_article"] = cur
                with open(ARTICLE_JURY_PATH, "w") as f:
                    json.dump(jury, f, indent=2)
                jury_sent = 1
    except FileNotFoundError:
        print(f"{ARTICLE_JURY_PATH} not found")

    print(
        f"Sent {sent_total} reactions + {reply_sent} replies + {substack_reply_sent} substack-replies + "
        f"{idea_sent} ideas + {article_sent} articles + {li_sent} LinkedIn posts + {jury_sent} jury notifications"
    )


if __name__ == "__main__":
    notify_pending()
