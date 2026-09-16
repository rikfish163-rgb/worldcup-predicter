from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.laliga import (
    discover_match_urls,
    fetch_laliga_lineups,
    parse_laliga_match_page,
)


AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
KICKOFF = "2026-08-15T17:30:00+00:00"
HOME_URL = "https://www.laliga.com/en-GB"
MATCH_URL = (
    "https://www.laliga.com/en-GB/match/"
    "temporada-2026-2027-laliga-ea-sports-deportivo-alaves-getafe-cf-1"
)
LOCALIZED_PARTIDO_URL = (
    "https://www.laliga.com/es-GB/partido/"
    "temporada-2026-2027-laliga-ea-sports-r-racing-club-villarreal-cf-1"
)


def _player(index: int, status: str) -> dict:
    return {
        "id": index,
        "status": "start" if status == "starter" else "sub",
        "position": index,
        "person": {"name": f"Player {index}", "nickname": f"P{index}"},
    }


def _payload(
    *,
    status: str = "PreMatch",
    home_count: int = 11,
    away_count: int = 11,
    home_name: str = "Deportivo Alavés",
    away_name: str = "Getafe",
) -> bytes:
    data = {
        "props": {
            "pageProps": {
                "match": {
                    "id": 98797,
                    "date": KICKOFF,
                    "status": status,
                    "slug": "temporada-2026-2027-laliga-ea-sports-deportivo-alaves-getafe-cf-1",
                    "competition": {"opta_id": "23"},
                    "home_team": {"nickname": home_name, "name": f"{home_name} SAD"},
                    "away_team": {"nickname": away_name, "name": f"{away_name} CF"},
                },
                "data": {
                    "lineups": {
                        "home": {
                            "starts": [_player(i, "starter") for i in range(home_count)],
                            "subs": [],
                        },
                        "away": {
                            "starts": [_player(100 + i, "starter") for i in range(away_count)],
                            "subs": [],
                        },
                    }
                },
            }
        }
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data, ensure_ascii=False)
        + "</script></html>"
    ).encode()


def _api_player(index: int, status: str) -> dict:
    return {
        "id": index,
        "status": status,
        "position": index,
        "person": {"name": f"API Player {index}", "nickname": f"API{index}"},
        "photos": {"003": {"64x64": f"https://assets.laliga.com/squad/2026/p{index}/64x64.png"}},
    }


def _api_lineup_payload() -> bytes:
    return json.dumps(
        {
            "home_team_lineups": [{"id": 9000, "position": 0, "person": {"name": "Coach"}}]
            + [_api_player(index, "start") for index in range(1001, 1012)]
            + [_api_player(index, "sub") for index in range(1012, 1014)],
            "away_team_lineups": [{"id": 9100, "position": 0, "person": {"name": "Away Coach"}}]
            + [_api_player(index, "start") for index in range(1101, 1112)]
            + [_api_player(index, "sub") for index in range(1112, 1114)],
        }
    ).encode()


def test_discover_match_urls_only_keeps_allowlisted_pages():
    payload = (
        '<a href="/en-GB/match/temporada-2026-2027-laliga-ea-sports-deportivo-alaves-getafe-cf-1">x</a>'
        '<a href="https://evil.example/en-GB/match/x">bad</a>'
        '<a href="/en-GB/not-match/x">bad</a>'
    ).encode()
    assert discover_match_urls(payload, url=HOME_URL) == [MATCH_URL]


def test_discover_match_urls_accepts_official_localized_partido_path():
    payload = f'<a href="{LOCALIZED_PARTIDO_URL}">match</a>'.encode()
    assert discover_match_urls(payload, url=HOME_URL) == [LOCALIZED_PARTIDO_URL]


def test_parse_laliga_pre_kickoff_complete_xi_is_model_eligible():
    row = parse_laliga_match_page(
        _payload(),
        fixture_id="espn:401882926",
        kickoff_at=KICKOFF,
        retrieved_at=AS_OF,
        url=MATCH_URL,
        expected_home="Alavés",
        expected_away="Getafe",
    )
    assert row["lineups"]["confirmed"] is True
    assert row["lineups"]["model_eligible"] is True
    assert row["lineups"]["home"]["players"][0]["starter"] is True
    assert row["lineups"]["home"]["players"][0]["aliases"] == ["Player 0", "P0"]
    assert row["source"]["time_basis"] == "observed_at_no_published_at"


def test_parse_laliga_accepts_localized_partido_path():
    row = parse_laliga_match_page(
        _payload(home_name="Racing Club", away_name="Villarreal CF"),
        fixture_id="espn:401882920",
        kickoff_at=KICKOFF,
        retrieved_at=AS_OF,
        url=LOCALIZED_PARTIDO_URL,
        expected_home="Racing Santander",
        expected_away="Villarreal",
    )
    assert row["official_fixture"]["id"] == "espn:401882920"
    assert row["lineups"]["model_eligible"] is True


def test_parse_laliga_incomplete_or_post_kickoff_xi_is_display_only():
    incomplete = parse_laliga_match_page(
        _payload(home_count=10),
        fixture_id="espn:401882926",
        kickoff_at=KICKOFF,
        retrieved_at=AS_OF,
        url=MATCH_URL,
        expected_home="Alavés",
        expected_away="Getafe",
    )
    assert incomplete["lineups"]["confirmed"] is False
    assert incomplete["lineups"]["model_eligible"] is False
    assert incomplete["lineups"]["diagnostic"]["status"] == "partial"

    after = parse_laliga_match_page(
        _payload(status="FullTime"),
        fixture_id="espn:401882926",
        kickoff_at=KICKOFF,
        retrieved_at=datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc),
        url=MATCH_URL,
        expected_home="Alavés",
        expected_away="Getafe",
    )
    assert after["lineups"]["confirmed"] is True
    assert after["lineups"]["model_eligible"] is False
    assert after["lineups"]["diagnostic"]["status"] == "post_kickoff_observation"


class _FakeResponse:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload

    def geturl(self) -> str:
        return self.url


class _FakeOpener:
    def open(self, request, timeout: float):
        del timeout
        if request.full_url.rstrip("/") == HOME_URL:
            return _FakeResponse(
                f'<a href="{MATCH_URL}">match</a>'.encode(),
                HOME_URL,
            )
        return _FakeResponse(_payload(), MATCH_URL)


class _FakeLineupApiOpener:
    def __init__(self, *, api_error: Exception | None = None, empty_api: bool = False):
        self.api_error = api_error
        self.empty_api = empty_api

    def open(self, request, timeout: float):
        del timeout
        if request.full_url.rstrip("/") == HOME_URL:
            home_data = {
                "runtimeConfig": {"webviewSubscription": "public-test-key"},
                "props": {"pageProps": {}},
            }
            payload = (
                '<html><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(home_data)
                + f'</script><a href="{MATCH_URL}">match</a></html>'
            ).encode()
            return _FakeResponse(payload, HOME_URL)
        if request.full_url.split("?", 1)[0] == MATCH_URL:
            return _FakeResponse(_payload(home_count=0, away_count=0), MATCH_URL)
        if request.full_url.startswith(
            "https://apim.laliga.com/webview/api/web/matches/98797/lineups?"
        ):
            if self.api_error is not None:
                raise self.api_error
            if self.empty_api:
                return _FakeResponse(
                    b'{"home_team_lineups": [], "away_team_lineups": []}',
                    "https://apim.laliga.com/webview/api/web/matches/98797/lineups",
                )
            return _FakeResponse(
                _api_lineup_payload(),
                "https://apim.laliga.com/webview/api/web/matches/98797/lineups",
            )
        raise AssertionError(f"unexpected URL: {request.full_url}")


class _FakeScheduleApiOpener:
    def open(self, request, timeout: float):
        del timeout
        if request.full_url.rstrip("/") == HOME_URL:
            home_data = {
                "runtimeConfig": {
                    "backendSubscription": "public-directory-test-key",
                    "webviewSubscription": "public-test-key",
                },
                "props": {"pageProps": {}},
            }
            payload = (
                '<html><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(home_data)
                + '</script><script>"primera-division":{"clubs":"2026",'
                + '"subscription_id":"395"}</script></html>'
            ).encode()
            return _FakeResponse(payload, HOME_URL)
        if request.full_url.split("?", 1)[0] == (
            "https://apim.laliga.com/public-service/api/v1/matches"
        ):
            match = {
                "id": 98797,
                "slug": MATCH_URL.rsplit("/", 1)[-1],
                "date": KICKOFF,
                "competition": {"opta_id": "23", "slug": "primera-division"},
                "home_team": {"nickname": "Deportivo Alavés"},
                "away_team": {"nickname": "Getafe"},
            }
            return _FakeResponse(
                json.dumps({"total": 1, "matches": [match]}).encode(),
                request.full_url.split("?", 1)[0],
            )
        if request.full_url.split("?", 1)[0] == MATCH_URL:
            return _FakeResponse(_payload(), MATCH_URL)
        raise AssertionError(f"unexpected URL: {request.full_url}")


def test_fetch_laliga_lineups_is_bounded_and_skips_crawl_delay_for_injected_opener():
    result = fetch_laliga_lineups(
        [
            {
                "id": "espn:401882926",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": KICKOFF,
                "home_team": "Alavés",
                "away_team": "Getafe",
            }
        ],
        now=AS_OF,
        opener=_FakeOpener(),
    )
    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["fixtures"] == []


def test_fetch_laliga_lineups_uses_official_public_directory_when_home_omits_match():
    result = fetch_laliga_lineups(
        [
            {
                "id": "espn:401882926",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": KICKOFF,
                "home_team": "Alavés",
                "away_team": "Getafe",
            }
        ],
        now=AS_OF,
        opener=_FakeScheduleApiOpener(),
    )

    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["fixtures"] == []
    assert result["errors"] == []


def test_fetch_laliga_lineups_uses_public_webview_api_when_page_has_no_xi():
    result = fetch_laliga_lineups(
        [
            {
                "id": "espn:401882926",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": KICKOFF,
                "home_team": "Alavés",
                "away_team": "Getafe",
            }
        ],
        now=AS_OF,
        opener=_FakeLineupApiOpener(),
    )

    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["fixtures"] == []
    assert result["errors"] == []


def test_laliga_empty_public_api_is_explicitly_not_published():
    result = fetch_laliga_lineups(
        [
            {
                "id": "espn:401882926",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": KICKOFF,
                "home_team": "Alavés",
                "away_team": "Getafe",
            }
        ],
        now=AS_OF,
        opener=_FakeLineupApiOpener(empty_api=True),
    )
    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["fixtures"] == []


def test_laliga_lineup_api_failure_keeps_page_observation_isolated():
    result = fetch_laliga_lineups(
        [
            {
                "id": "espn:401882926",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": KICKOFF,
                "home_team": "Alavés",
                "away_team": "Getafe",
            }
        ],
        now=AS_OF,
        opener=_FakeLineupApiOpener(api_error=ValueError("temporary API failure")),
    )

    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["fixtures"] == []
    assert result["errors"] == []


def test_parse_laliga_rejects_non_laliga_page():
    payload = _payload()
    payload = payload.replace(b'"opta_id": "23"', b'"opta_id": "19"')
    with pytest.raises(ValueError, match="not a LaLiga"):
        parse_laliga_match_page(
            payload,
            fixture_id="espn:401882926",
            kickoff_at=KICKOFF,
            retrieved_at=AS_OF,
            url=MATCH_URL,
        )
