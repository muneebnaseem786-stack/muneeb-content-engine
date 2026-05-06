"""Auto-fetches X engagement metrics for the authenticated user's recent tweets.

Pulls all recent tweets from the user behind the OAuth credentials, fetches
public metrics (likes, replies, reposts, quotes, bookmarks), writes them to
data/performance_log.json, then attributes matched tweets back to
data/reaction_queue.json so Reaction Radar entries gain a posted_tweet_id
and engagement_score for Phase 3 source-account analysis.
"""

import os
import re
import json
import tweepy
from datetime import datetime, timezone, timedelta

LOOKBACK_DAYS = 30
LOG_PATH      = "data/performance_log.json"
QUEUE_PATH    = "data/reaction_queue.json"

# Engagement score weights — proxy for impressions/profile clicks which require
# X API Pro tier. Quotes weighted highest because they signal "this drove a
# fresh take from someone else" which is the actual reach mechanism.
ENGAGEMENT_WEIGHTS = {
    "like_count":     1.0,
    "reply_count":    2.0,
    "retweet_count":  3.0,
    "quote_count":    5.0,
    "bookmark_count": 0.5,
}

# How many characters of the leading text to compare for attribution.
# Reaction Radar generates 230-330 word posts — the first ~50 chars are the
# load-bearing thesis sentence and almost never collide across reactions.
# Match window of 50 chars tolerates light user edits to the QT text.
ATTRIBUTION_MATCH_CHARS = 50


def _get_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=True,
    )


def fetch_metrics(log_path: str = LOG_PATH) -> None:
    """Pull the authenticated user's recent tweets with engagement metrics."""
    client = _get_client()

    me = client.get_me()
    if not me or not me.data:
        print("ERROR: get_me() returned no user. Check OAuth credentials.")
        return
    user_id = me.data.id
    handle  = me.data.username
    print(f"Authenticated as @{handle} (id={user_id})")

    start_time = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    response = client.get_users_tweets(
        id=user_id,
        max_results=100,
        start_time=start_time,
        tweet_fields=["public_metrics", "created_at", "text"],
        exclude=["retweets", "replies"],
    )

    if not response.data:
        print(f"No tweets from @{handle} in the last {LOOKBACK_DAYS} days.")
        _write_log(log_path, [])
        return

    posts = []
    for tweet in response.data:
        text = tweet.text or ""
        posts.append({
            "id":                 str(tweet.id),
            "url":                f"https://x.com/{handle}/status/{tweet.id}",
            "platform":           "X",
            "text":               text,
            "format":             _classify_format(text),
            "posted_at":          tweet.created_at.isoformat(),
            "metrics":            dict(tweet.public_metrics or {}),
            "metrics_updated_at": datetime.now(timezone.utc).isoformat(),
        })

    posts.sort(key=lambda p: p["posted_at"], reverse=True)
    _write_log(log_path, posts)
    print(f"Saved metrics for {len(posts)} tweets to {log_path}")

    # Attribute matched tweets back to reaction_queue.json so Phase 3 has
    # source-account engagement data to work with.
    _attribute_to_reactions(posts)


def _classify_format(text: str) -> str:
    """Crude heuristic for analytics buckets."""
    if "1/" in text or "🧵" in text:
        return "Thread"
    word_count = len(text.split())
    if word_count <= 40:
        return "Reaction post"
    if word_count <= 120:
        return "Single tweet"
    return "Long-form post"


def _normalize(s: str, n: int = ATTRIBUTION_MATCH_CHARS) -> str:
    """Lowercase, strip, collapse whitespace, take first n chars. Used for
    fuzzy attribution between fetched tweets and queued reactions."""
    return re.sub(r"\s+", " ", (s or "").lower().strip())[:n]


def _engagement_score(metrics: dict) -> float:
    return sum(
        weight * float(metrics.get(key, 0) or 0)
        for key, weight in ENGAGEMENT_WEIGHTS.items()
    )


def _attribute_to_reactions(posts: list[dict], queue_path: str = QUEUE_PATH) -> int:
    """Match fetched user tweets against auto_sent reactions in the queue.
    Writes posted_tweet_id + engagement metrics back into the matched queue
    entry. Also refreshes metrics for already-matched reactions so engagement
    scores update daily.

    Returns number of NEW attributions made.
    """
    try:
        with open(queue_path) as f:
            queue = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"Could not load {queue_path}; skipping attribution.")
        return 0

    posts_by_id = {p["id"]: p for p in posts}
    new_matches = 0
    refreshed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for entry in queue.get("posts", []):
        if entry.get("status") != "auto_sent":
            continue

        # Already matched → refresh metrics from latest fetch
        existing_id = entry.get("posted_tweet_id")
        if existing_id and existing_id in posts_by_id:
            tweet = posts_by_id[existing_id]
            metrics = tweet.get("metrics", {})
            entry["engagement_metrics"]   = metrics
            entry["engagement_score"]     = _engagement_score(metrics)
            entry["metrics_updated_at"]   = now_iso
            refreshed += 1
            continue
        if existing_id:
            continue  # matched but tweet outside lookback window

        # Try to match unmatched reactions to fetched tweets
        x_post_norm = _normalize(entry.get("x_post", ""))
        if not x_post_norm:
            continue

        for tweet in posts:
            tweet_norm = _normalize(tweet.get("text", ""))
            if not tweet_norm:
                continue
            # Match: tweet text starts with our x_post leading chars, OR
            # our x_post leading chars appear inside the tweet text.
            # Handles QT scenarios where user might prepend a short remark.
            if (tweet_norm[:ATTRIBUTION_MATCH_CHARS] == x_post_norm
                or x_post_norm in tweet_norm[:200]):
                metrics = tweet.get("metrics", {})
                entry["posted_tweet_id"]    = tweet["id"]
                entry["posted_tweet_url"]   = tweet["url"]
                entry["posted_at"]          = tweet["posted_at"]
                entry["engagement_metrics"] = metrics
                entry["engagement_score"]   = _engagement_score(metrics)
                entry["metrics_updated_at"] = now_iso
                new_matches += 1
                break

    if new_matches or refreshed:
        queue["last_updated"] = now_iso
        with open(queue_path, "w") as f:
            json.dump(queue, f, indent=2)

    print(f"Attribution: {new_matches} new + {refreshed} refreshed reactions linked to posted tweets")
    return new_matches


def _write_log(path: str, posts: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "posts":      posts,
        }, f, indent=2)
