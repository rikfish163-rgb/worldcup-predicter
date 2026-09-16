"""Conservative release gates for research-only football predictions."""

from __future__ import annotations

from league_platform.metrics import assert_no_future_leakage, assert_probability_contract


SAMPLE_THRESHOLDS = {
    "three_way": 1000,
    "handicap": 1000,
    "totals": 1000,
    "scoreline": 5000,
    "half_full": 1000,
}


def release_gate(
    target: str,
    *,
    sample_n: int,
    model_metrics: dict[str, float],
    frequency_metrics: dict[str, float] | None,
    market_metrics: dict[str, float] | None = None,
    prediction_rows: list[dict] | None = None,
    coverage_level: str = "high",
    primary_metric: str = "brier",
    comparison_sample_n: int | None = None,
) -> dict[str, object]:
    """Return an auditable status; never upgrades a model to betting advice."""

    failures: list[str] = []
    threshold = SAMPLE_THRESHOLDS.get(target)
    if threshold is None:
        failures.append("unknown_target")
    elif sample_n < threshold:
        failures.append("insufficient_sample")
    if comparison_sample_n is not None and threshold is not None and comparison_sample_n < threshold:
        failures.append("insufficient_comparison_sample")
    if prediction_rows is not None:
        try:
            assert_no_future_leakage(prediction_rows)
        except ValueError as exc:
            failures.append(f"future_leakage:{exc}")
        try:
            assert_probability_contract(prediction_rows)
        except ValueError as exc:
            failures.append(f"invalid_probability_contract:{exc}")
    if coverage_level == "low":
        failures.append("low_feature_coverage")
    model_value = model_metrics.get(primary_metric)
    frequency_value = frequency_metrics.get(primary_metric) if frequency_metrics else None
    market_value = market_metrics.get(primary_metric) if market_metrics else None
    if frequency_value is not None and model_value is not None:
        if model_value > frequency_value + 0.005:
            failures.append("worse_than_frequency_baseline")
    market_status = "not_available"
    if market_value is not None and model_value is not None:
        market_status = "beats_market" if model_value < market_value else "underperforms_market"
    if failures:
        status = "blocked"
    elif market_status == "underperforms_market":
        status = "research_only_underperforms_market"
    else:
        status = "research_ready"
    return {
        "target": target,
        "status": status,
        "sample_n": sample_n,
        "comparison_sample_n": comparison_sample_n,
        "sample_threshold": threshold,
        "primary_metric": primary_metric,
        "failures": failures,
        "market_status": market_status,
        "can_display": status != "blocked",
        "production_allowed": False,
    }


__all__ = ["SAMPLE_THRESHOLDS", "release_gate"]
