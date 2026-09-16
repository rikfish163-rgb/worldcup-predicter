from __future__ import annotations

from datetime import datetime, timezone

import pytest

from league_platform.current import _validate_live_snapshot
from league_platform.live_sources.news import build_match_news_queries, fetch_news, parse_rss_payload


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


def test_fetch_news_is_rights_blocked_before_feed_failure_handling(monkeypatch):
    calls = []

    class BrokenResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            raise OSError("offline")

    def opener(_request, timeout):
        calls.append(timeout)
        assert timeout == 20
        return BrokenResponse()

    result = fetch_news(now=AS_OF, opener=opener)

    assert result["provider"] == "Public RSS"
    assert result["status"] == "rights_blocked"
    assert result["news"] == []
    assert result["rights"]["source_id"] == "public_rss_news"
    assert result["network_opened"] is False
    assert result["errors"] == []
    assert calls == []


def test_fetch_news_rights_gate_precedes_dynamic_query_feed_construction():
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

    assert result["status"] == "rights_blocked"
    assert result["news"] == []
    assert result["errors"] == []
    assert calls == []


def test_build_match_news_queries_prioritizes_nearest_upcoming_team_pairs():
    fixtures = [
        {
            "id": "later",
            "status": "upcoming",
            "kickoff_at": "2026-08-12T12:00:00+00:00",
            "home_team": "Later FC",
            "away_team": "Away FC",
        },
        {
            "id": "nearest",
            "status": "upcoming",
            "kickoff_at": "2026-08-11T12:00:00+00:00",
            "home_team": "Home FC",
            "away_team": "Visitor FC",
        },
        {
            "id": "finished",
            "status": "finished",
            "kickoff_at": "2026-08-10T12:00:00+00:00",
            "home_team": "Finished FC",
            "away_team": "Old FC",
        },
        {
            "id": "invalid",
            "status": "upcoming",
            "kickoff_at": "not-a-time",
            "home_team": "Invalid FC",
            "away_team": "Away FC",
        },
    ]

    queries = build_match_news_queries(fixtures, max_queries=1)

    assert queries == ['"Home FC" "Visitor FC" injury lineup press conference']


def test_build_match_news_queries_rotates_urgent_and_seven_day_fixture_batches():
    fixtures = []
    for index in range(8):
        fixtures.append(
            {
                "id": f"urgent-{index}",
                "status": "upcoming",
                "kickoff_at": f"2026-08-11T{12 + index:02d}:00:00+00:00",
                "home_team": f"Urgent Home {index}",
                "away_team": f"Urgent Away {index}",
            }
        )
        fixtures.append(
            {
                "id": f"week-{index}",
                "status": "upcoming",
                "kickoff_at": f"2026-08-13T{12 + index:02d}:00:00+00:00",
                "home_team": f"Week Home {index}",
                "away_team": f"Week Away {index}",
            }
        )
    fixtures.append(
        {
            "id": "outside-seven-day-horizon",
            "status": "upcoming",
            "kickoff_at": "2026-08-20T12:00:00+00:00",
            "home_team": "Too Far Home",
            "away_team": "Too Far Away",
        }
    )

    first = build_match_news_queries(
        fixtures,
        max_queries=6,
        reference_time=datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc),
    )
    second = build_match_news_queries(
        fixtures,
        max_queries=6,
        reference_time=datetime(2026, 8, 11, 0, 15, tzinfo=timezone.utc),
    )

    assert len(first) == len(second) == 6
    assert sum("Urgent" in query for query in first) == 4
    assert sum("Week" in query for query in first) == 2
    assert not set(first) & set(second)
    assert all("Too Far" not in query for query in first + second)


def test_fetch_news_rights_gate_precedes_redirect_validation():
    calls = []

    class RedirectedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://evil.example/rss.xml"

        def read(self, _limit):
            return b"<rss><channel /></rss>"

    def opener(_request, timeout):
        calls.append(timeout)
        assert timeout == 20
        return RedirectedResponse()

    result = fetch_news(now=AS_OF, opener=opener)

    assert result["status"] == "rights_blocked"
    assert result["news"] == []
    assert result["errors"] == []
    assert calls == []


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
