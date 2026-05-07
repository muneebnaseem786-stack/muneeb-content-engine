"""Auto-attribute Reaction Radar reactions to posted X tweets via Nitter,
then refresh engagement metrics via X API free tier.

Pipeline (revised 2026-05-07 — fully automated, no manual URL paste):

  1. Nitter profile RSS scrape of @MuneebNaseem (last 7 days). Gets tweet IDs
     and text. No X API needed for this step — Nitter is free and we already
     use it for the 189-account source list.

  2. Fuzzy-match each Nitter-fetched tweet against unmatched auto_sent
     reactions in data/reaction_queue.json (50-char normalized fingerprint
     on x_post text). When matched, write posted_tweet_id back into the
     queue entry.

  3. For each queue entry with posted_tweet_id and stale/missing metrics,
     call client.get_tweet(id) (X API free tier, rate-limited but workable
     at our volume) to fetch fresh public_metrics.

  4. Compute engagement_score = 1×likes + 2×replies + 3×reposts + 5×quotes
     + 0.5×bookmarks. Quotes weighted highest because a QT-of-our-post = a
     fresh take from someone with their own audience extending us.

  5. Refresh metrics daily (nightly cron) for any tweet posted within the
     last 30 days. Older tweets effectively freeze.

Manual fallback: poller.py also detects pasted x.com URLs in Telegram replies
and writes posted_tweet_id directly. Used if Nitter scrape misses an edit
or there's an attribution gap.
"""

import os
import re
import json
import tweepy
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOG_PATH      = "data/performance_log.json"
QUEUE_PATH    = "data/reaction_queue.json"
SOURCES_PATH  = "data/reaction_sources.json"

# Refresh metrics for tweets posted in the last N days. Older tweets freeze.
REFRESH_LOOKBACK_DAYS = 30

# How far back to scrape my own profile via Nitter for auto-attribution.
# Longer than source-account fetch window so missed days can backfill.
NITTER_LOOKBACK_DAYS = 7

# Match window for fuzzy attribution between scraped tweets and queued reactions.
# 50 chars covers the load-bearing thesis sentence — distinctive across reactions,
# tolerates light edits to the QT text.
ATTRIBUTION_MATCH_CHARS = 50

# My X handle. Hardcoded since this script is single-user.
MY_HANDLE = "MuneebNaseem"

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


def _normalize(s: str, n: int = ATTRIBUTION_MATCH_CHARS) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())[:n]


def _engagement_score(metrics: dict) -> float:
    return sum(
        weight * float(metrics.get(key, 0) or 0)
        for key, weight in ENGAGEMENT_WEIGHTS.items()
    )


def _is_within_lookback(iso_str: str | None) -> bool:
    if not iso_str:
        return True
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(days=REFRESH_LOOKBACK_DAYS)


def _load_nitter_instances() -> list[str]:
    try:
        with open(SOURCES_PATH) as f:
            cfg = json.load(f).get("config", {})
        return cfg.get("nitter_instances", ["nitter.poast.org"])
    except Exception:
        return ["nitter.poast.org"]


def _auto_attribute_via_nitter(queue: dict) -> int:
    """Scrape my own recent X posts via Nitter, fuzzy-match against unmatched
    auto_sent reactions, write posted_tweet_id when matched. Returns count
    of new attributions.
    """
    # Late import to avoid Tweepy/Nitter dep coupling at module load
    from scripts.content.sources import fetch_my_recent_posts

    instances = _load_nitter_instances()
    print(f"Scraping @{MY_HANDLE} recent posts via Nitter...")
    my_posts = fetch_my_recent_posts(
        handle=MY_HANDLE,
        nitter_instances=instances,
        lookback_days=NITTER_LOOKBACK_DAYS,
    )
    print(f"  Found {len(my_posts)} recent posts in last {NITTER_LOOKBACK_DAYS} days")

    if not my_posts:
        return 0

    unmatched = [
        p for p in queue.get("posts", [])
        if p.get("status") == "auto_sent" and not p.get("posted_tweet_id")
    ]
    if not unmatched:
        print("  No unmatched reactions in queue.")
        return 0

    # Build normalized fingerprints for each unmatched reaction
    candidates = [
        (entry, _normalize(entry.get("x_post", "")))
        for entry in unmatched
    ]
    candidates = [(e, fp) for (e, fp) in candidates if fp]

    new_matches = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for tweet in my_posts:
        tweet_norm = _normalize(tweet["text"])
        if not tweet_norm:
            continue
        for entry, fp in candidates:
            if entry.get("posted_tweet_id"):
                continue  # already matched in this pass
            # Match if tweet leading chars equal fingerprint OR fingerprint
            # appears within first 200 chars (handles QT prefix).
            if (tweet_norm[:ATTRIBUTION_MATCH_CHARS] == fp
                or fp in tweet_norm[:200]):
                entry["posted_tweet_id"]  = tweet["tweet_id"]
                entry["posted_tweet_url"] = tweet["url"]
                entry["posted_at"]        = tweet["published_at"]
                entry["attribution_method"] = "nitter_auto"
                new_matches += 1
                print(f"  ✓ Matched: '{tweet_norm[:60]}...' → reaction msg #{entry.get('telegram_message_id')}")
                break

    print(f"  Auto-attributed {new_matches} reactions via Nitter")
    return new_matches


def _refresh_engagement_via_api(client: tweepy.Client, queue: dict) -> tuple[int, int]:
    """For each queue entry with posted_tweet_id within lookback, call
    client.get_tweet(id) for fresh metrics. Returns (refreshed, errors).
    """
    targets = [
        p for p in queue.get("posts", [])
        if p.get("posted_tweet_id") and _is_within_lookback(p.get("posted_at"))
    ]
    print(f"Refreshing engagement for {len(targets)} attributed reaction(s)...")

    refreshed = 0
    errors = 0
    fetched_summaries = []
    now_iso = datetime.now(timezone.utc).isoformat()

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

            fetched_summaries.append({
                "id":          str(tweet.id),
                "url":         entry.get("posted_tweet_url",""),
                "platform":    "X",
                "text":        (tweet.text or "")[:280],
                "topic":       entry.get("topic", ""),
                "posted_at":   entry.get("posted_at"),
                "metrics":     metrics,
                "engagement_score": entry["engagement_score"],
                "metrics_updated_at": now_iso,
            })
            score = entry["engagement_score"]
            print(f"  [{idx}/{len(targets)}] {tweet_id}: score {score:.1f} | likes {metrics.get('like_count',0)} | quotes {metrics.get('quote_count',0)}")
            refreshed += 1
        except tweepy.errors.NotFound:
            print(f"  [{idx}/{len(targets)}] {tweet_id}: tweet not found (deleted?)")
            errors += 1
        except tweepy.errors.TooManyRequests:
            print(f"  [{idx}/{len(targets)}] {tweet_id}: rate limited; bailing on remainder")
            break
        except Exception as e:
            print(f"  [{idx}/{len(targets)}] {tweet_id}: {type(e).__name__}: {e}")
            errors += 1

    fetched_summaries.sort(key=lambda p: p.get("posted_at") or "", reverse=True)
    _write_summary_log(LOG_PATH, fetched_summaries)
    return refreshed, errors


def fetch_metrics(log_path: str = LOG_PATH) -> None:
    client = _get_client()
    me = client.get_me()
    if not me or not me.data:
        print("ERROR: get_me() returned no user. Check OAuth credentials.")
        return
    print(f"Authenticated as @{me.data.username}")

    try:
        with open(QUEUE_PATH) as f:
            queue = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"Could not load {QUEUE_PATH}; nothing to track.")
        return

    # Step 1: auto-attribute via Nitter scraping of my own profile
    new_attribs = _auto_attribute_via_nitter(queue)

    # Step 2: refresh engagement metrics via X API for attributed entries
    refreshed, errors = _refresh_engagement_via_api(client, queue)

    queue["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)

    print(f"Done. Auto-attributed {new_attribs}, refreshed {refreshed}, {errors} errors.")


def _write_summary_log(path: str, posts: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "posts":      posts,
        }, f, indent=2)
