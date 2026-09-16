import math

import pytest

from league_platform.evaluation import evaluate_prediction_rows


def test_evaluation_rejects_future_features_and_scores_available_targets():
    row = {
        "cutoff_at": "2026-08-11T12:00:00+00:00",
        "kickoff_at": "2026-08-11T13:00:00+00:00",
        "feature_times": ["2026-08-11T11:59:00+00:00"],
        "dixon_coles_probability": {"home": 0.6, "draw": 0.2, "away": 0.2},
        "outcome_1x2": "home",
        "scoreline_top5": [{"score": "1-0", "probability": 0.3}],
        "scoreline_matrix": {"1-0": 0.3, "0-0": 0.7},
        "actual_score": "1-0",
        "total_goals_probability": {"0": 0.1, "1": 0.5, "2": 0.4},
        "actual_total_goals": "1",
        "expected_goals": {"home": 1.2, "away": 0.7},
        "actual_home_goals": 1,
        "actual_away_goals": 0,
    }
    result = evaluate_prediction_rows([row])
    assert result["targets"]["three_way"]["sample_n"] == 1
    assert result["targets"]["scoreline"]["top1_hit"] == 1
    assert result["targets"]["scoreline"]["exact_hit"] == 1
    assert result["targets"]["scoreline"]["distribution_log_loss"] == pytest.approx(-math.log(0.3))
    assert result["targets"]["goal_count"]["mae"] == pytest.approx(0.9)
    assert result["targets"]["goal_count"]["distribution_ece"] >= 0
    assert result["targets"]["goal_count"]["interval_coverage"]["coverage"] == 1


def test_evaluation_scores_each_additional_play_when_frozen_fields_exist():
    row = {
        "cutoff_at": "2026-08-11T12:00:00+00:00",
        "kickoff_at": "2026-08-11T13:00:00+00:00",
        "half_full_probability": {"H/H": 0.6, "D/D": 0.4},
        "actual_half_full": "H/H",
        "handicap_probability": {"win": 0.7, "push": 0.1, "loss": 0.2},
        "actual_handicap": "win",
        "total_over_under_probability": {"over": 0.65, "under": 0.35},
        "actual_total_ou": "over",
    }
    result = evaluate_prediction_rows([row])
    assert result["targets"]["half_full"]["sample_n"] == 1
    assert result["targets"]["half_full"]["rps"] >= 0
    assert result["targets"]["half_full"]["ece"] >= 0
    assert result["targets"]["handicap"]["log_loss"] == pytest.approx(-math.log(0.7))
    assert result["targets"]["handicap"]["rps"] >= 0
    assert result["targets"]["total_over_under"]["sample_n"] == 1


def test_evaluation_keeps_independent_targets_when_scoreline_marginal_disagrees():
    """Cross-target display distributions must not block usable metrics.

    ``scoreline_matrix`` and ``total_goals_probability`` are rendered by
    separate target lanes.  A stale/rounded marginal in one lane should not
    erase an otherwise valid 1X2 or totals evaluation; strict producers still
    validate their own contract before publication.
    """

    row = {
        "cutoff_at": "2026-08-11T12:00:00+00:00",
        "kickoff_at": "2026-08-11T13:00:00+00:00",
        "dixon_coles_probability": {"home": 0.6, "draw": 0.2, "away": 0.2},
        "outcome_1x2": "home",
        "scoreline_matrix": {"1-0": 0.3, "0-0": 0.7},
        "total_goals_probability": {
            "0": 0.1,
            "1": 0.5,
            "2": 0.4,
            "3": 0.0,
            "4": 0.0,
            "5+": 0.0,
        },
        "actual_total_goals": "1",
    }

    result = evaluate_prediction_rows([row])

    assert result["targets"]["three_way"]["sample_n"] == 1
    assert result["targets"]["totals"]["sample_n"] == 1
