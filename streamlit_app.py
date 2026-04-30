"""Content Studio — Muneeb Naseem's content engine for X / LinkedIn / Substack."""

import base64
import json
import os
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

GITHUB_REPO         = "muneebnaseem786-stack/muneeb-content-engine"
FEEDBACK_REPO_PATH  = "data/feedback_log.json"

st.set_page_config(
    page_title="Content Studio",
    page_icon="✍️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Content Studio")
    st.caption("X · LinkedIn · Substack")
    st.divider()

    st.markdown(
        "**Pipeline status**\n\n"
        "- 💡 Daily Ideas — *7am UAE*\n"
        "- ⚡ Reaction Radar — *every 2h, 6am-10pm UAE*\n"
        "- 📈 Performance — *11pm UAE*"
    )

    st.divider()

    if st.button("↻  Refresh", use_container_width=True):
        st.rerun()

    st.caption(f"Refreshed: {datetime.now(timezone.utc).strftime('%d %b %Y · %H:%M UTC')}")

    st.divider()
    st.caption("Routines:")
    st.markdown("- [Daily Ideas](https://claude.ai/code/routines/trig_013MbtySdMYJ5e5JifZbrS6n)")
    st.markdown("- [Reaction Radar](https://claude.ai/code/routines/trig_01EsQ85PvcfKUhcehMrWqb4D)")

# ── Header KPIs ───────────────────────────────────────────────────────────────

def _load_json(path: str, default: dict) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

ideas_data    = _load_json("data/content_ideas.json",   {"ideas": [], "generated_at": ""})
queue_data    = _load_json("data/reaction_queue.json",  {"posts": [], "last_updated": ""})
perf_data     = _load_json("data/performance_log.json", {"posts": []})
feedback_data = _load_json("data/feedback_log.json",    {"feedback": []})


def _record_feedback(post: dict, reason: str) -> None:
    """Append a skip-reason entry and persist (GitHub commit + local fallback)."""
    entry = {
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "platform":          post.get("platform", ""),
        "topic":             post.get("topic", ""),
        "source_headline":   post.get("source_headline", ""),
        "source_url":        post.get("source_url", ""),
        "post_preview":      (post.get("content", "")[:200]),
        "reason":            reason,   # "topic" | "post_quality"
        "generated_at":      post.get("generated_at", ""),
    }
    feedback_data["feedback"].append(entry)

    os.makedirs("data", exist_ok=True)
    with open("data/feedback_log.json", "w") as f:
        json.dump(feedback_data, f, indent=2)

    committed = _commit_feedback_to_github(feedback_data)
    if not committed:
        st.toast("⚠️ Saved locally. (GitHub commit unavailable — feedback may not survive redeploy.)", icon="⚠️")


def _commit_feedback_to_github(payload: dict) -> bool:
    """Push the updated feedback log to the repo via GitHub API."""
    token = st.secrets.get("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN"))
    if not token:
        return False

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FEEDBACK_REPO_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github+json",
        "User-Agent":    "muneeb-content-engine",
    }

    try:
        get_resp = requests.get(api_url, headers=headers, timeout=10)
        sha      = get_resp.json().get("sha") if get_resp.status_code == 200 else None

        content_b64 = base64.b64encode(json.dumps(payload, indent=2).encode("utf-8")).decode("utf-8")
        body = {
            "message": "chore: skip-reason feedback [skip ci]",
            "content": content_b64,
            "committer": {"name": "Content Studio", "email": "noreply@anthropic.com"},
        }
        if sha:
            body["sha"] = sha

        put_resp = requests.put(api_url, headers=headers, json=body, timeout=10)
        return put_resp.status_code in (200, 201)
    except requests.RequestException:
        return False

ideas         = ideas_data.get("ideas", [])
queue_posts   = queue_data.get("posts", [])
perf_posts    = perf_data.get("posts", [])
pending_count = sum(1 for p in queue_posts if p.get("status") == "pending")

k1, k2, k3, k4 = st.columns(4)
with k1: st.metric("Ideas Today",       str(len(ideas)))
with k2: st.metric("Pending Reactions", str(pending_count))
with k3: st.metric("Tweets Tracked",    str(len(perf_posts)))
with k4: st.metric("Total Likes (30d)", str(sum(p.get("metrics", {}).get("like_count", 0) for p in perf_posts)))

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────

sub_ideas, sub_queue, sub_perf = st.tabs(["💡 Ideas", "⚡ Reaction Queue", "📈 Performance"])

# ══════════════════════════════════════════════════════════════════════════════
# IDEAS (Form 1)
# ══════════════════════════════════════════════════════════════════════════════

with sub_ideas:
    st.markdown("### Today's Content Ideas")
    st.caption("Generated daily at 7am UAE. Full content packs pre-built for each idea.")

    gen_at    = ideas_data.get("generated_at", "")[:16].replace("T", " ")
    art_count = ideas_data.get("article_count", 0)
    if ideas:
        st.caption(f"Generated: {gen_at} UTC  ·  {art_count} articles scanned  ·  {len(ideas)} ideas")
    else:
        st.info("No ideas yet. The Daily Ideas agent runs at 7am UAE — or trigger it manually from the routines link in the sidebar.")

    urgency_icon = {"breaking": "🔴", "timely": "🟡", "evergreen": "🟢"}

    for i, idea in enumerate(ideas):
        icon = urgency_icon.get(idea.get("urgency", "timely"), "🟡")
        with st.expander(f"{icon} {idea.get('title', 'Idea')}", expanded=(i == 0)):
            c1, c2 = st.columns([1, 2])

            with c1:
                st.markdown(f"**Trend:** {idea.get('trend', '')}")
                st.markdown(f"**Consensus:** {idea.get('consensus', '')}")
                st.markdown(f"**Our angle:** {idea.get('angle', '')}")
                st.markdown(f"**Urgency:** {icon} {idea.get('urgency', '').upper()}")
                st.markdown(f"**Pillar:** {idea.get('pillar', '')}")

            with c2:
                pack = idea.get("content_pack", {})
                if pack and "raw" not in pack:
                    pt1, pt2, pt3, pt4 = st.tabs(["X Post", "X Thread", "LinkedIn", "Substack"])

                    with pt1:
                        xp = pack.get("x_longform", "")
                        st.text_area("Copy and post on X:", xp, height=240, key=f"xp_{i}")
                        st.caption(f"{len(xp.split())} words")

                    with pt2:
                        thread = pack.get("x_thread", [])
                        for j, tw in enumerate(thread):
                            st.text_area(f"Tweet {j+1} ({len(tw)} chars)", tw, height=90, key=f"tw_{i}_{j}")

                    with pt3:
                        li = pack.get("linkedin", "")
                        st.text_area("Copy and post on LinkedIn:", li, height=240, key=f"li_{i}")

                    with pt4:
                        ss = pack.get("substack_draft", "")
                        st.text_area("Substack outline:", ss, height=280, key=f"ss_{i}")
                elif pack and "raw" in pack:
                    st.text_area("Generated content (parse error — raw):", pack["raw"], height=320, key=f"raw_{i}")
                else:
                    st.info("Content pack not generated yet.")

# ══════════════════════════════════════════════════════════════════════════════
# REACTION QUEUE (Form 2)
# ══════════════════════════════════════════════════════════════════════════════

with sub_queue:
    import urllib.parse

    st.markdown("### Reaction Queue")
    st.caption("Updated every 2 hours, 6am–10pm UAE. Review the source, decide if you like the take, post in one click.")

    last_upd = queue_data.get("last_updated", "")[:16].replace("T", " ")
    if queue_posts:
        st.caption(f"Last updated: {last_upd} UTC")
    else:
        st.info("No reaction posts yet. The radar runs every 2 hours during UAE business hours.")

    pending = [p for p in queue_posts if p.get("status") == "pending" and not st.session_state.get(f"hidden_{p.get('generated_at', '')}_{p.get('topic', '')}")]

    if not pending and queue_posts:
        st.success("Queue is empty — nothing pending right now.")
    elif pending:
        st.markdown(f"**{len(pending)} stories pending**")
        st.caption("Each story has both an X version (punchy) and a Substack Note (reflective). Post one, both, or skip.")
        st.divider()

    for i, post in enumerate(pending):
        topic      = post.get("topic", "")
        src_text   = post.get("source_headline", "")
        src_url    = post.get("source_url", "")
        ts         = post.get("generated_at", "")[:16].replace("T", " ")
        unique_key = f"{post.get('generated_at', '')}_{topic}"

        # Schema migration: paired (x_post + substack_note) is new; legacy is single (content + platform)
        x_text       = post.get("x_post") or (post.get("content", "") if post.get("platform") == "X" else "")
        substack_text = post.get("substack_note") or (post.get("content", "") if post.get("platform") == "substack_note" else "")

        with st.container(border=True):
            st.markdown(f"**📡 {topic}** · _{ts} UTC_")
            if src_url and src_text:
                st.markdown(f"📰 [{src_text}]({src_url})")
            elif src_text:
                st.caption(f"Source: {src_text}")

            col_x, col_sub = st.columns(2)

            with col_x:
                if x_text:
                    st.markdown("##### 🐦 X (punchy)")
                    st.text_area("X post:", x_text, height=140, key=f"x_{i}", label_visibility="collapsed")
                    intent_url = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(x_text)}"
                    bcol1, bcol2 = st.columns(2)
                    with bcol1:
                        st.link_button("🚀 Post", intent_url, use_container_width=True, type="primary")
                    with bcol2:
                        if st.button("📋", key=f"copy_x_{i}", use_container_width=True, help="Show as copyable code"):
                            st.session_state[f"copied_x_{i}"] = True
                    if st.session_state.get(f"copied_x_{i}"):
                        st.code(x_text, language=None)

            with col_sub:
                if substack_text:
                    st.markdown("##### 📧 Substack Note (reflective)")
                    st.text_area("Substack note:", substack_text, height=200, key=f"sub_{i}", label_visibility="collapsed")
                    bcol3, bcol4 = st.columns(2)
                    with bcol3:
                        st.link_button("📝 Open", "https://substack.com/notes", use_container_width=True, type="primary")
                    with bcol4:
                        if st.button("📋", key=f"copy_sub_{i}", use_container_width=True, help="Show as copyable code"):
                            st.session_state[f"copied_sub_{i}"] = True
                    if st.session_state.get(f"copied_sub_{i}"):
                        st.code(substack_text, language=None)

            st.markdown("")
            if st.button("❌ Skip story", key=f"skip_{i}", use_container_width=True):
                st.session_state[f"asking_reason_{i}"] = True

            if st.session_state.get(f"asking_reason_{i}"):
                st.caption("Why skip? Helps train the radar.")
                rc1, rc2, rc3, rc4 = st.columns(4)
                with rc1:
                    if st.button("🚫 Topic", key=f"reason_topic_{i}", use_container_width=True, help="Story not for me"):
                        _record_feedback(post, "topic")
                        st.session_state[f"hidden_{unique_key}"] = True
                        st.session_state[f"asking_reason_{i}"] = False
                        st.rerun()
                with rc2:
                    if st.button("✏️ X quality", key=f"reason_x_{i}", use_container_width=True, help="X post writing failed"):
                        _record_feedback(post, "x_quality")
                        st.session_state[f"hidden_{unique_key}"] = True
                        st.session_state[f"asking_reason_{i}"] = False
                        st.rerun()
                with rc3:
                    if st.button("✏️ Substack quality", key=f"reason_sub_{i}", use_container_width=True, help="Substack note writing failed"):
                        _record_feedback(post, "substack_quality")
                        st.session_state[f"hidden_{unique_key}"] = True
                        st.session_state[f"asking_reason_{i}"] = False
                        st.rerun()
                with rc4:
                    if st.button("← Cancel", key=f"reason_cancel_{i}", use_container_width=True):
                        st.session_state[f"asking_reason_{i}"] = False
                        st.rerun()

    # ── FEEDBACK INSIGHTS ─────────────────────────────────────────────────
    feedback_log = feedback_data.get("feedback", [])
    if feedback_log:
        st.divider()
        with st.expander(f"📊 Feedback Insights ({len(feedback_log)} skips logged)"):
            df_fb = pd.DataFrame(feedback_log)

            f1, f2 = st.columns(2)
            with f1:
                st.markdown("**Skip reasons**")
                reason_counts = df_fb["reason"].value_counts()
                reason_counts.index = reason_counts.index.map(
                    {"topic": "🚫 Topic not for me", "post_quality": "✏️ Post quality"}
                )
                st.bar_chart(reason_counts)

            with f2:
                st.markdown("**Most-skipped topics**")
                top_topics = df_fb["topic"].value_counts().head(8)
                st.dataframe(
                    top_topics.reset_index().rename(columns={"index": "Topic", "count": "Skips"}),
                    use_container_width=True,
                    hide_index=True,
                )

            st.caption(
                "Topic skips = the radar is reading your interests wrong. "
                "Quality skips = the radar's writing needs better prompting."
            )

# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════

with sub_perf:
    st.markdown("### X Performance")
    st.caption("Auto-fetched nightly from your X account. No manual entry — every original tweet from the last 30 days is tracked automatically.")

    fetched_at = perf_data.get("fetched_at", "")[:16].replace("T", " ")
    if perf_posts:
        st.caption(f"Last refreshed: {fetched_at} UTC  ·  {len(perf_posts)} tweets tracked")
    else:
        st.info("No tweets tracked yet. The tracker runs nightly at 11pm UAE — or trigger the GitHub Actions workflow manually.")

    if perf_posts:
        rows = []
        for p in perf_posts:
            m = p.get("metrics", {})
            rows.append({
                "Posted":  p.get("posted_at", "")[:10],
                "Format":  p.get("format", ""),
                "Tweet":   (p.get("text", "")[:120] + ("…" if len(p.get("text", "")) > 120 else "")),
                "Likes":   m.get("like_count", 0),
                "Replies": m.get("reply_count", 0),
                "Reposts": m.get("retweet_count", 0),
                "Quotes":  m.get("quote_count", 0),
            })
        df_perf = pd.DataFrame(rows)

        st.dataframe(df_perf, use_container_width=True, hide_index=True)

        if len(perf_posts) >= 3:
            st.markdown("### What's Working")

            df_perf["Engagement"] = df_perf["Likes"] + df_perf["Replies"] * 2 + df_perf["Reposts"] * 3

            col_fmt, col_top = st.columns(2)
            with col_fmt:
                st.markdown("**Avg engagement by format**")
                by_format = df_perf.groupby("Format")["Engagement"].mean().sort_values(ascending=False)
                st.bar_chart(by_format)

            with col_top:
                st.markdown("**Top 5 tweets by engagement**")
                top = df_perf.nlargest(5, "Engagement")[["Tweet", "Likes", "Replies", "Reposts"]]
                st.dataframe(top, use_container_width=True, hide_index=True)

        st.caption("Engagement = likes + 2×replies + 3×reposts. (Replies and reposts are stronger algorithm signals on X.)")
