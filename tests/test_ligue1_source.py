import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.ligue1 import (
    LIGUE1_API_ROOT,
    fetch_ligue1_lineups,
    parse_ligue1_match_detail,
)


NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
KICKOFF = "2026-08-21T18:45:00+00:00"
MATCH_ID = "l1_championship_match_73825"
DETAIL_URL = f"{LIGUE1_API_ROOT}/championship-match/{MATCH_ID}"


def _player(number: int, *, starter: bool = False) -> dict:
    row = {
        "id": f"l1_championship_player_2026_1_{number}",
        "playerIdentity": {
            "id": f"l1_championship_player_2026_1_{number}",
            "firstName": "Player",
            "lastName": str(number),
            "jerseyNumber": number,
        },
    }
    if starter:
        row["formationPlace"] = number
    return row


def _detail(*, period: str = "preMatch", confirmed: bool = False) -> dict:
    def side(name: str) -> dict:
        rows = [_player(index, starter=confirmed) for index in range(1, 12)]
        if not confirmed:
            rows.extend(_player(index + 11) for index in range(1, 3))
        return {
            "clubIdentity": {"officialName": name, "name": name},
            "players": {row["id"]: row for row in rows},
        }

    return {
        "id": MATCH_ID,
        "championshipId": 1,
        "date": KICKOFF,
        "period": period,
        "home": side("Marseille"),
        "away": side("Strasbourg"),
    }


def test_pre_match_roster_is_display_only():
    raw = json.dumps(_detail()).encode()
    row = parse_ligue1_match_detail(
        _detail(),
        fixture_id="espn:1",
        kickoff_at=KICKOFF,
        retrieved_at=NOW,
        url=DETAIL_URL,
        raw_payload=raw,
        expected_home="Marseille",
        expected_away="Strasbourg",
    )
    assert row["lineups"]["confirmed"] is False
    assert row["lineups"]["model_eligible"] is False
    assert row["lineups"]["diagnostic"]["reason"] == "official_feed_returned_roster_only"


def test_confirmed_pre_kickoff_xi_is_model_eligible():
    payload = _detail(period="preMatchWithPlayers", confirmed=True)
    row = parse_ligue1_match_detail(
        payload,
        fixture_id="espn:1",
        kickoff_at=KICKOFF,
        retrieved_at=NOW,
        url=DETAIL_URL,
        raw_payload=json.dumps(payload).encode(),
        expected_home="Marseille",
        expected_away="Strasbourg",
    )
    assert row["lineups"]["confirmed"] is True
    assert row["lineups"]["model_eligible"] is True
    assert sum(item["starter"] for item in row["lineups"]["home"]["players"]) == 11


def test_post_kickoff_observation_never_enters_model():
    payload = _detail(period="fullTime", confirmed=True)
    row = parse_ligue1_match_detail(
        payload,
        fixture_id="espn:1",
        kickoff_at=KICKOFF,
        retrieved_at=datetime(2026, 8, 21, 19, tzinfo=timezone.utc),
        url=DETAIL_URL,
        raw_payload=json.dumps(payload).encode(),
    )
    assert row["lineups"]["confirmed"] is True
    assert row["lineups"]["model_eligible"] is False
    assert row["lineups"]["diagnostic"]["status"] == "post_kickoff_observation"


class _Response:
    def __init__(self, url: str, payload: bytes):
        self._url = url
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self._url

    def read(self, _limit=-1):
        return self._payload


class _Opener:
    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses

    def open(self, request, timeout=0):
        key = request.full_url.split("?", 1)[0]
        for url, payload in self.responses.items():
            if key == url.split("?", 1)[0]:
                return _Response(request.full_url, payload)
        raise AssertionError(f"unexpected URL: {request.full_url}")


def test_fetch_uses_calendar_and_exact_native_match_join():
    calendar_url = f"{LIGUE1_API_ROOT}/championship-calendar/1?season=2026"
    week_url = f"{LIGUE1_API_ROOT}/championship-matches/championship/1/game-week/1?season=2026"
    detail_url = DETAIL_URL
    calendar = {
        "gameWeeks": {
            "1": {
                "gameWeekNumber": 1,
                "startDate": "2026-08-21T18:45:00Z",
                "endDate": "2026-08-23T18:45:00Z",
            }
        }
    }
    summary = {
        "matches": [
            {
                "matchId": MATCH_ID,
                "date": KICKOFF,
                "home": {"clubIdentity": {"officialName": "Marseille"}},
                "away": {"clubIdentity": {"officialName": "Strasbourg"}},
            }
        ]
    }
    detail = _detail()
    opener = _Opener(
        {
            calendar_url: json.dumps(calendar).encode(),
            week_url: json.dumps(summary).encode(),
            detail_url: json.dumps(detail).encode(),
        }
    )
    result = fetch_ligue1_lineups(
        [
            {
                "id": "espn:1",
                "competition_id": "ligue-1",
                "status": "upcoming",
                "kickoff_at": KICKOFF,
                "home_team": "Marseille",
                "away_team": "Strasbourg",
            }
        ],
        now=NOW,
        opener=opener,
    )
    assert result["status"] == "rights_blocked"
    assert result["fixtures"] == []
    assert result["lineups"] == []
    assert result["rights"]["source_id"] == "official_ligue1_lineups"


def test_fetch_does_not_accept_wrong_team_or_time():
    with pytest.raises(ValueError):
        parse_ligue1_match_detail(
            _detail(),
            fixture_id="espn:1",
            kickoff_at=KICKOFF,
            retrieved_at=NOW,
            url=DETAIL_URL,
            raw_payload=b"{}",
            expected_home="Paris Saint-Germain",
        )
