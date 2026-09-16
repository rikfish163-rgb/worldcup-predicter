import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.sports_lottery import (
    SPORTS_LOTTERY_FALLBACK_URL,
    SPORTS_LOTTERY_URL,
    fetch_sports_lottery,
    parse_sports_lottery_payload,
)


def test_sports_lottery_parser_keeps_all_requested_market_probabilities():
    payload = json.dumps({
        "value": {
            "matchInfoList": [{
                "matchDate": "2026-08-12",
                "subMatchList": [{
                    "matchId": "lot-1",
                    "matchNumStr": "周三001",
                    "leagueAbbName": "英超",
                    "matchDate": "2026-08-12",
                    "matchTime": "19:00",
                    "homeTeamAllName": "Home FC",
                    "awayTeamAllName": "Away FC",
                    "homeTeamAbbEnName": "Home",
                    "awayTeamAbbEnName": "Away",
                    "had": {"h": 2.0, "d": 3.2, "a": 3.8},
                    "hhad": {"goalLineValue": "-1", "h": 3.0, "d": 3.3, "a": 2.1},
                    "ttg": {"s0": 8, "s1": 4, "s2": 3, "s3": 4},
                    "hafu": {"hh": 3, "hd": 4, "ha": 5},
                    "crs": {"s10": 6, "s11": 7, "s20": 8},
                }],
            }],
        }
    }).encode()
    rows = parse_sports_lottery_payload(
        payload,
        retrieved_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0]["had_probability"]
    assert abs(sum(rows[0]["had_probability"].values()) - 1) < 1e-6
    assert rows[0]["hhad_line"] == "-1"
    assert rows[0]["source"]["raw_sha256"]
    assert rows[0]["kickoff_at"] == "2026-08-12T11:00:00+00:00"


def test_sports_lottery_parser_rejects_non_allowlisted_url():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_sports_lottery_payload(
            b'{}',
            retrieved_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
            url="https://example.com/feed",
        )


def test_sports_lottery_parser_accepts_fixed_uniform_fallback_path():
    rows = parse_sports_lottery_payload(
        b'{"value":{"matchInfoList":[]}}',
        retrieved_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        url=SPORTS_LOTTERY_FALLBACK_URL,
    )
    assert rows == []


def test_sports_lottery_rights_gate_precedes_primary_and_fallback_requests():
    class Response:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, _limit):
            return b'{"value":{"matchInfoList":[]}}'

    seen = []

    class Opener:
        def open(self, request, timeout):
            assert timeout == 30
            seen.append(request.full_url)
            if request.full_url == SPORTS_LOTTERY_URL:
                raise OSError("primary blocked")
            return Response(request.full_url)

    result = fetch_sports_lottery(
        now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        opener=Opener(),
    )
    assert result["status"] == "rights_blocked"
    assert result["rights"]["source_id"] == "sports_lottery_official"
    assert result["network_opened"] is False
    assert result["matches"] == []
    assert result["fallbacks"] == []
    assert result["errors"] == []
    assert seen == []


def test_sports_lottery_allowlisted_channel_query_is_preserved():
    assert "channel=c" in SPORTS_LOTTERY_URL
    assert "channel=c" in SPORTS_LOTTERY_FALLBACK_URL


def test_sports_lottery_rights_gate_precedes_http_retry():
    class Response:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, _limit):
            return b'{"value":{"matchInfoList":[]}}'

    class Opener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                    request.full_url, 503, "temporary", {}, None
                )
            return Response(request.full_url)

    opener = Opener()
    result = fetch_sports_lottery(
        now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        opener=opener,
    )
    assert result["status"] == "rights_blocked"
    assert result["matches"] == []
    assert result["fallbacks"] == []
    assert result["errors"] == []
    assert opener.calls == 0


def test_sports_lottery_rights_gate_precedes_transport_retry():
    class Response:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, _limit):
            return b'{"value":{"matchInfoList":[]}}'

    class Opener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary transport failure")
            return Response(request.full_url)

    opener = Opener()
    result = fetch_sports_lottery(
        now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        opener=opener,
    )
    assert result["status"] == "rights_blocked"
    assert result["matches"] == []
    assert result["fallbacks"] == []
    assert result["errors"] == []
    assert opener.calls == 0
