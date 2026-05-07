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
REPLY_QUEUE_PATH = "data/reply_queue.json"
IDEAS_PATH       = "data/content_ideas.json"
OFFSET_PATH      = "data/telegram_offset.json"
POSTED_LOG_PATH  = "data/posted_log.json"
FEEDBACK_PATH    = "data/feedback_log.json"
LESSONS_PATH     = "data/lessons_learned.md"
DASHBOARD_URL    = "https://muneeb-content.streamlit.app"
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
    """Look up the reaction post that originated a Telegram message."""
    for p in queue.get("posts", []):
        if p.get("telegram_message_id") == message_id:
            return p
    return None


def _find_reply_by_message_id(reply_queue: dict, message_id: int) -> dict | None:
    """Look up the reply opportunity that originated a Telegram message."""
    for r in reply_queue.get("replies", []):
        if r.get("telegram_message_id") == message_id:
            return r
    return None


def _find_idea_by_message_id(ideas_data: dict, message_id: int) -> dict | None:
    """Look up the Daily Idea that originated a Telegram message."""
    for i in ideas_data.get("ideas", []):
        if i.get("telegram_message_id") == message_id:
            return i
    return None


def _find_anywhere_by_message_id(message_id: int) -> tuple[dict | None, str]:
    """Returns (item, kind) where kind ∈ {'reaction', 'reply', 'idea', ''}."""
    rq = _load(QUEUE_PATH, {"posts": []})
    p  = _find_post_by_message_id(rq, message_id)
    if p:
        return p, "reaction"
    repq = _load(REPLY_QUEUE_PATH, {"replies": []})
    r    = _find_reply_by_message_id(repq, message_id)
    if r:
        return r, "reply"
    idata = _load(IDEAS_PATH, {"ideas": []})
    i    = _find_idea_by_message_id(idata, message_id)
    if i:
        return i, "idea"
    return None, ""


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


def _log_feedback(post: dict, reason: str, free_form_text: str | None = None) -> None:
    fb = _load(FEEDBACK_PATH, {"feedback": []})
    entry = {
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "topic":           post.get("topic"),
        "source_headline": post.get("source_headline"),
        "source_url":      post.get("source_url"),
        "post_preview":    (post.get("x_post", "") + " | " + post.get("substack_note", ""))[:200],
        "reason":          reason,
        "generated_at":    post.get("generated_at"),
        "via":             "telegram",
    }
    if free_form_text:
        entry["free_form_text"] = free_form_text
    fb["feedback"].append(entry)
    _save(FEEDBACK_PATH, fb)


def _append_lesson(post: dict, free_form_text: str) -> None:
    """Append a free-form lesson to lessons_learned.md (read by agents)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = (
        f"\n## {timestamp} — {post.get('topic', '')}\n"
        f"**Feedback:** {free_form_text}\n"
        f"**Pillar:** {post.get('pillar', '?')}  ·  "
        f"**Source:** {post.get('source_url', '')}\n"
    )
    try:
        with open(LESSONS_PATH) as f:
            existing = f.read()
    except FileNotFoundError:
        existing = (
            "# Lessons Learned — Real-Time Feedback from Muneeb\n\n"
            "Auto-updated. Agents apply these on top of voice_context.py.\n\n---\n"
        )
    with open(LESSONS_PATH, "w") as f:
        f.write(existing + entry)


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

    # Follow-up 2: Substack note with Copy button + Open link
    # Substack has no public "compose" URL — copy_text + open is the cleanest flow
    _tg("sendMessage", {
        "chat_id":    chat_id,
        "text":       f"📧 <b>Substack note:</b>\n\n<pre>{_esc(substack_text)}</pre>",
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "📋 Copy note",            "copy_text": {"text": substack_text}}],
                [{"text": "📝 Open Substack Notes",  "url": "https://substack.com/notes"}],
            ]
        },
    })

    post["status"]      = "approved"
    post["approved_at"] = datetime.now(timezone.utc).isoformat()
    _log_approved(post)


def _handle_skip(callback, post, chat_id, message_id):
    post["awaiting_reason_at"] = datetime.now(timezone.utc).isoformat()
    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Why skip?"})
    _tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       (
            f"❌ Skipping: <b>{_esc(post.get('topic',''))}</b>\n\n"
            f"<b>Tell me why</b> — just type a reply in the chat (plain English) "
            f"and the radar will learn from it.\n\n"
            f"Or tap a quick category below."
        ),
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


def _handle_reply_yes(callback, reply, chat_id, message_id):
    """Handle Yes on a reply opportunity → send X intent URL with in_reply_to."""
    import urllib.parse

    tweet_id   = reply.get("tweet_id", "")
    reply_text = reply.get("reply_text", "")
    author     = reply.get("tweet_author", "")

    intent_url = (
        f"https://twitter.com/intent/tweet"
        f"?in_reply_to={urllib.parse.quote(str(tweet_id))}"
        f"&text={urllib.parse.quote(reply_text)}"
    )

    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "✅ Approved!"})

    _tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       (
            f"✅ <b>Reply approved</b> to {_esc(author)}\n\n"
            f"<pre>{_esc(reply_text)}</pre>\n\n"
            f"Tap below to publish — X opens the reply composer pre-loaded."
        ),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Reply on X", "url": intent_url}
            ]]
        },
    })

    reply["status"]      = "approved"
    reply["approved_at"] = datetime.now(timezone.utc).isoformat()


def _handle_idea_x(callback, idea, chat_id, message_id):
    """Show the X long-form post with one-tap publish via intent URL."""
    import urllib.parse
    pack    = idea.get("content_pack", {})
    x_text  = pack.get("x_longform", "")
    if not x_text:
        _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "No X post"})
        return

    intent_url = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(x_text)}"
    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "🐦 X post"})
    # Telegram copy_text button has a 256-char limit. Long-form X posts exceed that,
    # so omit the copy button when over the limit — the <pre> block is tap-to-copy on mobile.
    row = [{"text": "🚀 Post on X", "url": intent_url}]
    if len(x_text) <= 256:
        row.append({"text": "📋 Copy", "copy_text": {"text": x_text}})
    _tg("sendMessage", {
        "chat_id":    chat_id,
        "text":       (
            f"🐦 <b>X long-form</b> for: <i>{_esc(idea.get('title',''))}</i>\n\n"
            f"<pre>{_esc(x_text)}</pre>\n\n"
            f"<i>Tip: long-press the text above to copy.</i>"
        ),
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [row]},
    })


def _handle_idea_thread(callback, idea, chat_id, message_id):
    """Show the X thread tweet-by-tweet with copy buttons."""
    import urllib.parse
    pack   = idea.get("content_pack", {})
    thread = pack.get("x_thread", [])
    if not thread:
        _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "No thread"})
        return

    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "🧵 Thread"})

    # First tweet — gets the publish button (intent URL)
    first = thread[0]
    intent_url = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(first)}"
    row = [{"text": "🚀 Post first tweet", "url": intent_url}]
    if len(first) <= 256:
        row.append({"text": "📋 Copy", "copy_text": {"text": first}})
    _tg("sendMessage", {
        "chat_id":    chat_id,
        "text":       f"🧵 <b>Thread for</b>: <i>{_esc(idea.get('title',''))}</i>\n\n"
                       f"<b>Tweet 1/{len(thread)}:</b>\n<pre>{_esc(first)}</pre>",
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [row]},
    })

    # Remaining tweets — just copy buttons (user replies to their previous tweet manually)
    for i, tweet in enumerate(thread[1:], start=2):
        markup = None
        if len(tweet) <= 256:
            markup = {"inline_keyboard": [[
                {"text": "📋 Copy", "copy_text": {"text": tweet}},
            ]]}
        body = {
            "chat_id":    chat_id,
            "text":       f"<b>Tweet {i}/{len(thread)}:</b>\n<pre>{_esc(tweet)}</pre>",
            "parse_mode": "HTML",
        }
        if markup:
            body["reply_markup"] = markup
        _tg("sendMessage", body)


def _handle_idea_substack(callback, idea, chat_id, message_id):
    """Substack drafts are too long for Telegram — point to dashboard."""
    pack = idea.get("content_pack", {})
    sub  = pack.get("substack_draft", "")
    if not sub:
        _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "No draft"})
        return

    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "📧 Substack"})
    _tg("sendMessage", {
        "chat_id":    chat_id,
        "text":       (
            f"📧 <b>Substack draft outline</b> for: <i>{_esc(idea.get('title',''))}</i>\n\n"
            f"Drafts are long. Open the dashboard to expand into a full essay:\n"
            f"<a href=\"{DASHBOARD_URL}\">{DASHBOARD_URL}</a>\n\n"
            f"<i>Outline preview:</i>\n<pre>{_esc(sub[:800])}{('...' if len(sub) > 800 else '')}</pre>"
        ),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "📝 Open dashboard", "url": DASHBOARD_URL},
            ]]
        },
    })


def _handle_idea_skip(callback, idea, chat_id, message_id):
    idea["awaiting_reason_at"] = datetime.now(timezone.utc).isoformat()
    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Why skip?"})
    _tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       (
            f"❌ Skipping idea: <b>{_esc(idea.get('title',''))}</b>\n\n"
            f"<b>Tell me why</b> — type a reply in plain English (the radar will learn from it)."
        ),
        "parse_mode": "HTML",
    })


def _handle_reply_skip(callback, reply, chat_id, message_id):
    reply["awaiting_reason_at"] = datetime.now(timezone.utc).isoformat()
    _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Why skip?"})
    _tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       (
            f"❌ Skipping reply to <b>{_esc(reply.get('tweet_author',''))}</b>\n\n"
            f"<b>Tell me why</b> — just type a reply in the chat (plain English) "
            f"and the reply radar will learn from it.\n\n"
            f"Or tap a quick category below."
        ),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚫 Bad target",  "callback_data": "reason:topic"},
                {"text": "✏️ Bad reply",   "callback_data": "reason:xq"},
            ]]
        },
    })


# ── main ─────────────────────────────────────────────────────────────────────

_TWEET_URL_RE = __import__("re").compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/[^/\s]+/status/(\d+)",
    __import__("re").IGNORECASE,
)


def _extract_tweet_id(text: str) -> str | None:
    """Return tweet ID if text contains an x.com or twitter.com /status/<id> URL."""
    match = _TWEET_URL_RE.search(text or "")
    return match.group(1) if match else None


def _process_message(message) -> bool:
    """Process a free-form text message as feedback OR a posted-tweet URL.
    Returns True if attributed to any item (reaction / reply / idea).
    """
    text = (message.get("text") or "").strip()
    if not text or text.startswith("/"):
        return False

    chat_id = message["chat"]["id"]

    queue  = _load(QUEUE_PATH,       {"posts":   []})
    repq   = _load(REPLY_QUEUE_PATH, {"replies": []})
    idata  = _load(IDEAS_PATH,       {"ideas":   []})

    item   = None
    kind   = ""

    # Attribution rule 1: explicit reply_to_message
    reply_to = message.get("reply_to_message") or {}
    target_msg_id = reply_to.get("message_id") if reply_to else None
    if target_msg_id:
        item = _find_post_by_message_id(queue, target_msg_id)
        if item:
            kind = "reaction"
        else:
            item = _find_reply_by_message_id(repq, target_msg_id)
            if item:
                kind = "reply"
            else:
                item = _find_idea_by_message_id(idata, target_msg_id)
                if item:
                    kind = "idea"

    # Attribution rule 2: most recent item awaiting_reason_at (within 30 min)
    if not item:
        candidates = []
        for p in queue.get("posts", []):
            if p.get("awaiting_reason_at"):
                candidates.append(("reaction", p))
        for r in repq.get("replies", []):
            if r.get("awaiting_reason_at"):
                candidates.append(("reply", r))
        for i in idata.get("ideas", []):
            if i.get("awaiting_reason_at"):
                candidates.append(("idea", i))
        if candidates:
            most_recent_kind, most_recent = max(
                candidates, key=lambda kp: kp[1].get("awaiting_reason_at")
            )
            ts = datetime.fromisoformat(
                most_recent["awaiting_reason_at"].replace("Z", "+00:00")
            )
            if (datetime.now(timezone.utc) - ts).total_seconds() < 1800:
                item = most_recent
                kind = most_recent_kind

    if not item:
        return False

    # Build a normalized post-shaped dict for feedback/lesson logging
    if kind == "reaction":
        feedback_post = item
    elif kind == "reply":
        feedback_post = {
            "topic":           f"Reply to {item.get('tweet_author','')}",
            "source_headline": item.get("tweet_text", "")[:100],
            "source_url":      item.get("tweet_url", ""),
            "x_post":          item.get("reply_text", ""),
            "substack_note":   "",
            "pillar":          "reply_radar",
            "generated_at":    item.get("generated_at"),
        }
    else:  # idea
        pack = item.get("content_pack", {})
        feedback_post = {
            "topic":           item.get("title", ""),
            "source_headline": item.get("trend", ""),
            "source_url":      "",
            "x_post":          pack.get("x_longform", "")[:200],
            "substack_note":   pack.get("substack_draft", "")[:200],
            "pillar":          item.get("pillar", "daily_idea"),
            "generated_at":    item.get("generated_at"),
        }

    # If this is a posted-tweet URL, attribute it to the queued reaction so the
    # performance tracker can fetch metrics by ID (free-tier endpoint).
    tweet_id = _extract_tweet_id(text)
    if tweet_id and kind == "reaction":
        item["posted_tweet_id"]  = tweet_id
        item["posted_tweet_url"] = f"https://x.com/{message.get('from',{}).get('username','i')}/status/{tweet_id}"
        item["posted_at"]        = datetime.now(timezone.utc).isoformat()
        _save(QUEUE_PATH, queue)
        _tg("sendMessage", {
            "chat_id":    chat_id,
            "text":       (
                f"✅ Tweet ID logged for <b>{_esc(item.get('topic',''))}</b>: "
                f"<code>{tweet_id}</code>\n\n"
                f"Engagement metrics will populate on the next 11pm UAE tracker run."
            ),
            "parse_mode": "HTML",
        })
        return True

    _log_feedback(feedback_post, "free_form", free_form_text=text)
    _append_lesson(feedback_post, text)

    # Reaction Radar uses status="auto_sent" (fire-and-forget). Free-form feedback
    # on those should NOT mark the item as skipped — Muneeb may still post the
    # tweet AND give feedback for next time. Only flip status if it was pending.
    prior_status = item.get("status", "")
    if prior_status in ("auto_sent",):
        # Append feedback without overwriting status
        item.setdefault("feedback_history", []).append({
            "text":         text,
            "received_at":  datetime.now(timezone.utc).isoformat(),
        })
    else:
        item["status"]        = "skipped_with_reason"
        item["skipped_at"]    = datetime.now(timezone.utc).isoformat()
        item["feedback_text"] = text
        item.pop("awaiting_reason_at", None)

    if kind == "reaction":
        _save(QUEUE_PATH, queue)
        topic_label = item.get("topic", "")
    elif kind == "reply":
        _save(REPLY_QUEUE_PATH, repq)
        topic_label = f"reply to {item.get('tweet_author','')}"
    else:
        _save(IDEAS_PATH, idata)
        topic_label = item.get("title", "")

    _tg("sendMessage", {
        "chat_id":    chat_id,
        "text":       (
            f"✅ Logged feedback for <b>{_esc(topic_label)}</b>:\n\n"
            f"<i>{_esc(text)}</i>\n\n"
            f"This rule is now in <code>lessons_learned.md</code> and will inform every future run."
        ),
        "parse_mode": "HTML",
    })
    return True


def _process_callback(callback) -> None:
    chat_id    = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    data       = callback.get("data", "")

    item, kind = _find_anywhere_by_message_id(message_id)

    if not item:
        print(f"No reaction or reply found for message_id={message_id}")
        _tg("answerCallbackQuery", {
            "callback_query_id": callback["id"],
            "text":              "Item not found in queue",
        })
        return

    if kind == "reaction":
        queue = _load(QUEUE_PATH, {"posts": []})
        # Re-find inside this queue dict (to mutate-and-save the right object)
        item  = _find_post_by_message_id(queue, message_id)

        if data == "yes" or data.startswith("yes:"):
            _handle_yes(callback, item, queue, chat_id, message_id)
        elif data == "skip" or data.startswith("skip:"):
            _handle_skip(callback, item, chat_id, message_id)
        elif data.startswith("reason:"):
            parts  = data.split(":")
            reason = parts[1] if len(parts) >= 2 else ""
            _handle_reason(callback, item, queue, chat_id, message_id, reason)

        _save(QUEUE_PATH, queue)

    elif kind == "idea":
        idata = _load(IDEAS_PATH, {"ideas": []})
        item  = _find_idea_by_message_id(idata, message_id)

        if data == "idea_x":
            _handle_idea_x(callback, item, chat_id, message_id)
        elif data == "idea_thread":
            _handle_idea_thread(callback, item, chat_id, message_id)
        elif data == "idea_substack":
            _handle_idea_substack(callback, item, chat_id, message_id)
        elif data == "skip" or data.startswith("skip:"):
            _handle_idea_skip(callback, item, chat_id, message_id)

        _save(IDEAS_PATH, idata)

    elif kind == "reply":
        repq = _load(REPLY_QUEUE_PATH, {"replies": []})
        item = _find_reply_by_message_id(repq, message_id)

        if data == "yes" or data.startswith("yes:"):
            _handle_reply_yes(callback, item, chat_id, message_id)
        elif data == "skip" or data.startswith("skip:"):
            _handle_reply_skip(callback, item, chat_id, message_id)
        elif data.startswith("reason:"):
            parts  = data.split(":")
            reason = parts[1] if len(parts) >= 2 else ""
            label_map = {"topic": "Bad target", "xq": "Bad reply"}
            _log_feedback({
                "topic":           f"Reply to {item.get('tweet_author','')}",
                "source_url":      item.get("tweet_url", ""),
                "x_post":          item.get("reply_text", ""),
                "substack_note":   "",
                "generated_at":    item.get("generated_at"),
            }, reason)
            item["status"]     = f"skipped_{reason}"
            item["skipped_at"] = datetime.now(timezone.utc).isoformat()
            _tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Logged"})
            _tg("editMessageText", {
                "chat_id":    chat_id,
                "message_id": message_id,
                "text":       (
                    f"❌ Skipped reply to <b>{_esc(item.get('tweet_author',''))}</b>\n\n"
                    f"Reason: {label_map.get(reason, reason)}"
                ),
                "parse_mode": "HTML",
            })

        _save(REPLY_QUEUE_PATH, repq)


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
        elif "message" in u:
            try:
                if _process_message(u["message"]):
                    print(f"Feedback logged from message #{u['message']['message_id']}")
            except Exception as e:
                print(f"Message error: {e}")

    _save(OFFSET_PATH, {"offset": new_offset})
    print(f"Advanced offset to {new_offset}")


if __name__ == "__main__":
    poll()
