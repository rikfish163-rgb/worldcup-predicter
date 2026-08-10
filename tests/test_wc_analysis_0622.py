import math
from pathlib import Path

from wc_analysis import predict as legacy_predict
from wc_analysis.worldcup_0622_analysis import (
    dixon_coles_tau,
    poisson_score_matrix,
    summarize_score_matrix,
    weighted_mean,
)


def test_legacy_data_route_rejects_absolute_and_traversal_paths():
    assert legacy_predict._public_data_target("/data//etc/passwd") is None
    assert legacy_predict._public_data_target("/data/%2e%2e/predict.py") is None
    target = legacy_predict._public_data_target("/data/predictions.json")
    assert target == (legacy_predict.DATA_DIR / "predictions.json").resolve()


def test_legacy_mutation_routes_require_configured_bearer_token(monkeypatch):
    monkeypatch.delenv("WC_ADMIN_TOKEN", raising=False)
    assert not legacy_predict._admin_authorized({})
    monkeypatch.setenv("WC_ADMIN_TOKEN", "test-only-token")
    assert not legacy_predict._admin_authorized({})
    assert not legacy_predict._admin_authorized({"Authorization": "Bearer wrong"})
    assert legacy_predict._admin_authorized({"Authorization": "Bearer test-only-token"})


def test_legacy_network_helpers_keep_tls_verification_and_loopback_binding():
    sporttery_server = next(Path("wc_analysis").glob("**/sporttery_server.py")).read_text()
    assert 'HTTPServer(("127.0.0.1", port)' in sporttery_server
    assert 'HTTPServer(("0.0.0.0", port)' not in sporttery_server
    for path in (Path("wc_analysis/build_groups.py"), Path("wc_analysis/fetch_pinnacle.py")):
        assert "CERT_NONE" not in path.read_text()


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
