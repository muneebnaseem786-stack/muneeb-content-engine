"""Fetches X engagement metrics for tracked posts."""

import os
import json
import tweepy
from datetime import datetime, timezone


def _get_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=True,
    )


def fetch_metrics(log_path: str = "data/performance_log.json") -> None:
    """Update performance_log.json with latest X metrics for all tracked tweets."""
    try:
        with open(log_path) as f:
            log = json.load(f)
    except FileNotFoundError:
        print(f"{log_path} not found — nothing to track")
        return

    posts = log.get("posts", [])
    x_posts = [p for p in posts if p.get("platform") == "X" and p.get("id")]

    if not x_posts:
        print("No X posts to track")
        return

    client = _get_client()
    tweet_ids = [p["id"] for p in x_posts]

    try:
        response = client.get_tweets(
            ids=tweet_ids,
            tweet_fields=["public_metrics"],
        )
    except Exception as e:
        print(f"Twitter API error: {e}")
        return

    if not response.data:
        print("No tweet data returned")
        return

    metrics_by_id = {str(t.id): t.public_metrics for t in response.data}

    for post in posts:
        tweet_id = str(post.get("id", ""))
        if tweet_id in metrics_by_id:
            post["metrics"] = metrics_by_id[tweet_id]
            post["metrics_updated_at"] = datetime.now(timezone.utc).isoformat()

    log["posts"] = posts
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)

    print(f"Updated metrics for {len(metrics_by_id)} tweets")
