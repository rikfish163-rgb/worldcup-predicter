from __future__ import annotations

import json
from datetime import datetime, timezone
import urllib.error
from urllib.request import Request

import pytest

from league_platform.live_sources.seriea import (
    _AllowlistedRedirect,
    discover_match_urls,
    fetch_serie_a_lineups,
    parse_serie_a_header,
    parse_serie_a_lineups,
)


AS_OF = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
KICKOFF = "2026-08-22T16:30:00+00:00"
HOME_URL = "https://en.legaseriea.it/serie-a"
MATCH_URL = "https://en.legaseriea.it/serie-a/match/" + "a" * 32 + "/inter-vs-monza"
SEASON_ID = "serie-a::Football_Season::" + "b" * 32
MATCH_ID = "a" * 32
HEADER_URL = (
    "https://api-sdp.legaseriea.it/v1/serie-a/football/seasons/"
    "serie-a%3A%3AFootball_Season%3A%3A" + "b" * 32 + "/matches/"
    "serie-a%3A%3AFootball_Match%3A%3A" + "a" * 32 + "/header"
)
LINEUP_URL = HEADER_URL.removesuffix("/header") + "/lineups"


def _header_payload() -> bytes:
    return json.dumps(
        {
            "matchId": f"serie-a::Football_Match::{MATCH_ID}",
            "status": "UPCOMING",
            "matchDateUtc": KICKOFF.replace("+00:00", "Z"),
            "cityName": "Milan",
            "stadiumName": "Stadio Giuseppe Meazza",
            "stadiumId": "serie-a::Football_Stadium::" + "c" * 32,
            "home": {"shortName": "Inter", "officialName": "Internazionale"},
            "away": {"shortName": "Monza", "officialName": "Monza"},
        }
    ).encode()


def _player(index: int) -> dict:
    return {
        "playerId": f"serie-a::Football_Player::{index:032d}",
        "displayName": f"Player {index}",
        "roleLabel": "Defender",
        "bibNumber": str(index),
        "isCaptain": index == 1,
    }


def _lineup_payload(*, complete: bool = True) -> bytes:
    count = 11 if complete else 10
    return json.dumps(
        {
            "matchId": f"serie-a::Football_Match::{MATCH_ID}",
            "home": {"fielded": [_player(i) for i in range(count)], "benched": [_player(20)]},
            "away": {
                "fielded": [_player(100 + i) for i in range(count)],
                "benched": [_player(120)],
            },
        }
    ).encode()


class _Response:
    def __init__(self, url: str, payload: bytes):
        self.url = url
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, _limit: int):
        return self.payload


class _Opener:
    def __init__(self, *, complete: bool = True):
        self.complete = complete

    def open(self, request, timeout):
        del timeout
        url = request.full_url.rstrip("/")
        if url == HOME_URL.rstrip("/"):
            payload = (
                f'<a href="{MATCH_URL}">Inter - Monza</a>'
                f"<script>serie-a::Football_Season::{'b' * 32}</script>"
            ).encode()
            return _Response(HOME_URL, payload)
        if url == HEADER_URL.rstrip("/"):
            return _Response(HEADER_URL, _header_payload())
        if url == LINEUP_URL.rstrip("/"):
            return _Response(LINEUP_URL, _lineup_payload(complete=self.complete))
        raise AssertionError(f"unexpected URL: {request.full_url}")


def test_discover_match_urls_is_strictly_allowlisted():
    payload = (
        f'<a href="{MATCH_URL}">ok</a>'
        '<a href="https://evil.example/serie-a/match/' + "a" * 32 + '/inter-vs-monza">bad</a>'
        '<a href="/serie-a/not-a-match/x">bad</a>'
    ).encode()
    assert discover_match_urls(payload, url=HOME_URL) == [MATCH_URL]


def test_redirect_handler_allows_only_official_serie_a_paths():
    handler = _AllowlistedRedirect()
    request = Request(HOME_URL)
    redirected = handler.redirect_request(
        request,
        None,
        307,
        "Temporary Redirect",
        {},
        "https://en.legaseriea.it/serie-a/",
    )
    assert redirected is not None
    assert redirected.full_url == "https://en.legaseriea.it/serie-a/"
    with pytest.raises(urllib.error.URLError, match="outside the official hosts"):
        handler.redirect_request(
            request,
            None,
            307,
            "Temporary Redirect",
            {},
            "https://evil.example/serie-a/",
        )


def test_parse_header_accepts_explicit_team_alias_and_canonical_kickoff():
    row = parse_serie_a_header(
        json.loads(_header_payload()),
        fixture_id="espn:serie-a-1",
        match_id=MATCH_ID,
        kickoff_at=KICKOFF,
        retrieved_at=AS_OF,
        url=HEADER_URL,
        raw_payload=_header_payload(),
        expected_home="Internazionale",
        expected_away="Monza",
    )
    assert row["home_team"] == "Inter"
    assert row["source"]["source_kind"] == "official_fixture"
    assert row["source"]["native_fixture_id"] == MATCH_ID
    assert row["venue"] == {
        "name": "Stadio Giuseppe Meazza",
        "city": "Milan",
        "native_venue_id": "serie-a::Football_Stadium::" + "c" * 32,
    }


def test_parse_complete_pre_kickoff_lineup_is_model_eligible():
    header = parse_serie_a_header(
        json.loads(_header_payload()),
        fixture_id="espn:serie-a-1",
        match_id=MATCH_ID,
        kickoff_at=KICKOFF,
        retrieved_at=AS_OF,
        url=HEADER_URL,
        raw_payload=_header_payload(),
        expected_home="Internazionale",
        expected_away="Monza",
    )
    row = parse_serie_a_lineups(
        json.loads(_lineup_payload()),
        fixture_id="espn:serie-a-1",
        match_id=MATCH_ID,
        kickoff_at=KICKOFF,
        retrieved_at=AS_OF,
        url=LINEUP_URL,
        raw_payload=_lineup_payload(),
        header_source=header["source"],
    )
    assert row["lineups"]["confirmed"] is True
    assert row["lineups"]["model_eligible"] is True
    assert row["lineups"]["home"]["players"][0]["player_id"].startswith("seriea:")
    assert row["source"]["header_raw_sha256"] == header["source"]["raw_sha256"]


def test_incomplete_or_post_kickoff_lineup_is_display_only():
    header = parse_serie_a_header(
        json.loads(_header_payload()),
        fixture_id="espn:serie-a-1",
        match_id=MATCH_ID,
        kickoff_at=KICKOFF,
        retrieved_at=AS_OF,
        url=HEADER_URL,
        raw_payload=_header_payload(),
    )
    incomplete = parse_serie_a_lineups(
        json.loads(_lineup_payload(complete=False)),
        fixture_id="espn:serie-a-1",
        match_id=MATCH_ID,
        kickoff_at=KICKOFF,
        retrieved_at=AS_OF,
        url=LINEUP_URL,
        raw_payload=_lineup_payload(complete=False),
        header_source=header["source"],
    )
    assert incomplete["lineups"]["confirmed"] is False
    assert incomplete["lineups"]["model_eligible"] is False
    after = parse_serie_a_lineups(
        json.loads(_lineup_payload()),
        fixture_id="espn:serie-a-1",
        match_id=MATCH_ID,
        kickoff_at=KICKOFF,
        retrieved_at=datetime(2026, 8, 22, 17, tzinfo=timezone.utc),
        url=LINEUP_URL,
        raw_payload=_lineup_payload(),
        header_source=header["source"],
    )
    assert after["lineups"]["confirmed"] is True
    assert after["lineups"]["model_eligible"] is False


def test_fetch_serie_a_lineups_uses_bounded_public_api():
    result = fetch_serie_a_lineups(
        [
            {
                "id": "espn:serie-a-1",
                "competition_id": "serie-a",
                "status": "upcoming",
                "kickoff_at": KICKOFF,
                "home_team": "Internazionale",
                "away_team": "Monza",
            }
        ],
        now=AS_OF,
        opener=_Opener(),
    )
    assert result["status"] == "rights_blocked"
    assert result["fixtures"] == []
    assert result["lineups"] == []
    assert result["rights"]["source_id"] == "official_serie_a_lineups"


def test_fetch_incomplete_serie_a_lineup_remains_audit_only():
    result = fetch_serie_a_lineups(
        [
            {
                "id": "espn:serie-a-1",
                "competition_id": "serie-a",
                "status": "upcoming",
                "kickoff_at": KICKOFF,
                "home_team": "Internazionale",
                "away_team": "Monza",
            }
        ],
        now=AS_OF,
        opener=_Opener(complete=False),
    )
    assert result["status"] == "rights_blocked"
    assert result["fixtures"] == []
    assert result["lineups"] == []


def test_api_url_rejects_wrong_host_or_path():
    from league_platform.live_sources.seriea import parse_serie_a_header

    with pytest.raises(ValueError, match="API URL"):
        parse_serie_a_header(
            json.loads(_header_payload()),
            fixture_id="espn:serie-a-1",
            match_id=MATCH_ID,
            kickoff_at=KICKOFF,
            retrieved_at=AS_OF,
            url=HEADER_URL.replace("api-sdp.legaseriea.it", "evil.example"),
            raw_payload=_header_payload(),
        )
