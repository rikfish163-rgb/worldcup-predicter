"""Transparent model-step evidence for the Matchline research surface.

This module deliberately does not attempt SHAP-style causal attribution.  The
model is a sequence of declared transformations (historical rates, recent xG
and, where accepted, a market blend), so the useful audit object is the
before/after delta for each transformation.  Missing or display-only evidence
is kept in ``not_applied`` instead of being converted into a zero effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from league_platform.targets import one_x_two_from_matrix


_ONE_X_TWO = ("home", "draw", "away")


def _probabilities(matrix: Mapping[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(matrix, Mapping):
        return None
    try:
        values = one_x_two_from_matrix({str(key): float(value) for key, value in matrix.items()})
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    result: dict[str, float] = {}
    for label in _ONE_X_TWO:
        try:
            value = float(values[label])
        except (KeyError, TypeError, ValueError):
            return None
        result[label] = round(value, 8)
    return result


def probability_1x2(matrix: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Return the normalized 1X2 marginal used by the factor trace."""

    return _probabilities(matrix)


def _probability_delta(
    before: Mapping[str, float] | None,
    after: Mapping[str, float] | None,
) -> dict[str, float] | None:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return None
    try:
        return {
            label: round(float(after[label]) - float(before[label]), 8)
            for label in _ONE_X_TWO
        }
    except (KeyError, TypeError, ValueError):
        return None


def _goals(value: Mapping[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, float] = {}
    for side in ("home", "away"):
        try:
            number = float(value[side])
        except (KeyError, TypeError, ValueError):
            return None
        result[side] = round(number, 6)
    return result


def factor_step(
    *,
    factor_id: str,
    label: str,
    category: str,
    before_matrix: Mapping[str, Any] | None,
    after_matrix: Mapping[str, Any] | None,
    before_expected_goals: Mapping[str, Any] | None = None,
    after_expected_goals: Mapping[str, Any] | None = None,
    status: str = "used",
    enters_model: bool = True,
    observed_at: str | None = None,
    source_name: str | None = None,
    source_url: str | None = None,
    reference_probability: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Build one bounded, JSON-safe model-step row."""

    before = _probabilities(before_matrix)
    after = _probabilities(after_matrix)
    reference = None
    if isinstance(reference_probability, Mapping):
        try:
            reference = {
                label: round(float(reference_probability[label]), 8)
                for label in _ONE_X_TWO
            }
        except (KeyError, TypeError, ValueError):
            reference = None
    return {
        "id": factor_id,
        "label": label,
        "category": category,
        "status": status,
        "enters_model": bool(enters_model),
        "before_probability_1x2": before,
        "after_probability_1x2": after,
        "delta_probability_1x2": _probability_delta(before, after),
        "before_expected_goals": _goals(before_expected_goals),
        "after_expected_goals": _goals(after_expected_goals),
        "reference_probability_1x2": reference,
        "observed_at": observed_at,
        "source_name": source_name,
        "source_url": source_url,
        "note": note,
    }


def build_factor_trace(
    steps: Sequence[Mapping[str, Any]],
    *,
    not_applied: Sequence[Mapping[str, Any]] = (),
    note: str = "差值是声明模型步骤的前后变化，不是因果归因、命中保证或投注建议。",
) -> dict[str, Any]:
    """Return the stable public contract for a model-factor audit panel."""

    return {
        "schema_version": "matchline.model_factor_trace.v1",
        "method": "sequential_model_step_delta",
        "note": note,
        "steps": [dict(step) for step in steps],
        "not_applied": [dict(item) for item in not_applied],
    }


__all__ = ["build_factor_trace", "factor_step", "probability_1x2"]
