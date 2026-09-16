from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from league_platform.current import EXPECTED_SOURCE_ROLES, attach_current_data


SHA256 = "a" * 64
COMPETITIONS = {
    "premier-league",
    "la-liga",
    "bundesliga",
    "serie-a",
    "ligue-1",
    "csl",
}


def _source(name: str, url: str, retrieved_at: str) -> dict[str, str]:
    return {
        "name": name,
        "url": url,
        "retrieved_at": retrieved_at,
        "raw_sha256": SHA256,
    }


def test_bundesliga_lineup_in_legacy_self_authorized_snapshot_is_not_admitted(
    tmp_path: Path,
):
    as_of = datetime(2026, 8, 14, 0, 30, tzinfo=timezone.utc)
    kickoff = "2026-08-14T01:00:00+00:00"
    fixture_id = "espn:bundesliga-1"
    espn_source = _source("ESPN", "https://site.api.espn.com/example", as_of.isoformat())
    official_source = _source(
        "Bundesliga official",
        "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1/fc-bayern-muenchen-vs-vfb-stuttgart/lineup",
        as_of.isoformat(),
    )
    players = [
        {
            "player_id": f"bundesliga:{side}:{index}",
            "name": f"Player {side} {index}",
            "starter": True,
        }
        for side in ("home", "away")
        for index in range(11)
    ]
    fixture = {
        "id": fixture_id,
        "competition_id": "bundesliga",
        "season": "2026",
        "kickoff_at": kickoff,
        "home_team": "Bayern Munich",
        "away_team": "VfB Stuttgart",
        "status": "upcoming",
        "score": None,
        "home_provider_team_id": "home-1",
        "away_provider_team_id": "away-1",
        "source": {**espn_source, "native_fixture_id": "bundesliga-1"},
    }
    official_fixture = {
        "id": fixture_id,
        "competition_id": "bundesliga",
        "kickoff_at": kickoff,
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "status": "upcoming",
        "source": official_source,
    }
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": sorted(COMPETITIONS),
        "roles": EXPECTED_SOURCE_ROLES,
        "espn": {
            "provider": "ESPN",
            "authorization_reference": "test-fixture-only",
            "commercial_reuse_verified_by_code": True,
            "errors": [],
            "fixtures": [fixture],
        },
        "understat": {"provider": "Understat", "errors": [], "team_features": []},
        "espn_markets": {
            "provider": "ESPN event summary",
            "errors": [],
            "markets": [],
            "team_status": [],
        },
        "bundesliga_official": {
            "provider": "Bundesliga official",
            "retrieved_at": as_of.isoformat(),
            "fixtures": [official_fixture],
            "lineups": [
                {
                    "fixture_id": fixture_id,
                    "match_id": None,
                    "lineups": {
                        "confirmed": True,
                        "model_eligible": True,
                        "home": {"players": players[:11]},
                        "away": {"players": players[11:]},
                    },
                    "source": official_source,
                }
            ],
            "errors": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    base = {
        "schema_version": "1.1.0",
        "generated_at": as_of.isoformat(),
        "timezone": "Asia/Shanghai",
        "summary": {},
        "competitions": [{"id": value} for value in sorted(COMPETITIONS)],
        "matches": [],
    }

    snapshot = attach_current_data(base, live_path, now=as_of)
    assert snapshot["matches"] == []
    assert snapshot["current_data"]["status"] == "rights_blocked"
    assert snapshot["current_data"]["role_contract"]["generation"] == "legacy"
    assert snapshot["current_data"]["model_admission"]["eligible"] is False
    assert "bundesliga_official" not in snapshot
