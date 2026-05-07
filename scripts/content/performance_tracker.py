"""Fetch X engagement metrics for tweets that the user has explicitly reported
back via Telegram (poster URL paste).

Architecture (revised 2026-05-07 after X API free tier paywalled
GET /2/users/:id/tweets):

  1. User QTs a Reaction Radar tweet on X.
  2. User pastes the resulting tweet URL as a Telegram reply to the bot's
     reaction context message. scripts/telegram/poller.py extracts the tweet
     ID and writes posted_tweet_id into data/reaction_queue.json.
  3. This tracker iterates queue entries that have posted_tweet_id but stale
     or missing engagement_metrics, calls client.get_tweet(id) per entry
     (free-tier endpoint), and writes engagement_score back to the queue.

Free X API tier rate limit on get_tweet is 1 request per 15 minutes per user
auth context — plenty for ~4-6 reactions/day.

Daily refresh: re-fetches every entry's metrics so engagement compounds over
the lookback window.
"""

import os
import json
import time
import tweepy
from datetime import datetime, timezone, timedelta

LOG_PATH    = "data/performance_log.json"
QUEUE_PATH  = "data/reaction_queue.json"

# Refresh metrics for tweets posted in the last N days. Older tweets are
# effectively frozen and not re-fetched.
REFRESH_LOOKBACK_DAYS = 30

# Engagement score weights — proxy for impressions/profile clicks which
# require X API Pro tier. Quotes weighted highest because a QT of your post
# means someone with their own audience extended your take — that's the
# actual reach mechanism, not surface engagement.
ENGAGEMENT_WEIGHTS = {
    "like_count":     1.0,
    "reply_count":    2.0,
    "retweet_count":  3.0,
    "quote_count":    5.0,
    "bookmark_count": 0.5,
}


def _get_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=True,
    )


def _engagement_score(metrics: dict) -> float:
    return sum(
        weight * float(metrics.get(key, 0) or 0)
        for key, weight in ENGAGEMENT_WEIGHTS.items()
    )


def _is_within_lookback(iso_str: str | None) -> bool:
    if not iso_str:
        return True  # no posted_at → assume recent, fetch
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(days=REFRESH_LOOKBACK_DAYS)


def fetch_metrics(log_path: str = LOG_PATH) -> None:
    """Refresh engagement metrics for all queue entries with posted_tweet_id."""
    client = _get_client()

    me = client.get_me()
    if not me or not me.data:
        print("ERROR: get_me() returned no user. Check OAuth credentials.")
        return
    handle = me.data.username
    print(f"Authenticated as @{handle}")

    try:
        with open(QUEUE_PATH) as f:
            queue = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"Could not load {QUEUE_PATH}; nothing to track.")
        return

    targets = [
        p for p in queue.get("posts", [])
        if p.get("posted_tweet_id") and _is_within_lookback(p.get("posted_at"))
    ]
    print(f"Found {len(targets)} reaction(s) with posted_tweet_id within lookback.")

    if not targets:
        _write_summary_log(log_path, [])
        return

    fetched_posts = []
    now_iso = datetime.now(timezone.utc).isoformat()
    errors = 0

    for idx, entry in enumerate(targets, 1):
        tweet_id = entry["posted_tweet_id"]
        try:
            response = client.get_tweet(
                id=tweet_id,
                tweet_fields=["public_metrics", "created_at", "text"],
            )
            if not response or not response.data:
                print(f"  [{idx}/{len(targets)}] {tweet_id}: no data returned")
                errors += 1
                continue
            tweet = response.data
            metrics = dict(tweet.public_metrics or {})

            entry["engagement_metrics"]   = metrics
            entry["engagement_score"]     = _engagement_score(metrics)
            entry["metrics_updated_at"]   = now_iso
            if not entry.get("posted_at") and tweet.created_at:
                entry["posted_at"] = tweet.created_at.isoformat()

            fetched_posts.append({
                "id":          str(tweet.id),
                "url":         f"https://x.com/{handle}/status/{tweet.id}",
                "platform":    "X",
                "text":        tweet.text or "",
                "format":      _classify_format(tweet.text or ""),
                "posted_at":   entry.get("posted_at"),
                "metrics":     metrics,
                "engagement_score": entry["engagement_score"],
                "topic":       entry.get("topic", ""),
                "metrics_updated_at": now_iso,
            })

            score = entry["engagement_score"]
            print(f"  [{idx}/{len(targets)}] {tweet_id}: score {score:.1f} | likes {metrics.get('like_count',0)} | quotes {metrics.get('quote_count',0)}")
        except tweepy.errors.NotFound:
            print(f"  [{idx}/{len(targets)}] {tweet_id}: tweet not found (deleted?)")
            errors += 1
        except tweepy.errors.TooManyRequests:
            print(f"  [{idx}/{len(targets)}] {tweet_id}: rate limited; bailing on remainder")
            break
        except Exception as e:
            print(f"  [{idx}/{len(targets)}] {tweet_id}: {type(e).__name__}: {e}")
            errors += 1
            continue

    queue["last_updated"] = now_iso
    with open(QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)

    fetched_posts.sort(key=lambda p: p.get("posted_at") or "", reverse=True)
    _write_summary_log(log_path, fetched_posts)
    print(f"Done. Refreshed {len(fetched_posts)} entries, {errors} errors.")


def _classify_format(text: str) -> str:
    if "1/" in text or "🧵" in text:
        return "Thread"
    word_count = len(text.split())
    if word_count <= 40:
        return "Reaction post"
    if word_count <= 120:
        return "Single tweet"
    return "Long-form post"


def _write_summary_log(path: str, posts: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "posts":      posts,
        }, f, indent=2)
