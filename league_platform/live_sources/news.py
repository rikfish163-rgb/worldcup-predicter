"""Small, allow-listed public RSS adapter for pre-match context."""

from __future__ import annotations

import hashlib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode


NEWS_FEEDS = (
    {
        "name": "BBC Sport Football",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "host": "feeds.bbci.co.uk",
    },
    {
        "name": "Sky Sports Football",
        "url": "https://www.skysports.com/rss/12040",
        "host": "www.skysports.com",
    },
)
GOOGLE_NEWS_HOST = "news.google.com"
ALLOWED_NEWS_HOSTS = {item["host"] for item in NEWS_FEEDS} | {GOOGLE_NEWS_HOST}
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ITEMS_PER_FEED = 80
NON_FOOTBALL_TERMS = (
    "cricket",
    "rugby",
    "tennis",
    "golf",
    "boxing",
    "horse racing",
    "formula 1",
    "f1",
    "nba",
    "nfl",
    "darts",
    "snooker",
    "cycling",
    "athletics",
    "netball",
)


def _text(element: ET.Element | None, tag: str) -> str | None:
    if element is None:
        return None
    value = element.findtext(tag)
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def _published_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _is_football_item(title: str, summary: str | None, link: str = "") -> bool:
    text = f"{title} {summary or ''} {link}".casefold()
    return not any(term in text for term in NON_FOOTBALL_TERMS)


def parse_rss_payload(
    payload: bytes,
    *,
    feed: dict,
    retrieved_at: datetime,
    max_items: int = MAX_ITEMS_PER_FEED,
) -> dict:
    """Parse an RSS 2.0 feed without executing or rendering feed content."""

    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("news RSS response exceeded 5 MiB")
    if b"<!doctype" in payload.lower() or b"<!entity" in payload.lower():
        raise ValueError("news RSS payload contains a blocked XML declaration")
    root = ET.fromstring(payload)
    items = []
    for item in root.findall("./channel/item")[:max_items]:
        title = _text(item, "title")
        link = _text(item, "link")
        if not title or not link or not link.startswith("https://"):
            continue
        summary = _text(item, "description")
        if not _is_football_item(title, summary, link):
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "published_at": _published_at(_text(item, "pubDate")),
            }
        )
    return {
        "name": feed["name"],
        "url": feed["url"],
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "items": items,
    }


def _google_news_feed(query: str) -> dict:
    clean_query = " ".join(str(query).split())
    return {
        "name": f"Google News RSS: {clean_query}",
        "url": "https://news.google.com/rss/search?"
        + urlencode({"q": clean_query, "hl": "en-US", "gl": "US", "ceid": "US:en"}),
        "host": GOOGLE_NEWS_HOST,
    }


def fetch_news(
    *,
    now: datetime | None = None,
    opener=None,
    queries: list[str] | None = None,
    max_query_feeds: int = 6,
) -> dict:
    """Fetch the fixed public feeds; isolate failures per feed."""

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    if not isinstance(max_query_feeds, int) or max_query_feeds < 0:
        raise ValueError("max_query_feeds must be a non-negative integer")
    opener = opener or urllib.request.urlopen
    feeds = []
    errors = []
    observation_times = []
    feed_specs = list(NEWS_FEEDS)
    for query in (queries or [])[:max_query_feeds]:
        if str(query).strip():
            feed_specs.append(_google_news_feed(str(query)))
    for feed in feed_specs:
        request = urllib.request.Request(
            feed["url"],
            headers={
                "Accept": "application/rss+xml, application/xml, text/xml",
                "User-Agent": "Matchline/1.0 (+public-rss)",
            },
        )
        try:
            with opener(request, timeout=20) as response:
                payload = response.read(MAX_FEED_BYTES + 1)
            observed_at = reference_time if now is not None else datetime.now(timezone.utc)
            observation_times.append(observed_at)
            feeds.append(
                parse_rss_payload(
                    payload,
                    feed=feed,
                    retrieved_at=observed_at,
                )
            )
        except (OSError, ET.ParseError, ValueError, TypeError) as exc:
            errors.append({"feed": feed["name"], "url": feed["url"], "error": str(exc)})
    return {
        "provider": "Public RSS",
        "retrieved_at": max(observation_times, default=reference_time).isoformat(),
        "feeds": feeds,
        "item_count": sum(len(feed["items"]) for feed in feeds),
        "errors": errors,
    }


__all__ = ["ALLOWED_NEWS_HOSTS", "NEWS_FEEDS", "fetch_news", "parse_rss_payload"]
