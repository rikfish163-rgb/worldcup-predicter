from __future__ import annotations

from datetime import datetime, timezone

import pytest

from league_platform.current import _validate_live_snapshot
from league_platform.live_sources.news import fetch_news, parse_rss_payload


AS_OF = datetime(2026, 8, 11, tzinfo=timezone.utc)
FEED = {
    "name": "Example Football",
    "url": "https://feeds.example.test/football.xml",
    "host": "feeds.example.test",
}


def test_parse_rss_payload_preserves_provenance_and_published_time():
    payload = b"""<?xml version='1.0'?><rss><channel>
      <item><title>Team news</title><link>https://example.test/story</link>
      <description>Preview</description><pubDate>Tue, 11 Aug 2026 08:00:00 GMT</pubDate></item>
      <item><title>Missing link</title></item>
    </channel></rss>"""

    result = parse_rss_payload(payload, feed=FEED, retrieved_at=AS_OF)

    assert len(result["items"]) == 1
    assert result["items"][0]["published_at"] == "2026-08-11T08:00:00+00:00"
    assert len(result["raw_sha256"]) == 64


def test_parse_rss_payload_drops_obvious_non_football_items():
    payload = b"""<rss><channel>
      <item><title>Football preview</title><link>https://example.test/football</link></item>
      <item><title>Cricket result</title><link>https://example.test/cricket</link></item>
    </channel></rss>"""

    result = parse_rss_payload(payload, feed=FEED, retrieved_at=AS_OF)

    assert [item["title"] for item in result["items"]] == ["Football preview"]


def test_fetch_news_isolates_feed_failures_without_fabricating_items(monkeypatch):
    class BrokenResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            raise OSError("offline")

    def opener(_request, timeout):
        assert timeout == 20
        return BrokenResponse()

    result = fetch_news(now=AS_OF, opener=opener)

    assert result["provider"] == "Public RSS"
    assert result["feeds"] == []
    assert result["item_count"] == 0
    assert len(result["errors"]) == 2


def test_fetch_news_caps_low_frequency_google_query_feeds():
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return (
                b"<rss><channel><item><title>Football update</title>"
                b"<link>https://example.test/story</link></item></channel></rss>"
            )

    def opener(request, timeout):
        calls.append(request.full_url)
        assert timeout == 20
        return Response()

    result = fetch_news(
        now=AS_OF,
        opener=opener,
        queries=["Premier League injury", "La Liga injury"],
        max_query_feeds=1,
    )

    assert len(calls) == 3
    assert any("news.google.com/rss/search" in url for url in calls)
    assert result["item_count"] == 3


def test_current_validator_accepts_optional_news_and_rejects_unallowlisted_feed():
    feed = {
        "name": "BBC Sport Football",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "retrieved_at": AS_OF.isoformat(),
        "raw_sha256": "a" * 64,
        "items": [
            {
                "title": "Preview",
                "link": "https://example.test/preview",
                "summary": None,
                "published_at": None,
            }
        ],
    }
    live = {
        "espn": {"fixtures": []},
        "understat": {"team_features": []},
        "espn_markets": {"markets": []},
        "news": {"provider": "Public RSS", "feeds": [feed], "item_count": 1, "errors": []},
    }

    _validate_live_snapshot(live, AS_OF, set())

    feed["url"] = "https://evil.example/rss.xml"
    with pytest.raises(ValueError, match="allowlisted"):
        _validate_live_snapshot(live, AS_OF, set())
