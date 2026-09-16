from league_platform.model_factors import build_factor_trace, factor_step


def test_factor_trace_reports_step_delta_without_inventing_missing_effect():
    before = {"0-0": 0.25, "1-0": 0.25, "0-1": 0.50}
    after = {"0-0": 0.20, "1-0": 0.50, "0-1": 0.30}
    step = factor_step(
        factor_id="recent_xg",
        label="近期 xG 调整",
        category="model_feature",
        before_matrix=before,
        after_matrix=after,
        before_expected_goals={"home": 0.8, "away": 1.1},
        after_expected_goals={"home": 1.2, "away": 0.9},
        observed_at="2026-08-18T10:00:00+00:00",
    )
    trace = build_factor_trace(
        [step],
        not_applied=[
            {
                "id": "weather",
                "label": "天气",
                "status": "missing",
                "enters_model": False,
                "delta_probability_1x2": None,
            }
        ],
    )

    assert trace["schema_version"] == "matchline.model_factor_trace.v1"
    assert trace["method"] == "sequential_model_step_delta"
    assert trace["steps"][0]["after_probability_1x2"] == {
        "home": 0.5,
        "draw": 0.2,
        "away": 0.3,
    }
    assert trace["steps"][0]["delta_probability_1x2"] == {
        "home": 0.25,
        "draw": -0.05,
        "away": -0.2,
    }
    assert trace["not_applied"][0]["delta_probability_1x2"] is None
