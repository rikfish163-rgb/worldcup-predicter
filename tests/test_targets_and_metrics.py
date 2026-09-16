import pytest

from league_platform.metrics import (
    assert_no_future_leakage,
    assert_probability_contract,
    evaluate_three_way,
    top_k_scoreline_hit,
)
from league_platform.targets import (
    half_full_distribution,
    handicap_distribution,
    one_x_two_from_matrix,
    scoreline_matrix,
    settle_handicap,
    settle_total,
    top_scorelines,
    total_goals_distribution,
    total_over_under_distribution,
)


def test_scoreline_matrix_and_derived_targets_are_normalized():
    matrix = scoreline_matrix(1.35, 0.95, rho=-0.08)
    assert abs(sum(matrix.values()) - 1) < 1e-9
    assert abs(sum(one_x_two_from_matrix(matrix).values()) - 1) < 1e-9
    assert abs(sum(total_goals_distribution(matrix).values()) - 1) < 1e-9
    assert abs(sum(handicap_distribution(matrix, -0.25).values()) - 1) < 1e-9
    assert abs(sum(total_over_under_distribution(matrix).values()) - 1) < 1e-9
    totals = total_goals_distribution(matrix)
    assert totals["0"] == pytest.approx(matrix["0-0"])
    assert totals["5+"] == pytest.approx(sum(
        probability for score, probability in matrix.items()
        if sum(int(value) for value in score.split("-")) >= 5
    ))
    assert len(top_scorelines(matrix)) == 5
    assert abs(sum(half_full_distribution(1.35, 0.95).values()) - 1) < 1e-9


def test_official_style_quarter_handicap_settlement():
    assert settle_handicap(1, 1, -0.25) == "half_loss"
    assert settle_handicap(2, 1, -0.25) == "win"
    assert settle_handicap(1, 1, 0) == "push"
    assert settle_total(2, 2.25, over=True) == "half_loss"
    assert settle_total(3, 2.25, over=True) == "win"


def test_metrics_reject_future_information():
    rows = [{
        "cutoff_at": "2026-08-11T12:00:00+00:00",
        "kickoff_at": "2026-08-11T13:00:00+00:00",
        "feature_times": ["2026-08-11T12:01:00+00:00"],
    }]
    with pytest.raises(ValueError, match="feature timestamp"):
        assert_no_future_leakage(rows)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "model_training_cutoff",
            "2026-08-11T12:01:00+00:00",
            "model_training_cutoff",
        ),
        (
            "factor_trace",
            {
                "steps": [
                    {
                        "id": "late-factor",
                        "observed_at": "2026-08-11T12:01:00+00:00",
                    }
                ]
            },
            "factor_trace.steps",
        ),
    ],
)
def test_metrics_reject_training_and_factor_observations_after_cutoff(
    field, value, message
):
    row = {
        "cutoff_at": "2026-08-11T12:00:00+00:00",
        "kickoff_at": "2026-08-11T13:00:00+00:00",
        "feature_times": [],
        field: value,
    }
    with pytest.raises(ValueError, match=message):
        assert_no_future_leakage([row])


def test_probability_contract_rejects_non_normalized_distributions():
    assert_probability_contract([{"fixture_id": "ok", "elo_probability": {"home": 0.5, "draw": 0.25, "away": 0.25}}])
    with pytest.raises(ValueError, match="sum is not one"):
        assert_probability_contract([{"fixture_id": "bad", "elo_probability": {"home": 0.7, "draw": 0.2, "away": 0.2}}])


def test_probability_contract_rejects_inconsistent_scoreline_total_marginal():
    row = {
        "fixture_id": "bad-marginal",
        "scoreline_matrix": {"0-0": 0.5, "1-0": 0.5},
        "scoreline_probability": {"home": 0.5, "draw": 0.5, "away": 0.0},
        "total_goals_probability": {
            "0": 0.1,
            "1": 0.1,
            "2": 0.2,
            "3": 0.3,
            "4": 0.2,
            "5+": 0.1,
        },
    }
    with pytest.raises(ValueError, match="total_goals_probability.*scoreline_matrix"):
        assert_probability_contract([row])


def test_three_way_metrics_and_top_scoreline_hit():
    probabilities = [
        {"home": 0.6, "draw": 0.2, "away": 0.2},
        {"home": 0.2, "draw": 0.5, "away": 0.3},
    ]
    assert evaluate_three_way(probabilities, ["home", "draw"])["sample_n"] == 2
    predictions = [[{"score": "1-0", "probability": 0.4}], [{"score": "0-0", "probability": 0.3}]]
    assert top_k_scoreline_hit(predictions, ["1-0", "2-1"], 1) == 0.5
