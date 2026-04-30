"""Auto-fetches X engagement metrics for the authenticated user's recent tweets.

No manual tweet-ID entry required. Pulls all recent tweets from the user
behind the OAuth credentials, fetches public metrics (likes, replies,
reposts, quotes), and writes them to data/performance_log.json.
"""

import os
import json
import tweepy
from datetime import datetime, timezone, timedelta

LOOKBACK_DAYS = 30
LOG_PATH      = "data/performance_log.json"


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


def _write_log(path: str, posts: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "posts":      posts,
        }, f, indent=2)
