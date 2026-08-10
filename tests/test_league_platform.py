from __future__ import annotations

import gzip
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from league_platform.app import create_server
from league_platform.catalog import LEAGUES, get_league
from league_platform.current import attach_current_data
from league_platform.dixon_coles import evaluate_dixon_coles
from league_platform.live_sources.espn import parse_espn_payload
from league_platform.live_sources.espn_market import parse_espn_market
from league_platform.live_sources.understat import (
    aggregate_understat_payload,
    decode_understat_payload,
    fetch_understat_features,
)
from league_platform.identity import canonical_team_name, team_id
from league_platform.future import build_future_predictions
from league_platform.model import evaluate_league
from league_platform.snapshot import build_platform_snapshot
from league_platform.store import PlatformStore
from league_platform.sources.match_history import MatchHistorySource
from league_platform.sources.openfootball import OpenFootballSource


EXPECTED_LEAGUES = {"premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1", "csl"}
TEST_SHA256 = "a" * 64
TEST_ROLES = {
    "fixtures_and_results": "ESPN",
    "recent_xg_and_form": "Understat",
    "historical_training": ["football-data.co.uk", "OpenFootball"],
    "current_market": "ESPN event summary / named bookmaker",
    "injuries_and_lineups": None,
}


def _espn_source(as_of: datetime, native_id: str) -> dict:
    return {
        "name": "ESPN",
        "url": "https://site.api.espn.com/example",
        "native_fixture_id": native_id,
        "retrieved_at": as_of.isoformat(),
        "raw_sha256": TEST_SHA256,
    }


def _coverage_fixtures(as_of: datetime, *, exclude: str = "premier-league") -> list[dict]:
    fixtures = []
    for index, competition_id in enumerate(sorted(EXPECTED_LEAGUES - {exclude}), start=10):
        native_id = f"coverage-{index}"
        fixtures.append(
            {
                "id": f"espn:{native_id}",
                "competition_id": competition_id,
                "season": "2026",
                "kickoff_at": "2026-08-22T19:00:00+00:00",
                "home_team": f"Coverage Home {index}",
                "away_team": f"Coverage Away {index}",
                "status": "upcoming",
                "score": None,
                "home_provider_team_id": f"h{index}",
                "away_provider_team_id": f"a{index}",
                "source": _espn_source(as_of, native_id),
            }
        )
    return fixtures


def test_data_manifest_pins_all_six_league_inputs():
    manifest = json.loads(Path("league_platform/data_manifest.json").read_text())

    assert manifest["provider"] == "multiple"
    assert len(manifest["files"]) == 18
    assert all(item["url"].startswith("https://") for item in manifest["files"])
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_espn_current_fixture_contract_preserves_native_ids_and_as_of():
    payload = json.dumps(
        {
            "events": [
                {
                    "id": "401",
                    "date": "2026-08-21T19:00Z",
                    "season": {"year": 2026},
                    "status": {"type": {"name": "STATUS_SCHEDULED"}},
                    "competitions": [
                        {
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "team": {"id": "359", "displayName": "Arsenal"},
                                    "score": "0",
                                },
                                {
                                    "homeAway": "away",
                                    "team": {"id": "388", "displayName": "Coventry City"},
                                    "score": "0",
                                },
                            ]
                        }
                    ],
                }
            ]
        }
    ).encode()
    as_of = datetime(2026, 8, 10, tzinfo=timezone.utc)

    fixtures = parse_espn_payload(
        payload,
        competition_id="premier-league",
        retrieved_at=as_of,
        url="https://site.api.espn.com/example",
    )

    assert fixtures[0]["id"] == "espn:401"
    assert fixtures[0]["status"] == "upcoming"
    assert fixtures[0]["home_provider_team_id"] == "359"
    assert fixtures[0]["source"]["retrieved_at"] == as_of.isoformat()
    assert len(fixtures[0]["source"]["raw_sha256"]) == 64


def test_espn_market_is_devigged_and_bound_to_native_fixture():
    payload = json.dumps(
        {
            "pickcenter": [
                {
                    "provider": {"name": "ExampleBook"},
                    "homeTeamOdds": {"moneyLine": -150},
                    "drawOdds": {"moneyLine": 300},
                    "awayTeamOdds": {"moneyLine": 400},
                }
            ]
        }
    ).encode()
    as_of = datetime(2026, 8, 10, tzinfo=timezone.utc)

    market = parse_espn_market(
        payload,
        fixture_id="espn:401",
        retrieved_at=as_of,
        url="https://site.api.espn.com/example",
    )

    assert market["fixture_id"] == "espn:401"
    assert market["provider"] == "ExampleBook"
    assert abs(sum(market["probability"].values()) - 1) < 1e-5
    assert market["source"]["raw_sha256"]


def test_espn_market_rejects_non_finite_odds():
    payload = json.dumps(
        {
            "pickcenter": [
                {
                    "homeTeamOdds": {"moneyLine": math.nan},
                    "drawOdds": {"moneyLine": 300},
                    "awayTeamOdds": {"moneyLine": 400},
                }
            ]
        }
    ).encode()

    assert (
        parse_espn_market(
            payload,
            fixture_id="espn:bad-market",
            retrieved_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            url="https://site.api.espn.com/example",
        )
        is None
    )


def test_understat_form_uses_only_results_before_as_of():
    dates = []
    for day in range(1, 8):
        dates.append(
            {
                "id": str(day),
                "datetime": f"2026-05-{day:02d} 15:00:00",
                "isResult": True,
                "h": {"id": "1", "title": "Arsenal"},
                "a": {"id": str(day + 1), "title": f"Team {day}"},
                "xG": {"h": str(day), "a": "1.0"},
                "goals": {"h": str(day % 3), "a": "1"},
            }
        )
    dates.append(
        {
            "id": "future",
            "datetime": "2026-09-01 15:00:00",
            "isResult": True,
            "h": {"id": "1", "title": "Arsenal"},
            "a": {"id": "99", "title": "Future"},
            "xG": {"h": "99", "a": "99"},
            "goals": {"h": "9", "a": "9"},
        }
    )
    as_of = datetime(2026, 8, 10, tzinfo=timezone.utc)

    observations = aggregate_understat_payload(
        json.dumps({"dates": dates}).encode(),
        competition_id="premier-league",
        retrieved_at=as_of,
        url="https://understat.com/example",
    )
    arsenal = next(item for item in observations if item["provider_team_id"] == "1")

    assert arsenal["sample_n"] == 5
    assert arsenal["xg_for"] == 5.0
    assert arsenal["last_match_at"].startswith("2026-05-07")


def test_understat_gzip_payload_can_be_decoded_before_aggregation():
    payload = gzip.compress(json.dumps({"dates": []}).encode())

    assert json.loads(decode_understat_payload(payload))["dates"] == []


def test_understat_gzip_payload_has_decompressed_size_limit():
    oversized = gzip.compress(b"x" * 1025)

    with pytest.raises(ValueError, match="decompressed payload exceeded"):
        decode_understat_payload(oversized, max_bytes=1024)


def test_understat_bootstrap_failure_is_source_level_degradation():
    class BrokenOpener:
        def open(self, *_args, **_kwargs):
            raise OSError("offline")

    result = fetch_understat_features(
        now=datetime(2026, 8, 10, tzinfo=timezone.utc), opener=BrokenOpener()
    )

    assert result["team_features"] == []
    assert result["errors"][0]["stage"] == "cookie_bootstrap"


def test_matchline_rejects_remote_bind_without_explicit_opt_in(tmp_path):
    with pytest.raises(ValueError, match="remote binding requires"):
        create_server("0.0.0.0", 0, tmp_path)


def test_catalog_contains_big_five_and_chinese_super_league():
    assert {league.id for league in LEAGUES} == EXPECTED_LEAGUES
    assert get_league("csl").name_zh == "中超"
    assert all(league.timezone for league in LEAGUES)


def test_team_registry_aligns_provider_aliases_without_merging_promoted_clubs():
    assert canonical_team_name("premier-league", "Manchester City") == "Man City"
    assert team_id("premier-league", "Manchester City") == team_id("premier-league", "Man City")
    assert canonical_team_name("csl", "Shanghai Port") == "Shanghai Port FC"
    assert canonical_team_name("premier-league", "Coventry City") == "Coventry City"


def test_match_history_source_loads_real_cached_big_five_data():
    source = MatchHistorySource(Path("data/MatchHistory"))

    premier_league = source.load("premier-league")

    assert premier_league.status == "stale"
    assert premier_league.matches
    assert premier_league.matches[0].competition_id == "premier-league"
    assert premier_league.matches[0].status == "finished"
    assert premier_league.matches[0].score is not None
    assert premier_league.matches[0].home_team_id.startswith("premier-league:")
    assert len(premier_league.matches[0].source_sha256) == 64
    assert premier_league.matches[0].provider_fixture_id.endswith(tuple(str(n) for n in range(10)))
    assert premier_league.latest_event_at is not None


def test_openfootball_source_loads_three_complete_csl_seasons():
    source = OpenFootballSource(Path("data/MatchHistory"))

    csl = source.load("csl")

    assert csl.status == "stale"
    assert len(csl.matches) == 786
    assert {match.season for match in csl.matches} == {"2022", "2023", "2024"}
    assert all(match.source_name == "OpenFootball" for match in csl.matches)
    assert all(match.source_license_status == "CC0-1.0" for match in csl.matches)


def test_platform_snapshot_is_source_backed_and_deduplicated():
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)

    snapshot = build_platform_snapshot(Path("data/MatchHistory"), now=now)

    assert snapshot["generated_at"] == now.isoformat()
    assert len(snapshot["competitions"]) == 6
    assert snapshot["summary"]["finished_matches"] == 6190
    assert snapshot["summary"]["available_competitions"] == 6
    assert snapshot["summary"]["unavailable_competitions"] == 0

    match_ids = [match["id"] for match in snapshot["matches"]]
    assert len(match_ids) == len(set(match_ids))
    assert {match["source"]["name"] for match in snapshot["matches"]} == {
        "football-data.co.uk",
        "OpenFootball",
    }
    assert all(match["source"]["sha256"] for match in snapshot["matches"])
    assert all(match["home_team_id"] and match["away_team_id"] for match in snapshot["matches"])


def test_snapshot_exposes_quality_metrics_instead_of_unqualified_accuracy():
    snapshot = build_platform_snapshot(Path("data/MatchHistory"))

    premier_league = next(
        item for item in snapshot["competitions"] if item["id"] == "premier-league"
    )

    assert premier_league["data_quality"]["duplicate_fixture_rate"] == 0.0
    assert premier_league["data_quality"]["score_completeness"] == 1.0
    assert premier_league["data_quality"]["odds_completeness"] == 1.0
    assert premier_league["model_health"]["status"] == "evaluated"
    assert premier_league["model_health"]["brier_score"] > 0
    assert "accuracy" not in premier_league["model_health"]


def test_store_filters_matches_without_mutating_the_snapshot():
    store = PlatformStore(Path("data/MatchHistory"))

    response = store.matches(competition_id="bundesliga", season="2324", limit=12)

    assert response["count"] == 12
    assert response["total"] == 306
    assert all(match["competition_id"] == "bundesliga" for match in response["matches"])
    assert all(match["season"] == "2324" for match in response["matches"])
    assert store.snapshot()["summary"]["finished_matches"] == 6190


def test_store_validates_query_contract_and_filters_date():
    store = PlatformStore(Path("data/MatchHistory"))

    dated = store.matches(match_date="2024-05-19", limit=500)

    assert dated["total"] > 0
    assert all(match["kickoff_at"].startswith("2024-05-19") for match in dated["matches"])
    for kwargs in (
        {"competition_id": "unknown"},
        {"status": "unknown"},
        {"match_date": "19-05-2024"},
    ):
        try:
            store.matches(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected validation failure for {kwargs}")


def test_source_rejects_duplicate_fixtures(tmp_path):
    csv = "Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,AvgH,AvgD,AvgA\n19/05/2024,16:00,A,B,1,0,2,3,4\n"
    (tmp_path / "E0_2324.csv").write_text(csv + csv.split("\n", 1)[1], encoding="utf-8")

    source = MatchHistorySource(tmp_path)
    try:
        source.load("premier-league")
    except ValueError as exc:
        assert "duplicate fixtures" in str(exc)
    else:
        raise AssertionError("duplicate fixture must block the snapshot")


def test_walk_forward_evaluation_is_chronological_and_calibrated():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches

    evaluation = evaluate_league("premier-league", matches)

    assert evaluation["status"] == "evaluated"
    assert evaluation["calibration_season"] == "2223"
    assert evaluation["evaluation_season"] == "2324"
    assert evaluation["sample_n"] == 380
    assert 0 < evaluation["brier_score"] < 1
    assert 0 < evaluation["log_loss"] < 2
    assert 0 <= evaluation["rps"] < 1
    assert 0 <= evaluation["ece"] < 1
    assert 0 <= evaluation["calibration_alpha"] <= 1
    assert evaluation["prediction_time_rule"] == "pre_kickoff_group_update"
    assert len(evaluation["walk_forward_folds"]) == 4
    assert sum(fold["sample_n"] for fold in evaluation["walk_forward_folds"]) == 380
    assert evaluation["baselines"]["historical_frequency"]["sample_n"] == 380
    assert evaluation["baselines"]["market"]["sample_n"] == 380
    assert evaluation["quality_gate"] == "research_only_underperforms_market"


def test_dixon_coles_candidate_uses_disjoint_calibration_and_holdout():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches

    evaluation = evaluate_dixon_coles("premier-league", matches)

    assert evaluation["status"] == "evaluated"
    assert evaluation["model"] == "online_dixon_coles_v1"
    assert evaluation["calibration_season"] == "2223"
    assert evaluation["evaluation_season"] == "2324"
    assert evaluation["sample_n"] == 380
    assert -0.15 <= evaluation["rho"] <= 0.15
    assert 0 < evaluation["brier_score"] < 1


def test_snapshot_exposes_evaluated_six_league_historical_baselines():
    snapshot = build_platform_snapshot(Path("data/MatchHistory"))
    by_id = {item["id"]: item for item in snapshot["competitions"]}

    assert snapshot["summary"]["evaluated_models"] == 6
    assert by_id["premier-league"]["model_health"]["sample_n"] == 380
    assert by_id["csl"]["model_health"]["status"] == "evaluated"
    assert by_id["csl"]["model_health"]["sample_n"] == 240
    assert by_id["csl"]["model_health"]["selected_candidate"] in {
        "dynamic_elo_three_way_v1",
        "online_dixon_coles_v1",
    }


def test_store_health_exposes_source_and_model_gates():
    store = PlatformStore(Path("data/MatchHistory"))

    health = store.health()

    assert health["status"] == "degraded"
    assert health["sources"]["available"] == 6
    assert health["sources"]["fresh"] == 0
    assert health["sources"]["stale"] == 6
    assert health["sources"]["unavailable"] == 0
    assert health["models"]["evaluated"] == 6
    assert health["models"]["gate"] == "historical_baselines_evaluated_current_predictions_blocked"


def test_current_snapshot_is_attached_with_as_of_and_feature_coverage(tmp_path):
    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": sorted(EXPECTED_LEAGUES),
        "roles": TEST_ROLES,
        "espn": {
            "provider": "ESPN",
            "errors": [],
            "fixtures": [
                {
                    "id": "espn:1",
                    "competition_id": "premier-league",
                    "season": "2026",
                    "kickoff_at": "2026-08-21T19:00:00+00:00",
                    "home_team": "Manchester City",
                    "away_team": "Arsenal",
                    "status": "upcoming",
                    "score": None,
                    "home_provider_team_id": "1",
                    "away_provider_team_id": "2",
                    "source": _espn_source(as_of, "1"),
                },
                *_coverage_fixtures(as_of),
            ],
        },
        "understat": {
            "provider": "Understat",
            "errors": [],
            "team_features": [
                {
                    "competition_id": "premier-league",
                    "team": "Manchester City",
                    "sample_n": 5,
                    "xg_for": 1.5,
                    "xg_against": 1.0,
                    "source": {
                        "name": "Understat",
                        "url": "https://understat.com/example",
                        "retrieved_at": as_of.isoformat(),
                        "wire_sha256": TEST_SHA256,
                        "content_sha256": TEST_SHA256,
                    },
                }
            ],
        },
        "espn_markets": {
            "provider": "ESPN event summary",
            "errors": [],
            "markets": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")

    snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")),
        live_path,
        now=as_of,
    )
    fixture = next(item for item in snapshot["matches"] if item["id"] == "espn:1")

    assert snapshot["current_data"]["status"] == "fresh"
    assert snapshot["summary"]["current_fixture_count"] == 6
    assert fixture["home_team_id"] == "premier-league:man-city"
    assert fixture["current_features"]["home"]["sample_n"] == 5

    store = PlatformStore(Path("data/MatchHistory"), now=as_of, live_path=live_path)
    store._clock = lambda: as_of + timedelta(hours=7)
    assert store.predictions()["status"] == "unavailable"
    assert store.health()["current_data"]["status"] == "stale"

    valid_live = json.loads(json.dumps(live))
    live["espn"]["fixtures"] = []
    live_path.write_text(json.dumps(live), encoding="utf-8")
    empty_snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
    )
    assert empty_snapshot["current_data"]["status"] == "unavailable"

    live = json.loads(json.dumps(valid_live))
    del live["espn"]["fixtures"][0]["source"]["raw_sha256"]
    live_path.write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")),
            live_path,
            now=as_of,
        )

    live = json.loads(json.dumps(valid_live))
    del live["understat"]["team_features"][0]["source"]["wire_sha256"]
    live_path.write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
        )

    live = json.loads(json.dumps(valid_live))
    live["roles"]["injuries_and_lineups"] = "unverified-feed"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ValueError, match="roles"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
        )

    live = json.loads(json.dumps(valid_live))
    live["espn"]["fixtures"][0]["source"]["url"] = "https://evil.example/not-espn"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ValueError, match="allowlisted"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
        )

    live = json.loads(json.dumps(valid_live))
    live["espn"]["errors"] = [{"competition_id": "premier-league", "error": "offline"}]
    live_path.write_text(json.dumps(live), encoding="utf-8")
    degraded = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
    )
    assert degraded["current_data"]["status"] == "degraded"


def test_future_predictions_only_use_post_as_of_fixtures_and_disclose_gates(tmp_path):
    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": sorted(EXPECTED_LEAGUES),
        "roles": TEST_ROLES,
        "espn": {
            "provider": "ESPN",
            "errors": [],
            "fixtures": [
                {
                    "id": "espn:future",
                    "competition_id": "premier-league",
                    "season": "2026",
                    "kickoff_at": "2026-08-21T19:00:00+00:00",
                    "home_team": "Manchester City",
                    "away_team": "Arsenal",
                    "status": "upcoming",
                    "score": None,
                    "home_provider_team_id": "1",
                    "away_provider_team_id": "2",
                    "source": _espn_source(as_of, "future"),
                },
                *_coverage_fixtures(as_of),
            ],
        },
        "understat": {"provider": "Understat", "errors": [], "team_features": []},
        "espn_markets": {
            "provider": "ESPN event summary",
            "errors": [],
            "markets": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
    )

    result = build_future_predictions(snapshot)

    assert result["status"] == "research_only"
    assert len(result["predictions"]) == 1
    prediction = result["predictions"][0]
    assert prediction["kickoff_at"] > prediction["as_of"]
    assert prediction["training_cutoff"] < prediction["as_of"]
    assert prediction["quality_gate"].startswith("blocked_for_production")
    assert abs(sum(prediction["dixon_coles_probability"].values()) - 1) < 1e-5


def test_future_predictions_block_model_parameters_fitted_after_as_of(tmp_path):
    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": sorted(EXPECTED_LEAGUES),
        "roles": TEST_ROLES,
        "espn": {
            "provider": "ESPN",
            "errors": [],
            "fixtures": [
                {
                    "id": "espn:future-leak-check",
                    "competition_id": "premier-league",
                    "season": "2026",
                    "kickoff_at": "2026-08-21T19:00:00+00:00",
                    "home_team": "Manchester City",
                    "away_team": "Arsenal",
                    "status": "upcoming",
                    "score": None,
                    "home_provider_team_id": "1",
                    "away_provider_team_id": "2",
                    "source": _espn_source(as_of, "future-leak-check"),
                },
                *_coverage_fixtures(as_of),
            ],
        },
        "understat": {"provider": "Understat", "errors": [], "team_features": []},
        "espn_markets": {
            "provider": "ESPN event summary",
            "errors": [],
            "markets": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
    )
    competition = next(item for item in snapshot["competitions"] if item["id"] == "premier-league")
    competition["model_health"]["data_cutoff"] = "2026-08-11T00:00:00+00:00"

    result = build_future_predictions(snapshot)

    assert result["predictions"] == []
    target = next(
        item for item in result["blocked"] if item["fixture_id"] == "espn:future-leak-check"
    )
    assert target["reason"] == "historical_model_not_causal"
