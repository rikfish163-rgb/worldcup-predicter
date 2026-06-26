import math

from wc_analysis.worldcup_0622_analysis import (
    dixon_coles_tau,
    poisson_score_matrix,
    summarize_score_matrix,
    weighted_mean,
)


def test_weighted_mean_uses_newer_matches_more_heavily():
    assert math.isclose(weighted_mean([1.0, 3.0, 9.0], decay=0.5), 43 / 7)


def test_dixon_coles_tau_only_changes_low_scores():
    rho = -0.08
    assert dixon_coles_tau(0, 0, 1.4, 1.1, rho) != 1.0
    assert dixon_coles_tau(1, 0, 1.4, 1.1, rho) != 1.0
    assert dixon_coles_tau(2, 0, 1.4, 1.1, rho) == 1.0
    assert dixon_coles_tau(2, 2, 1.4, 1.1, rho) == 1.0


def test_score_matrix_is_normalized_and_summarized():
    matrix = poisson_score_matrix(1.6, 0.9, rho=-0.06, max_goals=8)
    total = sum(matrix.values())
    assert math.isclose(total, 1.0, abs_tol=1e-9)

    summary = summarize_score_matrix(matrix, handicap=-1.0)
    assert set(summary["wld"]) == {"home", "draw", "away"}
    assert set(summary["handicap"]) == {"home", "draw", "away"}
    assert set(summary["totals"]) >= {"0", "1", "2", "3", "4", "5", "6", "7"}
    assert summary["top_scores"][0]["prob"] >= summary["top_scores"][1]["prob"]
