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


def _extract_image_url(html_description: str) -> str | None:
    """Pull first image URL from Nitter RSS description HTML.
    Converts Nitter proxy URL to direct pbs.twimg.com URL when possible.
    Telegram sendPhoto needs a publicly fetchable URL.
    """
    if not html_description:
        return None
    m = re.search(r'<img[^>]+src="([^"]+)"', html_description, re.IGNORECASE)
    if not m:
        return None
    url = m.group(1)
    # Skip emojis and avatars
    if "/emoji/" in url or "/avatars/" in url:
        return None
    # Convert Nitter proxy URL to direct pbs.twimg.com URL
    # Patterns:
    #   https://nitter.X/pic/orig/media%2FXXX.jpg -> https://pbs.twimg.com/media/XXX.jpg
    #   https://nitter.X/pic/media%2FXXX.jpg      -> https://pbs.twimg.com/media/XXX.jpg
    from urllib.parse import unquote
    pic_match = re.search(r'/pic/(?:orig/)?(.+)$', url)
    if pic_match:
        path = unquote(pic_match.group(1))
        if path.startswith("media/"):
            return f"https://pbs.twimg.com/{path}"
        if path.startswith("pbs.twimg.com/"):
            return f"https://{path}"
    return url  # fall back to raw URL


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

                image_url = _extract_image_url(desc)
                text = _strip_html(desc) or title
                if len(text) < min_chars:
                    continue

                x_url = link.replace(f"https://{instance}/", "https://x.com/")
                posts.append({
                    "handle": handle,
                    "text": text[:500],
                    "url": x_url,
                    "image_url": image_url,
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
            raw_summary = entry.get("summary") or ""
            summary = _strip_html(raw_summary)[:400]

            # Try multiple places where RSS feeds put images
            image_url = None
            if hasattr(entry, "media_content") and entry.media_content:
                image_url = entry.media_content[0].get("url")
            elif hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
                image_url = entry.media_thumbnail[0].get("url")
            elif hasattr(entry, "enclosures") and entry.enclosures:
                for enc in entry.enclosures:
                    enc_type = enc.get("type", "")
                    if enc_type.startswith("image/"):
                        image_url = enc.get("href") or enc.get("url")
                        break
            if not image_url and raw_summary:
                image_url = _extract_image_url(raw_summary)

            if not headline or not link:
                continue

            out.append({
                "source": f"rss:{name}",
                "headline": headline,
                "url": link,
                "image_url": image_url,
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


def fetch_my_recent_posts(
    handle: str,
    nitter_instances: list[str],
    lookback_days: int = 7,
    max_posts: int = 30,
    min_chars: int = 40,
) -> list[dict]:
    """Pull the user's OWN recent X posts via Nitter profile RSS.

    Used by performance_tracker for auto-attribution — fuzzy-match these
    against unmatched auto_sent reactions in the queue. No X API needed
    for this step; X API only used for engagement metrics on matched IDs.

    Returns list of {handle, text, url, tweet_id, published_at} dicts.
    Drops retweets and replies. Lookback longer than source-account fetcher
    (default 7 days vs 24h) so missed attribution can backfill.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    out = []

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

            for item in channel.findall("item"):
                if len(out) >= max_posts:
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
                # Tweet ID is the last numeric segment in the URL,
                # possibly followed by #m or other fragment
                tweet_id_match = re.search(r"/status/(\d+)", x_url)
                if not tweet_id_match:
                    continue
                tweet_id = tweet_id_match.group(1)

                out.append({
                    "handle":       handle,
                    "tweet_id":     tweet_id,
                    "text":         text[:500],
                    "url":          f"https://x.com/{handle}/status/{tweet_id}",
                    "published_at": pub.isoformat() if pub else None,
                })

            if out:
                return out
        except Exception as e:
            print(f"[sources] Nitter {instance}/{handle} error: {e}")
            continue
    return out


def filter_recent_authors(
    candidates: list[dict],
    queue: dict,
    cooldown_days: int = 5,
) -> list[dict]:
    """Drop X candidates whose @handle was QT'd within the cooldown window.

    Only applies to X-account candidates (source starts with 'x:'). RSS news
    candidates are not affected — those are story-level, not handle-level.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    cooldown_handles: set[str] = set()
    for p in queue.get("posts", []):
        gen = p.get("generated_at", "")
        handle = (p.get("source_handle") or "").lower()
        if not gen or not handle:
            continue
        try:
            dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt < cutoff:
            continue
        cooldown_handles.add(handle)

    out = []
    dropped = 0
    for c in candidates:
        is_x = (c.get("source") or "").startswith("x:")
        handle = (c.get("handle") or "").lower()
        if is_x and handle in cooldown_handles:
            dropped += 1
            continue
        out.append(c)
    if dropped:
        print(f"[sources] cooldown: dropped {dropped} X candidates from {len(cooldown_handles)} recent authors")
    return out
