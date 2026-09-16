from league_platform.gates import release_gate


def test_gate_blocks_small_samples_and_low_coverage():
    result = release_gate(
        "scoreline",
        sample_n=100,
        model_metrics={"brier": 0.2},
        frequency_metrics={"brier": 0.3},
        coverage_level="low",
    )
    assert result["status"] == "blocked"
    assert result["can_display"] is False
    assert "insufficient_sample" in result["failures"]


def test_gate_keeps_market_underperformance_research_only():
    result = release_gate(
        "three_way",
        sample_n=1200,
        model_metrics={"brier": 0.58},
        frequency_metrics={"brier": 0.60},
        market_metrics={"brier": 0.56},
    )
    assert result["status"] == "research_only_underperforms_market"
    assert result["can_display"] is True
    assert result["production_allowed"] is False


def test_gate_rejects_future_feature_timestamp():
    result = release_gate(
        "three_way",
        sample_n=1200,
        model_metrics={"brier": 0.5},
        frequency_metrics={"brier": 0.6},
        prediction_rows=[{
            "cutoff_at": "2026-08-11T12:00:00+00:00",
            "kickoff_at": "2026-08-11T13:00:00+00:00",
            "feature_times": ["2026-08-11T12:01:00+00:00"],
        }],
    )
    assert result["status"] == "blocked"
    assert any(item.startswith("future_leakage:") for item in result["failures"])


def test_gate_supports_target_specific_metric_and_frequency_baseline():
    result = release_gate(
        "totals",
        sample_n=1200,
        model_metrics={"log_loss": 0.68},
        frequency_metrics={"log_loss": 0.70},
        market_metrics={"log_loss": 0.67},
        primary_metric="log_loss",
    )
    assert result["primary_metric"] == "log_loss"
    assert result["status"] == "research_only_underperforms_market"


def test_gate_requires_comparable_market_sample():
    result = release_gate(
        "three_way",
        sample_n=1200,
        comparison_sample_n=900,
        model_metrics={"brier": 0.5},
        frequency_metrics={"brier": 0.6},
        market_metrics={"brier": 0.55},
    )
    assert result["status"] == "blocked"
    assert "insufficient_comparison_sample" in result["failures"]
