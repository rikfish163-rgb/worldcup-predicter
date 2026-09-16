"""Evaluation metrics and leakage checks for pre-match targets."""

from __future__ import annotations

import math
import random
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping


PROBABILITY_FIELDS = (
    "elo_probability",
    "frequency_probability",
    "market_probability",
    "dixon_coles_probability",
    "primary_probability_1x2",
    "probability_1x2_from_scoreline",
    "scoreline_probability",
    "scoreline_matrix",
    "independent_scoreline_probability",
    "independent_scoreline_matrix",
    "scoreline_frequency_probability",
    "total_goals_probability",
    "independent_total_goals_probability",
    "total_frequency_probability",
    "total_over_under_probability",
    "independent_total_over_under_probability",
    "total_frequency_ou_probability",
    "total_market_probability",
    "market_total_goals_probability",
    "market_scoreline_probability",
    "market_half_full_probability",
    "half_full_probability",
    "half_full_frequency_probability",
    "handicap_probability",
    "handicap_frequency_probability",
    "handicap_market_probability",
    "market_handicap_probability",
)


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def brier_score(probabilities: list[dict[str, float]], outcomes: list[str]) -> float:
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("probabilities and outcomes must have equal non-zero length")
    classes = tuple(probabilities[0])
    return sum(
        sum((float(row.get(label, 0.0)) - (label == outcome)) ** 2 for label in classes)
        for row, outcome in zip(probabilities, outcomes)
    ) / len(outcomes)


def log_loss(probabilities: list[dict[str, float]], outcomes: list[str]) -> float:
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("probabilities and outcomes must have equal non-zero length")
    return -sum(math.log(max(1e-12, float(row.get(outcome, 0.0)))) for row, outcome in zip(probabilities, outcomes)) / len(outcomes)


def ranked_probability_score(probabilities: list[dict[str, float]], outcomes: list[str]) -> float:
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("probabilities and outcomes must have equal non-zero length")
    classes = tuple(probabilities[0])
    total = 0.0
    for row, outcome in zip(probabilities, outcomes):
        cumulative = 0.0
        actual = 0.0
        for label in classes[:-1]:
            cumulative += float(row.get(label, 0.0))
            actual += float(label == outcome)
            total += (cumulative - actual) ** 2
    return total / (2 * len(outcomes))


def expected_calibration_error(probabilities: list[dict[str, float]], outcomes: list[str], bins: int = 10) -> float:
    if bins < 2:
        raise ValueError("bins must be at least two")
    groups: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for row, outcome in zip(probabilities, outcomes):
        label, probability = max(row.items(), key=lambda item: item[1])
        groups[min(bins - 1, int(float(probability) * bins))].append((float(probability), float(label == outcome)))
    count = len(outcomes)
    return sum(len(items) / count * abs(sum(p for p, _ in items) / len(items) - sum(a for _, a in items) / len(items)) for items in groups.values())


def count_metrics(expected: list[float], actual: list[int]) -> dict[str, float]:
    if len(expected) != len(actual) or not expected:
        raise ValueError("expected and actual must have equal non-zero length")
    nll = sum(rate - goals * math.log(max(1e-12, rate)) + math.lgamma(goals + 1) for rate, goals in zip(expected, actual)) / len(actual)
    return {"mae": sum(abs(rate - goals) for rate, goals in zip(expected, actual)) / len(actual), "poisson_nll": nll}


def top_k_scoreline_hit(predictions: list[list[dict[str, float | str]]], actual: list[str], k: int) -> float:
    if len(predictions) != len(actual) or not predictions or k < 1:
        raise ValueError("predictions and actual must have valid equal lengths")
    return sum(any(row.get("score") == result for row in items[:k]) for items, result in zip(predictions, actual)) / len(actual)


def bootstrap_mean_ci(values: list[float], *, seed: int = 20260811, samples: int = 1000, level: float = 0.95) -> dict[str, float]:
    if not values or not 0 < level < 1 or samples < 100:
        raise ValueError("invalid bootstrap arguments")
    rng = random.Random(seed)
    means = [sum(rng.choice(values) for _ in values) / len(values) for _ in range(samples)]
    means.sort()
    lower_index = int((1 - level) / 2 * samples)
    upper_index = int((1 - (1 - level) / 2) * samples) - 1
    return {"mean": sum(values) / len(values), "lower": means[lower_index], "upper": means[upper_index], "level": level}


def assert_no_future_leakage(rows: list[dict]) -> None:
    """Reject any prediction feature or observation timestamp after its cutoff.

    The historical backtest and prospective archive use a few different names
    for the same causal boundary (``cutoff_at``/``freeze_cutoff_at`` and
    ``prediction_observed_at``/``as_of``).  This audit deliberately accepts
    those optional fields without requiring them, so old rows remain readable,
    while rejecting a supplied timestamp that could only have been known after
    the freeze.  ``factor_trace`` and source provenance are audited recursively
    because a late nested observation is still a future leak.
    """

    def _optional_timestamp(value: object, *, label: str) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            return _utc(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} is invalid") from exc

    def _audit_nested_observations(value: object, *, path: str, cutoff: datetime) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                key_text = str(key)
                if key_text in {
                    "observed_at",
                    "effective_at",
                    "retrieved_at",
                    "observation_time",
                    "observed_at_exact",
                }:
                    observed = _optional_timestamp(nested, label=f"{path}.{key_text}")
                    if observed is not None and observed > cutoff:
                        raise ValueError(f"{path}.{key_text} is after prediction cutoff")
                elif isinstance(nested, (Mapping, list, tuple)):
                    _audit_nested_observations(nested, path=f"{path}.{key_text}", cutoff=cutoff)
        elif isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                if isinstance(nested, (Mapping, list, tuple)):
                    _audit_nested_observations(nested, path=f"{path}[{index}]", cutoff=cutoff)

    for row in rows:
        cutoff_value = row.get("cutoff_at") or row.get("freeze_cutoff_at")
        if cutoff_value in (None, ""):
            raise ValueError("prediction cutoff is missing")
        if not isinstance(cutoff_value, (datetime, str)):
            raise ValueError("prediction cutoff is invalid")
        cutoff = _utc(cutoff_value)
        kickoff = _utc(row["kickoff_at"])
        if cutoff >= kickoff:
            raise ValueError("prediction cutoff must be before kickoff")

        # Training/calibration state must be frozen no later than the row's
        # cutoff.  These fields are optional for compatibility with compact
        # legacy rows, but if present they are never allowed to point forward.
        for field in (
            "model_training_cutoff",
            "scoreline_rho_training_cutoff",
            "fixture_freeze_observed_at",
            "lineup_observed_at",
        ):
            observed = _optional_timestamp(row.get(field), label=field)
            if observed is not None and observed > cutoff:
                raise ValueError(f"{field} is after prediction cutoff")

        # A prediction may be persisted shortly after the freeze, but it must
        # still be observed before kickoff and never before the declared
        # freeze cutoff.  ``as_of`` is treated as the same observation marker.
        for field in ("prediction_observed_at", "as_of"):
            observed = _optional_timestamp(row.get(field), label=field)
            if observed is not None:
                if observed < cutoff:
                    raise ValueError(f"{field} is before prediction cutoff")
                if observed >= kickoff:
                    raise ValueError(f"{field} is not before kickoff")

        # An exact kickoff observation is meaningful only when a freeze-stage
        # row declares the causal snapshot.  Historical training rows often
        # leave this field null because their source has no original HTTP
        # observation timestamp.
        if row.get("freeze_stage") is not None or row.get("fixture_freeze_observed_at"):
            observed = _optional_timestamp(
                row.get("kickoff_time_observed_at"), label="kickoff_time_observed_at"
            )
            if observed is not None and observed > cutoff:
                raise ValueError("kickoff_time_observed_at is after prediction cutoff")
        for timestamp in row.get("feature_times", []):
            parsed = _optional_timestamp(timestamp, label="feature timestamp")
            if parsed is not None and parsed > cutoff:
                raise ValueError("feature timestamp is after prediction cutoff")
        if row.get("market_retrieved_at") and _utc(row["market_retrieved_at"]) > cutoff:
            raise ValueError("market observation is after prediction cutoff")
        if row.get("market_time_bound_at") and _utc(row["market_time_bound_at"]) > cutoff:
            raise ValueError("market time bound is after prediction cutoff")
        for field in (
            "live_feature_sources",
            "factor_trace",
            "historical_xg",
            "player_availability_shadow",
        ):
            if row.get(field) is not None:
                _audit_nested_observations(row[field], path=field, cutoff=cutoff)


_SCORELINE_LABEL = re.compile(r"^(\d+)-(\d+)$")
_TOTAL_GOAL_LABELS = frozenset({"0", "1", "2", "3", "4", "5+"})


def _scoreline_matrix_marginals(
    matrix: Mapping[object, object],
    *,
    fixture_id: object,
    tolerance: float,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return 1X2 and total-goals marginals for a validated scoreline matrix."""

    values: dict[str, float] = {}
    for raw_label, raw_value in matrix.items():
        label = str(raw_label)
        match = _SCORELINE_LABEL.fullmatch(label)
        if match is None:
            raise ValueError(f"invalid scoreline matrix label {fixture_id}:{label}")
        try:
            home, away = int(match.group(1)), int(match.group(2))
            if not isinstance(raw_value, (float, int, str)):
                raise TypeError("scoreline probability must be numeric")
            number = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid scoreline matrix value {fixture_id}:{label}") from exc
        if home < 0 or away < 0 or not math.isfinite(number) or number < -tolerance:
            raise ValueError(f"invalid scoreline matrix value {fixture_id}:{label}")
        values[label] = number
    total = sum(values.values())
    if not values or total <= 0 or abs(total - 1.0) > tolerance:
        raise ValueError(f"scoreline matrix probability sum is not one {fixture_id}")
    values = {label: number / total for label, number in values.items()}
    one_x_two = {label: 0.0 for label in ("home", "draw", "away")}
    totals = {label: 0.0 for label in _TOTAL_GOAL_LABELS}
    for label, probability in values.items():
        match = _SCORELINE_LABEL.fullmatch(label)
        assert match is not None  # validated above
        home, away = int(match.group(1)), int(match.group(2))
        outcome = "home" if home > away else "draw" if home == away else "away"
        one_x_two[outcome] += probability
        total_label = str(home + away) if home + away < 5 else "5+"
        totals[total_label] += probability
    return one_x_two, totals


def _distribution_difference(
    observed: Mapping[object, object],
    expected: Mapping[str, float],
) -> float:
    try:
        differences: list[float] = []
        for label in expected:
            value = observed.get(label, 0.0)
            if not isinstance(value, (float, int, str)):
                return math.inf
            differences.append(abs(float(value) - expected[label]))
        return max(differences)
    except (AttributeError, TypeError, ValueError):
        return math.inf


def scoreline_target_consistency_error(
    row: Mapping[str, Any], *, tolerance: float = 1e-5
) -> str | None:
    """Return a human-readable scoreline/total-goals contract violation.

    A row may legitimately omit any of these optional target fields.  When a
    matrix is present, however, its 1X2 and total-goals marginals must agree
    with the corresponding distributions.  The independent matrix pair is
    checked independently so a post-market display blend cannot hide an
    inconsistent pre-market model.
    """

    if tolerance <= 0 or not math.isfinite(tolerance):
        raise ValueError("probability tolerance must be positive and finite")
    fixture_id = row.get("fixture_id", "unknown")
    pairs = (
        ("scoreline_matrix", "scoreline_probability", "total_goals_probability"),
        (
            "independent_scoreline_matrix",
            "independent_scoreline_probability",
            "independent_total_goals_probability",
        ),
    )
    for matrix_field, one_x_two_field, totals_field in pairs:
        matrix = row.get(matrix_field)
        if matrix is None:
            continue
        if not isinstance(matrix, Mapping):
            return f"invalid scoreline matrix {fixture_id}:{matrix_field}"
        try:
            marginals, totals = _scoreline_matrix_marginals(
                matrix, fixture_id=fixture_id, tolerance=tolerance
            )
        except ValueError as exc:
            return str(exc)
        for field, expected in ((one_x_two_field, marginals), (totals_field, totals)):
            observed = row.get(field)
            if observed is None:
                continue
            if not isinstance(observed, Mapping):
                return f"invalid probability distribution {fixture_id}:{field}"
            difference = _distribution_difference(observed, expected)
            if difference > tolerance:
                return f"{field} is inconsistent with {matrix_field} for {fixture_id}"
    return None


# Descriptive alias used by downstream callers that prefer an assertion-style
# name.  Keep both names public to avoid forcing a schema/API migration.
scoreline_total_consistency_error = scoreline_target_consistency_error


def assert_scoreline_target_consistency(
    rows: list[dict], *, tolerance: float = 1e-5
) -> None:
    for row in rows:
        error = scoreline_target_consistency_error(row, tolerance=tolerance)
        if error is not None:
            raise ValueError(error)


assert_scoreline_total_consistency = assert_scoreline_target_consistency


def assert_probability_contract(rows: list[dict], *, tolerance: float = 1e-5) -> None:
    """Reject malformed or non-normalized probability distributions."""

    if tolerance <= 0 or not math.isfinite(tolerance):
        raise ValueError("probability tolerance must be positive and finite")
    for row in rows:
        fixture_id = row.get("fixture_id", "unknown")
        for field in PROBABILITY_FIELDS:
            if field not in row or row[field] is None:
                continue
            values = row[field]
            if not isinstance(values, dict) or not values:
                raise ValueError(f"invalid probability distribution {fixture_id}:{field}")
            numbers: list[float] = []
            for label, value in values.items():
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid probability value {fixture_id}:{field}:{label}") from exc
                if not math.isfinite(number) or number < -tolerance or number > 1 + tolerance:
                    raise ValueError(f"probability out of range {fixture_id}:{field}:{label}")
                numbers.append(number)
            if abs(sum(numbers) - 1.0) > tolerance:
                raise ValueError(f"probability sum is not one {fixture_id}:{field}")
        consistency_error = scoreline_target_consistency_error(row, tolerance=tolerance)
        if consistency_error is not None:
            raise ValueError(consistency_error)


def evaluate_three_way(probabilities: list[dict[str, float]], outcomes: list[str]) -> dict[str, float]:
    assert set(outcomes) <= {"home", "draw", "away"}
    brier = brier_score(probabilities, outcomes)
    return {
        "sample_n": len(outcomes),
        "brier": brier,
        "log_loss": log_loss(probabilities, outcomes),
        "rps": ranked_probability_score(probabilities, outcomes),
        "ece": expected_calibration_error(probabilities, outcomes),
    }


__all__ = [
    "assert_no_future_leakage",
    "assert_probability_contract",
    "assert_scoreline_target_consistency",
    "assert_scoreline_total_consistency",
    "bootstrap_mean_ci",
    "brier_score",
    "count_metrics",
    "evaluate_three_way",
    "expected_calibration_error",
    "log_loss",
    "ranked_probability_score",
    "scoreline_target_consistency_error",
    "scoreline_total_consistency_error",
    "top_k_scoreline_hit",
]
