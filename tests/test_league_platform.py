from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from league_platform.catalog import LEAGUES, get_league
from league_platform.snapshot import build_platform_snapshot
from league_platform.store import PlatformStore
from league_platform.sources.match_history import MatchHistorySource


EXPECTED_LEAGUES = {"premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1", "csl"}


def test_data_manifest_pins_every_big_five_input():
    manifest = json.loads(Path("league_platform/data_manifest.json").read_text())

    assert manifest["provider"] == "football-data.co.uk"
    assert len(manifest["files"]) == 15
    assert all(
        item["url"].startswith("https://www.football-data.co.uk/") for item in manifest["files"]
    )
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
    assert premier_league.latest_event_at is not None


def test_match_history_source_marks_missing_csl_source_unavailable():
    source = MatchHistorySource(Path("data/MatchHistory"))

    csl = source.load("csl")

    assert csl.status == "unavailable"
    assert csl.matches == []
    assert "尚未接入" in csl.message


def test_platform_snapshot_is_source_backed_and_deduplicated():
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)

    snapshot = build_platform_snapshot(Path("data/MatchHistory"), now=now)

    assert snapshot["generated_at"] == now.isoformat()
    assert len(snapshot["competitions"]) == 6
    assert snapshot["summary"]["finished_matches"] == 5404
    assert snapshot["summary"]["available_competitions"] == 5
    assert snapshot["summary"]["unavailable_competitions"] == 1

    match_ids = [match["id"] for match in snapshot["matches"]]
    assert len(match_ids) == len(set(match_ids))
    assert all(match["source"]["name"] == "football-data.co.uk" for match in snapshot["matches"])


def test_snapshot_exposes_quality_metrics_instead_of_unqualified_accuracy():
    snapshot = build_platform_snapshot(Path("data/MatchHistory"))

    premier_league = next(
        item for item in snapshot["competitions"] if item["id"] == "premier-league"
    )

    assert premier_league["data_quality"]["duplicate_fixture_rate"] == 0.0
    assert premier_league["data_quality"]["score_completeness"] == 1.0
    assert premier_league["data_quality"]["odds_completeness"] == 1.0
    assert premier_league["model_health"]["status"] == "not_evaluated"
    assert "accuracy" not in premier_league["model_health"]


def test_store_filters_matches_without_mutating_the_snapshot():
    store = PlatformStore(Path("data/MatchHistory"))

    response = store.matches(competition_id="bundesliga", season="2324", limit=12)

    assert response["count"] == 12
    assert response["total"] == 306
    assert all(match["competition_id"] == "bundesliga" for match in response["matches"])
    assert all(match["season"] == "2324" for match in response["matches"])
    assert store.snapshot()["summary"]["finished_matches"] == 5404


def test_store_health_exposes_source_and_model_gates():
    store = PlatformStore(Path("data/MatchHistory"))

    health = store.health()

    assert health["status"] == "degraded"
    assert health["sources"]["available"] == 5
    assert health["sources"]["unavailable"] == 1
    assert health["models"]["evaluated"] == 0
    assert health["models"]["gate"] == "blocked_until_walk_forward_validation"
