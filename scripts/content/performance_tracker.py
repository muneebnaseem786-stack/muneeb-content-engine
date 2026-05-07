"""Auto-attribute Reaction Radar reactions to posted X tweets via Nitter.

ENGAGEMENT TRACKING IS DISABLED (2026-05-07) per user decision Option C:
defer until 5K X followers. Reasons engagement tracking is hard right now:
  - X API free tier blocks ALL tweet read endpoints (get_tweet → 401,
    get_users_tweets → 401)
  - X API Basic costs $100/mo
  - Nitter individual tweet pages broken across all 4 instances:
    poast.org → 403, nitter.net → 0 bytes, privacydev.net → DNS dead,
    nitter.it → 200 but no parseable engagement HTML

This script keeps the cheap part — Nitter profile RSS scrape that maps
posted tweets back to Reaction Radar queue entries via fuzzy text match.
Useful because:
  - Free (Nitter profile RSS works fine, proven)
  - Builds the reaction → tweet_id mapping Phase 3 will need later
  - Manual metrics paste fallback in poller.py can still flow into the
    queue keyed off attributed tweet_id
  - Zero ongoing cost

When we eventually flip engagement tracking back on (Option A or hybrid),
the attribution data will already be there.
"""

import os
import re
import json
from datetime import datetime, timezone, timedelta

LOG_PATH      = "data/performance_log.json"
QUEUE_PATH    = "data/reaction_queue.json"
SOURCES_PATH  = "data/reaction_sources.json"

NITTER_LOOKBACK_DAYS = 7
ATTRIBUTION_MATCH_CHARS = 50
MY_HANDLE = "MuneebNaseem"


def _normalize(s: str, n: int = ATTRIBUTION_MATCH_CHARS) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())[:n]


def _load_nitter_instances() -> list[str]:
    try:
        with open(SOURCES_PATH) as f:
            cfg = json.load(f).get("config", {})
        return cfg.get("nitter_instances", ["nitter.poast.org"])
    except Exception:
        return ["nitter.poast.org"]


def _auto_attribute_via_nitter(queue: dict) -> int:
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

    candidates = [
        (entry, _normalize(entry.get("x_post", "")))
        for entry in unmatched
    ]
    candidates = [(e, fp) for (e, fp) in candidates if fp]

    new_matches = 0
    for tweet in my_posts:
        tweet_norm = _normalize(tweet["text"])
        if not tweet_norm:
            continue
        for entry, fp in candidates:
            if entry.get("posted_tweet_id"):
                continue
            if (tweet_norm[:ATTRIBUTION_MATCH_CHARS] == fp
                or fp in tweet_norm[:200]):
                entry["posted_tweet_id"]    = tweet["tweet_id"]
                entry["posted_tweet_url"]   = tweet["url"]
                entry["posted_at"]          = tweet["published_at"]
                entry["attribution_method"] = "nitter_auto"
                new_matches += 1
                print(f"  ✓ Matched: '{tweet_norm[:60]}...' → reaction msg #{entry.get('telegram_message_id')}")
                break

    print(f"  Auto-attributed {new_matches} reactions via Nitter")
    return new_matches


def fetch_metrics(log_path: str = LOG_PATH) -> None:
    print("Performance tracker — attribution-only mode (engagement deferred until 5K followers)")

    try:
        with open(QUEUE_PATH) as f:
            queue = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"Could not load {QUEUE_PATH}; nothing to track.")
        return

    new_attribs = _auto_attribute_via_nitter(queue)

    queue["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)

    # Write a minimal log so the artifact still updates each run
    posts = queue.get("posts", [])
    attributed = [p for p in posts if p.get("status") == "auto_sent" and p.get("posted_tweet_id")]
    summary = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "mode": "attribution_only",
        "total_auto_sent": sum(1 for p in posts if p.get("status") == "auto_sent"),
        "attributed": len(attributed),
        "attributed_ids": [p["posted_tweet_id"] for p in attributed],
    }
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Done. Auto-attributed {new_attribs} new reactions this run. Total attributed: {len(attributed)}.")
