"""Small, allow-listed public RSS adapter for pre-match context."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse

from league_platform.source_rights import SourceId, rights_blocked_envelope


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
        "query": feed.get("query"),
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
        "query": clean_query,
    }


def build_match_news_queries(
    fixtures: list[dict],
    *,
    max_queries: int = 6,
    reference_time: datetime | None = None,
) -> list[str]:
    """Build bounded team-pair RSS queries with causal seven-day rotation.

    The query list is a lead generator only.  RSS rows remain immutable
    ``news_fact_candidate`` observations and never become model features unless
    a later, source-specific fact verifier establishes an exact entity and
    time-bound fact.  When ``reference_time`` is supplied, four of the default
    six slots rotate through matches inside 24 hours and the remainder rotate
    through the rest of the seven-day horizon.  A 15-minute runtime cycle can
    therefore cover the whole workbench instead of permanently starving every
    fixture after the first six.  The legacy no-reference mode still returns
    the nearest fixtures for deterministic callers and tests.
    """

    if isinstance(max_queries, bool) or not isinstance(max_queries, int) or max_queries < 0:
        raise ValueError("max_queries must be a non-negative integer")
    reference: datetime | None = None
    if reference_time is not None:
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise ValueError("reference_time must be timezone-aware")
        reference = reference_time.astimezone(timezone.utc)
    candidates: list[tuple[datetime, str, str]] = []
    for fixture in fixtures:
        if not isinstance(fixture, dict) or fixture.get("status") != "upcoming":
            continue
        home = " ".join(str(fixture.get("home_team") or "").split())
        away = " ".join(str(fixture.get("away_team") or "").split())
        kickoff_value = fixture.get("kickoff_at")
        if not home or not away or not isinstance(kickoff_value, str):
            continue
        try:
            kickoff = datetime.fromisoformat(kickoff_value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff.tzinfo is None or kickoff.utcoffset() is None:
            continue
        # Keep a bounded query string and remove quote characters so a team
        # name cannot alter the query syntax.
        home = home.replace('"', "")[:80].strip()
        away = away.replace('"', "")[:80].strip()
        if not home or not away:
            continue
        fixture_id = str(fixture.get("id") or "")
        candidates.append((kickoff.astimezone(timezone.utc), fixture_id, f'"{home}" "{away}" injury lineup press conference'))
    candidates.sort(key=lambda item: (item[0], item[1]))
    unique_candidates: list[tuple[datetime, str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        query = candidate[2]
        if query in seen:
            continue
        seen.add(query)
        unique_candidates.append(candidate)
    if reference is None:
        return [query for _kickoff, _fixture_id, query in unique_candidates[:max_queries]]
    if max_queries == 0:
        return []

    urgent = [
        row
        for row in unique_candidates
        if reference <= row[0] <= reference + timedelta(hours=24)
    ]
    seven_day = [
        row
        for row in unique_candidates
        if reference + timedelta(hours=24) < row[0] <= reference + timedelta(days=7)
    ]
    urgent_budget = min(len(urgent), max(1, (max_queries * 2) // 3)) if urgent else 0
    seven_day_budget = min(len(seven_day), max_queries - urgent_budget)
    remaining = max_queries - urgent_budget - seven_day_budget
    if remaining and urgent_budget < len(urgent):
        add = min(remaining, len(urgent) - urgent_budget)
        urgent_budget += add
        remaining -= add
    if remaining and seven_day_budget < len(seven_day):
        seven_day_budget += min(remaining, len(seven_day) - seven_day_budget)

    rotation_slot = int(reference.timestamp() // (15 * 60))

    def rotate(rows: list[tuple[datetime, str, str]], budget: int) -> list[str]:
        if not rows or budget <= 0:
            return []
        start = (rotation_slot * budget) % len(rows)
        return [rows[(start + index) % len(rows)][2] for index in range(budget)]

    return rotate(urgent, urgent_budget) + rotate(seven_day, seven_day_budget)


def _validate_feed_url(url: str, *, expected_host: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("news RSS URL is not allowlisted")


def _read_feed_payload(response: object, *, request_url: str, expected_host: str) -> bytes:
    final_url = getattr(response, "geturl", lambda: request_url)()
    _validate_feed_url(str(final_url), expected_host=expected_host)
    read = getattr(response, "read", None)
    if not callable(read):
        raise TypeError("news RSS response does not expose read()")
    payload = read(MAX_FEED_BYTES + 1)
    if not isinstance(payload, bytes):
        raise TypeError("news RSS response must be bytes")
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("news RSS response exceeded 5 MiB")
    return payload


def fetch_news(
    *,
    now: datetime | None = None,
    opener=None,
    queries: list[str] | None = None,
    max_query_feeds: int = 6,
) -> dict:
    """Return the v260 rights block before constructing feeds or using the opener."""

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_time = reference_time.astimezone(timezone.utc)
    return rights_blocked_envelope(
        SourceId.PUBLIC_RSS_NEWS,
        provider="Public RSS",
        checked_at=reference_time.isoformat(),
        empty_fields=("news",),
    )


__all__ = [
    "ALLOWED_NEWS_HOSTS",
    "NEWS_FEEDS",
    "build_match_news_queries",
    "fetch_news",
    "parse_rss_payload",
]
