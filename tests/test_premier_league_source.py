from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.premier_league import (
    fetch_premier_league_fixtures,
    fetch_premier_league_lineups,
    parse_premier_league_fixtures_payload,
    parse_premier_league_lineups_payload,
)
from league_platform.live_sources.lineup_archive import archive_premier_league_lineups
from league_platform.intelligence import ObservationLedger


AS_OF = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
FIXTURE_URL = (
    "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/"
    "v1/competitions/8/seasons/2026/matchweeks/1/matches"
)
LINEUP_URL = (
    "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v3/matches/2645195/lineups"
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

    def read(self, _limit):
        return self.payload


class _Routes:
    def __init__(self, routes: dict[str, bytes | Exception]):
        self.routes = routes

    def open(self, request, timeout):
        assert timeout == 30
        route = next(
            (value for suffix, value in self.routes.items() if request.full_url.endswith(suffix)),
            None,
        )
        if isinstance(route, Exception):
            raise route
        if route is None:
            raise AssertionError(request.full_url)
        return _Response(route, request.full_url)


def _matchweek_payload() -> bytes:
    return json.dumps(
        {
            "data": [
                {
                    "matchId": "2645195",
                    "period": "PreMatch",
                    "kickoff": "2026-08-21 20:00:00",
                    "homeTeam": {"id": "3", "name": "Arsenal"},
                    "awayTeam": {"id": "9", "name": "Coventry City"},
                    "ground": "Emirates Stadium, London",
                }
            ]
        }
    ).encode()


def _lineup_payload() -> bytes:
    def team(offset: int) -> dict:
        players = [
            {
                "id": str(offset + index),
                "firstName": f"Player{index}",
                "lastName": "Test",
                "position": "Midfielder",
            }
            for index in range(1, 12)
        ]
        players.extend(
            {
                "id": str(offset + index),
                "firstName": f"Sub{index}",
                "lastName": "Test",
                "position": "Substitute",
                "subPosition": "Defender",
            }
            for index in range(12, 15)
        )
        return {
            "players": players,
            "formation": {
                "lineup": [[str(offset + index) for index in range(1, 12)]],
                "subs": [str(offset + index) for index in range(12, 15)],
                "formation": "4-3-3",
            },
        }

    return json.dumps({"home_team": team(0), "away_team": team(100)}).encode()


def test_official_fixture_parser_keeps_native_id_and_raw_hash():
    payload = _matchweek_payload()
    rows = parse_premier_league_fixtures_payload(
        payload,
        season="2026",
        retrieved_at=AS_OF,
        url=FIXTURE_URL,
    )
    assert rows[0]["id"] == "premierleague:2645195"
    assert rows[0]["premier_league_match_id"] == "2645195"
    assert rows[0]["kickoff_at"] == "2026-08-21T19:00:00+00:00"
    assert rows[0]["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()


def test_official_fixture_fetch_isolates_one_failed_matchweek():
    payload = _matchweek_payload()
    routes = _Routes(
        {"/matchweeks/1/matches": payload, "/matchweeks/2/matches": RuntimeError("offline")}
    )
    result = fetch_premier_league_fixtures(
        [2, 1, 1],
        season="2026",
        now=AS_OF,
        opener=routes,
    )
    assert result["fixtures"] == []
    assert result["errors"] == []
    assert result["status"] == "rights_blocked"
    assert result["rights"]["source_id"] == "premier_league_public_pages"


def test_official_lineup_parser_requires_full_xi_and_does_not_invent_minutes():
    payload = _lineup_payload()
    row = parse_premier_league_lineups_payload(
        payload,
        match_id="2645195",
        fixture_id="espn:fixture-1",
        kickoff_at="2026-08-21T19:00:00+00:00",
        retrieved_at=AS_OF,
        url=LINEUP_URL,
    )
    assert row["lineups"]["confirmed"] is True
    assert row["lineups"]["model_eligible"] is True
    assert sum(player.get("starter") is True for player in row["lineups"]["home"]["players"]) == 11
    assert all(player["expected_minutes"] is None for player in row["lineups"]["home"]["players"])
    assert row["source"]["time_basis"] == "observed_at_no_published_at"


def test_official_lineup_observed_after_kickoff_is_not_model_eligible():
    row = parse_premier_league_lineups_payload(
        _lineup_payload(),
        match_id="2645195",
        fixture_id="espn:fixture-1",
        kickoff_at="2026-08-21T19:00:00+00:00",
        retrieved_at=datetime(2026, 8, 21, 20, tzinfo=timezone.utc),
        url=LINEUP_URL,
    )
    assert row["lineups"]["confirmed"] is True
    assert row["lineups"]["model_eligible"] is False


def test_official_lineup_fetch_uses_explicit_mapping_and_isolates_errors():
    payload = _lineup_payload()
    routes = _Routes({"/api/v3/matches/2645195/lineups": payload})
    result = fetch_premier_league_lineups(
        [
            {
                "id": "espn:fixture-1",
                "premier_league_match_id": "2645195",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
            },
            {"id": "unmapped", "kickoff_at": "2026-08-21T19:00:00+00:00"},
        ],
        now=AS_OF,
        opener=routes,
    )
    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["errors"] == []


def test_official_lineup_fetch_uses_fixture_source_native_id_when_mapping_is_absent():
    payload = _lineup_payload()
    routes = _Routes({"/api/v3/matches/2645195/lineups": payload})
    result = fetch_premier_league_lineups(
        [
            {
                "id": "premierleague:2645195",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
                "source": {"native_fixture_id": "2645195"},
            }
        ],
        now=AS_OF,
        opener=routes,
    )
    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["rights"]["source_id"] == "official_premier_league_lineups"


def test_official_lineup_parser_rejects_unallowlisted_redirect():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_premier_league_lineups_payload(
            _lineup_payload(),
            match_id="2645195",
            fixture_id=None,
            kickoff_at="2026-08-21T19:00:00+00:00",
            retrieved_at=AS_OF,
            url="https://example.invalid/api/v3/matches/2645195/lineups",
        )


def test_official_lineup_archive_is_append_only_and_keeps_pre_kickoff_eligibility(tmp_path):
    row = parse_premier_league_lineups_payload(
        _lineup_payload(),
        match_id="2645195",
        fixture_id="espn:fixture-1",
        kickoff_at="2026-08-21T19:00:00+00:00",
        retrieved_at=AS_OF,
        url=LINEUP_URL,
    )
    ledger = tmp_path / "observations.jsonl"
    result = {"lineups": [row], "errors": []}
    assert archive_premier_league_lineups(result, ledger) == {
        "accepted": 1,
        "skipped_duplicate": 0,
        "skipped": 0,
        "source_errors": 0,
    }
    stored = ObservationLedger(ledger).read()
    assert stored[0].kind == "official_lineup"
    assert stored[0].enters_model is True
    assert stored[0].effective_at == AS_OF.isoformat()
    assert archive_premier_league_lineups(result, ledger)["skipped_duplicate"] == 1
