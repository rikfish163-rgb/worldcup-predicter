from datetime import datetime, timezone
from copy import deepcopy
import json
from pathlib import Path

import pytest

from league_platform.current import CURRENT_SOURCE_ROLES, attach_current_data
from league_platform.snapshot import build_platform_snapshot


SHA256 = "a" * 64


def test_openfootball_fixture_feed_is_the_current_readmodel_identity(tmp_path: Path) -> None:
    as_of = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
    source = {
        "name": "OpenFootball",
        "source_id": "openfootball:football.json:2026-27:en.1",
        "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
        "retrieved_at": as_of.isoformat(),
        "raw_sha256": SHA256,
        "hash": f"sha256:{SHA256}",
        "license": "CC0-1.0",
    }
    official_source = {
        "name": "Premier League official",
        "url": (
            "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v1/"
            "competitions/8/seasons/2026/matchweeks/2/matches"
        ),
        "retrieved_at": "2026-08-23T23:59:00+00:00",
        "raw_sha256": "c" * 64,
        "native_fixture_id": "2645209",
        "source_kind": "official_fixture",
    }
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": [
            "bundesliga",
            "championship",
            "csl",
            "la-liga",
            "ligue-1",
            "premier-league",
            "serie-a",
        ],
        "roles": CURRENT_SOURCE_ROLES,
        "fixture_feed": {
            "provider": "OpenFootball",
            "retrieved_at": as_of.isoformat(),
            "status": "ok",
            "errors": [],
            "fixtures": [
                {
                    "id": "openfootball:premier-league:fixture-one",
                    "competition_id": "premier-league",
                    "season": "2026-27",
                    "kickoff_at": "2026-08-25T18:00:00+00:00",
                    "kickoff_date": "2026-08-25",
                    "kickoff_time_quality": "exact",
                    "kickoff_time_source": "Premier League official",
                    "kickoff_time_observed_at": "2026-08-23T23:59:00+00:00",
                    "provider_fixture_ids": {
                        "OpenFootball": "openfootball:premier-league:fixture-one",
                        "premier_league_official": "2645209"
                    },
                    "field_sources": {
                        "kickoff_at": official_source,
                        "venue": official_source,
                    },
                    "schedule_overlay": {
                        "provider": "Premier League official",
                        "provider_fixture_id": "2645209",
                        "join_basis": (
                            "exact_competition_and_explicit_canonical_team_pair"
                        ),
                        "observed_at": "2026-08-23T23:59:00+00:00",
                        "prior_kickoff_at": "2026-08-25T19:00:00+00:00",
                        "kickoff_delta_seconds": -3600,
                        "fields": ["kickoff_at", "venue"],
                    },
                    "venue": {
                        "name": "Emirates Stadium",
                        "city": "London",
                        "country": "England",
                        "country_code": "GB",
                        "source": official_source,
                    },
                    "home_team": "Arsenal FC",
                    "away_team": "Manchester City FC",
                    "status": "upcoming",
                    "score": None,
                    "result_scope": None,
                    "source": source,
                    "lineage": {
                        **source,
                        "source_row_id": "openfootball:football.json:2026-27:en.1#$.matches[0]",
                        "record_path": "$.matches[0]",
                    },
                }
            ],
            "date_only_fixtures": [],
        },
        "espn": {
            "provider": "ESPN",
            "status": "rights_blocked",
            "fixtures": [],
            "errors": [],
            "network_opened": False,
        },
        "espn_markets": {
            "provider": "ESPN event summary",
            "status": "rights_blocked",
            "markets": [],
            "team_status": [],
            "errors": [],
            "network_opened": False,
        },
        "espn_rosters": {
            "provider": "ESPN team roster",
            "status": "rights_blocked",
            "rosters": [],
            "errors": [],
            "network_opened": False,
        },
        "understat": {"provider": "Understat", "team_features": [], "errors": []},
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")

    result = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")),
        live_path,
        now=as_of,
    )

    current = result["current_data"]
    assert current["status"] == "research_only"
    assert current["role_contract"]["generation"] == "legacy"
    assert current["role_contract"]["model_admission"] is False
    assert current["model_admission"] == {
        "eligible": False,
        "reason": "consumer_raw_replay_required",
        "status": "blocked",
    }
    assert current["canonical_fixture_provider"] == "OpenFootball"
    assert current["canonical_fixture_count"] == 1
    assert "espn_fixture_count" not in current
    row = next(
        item
        for item in result["matches"]
        if item.get("id") == "openfootball:premier-league:fixture-one"
    )
    assert row["source"]["name"] == "OpenFootball"
    assert row["source"]["license"] == "CC0-1.0"
    assert row["home_team"] == "Arsenal FC"
    assert row["away_team"] == "Manchester City FC"
    assert row["home_team_canonical"] == "Arsenal"
    assert row["away_team_canonical"] == "Man City"
    assert row["identity_resolution"] == {
        "scheme": "competition_scoped_explicit_alias_v1",
        "home": {
            "provider_name": "Arsenal FC",
            "canonical_name": "Arsenal",
            "canonical_team_id": "premier-league:arsenal",
            "resolution": "explicit_alias",
        },
        "away": {
            "provider_name": "Manchester City FC",
            "canonical_name": "Man City",
            "canonical_team_id": "premier-league:man-city",
            "resolution": "explicit_alias",
        },
    }
    # The official overlay remains validation evidence in the immutable input,
    # but its blocked material fields never cross the model/read projection.
    assert "kickoff_time_observed_at" not in row
    assert "field_sources" not in row
    assert "schedule_overlay" not in row
    assert "venue" not in row
    assert row["current_features"] == {}
    assert row["model_admission"]["eligible"] is False
    assert row["source_verification"]["verified_by_consumer"] is False

    tampered = deepcopy(live)
    tampered["fixture_feed"]["fixtures"][0]["field_sources"]["kickoff_at"][
        "url"
    ] = "https://example.com/api/v1/competitions/8/seasons/2026/matchweeks/2/matches"
    tampered_path = tmp_path / "tampered-current.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="official schedule overlay"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")),
            tampered_path,
            now=as_of,
        )

    tampered_identity = deepcopy(live)
    tampered_identity["fixture_feed"]["fixtures"][0]["provider_fixture_ids"][
        "OpenFootball"
    ] = "openfootball:premier-league:different"
    tampered_identity_path = tmp_path / "tampered-identity-current.json"
    tampered_identity_path.write_text(json.dumps(tampered_identity), encoding="utf-8")
    with pytest.raises(ValueError, match="OpenFootball fixture source contract"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")),
            tampered_identity_path,
            now=as_of,
        )
