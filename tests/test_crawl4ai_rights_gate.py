from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from types import SimpleNamespace

import league_platform.live_sources.crawl4ai as crawl4ai
from league_platform.source_rights import (
    SourceRightsBlocked,
    UseCase,
    decide_source_rights,
)


NOW = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)


def _config(**overrides):
    values = {
        "name": "WhoScored operator page",
        "source_id": "whoscored_public_pages",
        "parser": "whoscored_public_fixtures_v1",
        "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
        "allowed_hosts": ("www.whoscored.com",),
        "allowed_path_prefixes": ("/Regions/",),
        "license_status": "open",
        "rights_reference": "https://example.invalid/operator-claim",
        "min_delay_seconds": 0,
    }
    values.update(overrides)
    return crawl4ai.Crawl4AIConfig(**values)


def test_central_rights_gate_runs_before_robots_or_browser(monkeypatch):
    events: list[str] = []
    blocked = decide_source_rights("whoscored_public_pages", UseCase.NETWORK_FETCH)

    def require(*args, **kwargs):
        events.append("rights")
        raise SourceRightsBlocked(blocked)

    monkeypatch.setattr(crawl4ai.source_rights, "require_source_rights", require)
    result = crawl4ai.fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: events.append("robots") or {"allowed": True},
        runner=lambda _config: events.append("browser") or SimpleNamespace(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert events == ["rights"]
    assert result["status"] == "rights_blocked"
    assert result["network_opened"] is False
    assert result["model_eligible"] is False
    assert result["errors"][0]["status"] == "rights_blocked"
    assert result["errors"][0]["error_code"] == "source_rights_not_verified"


def test_registry_self_report_cannot_uplift_central_block():
    calls: list[str] = []
    result = crawl4ai.fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True},
        runner=lambda _config: calls.append("browser") or SimpleNamespace(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "rights_blocked"
    assert result["rights_blocked_count"] == 1
    assert result["rights_blocked_sources"][0]["source_id"] == "whoscored_public_pages"
    assert result["rights_blocked_sources"][0]["license_status"] == "open"
    assert calls == []


def test_custom_runner_is_test_only_and_never_runtime_evidence(monkeypatch):
    calls: list[str] = []
    allowed = replace(
        decide_source_rights("openfootball_current", UseCase.NETWORK_FETCH),
        source_id="whoscored_public_pages",
    )
    # The test runner seam is exercised only with a central ALLOW decision;
    # a real WhoScored decision remains BLOCK in the preceding test.
    monkeypatch.setattr(crawl4ai.source_rights, "require_source_rights", lambda *args, **kwargs: allowed)
    fake_result = SimpleNamespace(
        success=True,
        html="<html><body>fixture</body></html>",
        markdown="fixture",
        url="https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
        redirected_url=None,
        status_code=200,
        response_headers={"content-type": "text/html; charset=utf-8"},
        metadata={},
        error_message=None,
    )
    result = crawl4ai.fetch_crawl4ai(
        now=NOW,
        configs=[_config(test_only=True)],
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True},
        runner=lambda _config: calls.append("browser") or fake_result,
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert calls == ["robots", "browser"]
    assert result["runner_mode"] == "test_only"
    assert result["runtime_evidence"] is False
    assert result["network_opened"] is False
    assert result["model_eligible"] is False
    assert result["pages"][0]["runtime_evidence"] is False
    assert result["pages"][0]["test_only"] is True


def test_parser_source_mismatch_is_structured_and_pre_network(monkeypatch):
    calls: list[str] = []
    allowed = replace(
        decide_source_rights("openfootball_current", UseCase.NETWORK_FETCH),
        source_id="whoscored_public_pages",
    )
    monkeypatch.setattr(crawl4ai.source_rights, "require_source_rights", lambda *args, **kwargs: allowed)
    result = crawl4ai.fetch_crawl4ai(
        now=NOW,
        configs=[
            _config(
                source_id="whoscored_public_pages",
                parser="sports_event_jsonld_v1",
                test_only=True,
            )
        ],
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True},
        runner=lambda _config: calls.append("browser") or SimpleNamespace(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert calls == []
    assert result["status"] == "unavailable"
    assert result["network_opened"] is False
    assert result["model_eligible"] is False
    assert result["errors"][0]["error_code"] == "parser_source_mismatch"


def test_auto_parser_cannot_route_a_registered_source_to_another_provider_host(monkeypatch):
    """A host-routed parser must not silently relabel another provider."""

    calls: list[str] = []
    allowed = replace(
        decide_source_rights("openfootball_current", UseCase.NETWORK_FETCH),
        source_id="whoscored_public_pages",
    )
    monkeypatch.setattr(
        crawl4ai.source_rights,
        "require_source_rights",
        lambda *args, **kwargs: allowed,
    )
    config = _config(
        # Keep the legacy test-only central-ALLOW seam, but point the declared
        # WhoScored lane at a host owned by the LaLiga parser contract.
        url="https://www.laliga.com/en-GB",
        allowed_hosts=("www.laliga.com",),
        allowed_path_prefixes=("/en-GB",),
        parser="auto",
        test_only=True,
    )
    result = crawl4ai.fetch_crawl4ai(
        now=NOW,
        configs=[config],
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True},
        runner=lambda _config: calls.append("browser") or SimpleNamespace(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert calls == []
    assert result["pages"] == []
    assert result["errors"][0]["error_code"] == "parser_source_mismatch"
    assert result["errors"][0]["network_opened"] is False


def test_known_non_crawl4ai_source_cannot_be_attached_to_browser_runner(monkeypatch):
    """A central ALLOW for another adapter is not a Crawl4AI identity grant."""

    calls: list[str] = []
    allowed = decide_source_rights("openfootball_current", UseCase.NETWORK_FETCH)
    monkeypatch.setattr(
        crawl4ai.source_rights,
        "require_source_rights",
        lambda *args, **kwargs: allowed,
    )
    config = _config(
        source_id="openfootball_current",
        parser="generic_public_page_v1",
        test_only=True,
    )
    result = crawl4ai.fetch_crawl4ai(
        now=NOW,
        configs=[config],
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True},
        runner=lambda _config: calls.append("browser") or SimpleNamespace(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert calls == []
    assert result["pages"] == []
    assert result["errors"][0]["error_code"] == "parser_source_mismatch"


def test_follow_up_detail_lane_receives_serial_typed_admission_before_worker(
    monkeypatch,
):
    """Detail workers must not re-run central rights concurrently with I/O."""

    root_url = "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League"
    detail_url = root_url + "/MatchReport"
    config = _config(
        url=root_url,
        follow_links=True,
        max_follow_up_pages=1,
        follow_record_fields=("match_url",),
        test_only=True,
    )
    allowed = replace(
        decide_source_rights("openfootball_current", UseCase.NETWORK_FETCH),
        source_id=config.source_id,
    )
    monkeypatch.setattr(
        crawl4ai.source_rights,
        "require_source_rights",
        lambda *args, **kwargs: allowed,
    )

    events: list[str] = []
    admission_calls: list[tuple[list[object], tuple[dict[int, object], dict[int, dict]]]] = []
    crawl_calls: list[tuple[list[object], dict[str, object]]] = []
    real_admission = crawl4ai._admission_preflight

    def admission_spy(configs, **kwargs):
        config_list = list(configs)
        events.append(f"admission:{config_list[0].url}")
        decisions, errors = real_admission(config_list, **kwargs)
        admission_calls.append((config_list, (decisions, errors)))
        return decisions, errors

    def crawl_spy(configs, **kwargs):
        config_list = list(configs)
        page_context = kwargs.get("page_context_by_config")
        stage = "detail" if page_context else "index"
        events.append(f"crawl:{stage}")
        crawl_calls.append((config_list, kwargs))
        if stage == "index":
            return {
                "provider": "Crawl4AI",
                "status": "ok",
                "pages": [
                    {
                        "source_id": config_list[0].source_id,
                        "url": root_url,
                        "final_url": root_url,
                        "parser_contract": config_list[0].parser,
                        "stale": False,
                        "raw_sha256": "a" * 64,
                        "extracted_records": [{"match_url": detail_url}],
                        "runtime_evidence": False,
                        "network_opened": False,
                        "model_eligible": False,
                        "enters_model": False,
                    }
                ],
                "errors": [],
                "page_count": 1,
                "fresh_page_count": 1,
                "stale_page_count": 0,
                "execution": {},
            }
        return {
            "provider": "Crawl4AI",
            "status": "ok",
            "pages": [],
            "errors": [],
        }

    monkeypatch.setattr(crawl4ai, "_admission_preflight", admission_spy)
    monkeypatch.setattr(crawl4ai, "_crawl_many", crawl_spy)

    result = crawl4ai.fetch_crawl4ai(
        now=NOW,
        configs=[config],
        runner=lambda _config: SimpleNamespace(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "ok"
    assert events == [
        f"admission:{root_url}",
        "crawl:index",
        f"admission:{detail_url}",
        "crawl:detail",
    ]
    assert len(admission_calls) == 2
    detail_configs, (detail_decisions, detail_errors) = admission_calls[1]
    assert len(detail_configs) == 1
    detail_config = detail_configs[0]
    assert detail_config.url == detail_url
    assert detail_errors == {}
    detail_decision = detail_decisions[id(detail_config)]
    assert isinstance(detail_decision, crawl4ai.source_rights.Decision)
    assert detail_decision.source_id == detail_config.source_id
    assert detail_decision.use_case == UseCase.NETWORK_FETCH.value

    assert len(crawl_calls) == 2
    detail_call_configs, detail_call_kwargs = crawl_calls[1]
    assert detail_call_configs == detail_configs
    assert detail_call_kwargs["admission_decisions"] == detail_decisions
    assert detail_call_kwargs["admission_errors"] == detail_errors
