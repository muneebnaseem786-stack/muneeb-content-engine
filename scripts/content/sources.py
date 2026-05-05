"""Reaction Radar source fetchers — Nitter profile RSS + news RSS.

Profile RSS via Nitter is proven to work from GitHub Actions IPs.
Search RSS does NOT work — never use it.
"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Iterable

import requests


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ReactionRadar/1.0)"}
TIMEOUT = 8


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _parse_rss_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fetch_nitter_profile(
    handle: str,
    nitter_instances: list[str],
    max_posts: int = 2,
    min_chars: int = 80,
    lookback_hours: int = 24,
) -> list[dict]:
    """Fetch recent posts from a single X handle via Nitter profile RSS.

    Returns list of {handle, text, url, published_at, source} dicts.
    Drops retweets, replies, posts shorter than min_chars, posts older than lookback.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    for instance in nitter_instances:
        try:
            url = f"https://{instance}/{handle}/rss"
            resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            if channel is None:
                continue

            posts = []
            for item in channel.findall("item"):
                if len(posts) >= max_posts:
                    break
                title = item.findtext("title", "") or ""
                link = item.findtext("link", "") or ""
                desc = item.findtext("description", "") or ""
                pub = _parse_rss_date(item.findtext("pubDate", ""))

                if pub and pub < cutoff:
                    continue
                if "RT by" in title or title.startswith("R to "):
                    continue

                text = _strip_html(desc) or title
                if len(text) < min_chars:
                    continue

                x_url = link.replace(f"https://{instance}/", "https://x.com/")
                posts.append({
                    "handle": handle,
                    "text": text[:500],
                    "url": x_url,
                    "published_at": pub.isoformat() if pub else None,
                    "source": f"x:@{handle}",
                })

            if posts:
                return posts
        except Exception as e:
            print(f"[sources] Nitter {instance}/{handle} error: {e}")
            continue
    return []


def fetch_x_accounts(
    accounts: list[dict],
    nitter_instances: list[str],
    max_posts_per_account: int = 2,
    min_chars: int = 80,
    lookback_hours: int = 24,
    sleep_between: float = 0.4,
) -> list[dict]:
    """Iterate the curated account list and pull recent posts from each."""
    out = []
    for acct in accounts:
        handle = acct.get("handle", "").lstrip("@")
        if not handle:
            continue
        posts = fetch_nitter_profile(
            handle,
            nitter_instances,
            max_posts=max_posts_per_account,
            min_chars=min_chars,
            lookback_hours=lookback_hours,
        )
        for p in posts:
            p["tier"] = acct.get("tier", 5)
        out.extend(posts)
        time.sleep(sleep_between)
    return out


def fetch_rss_news(
    feeds: list[dict],
    lookback_hours: int = 6,
    max_per_feed: int = 5,
) -> list[dict]:
    """Pull recent items from news RSS feeds.

    Uses feedparser for resilient parsing. Returns list of
    {source, headline, url, published_at, summary} dicts.
    """
    try:
        import feedparser
    except ImportError:
        print("[sources] feedparser not installed; skipping RSS news")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out = []

    for feed in feeds:
        name = feed.get("name", "?")
        url = feed.get("url", "")
        if not url:
            continue
        try:
            parsed = feedparser.parse(url, request_headers=HEADERS, agent=HEADERS["User-Agent"])
        except Exception as e:
            print(f"[sources] RSS {name} fetch error: {e}")
            continue

        count = 0
        for entry in parsed.entries:
            if count >= max_per_feed:
                break
            pub = None
            for key in ("published", "updated", "pubDate"):
                if hasattr(entry, key):
                    pub = _parse_rss_date(getattr(entry, key))
                    if pub:
                        break
            if pub and pub < cutoff:
                continue

            headline = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            summary = _strip_html(entry.get("summary") or "")[:400]

            if not headline or not link:
                continue

            out.append({
                "source": f"rss:{name}",
                "headline": headline,
                "url": link,
                "published_at": pub.isoformat() if pub else None,
                "summary": summary,
            })
            count += 1

    return out


def filter_already_reacted(
    candidates: list[dict],
    queue: dict,
    dedup_window_days: int = 14,
) -> list[dict]:
    """Drop candidates whose source URL is already in the reaction queue
    within the dedup window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=dedup_window_days)
    seen_urls: set[str] = set()
    for p in queue.get("posts", []):
        gen = p.get("generated_at", "")
        if not gen:
            continue
        try:
            dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt < cutoff:
            continue
        url = p.get("source_url") or ""
        if url:
            seen_urls.add(url)

    return [c for c in candidates if (c.get("url") or "") not in seen_urls]
