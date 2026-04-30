"""Content Studio — Muneeb Naseem's content engine for X / LinkedIn / Substack."""

import json
import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

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
        st.markdown(f"**{len(pending)} posts pending**")
        st.divider()

    platform_icon = {"X": "🐦", "substack_note": "📧"}

    for i, post in enumerate(pending):
        plat       = post.get("platform", "X")
        icon       = platform_icon.get(plat, "📝")
        topic      = post.get("topic", "")
        content    = post.get("content", "")
        src_text   = post.get("source_headline", "")
        src_url    = post.get("source_url", "")
        ts         = post.get("generated_at", "")[:16].replace("T", " ")
        unique_key = f"{post.get('generated_at', '')}_{topic}"

        with st.container(border=True):
            st.markdown(f"**{icon} {plat}** · {topic} · _{ts} UTC_")

            if src_url and src_text:
                st.markdown(f"📰 **Source:** [{src_text}]({src_url})")
            elif src_text:
                st.caption(f"Source: {src_text}")

            st.text_area("Reaction post:", content, height=110 if plat == "X" else 180, key=f"rq_{i}")

            col_post, col_copy, col_skip = st.columns(3)

            if plat == "X":
                intent_url = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(content)}"
                with col_post:
                    st.link_button("🚀 Post on X", intent_url, use_container_width=True, type="primary")
            else:
                with col_post:
                    st.link_button("📝 Open Substack Notes", "https://substack.com/notes", use_container_width=True, type="primary")

            with col_copy:
                if st.button("📋 Copy", key=f"copy_{i}", use_container_width=True):
                    st.session_state[f"copied_{i}"] = True
            with col_skip:
                if st.button("❌ Skip", key=f"skip_{i}", use_container_width=True):
                    st.session_state[f"hidden_{unique_key}"] = True
                    st.rerun()

            if st.session_state.get(f"copied_{i}"):
                st.code(content, language=None)
                st.caption("Select all above to copy.")

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
