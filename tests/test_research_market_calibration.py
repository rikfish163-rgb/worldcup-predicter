from __future__ import annotations

import numpy as np
import pytest

from league_platform.research_market_calibration import (
    Example,
    examples_from_rows,
    fit_calibrator,
    predict_calibrator,
)


def _example(index: int) -> Example:
    outcome = ("home", "draw", "away")[index % 3]
    return Example(
        league="premier-league",
        season="1819",
        fixture_id=str(index),
        outcome=outcome,
        raw_probability=(0.46, 0.25, 0.29),
        opening_probability=(0.45, 0.26, 0.29),
        model_probability=(0.43, 0.28, 0.29),
        expected_goals=(1.45, 1.15),
    )


def test_pooled_calibrator_produces_finite_normalized_probabilities():
    examples = [_example(index) for index in range(120)]
    model = fit_calibrator(
        examples,
        target="three_way",
        feature_set="closing_opening_model_league",
        regularisation=0.1,
        iterations=120,
    )
    predictions = predict_calibrator(model, examples[:5])
    assert predictions.shape == (5, 3)
    assert np.all(np.isfinite(predictions))
    assert np.all(predictions >= 0)
    assert np.allclose(predictions.sum(axis=1), 1.0)


def test_complete_case_builder_drops_missing_opening_market_instead_of_filling_zero():
    valid = {
        "fixture_id": "fixture-1",
        "season": "1819",
        "market_closing_probability": {"home": 0.5, "draw": 0.25, "away": 0.25},
        "market_opening_probability": {"home": 0.48, "draw": 0.27, "away": 0.25},
        "scoreline_probability": {"home": 0.45, "draw": 0.3, "away": 0.25},
        "outcome_1x2": "home",
        "expected_goals": {"home": 1.6, "away": 1.0},
    }
    missing = {**valid, "fixture_id": "fixture-2", "market_opening_probability": None}
    rows = examples_from_rows(
        {"premier-league": [valid, missing]},
        "three_way",
    )
    assert [row.fixture_id for row in rows] == ["fixture-1"]


def test_calibrator_rejects_too_few_rows_and_unknown_target():
    with pytest.raises(ValueError, match="at least 100"):
        fit_calibrator(
            [_example(index) for index in range(99)],
            target="three_way",
            feature_set="closing_only",
            regularisation=0.1,
        )
    with pytest.raises(ValueError, match="unsupported target"):
        examples_from_rows({}, "scoreline")
