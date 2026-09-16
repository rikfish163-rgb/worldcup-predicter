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


def test_laliga_lineup_in_legacy_self_authorized_snapshot_is_not_admitted(
    tmp_path: Path,
):
    as_of = datetime(2026, 8, 16, 4, 30, tzinfo=timezone.utc)
    kickoff = "2026-08-16T15:00:00+00:00"
    fixture_id = "espn:401882920"
    espn_source = _source(
        "ESPN",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
        as_of.isoformat(),
    )
    official_url = (
        "https://www.laliga.com/en-GB/match/"
        "temporada-2026-2027-laliga-ea-sports-r-racing-club-villarreal-cf-1"
    )
    official_source = _source("LaLiga official", official_url, as_of.isoformat())
    players = [
        {
            "player_id": f"laliga:{side}:{index}",
            "name": f"Player {side} {index}",
            "starter": True,
        }
        for side in ("home", "away")
        for index in range(11)
    ]
    fixture = {
        "id": fixture_id,
        "competition_id": "la-liga",
        "season": "2026",
        "kickoff_at": kickoff,
        "home_team": "Racing Santander",
        "away_team": "Villarreal",
        "status": "upcoming",
        "score": None,
        "home_provider_team_id": "87",
        "away_provider_team_id": "102",
        "source": {**espn_source, "native_fixture_id": "401882920"},
    }
    official_fixture = {
        "id": fixture_id,
        "competition_id": "la-liga",
        "kickoff_at": kickoff,
        "home_team": "R. Racing Club",
        "away_team": "Villarreal CF",
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
        "laliga_official": {
            "provider": "LaLiga official",
            "retrieved_at": as_of.isoformat(),
            "fixtures": [official_fixture],
            "lineups": [
                {
                    "fixture_id": fixture_id,
                    "match_id": "102255",
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
        "lineup_poll_diagnostics": {
            "la-liga": {
                "provider": "LaLiga official",
                "source_key": "laliga_official",
                "source_status": "ok",
                "state": "lineups_observed",
                "reason_code": "lineup_rows_observed",
                "requested": True,
                "candidate_count": 1,
                "invalid_kickoff_count": 0,
                "source_lineup_count": 1,
                "confirmed_lineup_count": 1,
                "model_eligible_lineup_count": 1,
                "error_count": 0,
            }
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
    assert "laliga_official" not in snapshot


def test_laliga_official_espanyol_display_alias_is_explicit():
    from league_platform.current import _official_join_name

    assert _official_join_name("la-liga", "RCD Espanyol de Barcelona") == "espanyol"
    assert _official_join_name("la-liga", "Espanyol") == "espanyol"
