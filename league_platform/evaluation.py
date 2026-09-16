"""Strict, as-of evaluation helpers for frozen Matchline predictions."""

from __future__ import annotations

import math
from collections.abc import Iterable

from league_platform.metrics import (
    assert_no_future_leakage,
    assert_probability_contract,
    bootstrap_mean_ci,
    brier_score,
    count_metrics,
    evaluate_three_way,
    expected_calibration_error,
    log_loss,
    ranked_probability_score,
    top_k_scoreline_hit,
)


_MATRIX_PROBABILITY_FIELDS = frozenset(
    {"scoreline_matrix", "independent_scoreline_matrix"}
)


def _assert_evaluation_probability_contract(rows: list[dict]) -> None:
    """Validate evaluation rows without coupling independent target lanes.

    The strict producer/release gates continue to call
    :func:`assert_probability_contract` on complete rows, including the
    scoreline-to-marginal consistency check.  This generic evaluator is also
    used to inspect legacy/display rows where scoreline and totals
    distributions were rendered independently.  Validate every distribution
    (and each scoreline matrix's own labels/sum) while keeping those target
    lanes separable so one stale marginal does not erase valid metrics for
    another target.
    """

    scalar_rows: list[dict] = []
    matrix_rows: list[dict] = []
    for row in rows:
        scalar = dict(row)
        matrix = {"fixture_id": row.get("fixture_id", "unknown")}
        for field in _MATRIX_PROBABILITY_FIELDS:
            if field in scalar:
                matrix[field] = scalar.pop(field)
        scalar_rows.append(scalar)
        if len(matrix) > 1:
            matrix_rows.append(matrix)
    # With matrix fields removed, all remaining distributions are still
    # checked for type, finite values, range and normalization.  Matrix-only
    # rows retain the metrics-level scoreline label/sum validation but have no
    # derived marginals against which to enforce a cross-target equality.
    assert_probability_contract(scalar_rows)
    if matrix_rows:
        assert_probability_contract(matrix_rows)


def _ordered_probabilities(rows: list[dict], prediction_key: str, labels: tuple[str, ...]) -> list[dict[str, float]]:
    return [
        {label: float(row[prediction_key].get(label, 0.0)) for label in labels}
        for row in rows
    ]


def _distribution_metrics(
    rows: list[dict],
    prediction_key: str,
    outcome_key: str,
    *,
    labels: tuple[str, ...] | None = None,
) -> dict[str, object]:
    probabilities = (
        _ordered_probabilities(rows, prediction_key, labels)
        if labels is not None
        else [row[prediction_key] for row in rows]
    )
    outcomes = [str(row[outcome_key]) for row in rows]
    result: dict[str, object] = {
        "sample_n": len(rows),
        "brier": brier_score(probabilities, outcomes),
        "log_loss": log_loss(probabilities, outcomes),
    }
    if labels is not None:
        result["rps"] = ranked_probability_score(probabilities, outcomes)
        result["ece"] = expected_calibration_error(probabilities, outcomes)
    if len(rows) >= 100:
        row_losses = [-math.log(max(1e-12, probability[outcome])) for probability, outcome in zip(probabilities, outcomes)]
        row_briers = [
            sum((probability[label] - (label == outcome)) ** 2 for label in probability)
            for probability, outcome in zip(probabilities, outcomes)
        ]
        result["log_loss_ci"] = bootstrap_mean_ci(row_losses)
        result["brier_ci"] = bootstrap_mean_ci(row_briers)
    return result


_TOTAL_LABELS = ("0", "1", "2", "3", "4", "5+")
_HALF_FULL_LABELS = tuple(f"{half}/{full}" for half in "HDA" for full in "HDA")
_HANDICAP_LABELS = ("win", "push", "loss", "half_win", "half_loss")


def _total_label(value: object) -> str:
    try:
        return str(int(value)) if int(value) < 5 else "5+"
    except (TypeError, ValueError):
        text = str(value)
        return text if text in _TOTAL_LABELS else "5+"


def _central_interval_coverage(rows: list[dict], *, level: float = 0.8) -> dict[str, object]:
    """Measure central categorical interval coverage for total-goal buckets."""

    if not 0 < level < 1:
        raise ValueError("interval level must be between zero and one")
    alpha = (1.0 - level) / 2.0
    covered = 0
    usable = 0
    for row in rows:
        probabilities = row.get("total_goals_probability")
        if not isinstance(probabilities, dict) or row.get("actual_total_goals") is None:
            continue
        values = [max(0.0, float(probabilities.get(label, 0.0))) for label in _TOTAL_LABELS]
        total = sum(values)
        if not math.isfinite(total) or total <= 0:
            continue
        values = [value / total for value in values]
        cumulative = 0.0
        lower = 0
        upper = len(_TOTAL_LABELS) - 1
        lower_set = False
        for index, value in enumerate(values):
            cumulative += value
            if cumulative >= alpha and not lower_set:
                lower = index
                lower_set = True
            if cumulative >= 1.0 - alpha:
                upper = index
                break
        actual_index = _TOTAL_LABELS.index(_total_label(row["actual_total_goals"]))
        covered += lower <= actual_index <= upper
        usable += 1
    return {
        "level": level,
        "sample_n": usable,
        "coverage": covered / usable if usable else None,
    }


def evaluate_prediction_rows(rows: Iterable[dict]) -> dict[str, object]:
    """Evaluate only rows whose frozen features pass the causal timestamp check.

    Actual outcomes must be joined after the match, while prediction fields are
    the immutable values recorded at their freeze time.  Missing target rows are
    reported as unavailable rather than silently treated as failures or zeros.
    """

    materialized = list(rows)
    assert_no_future_leakage(materialized)
    _assert_evaluation_probability_contract(materialized)
    result: dict[str, object] = {"sample_n": len(materialized), "targets": {}}
    three_way_rows = [
        row
        for row in materialized
        if row.get("outcome_1x2") is not None
        and isinstance(row.get("dixon_coles_probability"), dict)
    ]
    if three_way_rows:
        result["targets"]["three_way"] = evaluate_three_way(
            [row["dixon_coles_probability"] for row in three_way_rows],
            [row["outcome_1x2"] for row in three_way_rows],
        )
    else:
        result["targets"]["three_way"] = {"status": "unavailable", "sample_n": 0}

    score_rows = [
        row
        for row in materialized
        if row.get("actual_score") is not None and isinstance(row.get("scoreline_top5"), list)
    ]
    if score_rows:
        result["targets"]["scoreline"] = {
            "sample_n": len(score_rows),
            "exact_hit": top_k_scoreline_hit(
                [row["scoreline_top5"] for row in score_rows],
                [str(row["actual_score"]) for row in score_rows],
                1,
            ),
            "top1_hit": top_k_scoreline_hit(
                [row["scoreline_top5"] for row in score_rows],
                [str(row["actual_score"]) for row in score_rows],
                1,
            ),
            "top3_hit": top_k_scoreline_hit(
                [row["scoreline_top5"] for row in score_rows],
                [str(row["actual_score"]) for row in score_rows],
                3,
            ),
            "top5_hit": top_k_scoreline_hit(
                [row["scoreline_top5"] for row in score_rows],
                [str(row["actual_score"]) for row in score_rows],
                5,
            ),
        }
        matrix_rows = [row for row in score_rows if isinstance(row.get("scoreline_matrix"), dict)]
        if matrix_rows:
            score_labels = tuple(
                sorted(
                    {score for row in matrix_rows for score in row["scoreline_matrix"]},
                    key=lambda score: tuple(int(value) for value in score.split("-", 1)),
                )
            )
            matrix_metrics = _distribution_metrics(
                matrix_rows,
                "scoreline_matrix",
                "actual_score",
                labels=score_labels,
            )
            result["targets"]["scoreline"].update(
                {
                    "distribution_log_loss": matrix_metrics["log_loss"],
                    "distribution_rps": matrix_metrics["rps"],
                    "distribution_ece": matrix_metrics["ece"],
                    "distribution_sample_n": len(matrix_rows),
                }
            )
    else:
        result["targets"]["scoreline"] = {"status": "unavailable", "sample_n": 0}

    total_rows = [
        row
        for row in materialized
        if row.get("actual_total_goals") is not None
        and isinstance(row.get("total_goals_probability"), dict)
    ]
    if total_rows:
        result["targets"]["totals"] = _distribution_metrics(
            total_rows, "total_goals_probability", "actual_total_goals", labels=_TOTAL_LABELS
        )
    else:
        result["targets"]["totals"] = {"status": "unavailable", "sample_n": 0}

    goal_rows = [
        row
        for row in materialized
        if isinstance(row.get("expected_goals"), dict)
        and row.get("actual_home_goals") is not None
        and row.get("actual_away_goals") is not None
    ]
    if goal_rows:
        expected = [
            float(row["expected_goals"]["home"]) + float(row["expected_goals"]["away"])
            for row in goal_rows
        ]
        actual = [int(row["actual_home_goals"]) + int(row["actual_away_goals"]) for row in goal_rows]
        result["targets"]["goal_count"] = {"sample_n": len(goal_rows), **count_metrics(expected, actual)}
        total_probability_rows = [
            row for row in materialized
            if isinstance(row.get("total_goals_probability"), dict)
            and row.get("actual_total_goals") is not None
        ]
        if total_probability_rows:
            distribution = _distribution_metrics(
                total_probability_rows,
                "total_goals_probability",
                "actual_total_goals",
                labels=_TOTAL_LABELS,
            )
            result["targets"]["goal_count"].update({
                "distribution_log_loss": distribution["log_loss"],
                "distribution_ece": distribution["ece"],
                "interval_coverage": _central_interval_coverage(total_probability_rows),
            })
    else:
        result["targets"]["goal_count"] = {"status": "unavailable", "sample_n": 0}

    half_full_rows = [
        row
        for row in materialized
        if row.get("actual_half_full") is not None
        and isinstance(row.get("half_full_probability"), dict)
    ]
    if half_full_rows:
        result["targets"]["half_full"] = _distribution_metrics(
            half_full_rows, "half_full_probability", "actual_half_full", labels=_HALF_FULL_LABELS
        )
    else:
        result["targets"]["half_full"] = {"status": "unavailable", "sample_n": 0}

    handicap_rows = [
        row
        for row in materialized
        if row.get("actual_handicap") is not None
        and isinstance(row.get("handicap_probability"), dict)
    ]
    if handicap_rows:
        result["targets"]["handicap"] = _distribution_metrics(
            handicap_rows, "handicap_probability", "actual_handicap", labels=_HANDICAP_LABELS
        )
    else:
        result["targets"]["handicap"] = {"status": "unavailable", "sample_n": 0}

    total_ou_rows = [
        row
        for row in materialized
        if row.get("actual_total_ou") in {"over", "under"}
        and isinstance(row.get("total_over_under_probability"), dict)
    ]
    if total_ou_rows:
        result["targets"]["total_over_under"] = _distribution_metrics(
            total_ou_rows,
            "total_over_under_probability",
            "actual_total_ou",
            labels=("over", "under"),
        )
    else:
        result["targets"]["total_over_under"] = {"status": "unavailable", "sample_n": 0}
    return result


__all__ = ["evaluate_prediction_rows"]
