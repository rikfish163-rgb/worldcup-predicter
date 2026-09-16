from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from league_platform.current import (
    EXPECTED_SOURCE_ROLES,
    _official_join_name,
    _validate_official_schedule_overlay,
    attach_current_data,
)


AS_OF = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
SHA256 = "a" * 64
SEASON = "b" * 32
MATCH = "c" * 32
HEADER_URL = (
    "https://api-sdp.legaseriea.it/v1/serie-a/football/seasons/"
    "serie-a%3A%3AFootball_Season%3A%3A"
    f"{SEASON}/matches/serie-a%3A%3AFootball_Match%3A%3A{MATCH}/header"
)
LINEUP_URL = HEADER_URL.removesuffix("/header") + "/lineups"


def _source(url: str) -> dict[str, str]:
    return {
        "name": "Serie A official",
        "url": url,
        "retrieved_at": AS_OF.isoformat(),
        "raw_sha256": SHA256,
    }


def test_serie_a_schedule_overlay_accepts_only_the_official_header_source():
    official_source = {
        **_source(HEADER_URL),
        "native_fixture_id": MATCH,
        "native_match_id": MATCH,
        "source_kind": "official_fixture",
    }
    fixture = {
        "id": "openfootball:serie-a:inter-monza",
        "competition_id": "serie-a",
        "kickoff_at": "2026-08-22T16:30:00+00:00",
        "kickoff_time_source": "Serie A official",
        "kickoff_time_observed_at": AS_OF.isoformat(),
        "provider_fixture_ids": {"serie_a_official": MATCH},
        "field_sources": {
            "kickoff_at": official_source,
            "venue": official_source,
        },
        "schedule_overlay": {
            "provider": "Serie A official",
            "provider_fixture_id": MATCH,
            "join_basis": "exact_competition_and_explicit_canonical_team_pair",
            "observed_at": AS_OF.isoformat(),
            "prior_kickoff_at": "2026-08-22T16:30:00+00:00",
            "kickoff_delta_seconds": 0,
            "fields": ["kickoff_at", "venue"],
        },
        "venue": {
            "name": "Stadio Giuseppe Meazza",
            "city": "Milan",
            "country": "Italy",
            "country_code": "IT",
            "source": official_source,
        },
    }

    _validate_official_schedule_overlay(fixture, AS_OF)

    tampered = json.loads(json.dumps(fixture))
    tampered["field_sources"]["kickoff_at"]["url"] = (
        "https://example.com/v1/serie-a/header"
    )
    with pytest.raises(ValueError, match="official schedule overlay"):
        _validate_official_schedule_overlay(tampered, AS_OF)


def test_serie_a_lineup_in_legacy_self_authorized_snapshot_is_not_admitted(
    tmp_path: Path,
):
    fixture_id = "espn:serie-a-1"
    kickoff = "2026-08-22T16:30:00+00:00"
    players = [
        {
            "player_id": f"seriea:{side}:{index}",
            "name": f"Player {side} {index}",
            "starter": True,
        }
        for side in ("home", "away")
        for index in range(11)
    ]
    espn_source = {
        "name": "ESPN",
        "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard",
        "retrieved_at": AS_OF.isoformat(),
        "raw_sha256": SHA256,
        "native_fixture_id": "401000001",
    }
    official_source = _source(LINEUP_URL)
    live = {
        "schema_version": "1.0.0",
        "as_of": AS_OF.isoformat(),
        "expected_competitions": ["serie-a"],
        "roles": EXPECTED_SOURCE_ROLES,
        "espn": {
            "provider": "ESPN",
            "authorization_reference": "test-fixture-only",
            "commercial_reuse_verified_by_code": True,
            "errors": [],
            "fixtures": [
                {
                    "id": fixture_id,
                    "competition_id": "serie-a",
                    "season": "2026",
                    "kickoff_at": kickoff,
                    "home_team": "Internazionale",
                    "away_team": "Monza",
                    "home_provider_team_id": "inter",
                    "away_provider_team_id": "monza",
                    "status": "upcoming",
                    "score": None,
                    "source": espn_source,
                }
            ],
        },
        "espn_markets": {"provider": "ESPN event summary", "errors": [], "markets": [], "team_status": []},
        "understat": {"provider": "Understat", "errors": [], "team_features": []},
        "serie_a_official": {
            "provider": "Serie A official",
            "fixtures": [
                {
                    "id": fixture_id,
                    "competition_id": "serie-a",
                    "kickoff_at": kickoff,
                    "home_team": "Inter",
                    "away_team": "Monza",
                    "status": "upcoming",
                    "source": _source(HEADER_URL),
                }
            ],
            "lineups": [
                {
                    "fixture_id": fixture_id,
                    "match_id": MATCH,
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
        "generated_at": AS_OF.isoformat(),
        "timezone": "Asia/Shanghai",
        "summary": {},
        "competitions": [{"id": "serie-a"}],
        "matches": [],
    }

    snapshot = attach_current_data(base, live_path, now=AS_OF)
    assert _official_join_name("serie-a", "Internazionale") == "internazionale"
    assert _official_join_name("serie-a", "Inter") == "internazionale"
    assert snapshot["matches"] == []
    assert snapshot["current_data"]["status"] == "rights_blocked"
    assert snapshot["current_data"]["role_contract"]["generation"] == "legacy"
    assert snapshot["current_data"]["model_admission"]["eligible"] is False
    assert "serie_a_official" not in snapshot
