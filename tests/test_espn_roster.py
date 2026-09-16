from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.espn_roster import (
    DEFAULT_MAX_ROSTER_TEAMS,
    _candidate_teams,
    fetch_espn_team_rosters,
    parse_espn_roster,
)
from league_platform.player_evidence import snapshot_player_observations
from league_platform.build_offline_bundle import _compact_fixture_evidence


AS_OF = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def _athlete(index: int) -> dict:
    return {
        "id": str(1000 + index),
        "displayName": f"Player {index}",
        "position": {"displayName": "Midfielder", "abbreviation": "M"},
        "status": {"displayName": "Active", "type": "active"},
        "jersey": str(index + 1),
        "age": 20 + index,
        "citizenship": "Testland",
        "links": [
            {"href": f"https://www.espn.com/soccer/player/_/id/{1000 + index}/player-{index}"}
        ],
    }


def _payload(team_id: str = "10") -> bytes:
    return json.dumps(
        {
            "timestamp": "2026-08-18T00:00:00Z",
            "season": {"year": 2026, "displayName": "2026-27 Test League"},
            "team": {"id": team_id, "displayName": "Test FC", "abbreviation": "TST"},
            "athletes": [_athlete(index) for index in range(3)],
        }
    ).encode()


def _fixture(fixture_id: str, home_id: str, away_id: str) -> dict:
    return {
        "id": fixture_id,
        "competition_id": "premier-league",
        "status": "upcoming",
        "kickoff_at": "2026-08-18T12:00:00+00:00",
        "home_provider_team_id": home_id,
        "away_provider_team_id": away_id,
    }


def test_parse_espn_roster_is_bounded_and_display_only():
    row = parse_espn_roster(
        _payload(),
        competition_id="premier-league",
        provider_team_id="10",
        scheduled_fixture_ids=["espn:1"],
        scheduled_kickoffs=["2026-08-18T12:00:00+00:00"],
        retrieved_at=AS_OF,
        url="https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams/10/roster",
    )

    assert row["status"] == "ok"
    assert row["athlete_count"] == 3
    assert row["athletes"][0]["provider_player_id"] == "1000"
    assert row["athletes"][0]["position"] == "Midfielder"
    assert row["enters_model"] is False
    assert row["model_eligible"] is False
    assert row["source"]["raw_sha256"] == SHA or len(row["source"]["raw_sha256"]) == 64


def test_parse_espn_roster_rejects_team_identity_mismatch():
    with pytest.raises(ValueError, match="team id does not match"):
        parse_espn_roster(
            _payload("11"),
            competition_id="premier-league",
            provider_team_id="10",
            scheduled_fixture_ids=[],
            scheduled_kickoffs=[],
            retrieved_at=AS_OF,
            url="https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams/10/roster",
        )


def test_fetch_espn_team_rosters_ignores_payload_authorization_before_network():
    def opener(*_args, **_kwargs):
        raise AssertionError("ESPN roster network must remain blocked")

    result = fetch_espn_team_rosters(
        [
            _fixture("espn:1", "10", "11"),
            _fixture("espn:late", "12", "13").copy() | {"kickoff_at": "2026-08-30T12:00:00+00:00"},
        ],
        now=AS_OF,
        opener=opener,
        authorization_reference="test-fixture",
    )

    assert result["status"] == "rights_blocked"
    assert result["requested_teams"] == 0
    assert result["rosters"] == []
    assert result["errors"] == []
    assert result["network_opened"] is False


def test_roster_fanout_is_bounded_and_covers_more_than_legacy_forty_team_limit():
    fixtures = []
    for index in range(50):
        fixtures.append(_fixture(f"espn:{index}", str(100 + index * 2), str(101 + index * 2)))

    candidates, invalid, truncated = _candidate_teams(
        fixtures,
        reference_time=AS_OF,
        horizon_hours=72,
        max_teams=DEFAULT_MAX_ROSTER_TEAMS,
    )

    assert DEFAULT_MAX_ROSTER_TEAMS == 96
    assert len(candidates) == DEFAULT_MAX_ROSTER_TEAMS
    assert invalid == 0
    assert truncated == 4


def test_roster_fanout_clamps_an_oversized_caller_request():
    fixtures = []
    for index in range(50):
        fixtures.append(_fixture(f"espn:{index}", str(200 + index * 2), str(201 + index * 2)))

    candidates, invalid, truncated = _candidate_teams(
        fixtures,
        reference_time=AS_OF,
        horizon_hours=72,
        max_teams=10_000,
    )

    assert len(candidates) == DEFAULT_MAX_ROSTER_TEAMS
    assert invalid == 0
    assert truncated == 4


def test_roster_rows_become_team_level_player_evidence_only():
    snapshot = {
        "espn_rosters": {
            "rosters": [
                {
                    "competition_id": "premier-league",
                    "provider_team_id": "10",
                    "team_name": "Test FC",
                    "retrieved_at": AS_OF.isoformat(),
                    "athletes": [
                        {
                            "provider_player_id": "1000",
                            "name": "Player 0",
                            "position": "Midfielder",
                        }
                    ],
                    "source": {
                        "name": "ESPN team roster",
                        "url": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams/10/roster",
                        "retrieved_at": AS_OF.isoformat(),
                        "raw_sha256": SHA,
                    },
                }
            ]
        }
    }
    rows = snapshot_player_observations(snapshot, fallback=AS_OF.isoformat())

    assert len(rows) == 1
    assert rows[0].kind == "player_roster"
    assert rows[0].fixture_id is None
    assert rows[0].team_id is None
    assert rows[0].enters_model is False
    assert rows[0].model_exclusion_reason == "player_identity_not_canonical"


def test_roster_projection_joins_only_the_scheduled_fixture_and_stays_display_only():
    snapshot = {
        "espn": {
            "fixtures": [
                {
                    "id": "espn:1",
                    "home_provider_team_id": "10",
                    "away_provider_team_id": "11",
                }
            ]
        },
        "espn_rosters": {
            "rosters": [
                {
                    "provider_team_id": "10",
                    "team_name": "Test FC",
                    "scheduled_fixture_ids": ["espn:1", "espn:unlisted"],
                    "retrieved_at": AS_OF.isoformat(),
                    "athletes": [{"provider_player_id": "1000", "name": "Player 0"}],
                    "source": {
                        "name": "ESPN team roster",
                        "url": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams/10/roster",
                        "retrieved_at": AS_OF.isoformat(),
                        "raw_sha256": SHA,
                    },
                }
            ]
        },
    }
    evidence = _compact_fixture_evidence(
        snapshot,
        [{"id": "espn:1", "kickoff_at": "2026-08-18T12:00:00+00:00"}],
    )

    assert len(evidence["espn:1"]["lineups"]) == 1
    row = evidence["espn:1"]["lineups"][0]
    assert row["stage"] == "roster_evidence"
    assert row["confirmed"] is False
    assert row["modelEligible"] is False
    assert row["players"][0]["provider_player_id"] == "1000"
    assert "espn:unlisted" not in evidence
