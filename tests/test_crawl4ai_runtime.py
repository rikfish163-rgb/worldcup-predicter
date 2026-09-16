from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import league_platform.live_sources.crawl4ai as crawl4ai_module
import league_platform.crawl4ai_runtime as runtime
from league_platform.crawl4ai_runtime import inspect_crawl4ai_runtime
from league_platform.source_rights import UseCase, decide_source_rights


def _allow_test_only_rights(monkeypatch):
    """Make runtime diagnostics' explicitly test-only rows admissible.

    Candidate Crawl4AI IDs are central-BLOCKed in production.  The few
    dependency/registry tests that need a configured row opt into
    ``test_only`` and use this narrowly scoped central ALLOW seam.
    """

    real_require = crawl4ai_module.source_rights.require_source_rights
    allow = decide_source_rights("openfootball_current", UseCase.NETWORK_FETCH)

    def require(source_id, use_case, *args, **kwargs):
        config = kwargs.get("config")
        if isinstance(config, dict) and config.get("test_only") is True:
            return replace(allow, source_id=str(source_id or ""))
        return real_require(source_id, use_case, *args, **kwargs)

    monkeypatch.setattr(crawl4ai_module.source_rights, "require_source_rights", require)


def test_runtime_diagnostic_distinguishes_empty_configuration():
    report = inspect_crawl4ai_runtime(raw={"sources": []})

    assert report["configuration"]["status"] == "not_configured"
    assert report["configuration"]["configured_count"] == 0
    assert report["readiness"] == "not_configured"
    assert report["browser_started"] is False
    assert report["model_use"].startswith("display_only")
    assert report["security_policy"]["ignore_https_errors"] is False
    assert report["security_policy"]["robots_check"] == "required"
    assert report["security_policy"]["redirects"] == "same_host_and_allowlisted_path_only"
    assert report["security_policy"]["login_captcha_bypass"] is False


def test_operator_config_keeps_unknown_rights_sources_disabled():
    report = inspect_crawl4ai_runtime(config_path=Path("config/crawl4ai_operator_public.json"))
    assert report["configuration"]["status"] == "not_configured"
    assert report["configuration"]["configured_count"] == 0
    assert report["configuration"]["sources"] == []
    assert report["configuration"]["errors"] == []
    assert report["readiness"] == "not_configured"
    assert report["source_registry_audit"]["status"] == "ok"
    assert report["source_registry_audit"]["capture_engine"] == "Crawl4AI"
    assert report["source_registry_audit"]["fact_source"] is False
    assert report["source_registry_audit"]["execution_layer_count"] == 1
    assert report["source_registry_audit"]["fact_source_count"] == 0
    assert report["source_registry_audit"]["fanout_source_count"] == 0
    assert report["source_registry_audit"]["declared_source_count"] == 0
    assert report["source_registry_audit"]["unregistered_source_ids"] == []
    assert report["source_registry_audit"]["fetch_runtime_ids_used_as_fact_source"] == []
    audit = report["source_registry_audit"]
    assert audit["execution_layer_count"] == 1
    assert audit["registered_crawl4ai_fact_source_count"] >= audit["fact_source_count"]
    assert set(audit["configured_crawl4ai_fact_source_ids"]) <= set(
        audit["registered_crawl4ai_fact_source_ids"]
    )
    assert set(audit["blocked_crawl4ai_fact_source_ids"]) <= set(
        audit["registered_crawl4ai_fact_source_ids"]
    )
    assert audit["unconfigured_crawl4ai_fact_source_count"] == len(
        audit["unconfigured_crawl4ai_fact_source_ids"]
    )
    raw_sources = json.loads(
        Path("config/crawl4ai_operator_public.json").read_text(encoding="utf-8")
    )["sources"]
    assert all(row["enabled"] is False for row in raw_sources)
    assert all(row["license_status"] == "unknown" for row in raw_sources)
    source_ids = {row["source_id"] for row in raw_sources}
    assert source_ids == {
        "football_data_historical",
        "whoscored_public_pages",
        "premier_league_public_pages",
        "laliga_public_pages",
        "laliga_official_news",
        "bundesliga_public_pages",
        "seriea_public_pages",
        "ligue1_official_news",
    }
    parser_contracts = {row["source_id"]: row["parser"] for row in raw_sources}
    assert parser_contracts["whoscored_public_pages"] == "whoscored_public_fixtures_v1"
    assert parser_contracts["premier_league_public_pages"] == "premier_league_public_fixtures_v1"
    assert parser_contracts["laliga_public_pages"] == "laliga_public_sports_events_v1"
    assert parser_contracts["laliga_official_news"] == "official_news_jsonld_v1"
    assert parser_contracts["bundesliga_public_pages"] == "sports_event_jsonld_v1"
    assert parser_contracts["seriea_public_pages"] == "seriea_public_fixtures_v1"
    assert parser_contracts["ligue1_official_news"] == "official_news_cards_v1"
    premier = next(row for row in raw_sources if row["source_id"] == "premier_league_public_pages")
    assert premier["follow_links"] is True
    assert premier["max_follow_up_pages"] == 3
    assert premier["follow_record_fields"] == ["match_url"]
    assert premier["follow_parser"] == "sports_event_jsonld_v1"
    assert premier["follow_up_min_delay_seconds"] == 15
    laliga = next(row for row in raw_sources if row["source_id"] == "laliga_public_pages")
    assert laliga["follow_links"] is True
    assert laliga["max_follow_up_pages"] == 3
    assert laliga["follow_parser"] == "laliga_public_sports_events_v1"
    bundesliga = next(row for row in raw_sources if row["source_id"] == "bundesliga_public_pages")
    assert bundesliga["follow_links"] is True
    serie_a = next(row for row in raw_sources if row["source_id"] == "seriea_public_pages")
    assert serie_a["follow_links"] is True
    assert next(row["url"] for row in raw_sources if row["source_id"] == "bundesliga_public_pages").endswith("/en/bundesliga/matchday/2026-2027/1")
    assert report["model_use"].startswith("display_only")
    assert report["security_policy"]["access_control_bypass"] is False


def test_runtime_diagnostic_blocks_unknown_or_execution_layer_source_identity(monkeypatch):
    _allow_test_only_rights(monkeypatch)
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "Unknown public page",
                    "source_id": "unknown_public_page",
                    "url": "https://public.example/news/1",
                    "allowed_hosts": ["public.example"],
                    "allowed_path_prefixes": ["/news/"],
                        "license_status": "open",
                        "rights_reference": "https://public.example/open-data-license",
                        "test_only": True,
                },
                {
                    "name": "Crawl runtime identity",
                    "source_id": "crawl4ai_allowlisted_pages",
                    "url": "https://public.example/news/2",
                    "allowed_hosts": ["public.example"],
                    "allowed_path_prefixes": ["/news/"],
                        "license_status": "open",
                        "rights_reference": "https://public.example/open-data-license",
                        "test_only": True,
                },
            ]
        }
    )

    audit = report["source_registry_audit"]
    assert report["readiness"] == "blocked_source_registry"
    assert audit["status"] == "invalid"
    assert audit["execution_layer_count"] == 1
    assert audit["fact_source_count"] == 0
    assert audit["fanout_source_count"] == 2
    assert audit["unregistered_source_ids"] == ["unknown_public_page"]
    assert audit["fetch_runtime_ids_used_as_fact_source"] == ["crawl4ai_allowlisted_pages"]
    statuses = {row["source_id"]: row["source_identity_status"] for row in report["configuration"]["sources"]}
    assert statuses["unknown_public_page"] == "unregistered_source_id"
    assert statuses["crawl4ai_allowlisted_pages"] == "fetch_runtime_id_not_fact_source"


def test_runtime_diagnostic_reports_explicit_source_without_starting_browser(monkeypatch):
    _allow_test_only_rights(monkeypatch)
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "Allowed public page",
                    "source_id": "whoscored_public_pages",
                    "url": "https://public.example/news/1",
                    "allowed_hosts": ["public.example"],
                    "allowed_path_prefixes": ["/news/"],
                    "enabled": True,
                    "license_status": "open",
                    "rights_reference": "https://public.example/open-data-license",
                    "test_only": True,
                    "min_delay_seconds": 0,
                }
            ]
        }
    )

    assert report["configuration"]["status"] == "configured"
    assert report["configuration"]["configured_count"] == 1
    assert report["configuration"]["sources"][0]["host"] == "public.example"
    assert report["browser_started"] is False
    assert report["readiness"] in {
        "ready_for_explicit_crawl",
        "blocked_dependency_missing",
        "blocked_browser_missing",
        "blocked_tls_policy_unsupported",
    }


def test_runtime_diagnostic_rejects_denied_source_and_keeps_central_rights_error():
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "Denied page",
                    "url": "https://public.example/news/1",
                    "allowed_hosts": ["public.example"],
                    "allowed_path_prefixes": ["/news/"],
                    "license_status": "denied",
                }
            ]
        }
    )

    assert report["configuration"]["status"] == "rights_blocked"
    assert report["readiness"] == "blocked_rights"
    error = report["configuration"]["errors"][0]
    assert error["status"] == "rights_blocked"
    assert error["error_code"] == "source_rights_not_verified"
    assert error["rights"]["decision"] == "block"
    assert error["network_opened"] is False
    assert error["model_eligible"] is False
    assert report["browser_started"] is False


def test_runtime_diagnostic_preserves_unknown_rights_source_as_blocked():
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "Unknown-rights source",
                    "source_id": "whoscored_public_pages",
                    "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
                    "allowed_hosts": ["www.whoscored.com"],
                    "allowed_path_prefixes": ["/Regions/"],
                    "enabled": True,
                    "license_status": "unknown",
                }
            ]
        }
    )

    assert report["configuration"]["status"] == "rights_blocked"
    assert report["configuration"]["configured_count"] == 0
    assert report["configuration"]["rights_blocked_count"] == 1
    assert report["configuration"]["rights_blocked_sources"] == [
        {
            "source_id": "whoscored_public_pages",
            "name": "Unknown-rights source",
            "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
            "license_status": "unknown",
            "rights_reference": None,
            "network_opened": False,
        }
    ]
    assert report["readiness"] == "blocked_rights"
    assert report["browser_started"] is False


def test_runtime_diagnostic_self_attested_whoscored_stays_central_blocked():
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "WhoScored operator claim",
                    "source_id": "whoscored_public_pages",
                    "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
                    "allowed_hosts": ["www.whoscored.com"],
                    "allowed_path_prefixes": ["/Regions/"],
                    "enabled": True,
                    # Registry/operator declarations are deliberately
                    # self-attested; they cannot uplift the central BLOCK.
                    "license_status": "open",
                    "rights_reference": "https://example.invalid/operator-claim",
                }
            ]
        }
    )

    assert report["configuration"]["status"] == "rights_blocked"
    assert report["configuration"]["configured_count"] == 0
    assert report["configuration"]["rights_blocked_count"] == 1
    error = report["configuration"]["errors"][0]
    assert error["rights"]["source_id"] == "whoscored_public_pages"
    assert error["rights"]["decision"] == "block"
    assert error["network_opened"] is False
    assert error["model_eligible"] is False
    assert report["readiness"] == "blocked_rights"
    assert report["browser_started"] is False


def test_runtime_diagnostic_marks_approved_lane_partial_with_rights_blocks(monkeypatch):
    _allow_test_only_rights(monkeypatch)
    monkeypatch.setattr(
        runtime, "_dependency_status", lambda: {"status": "installed", "version": "0.9.2"}
    )
    monkeypatch.setattr(
        runtime,
        "_browser_status",
        lambda: {"status": "available", "mode": "test", "executable": "/test/chrome"},
    )
    monkeypatch.setattr(
        runtime,
        "strict_tls_runtime_status",
        lambda: {"status": "supported", "reason": "test"},
    )
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "Unknown-rights source",
                    "source_id": "whoscored_public_pages",
                    "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
                    "allowed_hosts": ["www.whoscored.com"],
                    "allowed_path_prefixes": ["/Regions/"],
                    "enabled": True,
                    "license_status": "unknown",
                },
                {
                    "name": "Approved source",
                    "url": "https://public.example/news/1",
                    "allowed_hosts": ["public.example"],
                    "allowed_path_prefixes": ["/news/"],
                    "enabled": True,
                    "license_status": "open",
                    "rights_reference": "https://public.example/open-data-license",
                    "test_only": True,
                },
            ]
        }
    )

    assert report["configuration"]["status"] == "configured_with_rights_blocks"
    assert report["configuration"]["configured_count"] == 1
    assert report["configuration"]["rights_blocked_count"] == 1
    assert report["readiness"] == "blocked_rights_partial"
    assert report["browser_started"] is False


def test_runtime_diagnostic_blocks_configured_source_without_package_or_browser(monkeypatch):
    _allow_test_only_rights(monkeypatch)
    monkeypatch.setattr(runtime, "_dependency_status", lambda: {"status": "installed", "version": "0.9.2"})
    monkeypatch.setattr(runtime, "_browser_status", lambda: {"status": "missing", "mode": None, "executable": None})
    monkeypatch.setattr(runtime, "strict_tls_runtime_status", lambda: {"status": "supported", "reason": "test"})
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "Allowed public page",
                    "source_id": "whoscored_public_pages",
                    "url": "https://public.example/news/1",
                    "allowed_hosts": ["public.example"],
                    "allowed_path_prefixes": ["/news/"],
                    "enabled": True,
                    "license_status": "open",
                    "rights_reference": "https://public.example/open-data-license",
                    "test_only": True,
                    "min_delay_seconds": 0,
                }
            ]
        }
    )

    assert report["readiness"] == "blocked_browser_missing"
    assert report["browser"]["status"] == "missing"
    assert report["browser_started"] is False


def test_runtime_loader_rejects_parser_contract_not_owned_by_source(monkeypatch):
    _allow_test_only_rights(monkeypatch)
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "WhoScored with LaLiga parser",
                    "source_id": "whoscored_public_pages",
                    "parser": "laliga_public_sports_events_v1",
                    "url": "https://public.example/news/1",
                    "allowed_hosts": ["public.example"],
                    "allowed_path_prefixes": ["/news/"],
                    "license_status": "open",
                    "rights_reference": "https://public.example/open-data-license",
                    "test_only": True,
                }
            ]
        }
    )

    assert report["configuration"]["status"] == "invalid_config"
    assert report["configuration"]["configured_count"] == 0
    assert report["configuration"]["errors"][0]["error_code"] == "parser_source_mismatch"
    assert report["readiness"] == "blocked_invalid_config"
    assert report["browser_started"] is False


def test_runtime_diagnostic_blocks_crawl4ai_build_with_certificate_bypass(monkeypatch):
    _allow_test_only_rights(monkeypatch)
    monkeypatch.setattr(runtime, "_dependency_status", lambda: {"status": "installed", "version": "0.9.2"})
    monkeypatch.setattr(runtime, "_browser_status", lambda: {"status": "available", "mode": "chrome_channel", "executable": "/usr/bin/google-chrome"})
    monkeypatch.setattr(
        runtime,
        "strict_tls_runtime_status",
        lambda: {
            "status": "unsupported",
            "reason": "certificate bypass flags",
            "unsafe_flags": ["--ignore-certificate-errors"],
        },
    )
    report = inspect_crawl4ai_runtime(
        raw={
            "sources": [
                {
                    "name": "Allowed public page",
                    "source_id": "whoscored_public_pages",
                    "url": "https://public.example/news/1",
                    "allowed_hosts": ["public.example"],
                    "allowed_path_prefixes": ["/news/"],
                    "enabled": True,
                    "license_status": "open",
                    "rights_reference": "https://public.example/open-data-license",
                    "test_only": True,
                    "min_delay_seconds": 0,
                }
            ]
        }
    )
    assert report["readiness"] == "blocked_tls_policy_unsupported"
    assert report["tls_runtime"]["unsafe_flags"] == ["--ignore-certificate-errors"]
    assert report["browser_started"] is False
