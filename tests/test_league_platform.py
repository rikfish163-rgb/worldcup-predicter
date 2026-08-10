from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from league_platform.catalog import LEAGUES, get_league
from league_platform.dixon_coles import evaluate_dixon_coles
from league_platform.model import evaluate_league
from league_platform.snapshot import build_platform_snapshot
from league_platform.store import PlatformStore
from league_platform.sources.match_history import MatchHistorySource
from league_platform.sources.openfootball import OpenFootballSource


EXPECTED_LEAGUES = {"premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1", "csl"}


def test_data_manifest_pins_all_six_league_inputs():
    manifest = json.loads(Path("league_platform/data_manifest.json").read_text())

    assert manifest["provider"] == "multiple"
    assert len(manifest["files"]) == 18
    assert all(item["url"].startswith("https://") for item in manifest["files"])
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_catalog_contains_big_five_and_chinese_super_league():
    assert {league.id for league in LEAGUES} == EXPECTED_LEAGUES
    assert get_league("csl").name_zh == "中超"
    assert all(league.timezone for league in LEAGUES)


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
