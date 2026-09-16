from pathlib import Path

import pytest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from league_platform.sources.match_history import MatchHistorySource
from league_platform.sources.openfootball import OpenFootballSource
from league_platform.strict_backtest import (
    PROSPECTIVE_FREEZE_MODEL_VERSION,
    _not_applied_factor_rows,
    build_strict_prospective_predictions,
    evaluate_freeze_stage_walk_forward,
    evaluate_strict_walk_forward,
)
from league_platform.strict_backtest import _temperature_scale_matrix
from league_platform.strict_report import _market_failure_targets, _time_audit


def test_lineup_factor_provenance_uses_the_exact_confirmation_event_provider():
    cutoff = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    features = {
        "official_lineup": {
            "source": {
                "name": "Cached official feed",
                "retrieved_at": "2026-08-23T11:50:00+00:00",
                "url": "https://official.example/lineup",
            }
        },
        "team_status": {
            "retrieved_at": "2026-08-23T11:55:00+00:00",
            "source": {
                "name": "ESPN event summary",
                "url": "https://example.test/summary",
            },
        },
        "lineup_confirmation_event": {
            "provider_key": "team_status",
            "observed_at": "2026-08-23T11:55:00+00:00",
        },
    }

    rows = _not_applied_factor_rows(
        features,
        cutoff_at=cutoff,
        accepted_fields=set(),
        feature_sources=[],
    )
    lineup = next(row for row in rows if row["id"] == "lineups")
    assert lineup["observed_at"] == "2026-08-23T11:55:00+00:00"
    assert lineup["source_name"] == "ESPN event summary"
    assert lineup["source_url"] == "https://example.test/summary"


def test_injury_factor_provenance_prefers_structured_espn_report_lane():
    cutoff = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    rows = _not_applied_factor_rows(
        {
            "espn_injuries": {
                "source": {
                    "name": "ESPN injury report",
                    "url": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries",
                    "retrieved_at": "2026-08-23T11:50:00+00:00",
                },
                "injuries": {"home": [], "away": [], "available": False, "partial": True},
            }
        },
        cutoff_at=cutoff,
        accepted_fields=set(),
        feature_sources=[],
    )

    injury = next(row for row in rows if row["id"] == "injuries")
    assert injury["status"] == "available_not_applied"
    assert injury["source_name"] == "ESPN injury report"
    assert injury["observed_at"] == "2026-08-23T11:50:00+00:00"


def test_strict_backtest_uses_all_post_warmup_seasons_without_future_features():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    result = evaluate_strict_walk_forward(matches, warmup_seasons=2)
    assert result["status"] == "evaluated"
    assert result["sample_n"] > 1000
    assert all(row["cutoff_at"] < row["kickoff_at"] for row in result["rows"])
    assert result["targets"]["scoreline"]["top5_hit"] >= 0
    assert result["targets"]["scoreline"]["exact_hit"] == result["targets"]["scoreline"]["top1_hit"]
    assert result["targets"]["scoreline"]["rps"] >= 0
    assert result["targets"]["scoreline"]["ece"] >= 0
    assert result["targets"]["scoreline"]["frequency_log_loss"] > 0
    assert result["targets"]["totals"]["rps"] >= 0
    assert result["targets"]["half_full"]["ece"] >= 0
    assert result["targets"]["handicap"]["model"]["log_loss"] < result["targets"]["handicap"]["frequency"]["log_loss"]
    assert result["targets"]["total_over_under"]["model"]["brier"] >= 0
    assert result["three_way"]["model_minus_market_brier_ci"] is not None
    assert result["targets"]["total_over_under"]["model_minus_market_brier_ci"] is not None
    market_rows = [row for row in result["rows"] if row["market_probability"] is not None]
    assert market_rows
    assert all(row["live_feature_fields"] == ["market_1x2"] for row in market_rows)
    assert all(row["live_market_blend_weight"] > 0 for row in market_rows)
    assert result["targets"]["handicap"]["sample_n"] >= 1000
    assert all(match.kickoff_at.isoformat() != "NaT" for match in matches)
    assert result["model"] == "strict_dynamic_elo_scoreline_v2_causal_rho"
    assert all(
        row["market_retrieved_at"] is None
        or row["market_retrieved_at"] <= row["cutoff_at"]
        for row in result["rows"]
    )
    assert all(
        row.get("market_time_bound_at") is None
        or row["market_time_bound_at"] <= row["cutoff_at"]
        for row in result["rows"]
    )
    assert result["hyperparameters"]["rho_calibration_refresh"] == 250
    assert result["hyperparameters"]["rate_learning_rate"] == 0.015
    for row in result["rows"]:
        assert row["model_training_cutoff"] is None or row["model_training_cutoff"] < row["kickoff_at"]
        assert row["scoreline_rho_training_cutoff"] is None or row["scoreline_rho_training_cutoff"] < row["kickoff_at"]


def test_strict_backtest_reaches_csl_sample_gate_after_history_expansion():
    matches = OpenFootballSource(Path("data/MatchHistory")).load("csl").matches
    result = evaluate_strict_walk_forward(matches, warmup_seasons=2)
    assert result["sample_n"] >= 1000


def test_strict_report_derives_market_failures_from_target_gate_status():
    leagues = {
        "demo": {
            "gates": {
                "three_way": {"market_status": "underperforms_market"},
                "totals": {"market_status": "not_available"},
            }
        }
    }

    assert _market_failure_targets(leagues) == ["demo:three_way"]


def test_strict_report_time_audit_records_market_basis_and_leakage_status():
    rows = [{
        "fixture_id": "demo:1",
        "cutoff_at": "2026-08-11T12:00:00+00:00",
        "kickoff_at": "2026-08-11T13:00:00+00:00",
        "feature_times": [],
        "market_retrieved_at": "2026-08-11T12:00:00+00:00",
        "market_time_bound_at": "2026-08-11T12:00:00+00:00",
        "market_time_quality": "exact_observed_at",
        "market_probability": {"home": 0.4, "draw": 0.3, "away": 0.3},
        "market_time_basis": "closing_average_pre_kickoff",
    }]

    assert _time_audit(rows) == {
        "market_rows": 1,
        "kickoff_time_quality_counts": {"exact": 1},
        "kickoff_time_source_counts": {"unavailable": 1},
        "market_time_basis_counts": {"closing_average_pre_kickoff": 1},
        "market_time_quality_counts": {"exact_observed_at": 1},
        "market_opening_rows": 0,
        "market_closing_rows": 0,
        "market_bound_violations": 0,
        "feature_time_violations": 0,
        "future_leakage_violations": 0,
        "probability_contract_error": None,
        "probability_contract_status": "pass",
        "status": "pass",
    }


def test_strict_prospective_predictions_are_causal_and_result_free():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    upcoming = replace(
        template,
        id="prospective:fixture-1",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    as_of = kickoff - timedelta(minutes=30)
    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=as_of,
        lineup_observed_at={upcoming.id: as_of - timedelta(hours=1)},
    )
    assert result["status"] == "evaluated"
    assert {row["freeze_stage"] for row in result["predictions"]} == {
        "t_minus_24h",
        "t_minus_6h",
        "t_minus_90m",
    }
    assert any(
        item.get("freeze_stage") == "lineup_confirmation"
        and item.get("reason") == "lineup_confirmation_evidence_unavailable"
        for item in result["blocked"]
    )
    for row in result["predictions"]:
        assert row["fixture_id"] == upcoming.id
        assert row["home_team"] == upcoming.home_team
        assert row["away_team"] == upcoming.away_team
        assert row["home_team_id"] == upcoming.home_team_id
        assert row["away_team_id"] == upcoming.away_team_id
        assert row["source_name"] == upcoming.source_name
        assert row["source_license_status"] == upcoming.source_license_status
        assert row["source_file"] == upcoming.source_file
        assert row["source_sha256"] == upcoming.source_sha256
        assert row["provider_fixture_id"] == upcoming.provider_fixture_id
        assert row["cutoff_at"] < row["kickoff_at"]
        assert row["freeze_cutoff_at"] <= row["as_of"]
        assert row["prediction_observed_at"] == row["as_of"]
        assert "outcome_1x2" not in row
        assert "actual_score" not in row
        assert row["model_version"] == PROSPECTIVE_FREEZE_MODEL_VERSION
        assert row["player_availability_shadow"]["model_eligible"] is False
        assert row["player_availability_shadow"]["impact_delta_probability_1x2"] is None


def test_strict_prospective_does_not_backfill_freezes_before_fixture_was_observed():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    first_observed_at = kickoff - timedelta(minutes=94)
    upcoming = replace(
        template,
        id="prospective:first-observed-after-early-freezes",
        kickoff_at=kickoff,
        kickoff_time_observed_at=first_observed_at.isoformat(),
        status="upcoming",
        score=None,
    )

    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=kickoff - timedelta(minutes=30),
    )

    assert [row["freeze_stage"] for row in result["predictions"]] == ["t_minus_90m"]
    assert result["predictions"][0]["kickoff_time_observed_at"] == first_observed_at.isoformat()
    blocked = {
        row["freeze_stage"]: row
        for row in result["blocked"]
        if row.get("reason") == "fixture_first_observed_after_freeze_cutoff"
    }
    assert set(blocked) == {"t_minus_24h", "t_minus_6h"}
    assert all(row["fixture_id"] == upcoming.id for row in blocked.values())
    assert all(row["kickoff_time_observed_at"] == first_observed_at.isoformat() for row in blocked.values())


def test_strict_prospective_accepts_archived_fixture_identity_before_the_cutoff():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    latest_poll = kickoff - timedelta(hours=1)
    t24_observed = kickoff - timedelta(hours=24, minutes=10)
    t90_observed = kickoff - timedelta(minutes=100)
    upcoming = replace(
        template,
        id="prospective:archived-fixture-observation",
        kickoff_at=kickoff,
        kickoff_time_observed_at=latest_poll.isoformat(),
        status="upcoming",
        score=None,
    )

    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=kickoff - timedelta(minutes=30),
        feature_snapshots={
            upcoming.id: {
                "by_stage": {},
                "fixture_observed_at_by_stage": {
                    "t_minus_24h": t24_observed.isoformat(),
                    "t_minus_90m": t90_observed.isoformat(),
                },
                "fixture_by_stage": {
                    "t_minus_24h": {
                        "kickoff_at": kickoff.isoformat(),
                        "kickoff_time_quality": "exact",
                        "kickoff_time_source": "archived schedule",
                        "kickoff_time_observed_at": t24_observed.isoformat(),
                    },
                    "t_minus_90m": {
                        "kickoff_at": kickoff.isoformat(),
                        "kickoff_time_quality": "exact",
                        "kickoff_time_source": "archived schedule",
                        "kickoff_time_observed_at": t90_observed.isoformat(),
                    },
                },
            }
        },
    )

    rows = {row["freeze_stage"]: row for row in result["predictions"]}
    assert set(rows) == {"t_minus_24h", "t_minus_90m"}
    assert rows["t_minus_24h"]["fixture_freeze_observed_at"] == t24_observed.isoformat()
    assert rows["t_minus_90m"]["fixture_freeze_observed_at"] == t90_observed.isoformat()
    assert rows["t_minus_24h"]["kickoff_time_observed_at"] == t24_observed.isoformat()
    assert rows["t_minus_90m"]["kickoff_time_observed_at"] == t90_observed.isoformat()
    assert all(
        row["fixture_observation_basis"] == "archived_fixture_snapshot"
        for row in rows.values()
    )
    assert [row["freeze_stage"] for row in result["blocked"]] == ["t_minus_6h"]


def test_strict_prospective_does_not_backfill_a_freeze_before_model_lock():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    upcoming = replace(
        template,
        id="prospective:model-lock-after-t24",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    window_start = kickoff - timedelta(hours=23, minutes=30)

    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=kickoff - timedelta(minutes=30),
        evaluation_window_started_at=window_start,
    )

    assert {row["freeze_stage"] for row in result["predictions"]} == {
        "t_minus_6h",
        "t_minus_90m",
    }
    blocked = next(
        row
        for row in result["blocked"]
        if row.get("freeze_stage") == "t_minus_24h"
    )
    assert blocked["reason"] == "freeze_cutoff_before_evaluation_window"
    assert blocked["evaluation_window_started_at"] == window_start.isoformat()


def test_strict_prospective_rejects_schedule_revision_first_seen_after_freeze():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    cutoff = kickoff - timedelta(hours=24)
    current_observed = cutoff + timedelta(hours=1)
    archived_observed = cutoff - timedelta(minutes=30)
    upcoming = replace(
        template,
        id="prospective:schedule-revised-after-freeze",
        kickoff_at=kickoff,
        kickoff_time_observed_at=current_observed.isoformat(),
        status="upcoming",
        score=None,
    )

    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=cutoff + timedelta(hours=2),
        feature_snapshots={
            upcoming.id: {
                "by_stage": {},
                "fixture_observed_at_by_stage": {
                    "t_minus_24h": archived_observed.isoformat(),
                },
                "fixture_by_stage": {
                    "t_minus_24h": {
                        "kickoff_at": (kickoff - timedelta(hours=1)).isoformat(),
                        "kickoff_time_quality": "exact",
                        "kickoff_time_source": "old schedule",
                        "kickoff_time_observed_at": archived_observed.isoformat(),
                    }
                },
            }
        },
    )

    assert result["predictions"] == []
    blocked = next(
        row
        for row in result["blocked"]
        if row.get("freeze_stage") == "t_minus_24h"
    )
    assert blocked["reason"] == "fixture_kickoff_changed_after_freeze_cutoff"
    assert blocked["archived_kickoff_at"] == (
        kickoff - timedelta(hours=1)
    ).isoformat()
    assert blocked["current_kickoff_at"] == kickoff.isoformat()


def test_live_result_updates_only_after_result_observed_at():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    upcoming = replace(
        template,
        id="prospective:result-observation-boundary",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    late_result = replace(
        template,
        id="live:result-observation-boundary",
        kickoff_at=kickoff - timedelta(days=2),
        result_observed_at=(kickoff - timedelta(hours=8)).isoformat(),
    )
    as_of = kickoff - timedelta(minutes=30)
    baseline = build_strict_prospective_predictions(matches, [upcoming], as_of=as_of)
    with_live_result = build_strict_prospective_predictions(
        [*matches, late_result], [upcoming], as_of=as_of
    )
    baseline_rows = {row["freeze_stage"]: row for row in baseline["predictions"]}
    live_rows = {row["freeze_stage"]: row for row in with_live_result["predictions"]}
    # The result was observed after the 24-hour cutoff, so it cannot affect
    # that frozen row, but it is available at the later 6-hour cutoff.
    assert live_rows["t_minus_24h"]["scoreline_matrix"] == baseline_rows["t_minus_24h"]["scoreline_matrix"]
    assert live_rows["t_minus_6h"]["scoreline_matrix"] != baseline_rows["t_minus_6h"]["scoreline_matrix"]


def test_combined_prospective_metadata_uses_locked_model_version():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    primary = replace(
        template,
        id="prospective:combined-primary",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    secondary = replace(
        primary,
        id="prospective:combined-secondary",
        competition_id="unseen-competition",
    )
    result = build_strict_prospective_predictions(
        matches,
        [primary, secondary],
        as_of=kickoff - timedelta(minutes=30),
    )
    assert result["model"] == PROSPECTIVE_FREEZE_MODEL_VERSION
    assert result["blocked"] == [{
        "reason": "not_enough_historical_seasons",
        "competition_id": "unseen-competition",
    }]
    single_result = build_strict_prospective_predictions(
        matches,
        [secondary],
        as_of=kickoff - timedelta(minutes=30),
    )
    assert single_result["blocked"] == [{
        "reason": "not_enough_historical_seasons",
        "competition_id": "unseen-competition",
    }]


def test_strict_prospective_live_features_are_cutoff_bounded():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    upcoming = replace(
        template,
        id="prospective:live-feature",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    observed = (kickoff - timedelta(minutes=100)).isoformat()
    feature_snapshot = {
        "home": {
            "xg_for": 2.2,
            "xg_against": 0.8,
            "source": {
                "name": "Understat",
                "url": "https://understat.com/getLeagueData/EPL/2025",
                "retrieved_at": observed,
                "wire_sha256": "h" * 64,
                "content_sha256": "c" * 64,
            },
        },
        "away": {
            "xg_for": 0.8,
            "xg_against": 2.2,
            "source": {
                "name": "Understat",
                "url": "https://understat.com/getLeagueData/EPL/2025",
                "retrieved_at": observed,
                "wire_sha256": "h" * 64,
                "content_sha256": "c" * 64,
            },
        },
        "market": {
            "probability": {"home": 0.7, "draw": 0.2, "away": 0.1},
            "retrieved_at": observed,
            "source": {
                "name": "ESPN event summary",
                "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=1",
                "raw_sha256": "m" * 64,
            },
        },
    }
    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=kickoff - timedelta(minutes=30),
        feature_snapshots={upcoming.id: feature_snapshot},
    )
    rows = {row["freeze_stage"]: row for row in result["predictions"]}
    assert rows["t_minus_24h"]["live_feature_fields"] == []
    assert rows["t_minus_6h"]["live_feature_fields"] == []
    assert rows["t_minus_90m"]["live_feature_fields"] == ["market_1x2", "recent_xg"]
    assert rows["t_minus_90m"]["feature_times"] == [observed]
    assert [item["field"] for item in rows["t_minus_90m"]["live_feature_sources"]] == [
        "market_1x2",
        "recent_xg_away",
        "recent_xg_home",
    ]
    assert all(item["observed_at"] == observed for item in rows["t_minus_90m"]["live_feature_sources"])
    provenance = {item["field"]: item for item in rows["t_minus_90m"]["live_feature_sources"]}
    assert provenance["market_1x2"]["url"].startswith("https://site.api.espn.com/")
    assert provenance["market_1x2"]["raw_sha256"] == "m" * 64
    assert provenance["recent_xg_home"]["wire_sha256"] == "h" * 64
    assert provenance["recent_xg_away"]["content_sha256"] == "c" * 64
    assert rows["t_minus_90m"]["live_market_blend_weight"] > 0
    assert rows["t_minus_90m"]["live_xg_blend_weight"] > 0
    assert rows["t_minus_90m"]["market_probability"] == {
        "home": pytest.approx(0.7),
        "draw": pytest.approx(0.2),
        "away": pytest.approx(0.1),
    }
    assert rows["t_minus_90m"]["market_retrieved_at"] == observed
    assert rows["t_minus_90m"]["market_time_bound_at"] == observed
    assert rows["t_minus_90m"]["market_time_quality"] == "exact_source_retrieved_at"
    assert rows["t_minus_90m"]["market_time_basis"] == "source_retrieved_at"
    assert rows["t_minus_90m"]["probability_delta_model_minus_market"] is not None
    assert rows["t_minus_90m"]["stage_market_note"].startswith("市场基线在冻结截止前")
    assert rows["t_minus_90m"]["player_availability_shadow"]["status"] == "unavailable"


def test_strict_prospective_uses_stage_specific_archived_features():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    upcoming = replace(
        template,
        id="prospective:archived-stages",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    old_observed = (kickoff - timedelta(hours=25)).isoformat()
    recent_observed = (kickoff - timedelta(minutes=100)).isoformat()

    def feature_snapshot(observed: str, home_xg: float) -> dict:
        return {
            "home": {"xg_for": home_xg, "xg_against": 0.8, "source": {"retrieved_at": observed}},
            "away": {"xg_for": 0.8, "xg_against": home_xg, "source": {"retrieved_at": observed}},
            "market": {
                "probability": {"home": 0.7, "draw": 0.2, "away": 0.1},
                "retrieved_at": observed,
            },
        }

    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=kickoff - timedelta(minutes=30),
        feature_snapshots={
            upcoming.id: {
                "by_stage": {
                    "t_minus_24h": feature_snapshot(old_observed, 1.4),
                    "t_minus_90m": feature_snapshot(recent_observed, 2.4),
                }
            }
        },
    )
    rows = {row["freeze_stage"]: row for row in result["predictions"]}
    assert rows["t_minus_24h"]["live_feature_fields"] == ["market_1x2", "recent_xg"]
    assert rows["t_minus_24h"]["feature_times"] == [old_observed]
    assert rows["t_minus_6h"]["live_feature_fields"] == []
    assert rows["t_minus_90m"]["live_feature_fields"] == ["market_1x2", "recent_xg"]
    assert rows["t_minus_90m"]["feature_times"] == [recent_observed]


def test_strict_prospective_replays_causal_lineup_confirmation_features():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    upcoming = replace(
        template,
        id="prospective:lineup-stage-features",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    lineup_observed = kickoff - timedelta(minutes=45)
    observed = (kickoff - timedelta(minutes=50)).isoformat()

    def feature_snapshot() -> dict:
        return {
            "home": {"xg_for": 1.8, "xg_against": 0.9, "source": {"retrieved_at": observed}},
            "away": {"xg_for": 0.9, "xg_against": 1.8, "source": {"retrieved_at": observed}},
            "market": {
                "probability": {"home": 0.55, "draw": 0.25, "away": 0.20},
                "retrieved_at": observed,
            },
            "team_status": {
                "confirmed": True,
                "model_eligible": True,
                "source": {"retrieved_at": lineup_observed.isoformat()},
                "teams": {
                    "home": {
                        "players": [
                            {
                                "player_id": f"home-{index}",
                                "starter": True,
                                "status": "starter",
                                "position": "starter",
                            }
                            for index in range(11)
                        ]
                    },
                    "away": {
                        "players": [
                            {
                                "player_id": f"away-{index}",
                                "starter": True,
                                "status": "starter",
                                "position": "starter",
                            }
                            for index in range(11)
                        ]
                    },
                },
            },
        }

    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=kickoff - timedelta(minutes=30),
        lineup_observed_at={upcoming.id: lineup_observed},
        feature_snapshots={
            upcoming.id: {
                "by_stage": {
                    "lineup_confirmation": feature_snapshot(),
                }
            }
        },
    )
    rows = {row["freeze_stage"]: row for row in result["predictions"]}
    lineup_row = rows["lineup_confirmation"]
    assert lineup_row["lineup_observed_at"] == lineup_observed.isoformat()
    assert lineup_row["live_feature_fields"] == ["market_1x2", "recent_xg"]
    assert lineup_row["feature_times"] == [observed]
    assert lineup_row["market_probability"] == {
        "home": pytest.approx(0.55),
        "draw": pytest.approx(0.25),
        "away": pytest.approx(0.20),
    }
    assert lineup_row["lineup_confirmation_fingerprint"] is not None
    assert len(lineup_row["lineup_confirmation_fingerprint"]) == 64
    assert lineup_row["player_availability_shadow"]["status"] == "shadow_ready"
    assert lineup_row["player_availability_shadow"]["deduplicated_player_count"] == 22
    assert lineup_row["player_availability_shadow"]["model_eligible"] is False
    assert lineup_row["player_availability_shadow"]["enters_model"] is False


def test_strict_prospective_predictions_keep_leagues_state_isolated():
    source = MatchHistorySource(Path("data/MatchHistory"))
    premier = source.load("premier-league").matches
    la_liga = source.load("la-liga").matches
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    premier_fixture = replace(
        premier[-1],
        id="prospective:premier",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    la_fixture = replace(
        la_liga[-1],
        id="prospective:la-liga",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    as_of = kickoff - timedelta(minutes=30)
    single = build_strict_prospective_predictions(premier, [premier_fixture], as_of=as_of)
    combined = build_strict_prospective_predictions(
        premier + la_liga, [premier_fixture, la_fixture], as_of=as_of
    )
    single_row = [row for row in single["predictions"] if row["fixture_id"] == premier_fixture.id]
    combined_row = [row for row in combined["predictions"] if row["fixture_id"] == premier_fixture.id]
    assert single_row and combined_row
    assert single_row[0]["scoreline_matrix"] == combined_row[0]["scoreline_matrix"]


def test_strict_prospective_exports_causal_sports_lottery_handicap():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    template = matches[-1]
    kickoff = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    upcoming = replace(
        template,
        id="prospective:lottery-handicap",
        kickoff_at=kickoff,
        kickoff_time_observed_at=(kickoff - timedelta(days=2)).isoformat(),
        status="upcoming",
        score=None,
    )
    observed = (kickoff - timedelta(minutes=100)).isoformat()
    result = build_strict_prospective_predictions(
        matches,
        [upcoming],
        as_of=kickoff - timedelta(minutes=30),
        feature_snapshots={
            upcoming.id: {
                "lottery_market": {
                    "hhad_line": "-0.5",
                    "hhad_probability": {"h": 0.52, "d": 0.08, "a": 0.40},
                    "retrieved_at": observed,
                    "source": {
                        "name": "Sports Lottery",
                        "url": "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry",
                        "retrieved_at": observed,
                        "raw_sha256": "l" * 64,
                    },
                }
            }
        },
    )
    rows = {row["freeze_stage"]: row for row in result["predictions"]}
    assert rows["t_minus_24h"]["handicap_probability"] is None
    assert rows["t_minus_90m"]["handicap_line"] == -0.5
    assert set(rows["t_minus_90m"]["handicap_probability"]) == {"win", "loss"}
    assert sum(rows["t_minus_90m"]["handicap_probability"].values()) == pytest.approx(1.0)
    assert rows["t_minus_90m"]["handicap_market_probability"] == {
        "win": pytest.approx(0.52),
        "push": pytest.approx(0.08),
        "loss": pytest.approx(0.40),
    }
    source = next(
        item
        for item in rows["t_minus_90m"]["live_feature_sources"]
        if item["field"] == "sports_lottery_hhad"
    )
    assert source["raw_sha256"] == "l" * 64


def test_strict_report_time_audit_blocks_invalid_probability_contract():
    rows = [{
        "fixture_id": "demo:bad",
        "cutoff_at": "2026-08-11T12:00:00+00:00",
        "kickoff_at": "2026-08-11T13:00:00+00:00",
        "feature_times": [],
        "elo_probability": {"home": 0.7, "draw": 0.2, "away": 0.2},
    }]

    audit = _time_audit(rows)
    assert audit["status"] == "blocked"
    assert audit["probability_contract_status"] == "blocked"
    assert "sum is not one" in audit["probability_contract_error"]


def test_scoreline_temperature_preserves_probability_contract():
    matrix = {"0-0": 0.5, "1-0": 0.3, "0-1": 0.2}
    scaled = _temperature_scale_matrix(matrix, 0.9)
    assert abs(sum(scaled.values()) - 1) < 1e-12
    assert all(value > 0 for value in scaled.values())
    assert _temperature_scale_matrix(matrix, 1.0) is matrix
    with pytest.raises(ValueError, match="scoreline_temperature"):
        _temperature_scale_matrix(matrix, 0)


def test_rating_halflife_requires_a_positive_finite_value():
    from pathlib import Path

    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    with pytest.raises(ValueError, match="rating_halflife_days"):
        evaluate_strict_walk_forward(matches, rating_halflife_days=0)


def test_strength_halflife_requires_a_positive_finite_value():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    with pytest.raises(ValueError, match="strength_halflife_days"):
        evaluate_strict_walk_forward(matches, strength_halflife_days=0)


def test_rho_objective_must_be_explicitly_supported():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    with pytest.raises(ValueError, match="rho_objective"):
        evaluate_strict_walk_forward(matches, rho_objective="market")


def test_freeze_stage_replay_is_causal_and_does_not_fake_lineup_stage():
    from datetime import datetime, timedelta, timezone

    from league_platform.domain import Match, Score

    base = datetime(2020, 8, 1, tzinfo=timezone.utc)
    matches = []
    for season_index in range(3):
        kickoff = base + timedelta(days=365 * season_index)
        matches.append(
            Match(
                id=f"demo:{season_index}",
                competition_id="demo",
                season=str(2020 + season_index),
                kickoff_at=kickoff,
                home_team="Home",
                away_team="Away",
                home_team_id="home",
                away_team_id="away",
                status="finished",
                score=Score(1, 0, 1, 0),
                market_probability=None,
                source_file="demo.csv",
                source_sha256="sha",
                provider_fixture_id=str(season_index),
                source_name="demo",
                source_license_status="test",
            )
        )
    result = evaluate_freeze_stage_walk_forward(matches)
    assert result["stages"]["t_minus_24h"]["sample_n"] == 1
    assert result["stages"]["t_minus_6h"]["sample_n"] == 1
    assert result["stages"]["t_minus_90m"]["sample_n"] == 1
    assert result["stages"]["lineup_confirmation"]["status"] == "unavailable"
    assert result["stages"]["t_minus_24h"]["time_audit"]["status"] == "pass"
