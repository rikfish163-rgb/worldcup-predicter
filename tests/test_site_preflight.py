from __future__ import annotations

import pytest

from league_platform.site_preflight import probe_public_site


def test_public_site_probe_distinguishes_reachable_empty_runtime() -> None:
    def fetcher(url: str, **_kwargs):
        if url.endswith("/api/ledger"):
            return {"status_code": 200, "content_type": "application/json", "payload": {"fixtures": 0, "sourceRuns": 0}}
        if "/api/source-runs" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"status": "ok", "runs": []}}
        if "/api/matches" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"status": "ok", "matches": []}}
        if "/api/alerts" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"status": "ok", "alerts": []}}
        return {"status_code": 200, "content_type": "application/json", "payload": {"status": "ok"}}

    report = probe_public_site("https://predict.example", fetcher=fetcher)

    assert report["status"] == "reachable_empty_runtime"
    assert report["reachable"] is True
    assert report["all_expected_routes_ok"] is True
    assert report["deployment_ready"] is False
    assert report["production_ready"] is False
    assert report["data_counts"] == {"upcoming_matches": 0, "live_matches": 0, "source_runs": 0}
    assert next(route for route in report["routes"] if route["name"] == "research_alerts")["item_count"] == 0


def test_public_site_probe_marks_stale_routes_and_never_uses_local_fallback() -> None:
    def fetcher(url: str, **_kwargs):
        if url.endswith("/api/evaluations") or url.endswith("/api/v1/catalog"):
            return {"status_code": 404, "content_type": "application/json", "error_code": "http_404"}
        if "/api/source-runs" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"runs": [{"id": "r1"}]}}
        if "/api/matches" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"matches": [{"id": "m1"}]}}
        return {"status_code": 200, "content_type": "application/json", "payload": {"fixtures": 1}}

    report = probe_public_site("https://predict.example/", fetcher=fetcher)

    assert report["status"] == "stale_or_incomplete_routes"
    assert report["has_runtime_rows"] is True
    assert report["deployment_ready"] is False
    assert report["production_ready"] is False
    assert next(route for route in report["routes"] if route["name"] == "evaluations")["error_code"] == "http_404"


def test_public_site_probe_checks_current_versioned_ui_contracts() -> None:
    def fetcher(url: str, **_kwargs):
        if url.endswith("/api/v1/service-status"):
            return {"status_code": 404, "content_type": "text/plain", "error_code": "http_404"}
        if "/api/v1/matches" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"matches": [{"id": "m1"}]}}
        if "/api/v1/sources" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"sources": [{"id": "s1"}]}}
        return {"status_code": 200, "content_type": "application/json", "payload": {"status": "ok", "fixtures": 1}}

    report = probe_public_site("https://predict.example", fetcher=fetcher)

    assert report["status"] == "stale_or_incomplete_routes"
    assert report["deployment_ready"] is False
    service = next(route for route in report["routes"] if route["name"] == "service_status")
    assert service["path"] == "/api/v1/service-status"
    assert service["error_code"] == "http_404"
    assert next(route for route in report["routes"] if route["name"] == "versioned_sources")["item_count"] == 1


def test_public_site_probe_separates_route_readiness_from_model_production_gate() -> None:
    def fetcher(url: str, **_kwargs):
        if url.endswith("/api/prospective-status"):
            return {
                "status_code": 200,
                "content_type": "application/json",
                "payload": {
                    "productionReady": False,
                    "gate": {
                        "status": "research_only_underperforms_market",
                        "productionAllowed": False,
                        "sampleRequirementsMet": False,
                        "allRequiredTargetsScored": False,
                        "predictionFreezesVerified": True,
                    },
                },
            }
        if url.endswith("/api/ledger"):
            return {"status_code": 200, "content_type": "application/json", "payload": {"fixtures": 1}}
        if "/api/source-runs" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"runs": [{"id": "r1"}]}}
        if "/api/matches" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"matches": [{"id": "m1"}]}}
        return {"status_code": 200, "content_type": "application/json", "payload": {"status": "ok"}}

    report = probe_public_site("https://predict.example", fetcher=fetcher)

    assert report["status"] == "ready_for_data_review"
    assert report["deployment_ready"] is True
    assert report["production_ready"] is False
    assert report["production_gate"] == {
        "available": True,
        "status": "research_only_underperforms_market",
        "production_allowed": False,
        "sample_requirements_met": False,
        "all_required_targets_scored": False,
        "prediction_freezes_verified": True,
    }


def test_public_site_probe_checks_server_rendered_html_contract() -> None:
    def fetcher(url: str, **_kwargs):
        if url.endswith("/api/prospective-status"):
            return {
                "status_code": 200,
                "content_type": "application/json",
                "payload": {"productionReady": False, "gate": {"productionAllowed": False}},
            }
        if "/api/source-runs" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"runs": [{"id": "r1"}]}}
        if "/api/matches" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"matches": [{"id": "m1"}]}}
        return {"status_code": 200, "content_type": "application/json", "payload": {"fixtures": 1}}

    def html_fetcher(url: str, **_kwargs):
        if url.endswith("/matches/61"):
            return {
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "body": '<main class="detail-page"><span>Matchline</span><strong>单场数据不可用</strong></main>',
            }
        return {
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "body": "<title>Matchline · 比赛数据台</title><main>Matchline 比赛数据台</main>",
        }

    report = probe_public_site("https://predict.example", fetcher=fetcher, html_fetcher=html_fetcher)

    assert report["html_checks_skipped"] is False
    assert report["all_expected_routes_ok"] is True
    assert all(route["contract_ok"] is True for route in report["html_routes"])


def test_public_site_probe_blocks_single_match_loading_shell() -> None:
    def fetcher(url: str, **_kwargs):
        if "/api/source-runs" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"runs": [{"id": "r1"}]}}
        if "/api/matches" in url:
            return {"status_code": 200, "content_type": "application/json", "payload": {"matches": [{"id": "m1"}]}}
        return {"status_code": 200, "content_type": "application/json", "payload": {"fixtures": 1}}

    def html_fetcher(url: str, **_kwargs):
        body = '<main class="detail-page"><span>Matchline</span><div>读取单场账本…</div></main>' if url.endswith("/matches/61") else "<main>Matchline 比赛数据台</main>"
        return {"status_code": 200, "content_type": "text/html", "body": body}

    report = probe_public_site("https://predict.example", fetcher=fetcher, html_fetcher=html_fetcher)

    assert report["all_expected_routes_ok"] is False
    match = next(route for route in report["html_routes"] if route["name"] == "single_match_html")
    assert match["contract_ok"] is False
    assert "forbidden_marker_present" in match["contract_errors"]


def test_public_site_probe_rejects_credentials_and_non_http_urls() -> None:
    with pytest.raises(ValueError, match="credentials"):
        probe_public_site("https://user:pass@predict.example")
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        probe_public_site("file:///tmp/site")
