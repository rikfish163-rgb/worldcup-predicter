from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.espn_injuries import (
    MAX_INJURY_BYTES,
    _read_injury_response,
    fetch_espn_injuries,
    parse_espn_injuries_payload,
)


AS_OF = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
URL = "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries"


def _payload(*, injuries: list[dict] | None = None) -> bytes:
    return json.dumps(
        {
            "timestamp": "2026-08-23T23:58:00Z",
            "season": {"year": 2026, "displayName": "2026-27"},
            "status": "success",
            "injuries": injuries if injuries is not None else [],
        }
    ).encode()


def _arsenal_bucket() -> dict:
    return {
        "displayName": "Arsenal",
        "team": {"id": "359", "displayName": "Arsenal", "abbreviation": "ARS"},
        "injuries": [
            {
                "athlete": {
                    "id": "123",
                    "displayName": "Example Player",
                    "position": {"abbreviation": "DF"},
                    "team": {"id": "359", "abbreviation": "ARS"},
                },
                "status": "Out",
                "date": "2026-08-23T10:00:00Z",
                "details": {
                    "detail": "Hamstring",
                    "returnDate": "2026-09-10T00:00:00Z",
                },
                "shortComment": "Unavailable for the next fixture.",
            },
            {
                "athlete": {"id": "124", "displayName": "Healthy Player"},
                "status": "Active",
                "shortComment": "Normal service.",
            },
        ],
    }


def test_parser_keeps_bounded_structured_injury_and_source_lineage():
    payload = _payload(injuries=[_arsenal_bucket()])

    result = parse_espn_injuries_payload(
        payload,
        competition_id="premier-league",
        retrieved_at=AS_OF,
        url=URL,
        fixture_index={"359": [("espn:1", "2026-08-24T19:00:00+00:00")]},
    )

    assert result["status"] == "published_reports"
    assert result["published_team_count"] == 1
    assert result["reported_player_count"] == 1
    report = result["reports"][0]
    assert report["provider_team_id"] == "359"
    assert report["scheduled_fixture_ids"] == ["espn:1"]
    assert report["injuries"][0] == {
        "provider_player_id": "123",
        "name": "Example Player",
        "position": "DF",
        "status": "Out",
        "status_type": "injury_report",
        "reason": "Hamstring",
        "provider_date": "2026-08-23T10:00:00Z",
        "return_date": "2026-09-10T00:00:00Z",
        "note": "Unavailable for the next fixture.",
        "expected_minutes": None,
        "replacement_value": None,
    }
    assert result["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["source"]["time_basis"] == "observed_at_no_row_published_at"
    assert result["source"]["policy"]["allow_model"] is False
    assert result["model_eligible"] is False
    assert result["enters_model"] is False


def test_empty_endpoint_is_observed_empty_not_a_healthy_team_claim():
    result = parse_espn_injuries_payload(
        _payload(),
        competition_id="premier-league",
        retrieved_at=AS_OF,
        url=URL,
        fixture_index={},
    )

    assert result["status"] == "observed_empty"
    assert result["reports"] == []
    assert result["reported_player_count"] == 0
    assert result["coverage_semantics"] == "missing_team_bucket_is_unknown_not_healthy"
    assert result["healthy_team_count"] is None


@pytest.mark.parametrize(
    "url",
    [
        "http://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries",
        "https://example.com/apis/site/v2/sports/soccer/eng.1/injuries",
        "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams",
        "https://user:pass@site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries",
    ],
)
def test_parser_rejects_non_allowlisted_injury_urls(url: str):
    with pytest.raises(ValueError, match="allowlisted"):
        parse_espn_injuries_payload(
            _payload(),
            competition_id="premier-league",
            retrieved_at=AS_OF,
            url=url,
            fixture_index={},
        )


class _Response:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, size: int):
        return self.payload[:size]


class _FallbackOpener:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.urls: list[str] = []

    def open(self, request, timeout: int):
        assert timeout == 30
        self.urls.append(request.full_url)
        if request.full_url.startswith("https://site.api.espn.com/"):
            raise OSError("primary unavailable")
        return _Response(self.payload, request.full_url)


def test_fetcher_fails_closed_without_written_access_authorization():
    class UnexpectedNetworkOpener:
        def open(self, *_args, **_kwargs):
            raise AssertionError("network access must not occur without authorization")

    result = fetch_espn_injuries(
        [
            {
                "id": "espn:1",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-24T19:00:00+00:00",
                "home_provider_team_id": "359",
                "away_provider_team_id": "370",
            }
        ],
        now=AS_OF,
        opener=UnexpectedNetworkOpener(),
    )

    assert result["status"] == "rights_blocked"
    assert result["requested_competitions"] == 0
    assert result["observed_competitions"] == 0
    assert result["reports"] == []
    assert result["access_allowed"] is False
    assert result["network_opened"] is False
    assert result["authorization_required"] == "express_written_permission"
    assert result["terms_url"] == "https://disneytermsofuse.com/english/"


def test_payload_authorization_cannot_enable_injury_fallback_network():
    opener = _FallbackOpener(_payload(injuries=[_arsenal_bucket()]))
    fixtures = [
        {
            "id": "espn:1",
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-08-24T19:00:00+00:00",
            "home_provider_team_id": "359",
            "away_provider_team_id": "370",
        }
    ]

    result = fetch_espn_injuries(
        fixtures,
        now=AS_OF,
        horizon_hours=168,
        opener=opener,
        authorization_reference="test-fixture-only",
    )

    assert result["status"] == "rights_blocked"
    assert result["requested_competitions"] == 0
    assert result["published_team_count"] == 0
    assert result["reported_player_count"] == 0
    assert result["errors"] == []
    assert result["fallbacks"] == []
    assert result["reports"] == []
    assert opener.urls == []


def test_fetcher_reports_all_successful_empty_competitions_as_observed_empty():
    class EmptyOpener:
        def open(self, request, timeout: int):
            assert timeout == 30
            return _Response(_payload(), request.full_url)

    result = fetch_espn_injuries(
        [
            {
                "id": "espn:1",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-24T19:00:00+00:00",
                "home_provider_team_id": "359",
                "away_provider_team_id": "370",
            }
        ],
        now=AS_OF,
        opener=EmptyOpener(),
        authorization_reference="test-fixture-only",
    )

    assert result["status"] == "rights_blocked"
    assert result["observed_competitions"] == 0
    assert result["observed_empty_competitions"] == 0
    assert result["healthy_team_count"] is None


class _FixedResponseOpener:
    def __init__(self, payload: bytes, final_url: str):
        self.payload = payload
        self.final_url = final_url

    def open(self, _request, timeout: int):
        assert timeout == 30
        return _Response(self.payload, self.final_url)


def test_fetcher_rejects_same_host_redirect_to_unexpected_path():
    with pytest.raises(ValueError, match="allowlisted"):
        _read_injury_response(
            _FixedResponseOpener(
                _payload(),
                "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams",
            ),
            urllib.request.Request(URL),
            competition_id="premier-league",
        )


def test_fetcher_rejects_response_larger_than_lane_specific_cap():
    with pytest.raises(ValueError, match="size limit"):
        _read_injury_response(
            _FixedResponseOpener(b"x" * (MAX_INJURY_BYTES + 1), URL),
            urllib.request.Request(URL),
            competition_id="premier-league",
        )
