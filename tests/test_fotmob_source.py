from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.fotmob import (
    fetch_fotmob_lineups,
    parse_fotmob_match_details_payload,
    parse_fotmob_matches_payload,
)


AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
SCHEDULE_URL = "https://www.fotmob.com/api/data/matches?date=20260814"
DETAILS_URL = "https://www.fotmob.com/api/data/matchDetails?matchId=12345"


def _schedule() -> bytes:
    return json.dumps({"leagues": [{"name": "Example", "matches": [{
        "id": 12345,
        "status": {"utcTime": "2026-08-14T18:00:00.000Z", "finished": False},
        "home": {"id": 1, "name": "Arsenal"},
        "away": {"id": 2, "name": "Coventry City"},
    }]}]}).encode()


def _details(*, lineup_type: str = "standard", starters: int = 11) -> bytes:
    def team(team_id: int, name: str):
        return {
            "id": team_id,
            "name": name,
            "formation": "4-3-3",
            "starters": [{"id": team_id * 100 + i, "name": f"Player {i}"} for i in range(starters)],
            "subs": [{"id": team_id * 1000 + 1, "name": "Sub"}],
        }
    return json.dumps({"content": {"lineup": {
        "lineupType": lineup_type,
        "homeTeam": team(1, "Arsenal"),
        "awayTeam": team(2, "Coventry City"),
    }}}).encode()


class _Response:
    def __init__(self, payload: bytes, url: str):
        self.payload, self.url = payload, url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, _limit):
        return self.payload


class _Routes:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request.full_url)
        return _Response(self.routes[request.full_url], request.full_url)


def test_fotmob_schedule_and_details_are_hashed_and_causally_eligible():
    schedule = _schedule()
    events = parse_fotmob_matches_payload(schedule, retrieved_at=AS_OF, url=SCHEDULE_URL)
    assert events[0]["event_id"] == "12345"
    assert events[0]["source"]["raw_sha256"] == hashlib.sha256(schedule).hexdigest()
    details = _details()
    row = parse_fotmob_match_details_payload(
        details,
        fixture_id="espn:1",
        event_id="12345",
        kickoff_at="2026-08-14T18:00:00+00:00",
        home_team="Arsenal",
        away_team="Coventry City",
        retrieved_at=AS_OF,
        url=DETAILS_URL,
    )
    assert row["lineups"]["confirmed"] is True
    assert row["lineups"]["model_eligible"] is True
    assert len(row["lineups"]["home"]["starters"]) == 11
    assert row["source"]["effective_at"] is None


def test_fotmob_predicted_or_partial_lineup_never_enters_model():
    for payload in (_details(lineup_type="predicted"), _details(starters=10)):
        row = parse_fotmob_match_details_payload(
            payload,
            fixture_id="espn:1",
            event_id="12345",
            kickoff_at="2026-08-14T18:00:00+00:00",
            home_team="Arsenal",
            away_team="Coventry City",
            retrieved_at=AS_OF,
            url=DETAILS_URL,
        )
        assert row["lineups"]["confirmed"] is False
        assert row["lineups"]["model_eligible"] is False


def test_fotmob_robots_override_is_ignored_and_fetch_stays_rights_blocked():
    routes = _Routes({SCHEDULE_URL: _schedule(), DETAILS_URL: _details()})
    result = fetch_fotmob_lineups(
        [{
            "id": "espn:1",
            "status": "upcoming",
            "kickoff_at": "2026-08-14T18:00:00+00:00",
            "home_team": "Arsenal",
            "away_team": "Coventry City",
        }],
        now=AS_OF,
        opener=routes,
        allow_robots_disallowed=True,
    )
    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["errors"] == []
    assert result["network_opened"] is False
    assert result["rights"]["source_id"] == "fotmob_public_api"
    assert result["rights"]["ignored_inputs"] == ["config"]
    assert routes.calls == []


def test_fotmob_rejects_unallowlisted_url():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_fotmob_matches_payload(_schedule(), retrieved_at=AS_OF, url="https://evil.example/api/data/matches?date=20260814")


def test_fotmob_default_fetch_uses_canonical_rights_block():
    result = fetch_fotmob_lineups([], now=AS_OF)
    assert result["status"] == "rights_blocked"
    assert result["lineups"] == []
    assert result["network_opened"] is False
    assert result["rights"]["source_id"] == "fotmob_public_api"
    assert result["rights"]["ignored_inputs"] == []
