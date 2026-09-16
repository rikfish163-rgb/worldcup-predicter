from __future__ import annotations

import asyncio
import json
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform.ingestion import snapshot_observations
from league_platform.live_sources.crawl4ai import (
    Crawl4AIConfig,
    Crawl4AIConfigError,
    fetch_crawl4ai,
    load_crawl4ai_configs,
)
import league_platform.live_sources.crawl4ai as crawl4ai_module
from league_platform.source_rights import UseCase, decide_source_rights
from league_platform.source_archive import summarize_snapshot


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


class FakeCrawlResult:
    success = True
    html = "<html><head><script type='application/ld+json'>{\"@type\":\"SportsEvent\",\"name\":\"A v B\",\"startDate\":\"2026-08-16T17:00:00Z\",\"homeTeam\":{\"name\":\"A\"},\"awayTeam\":{\"name\":\"B\"}}</script></head><body><h1>Match report</h1></body></html>"
    markdown = "# Match report"
    url = "https://public.example/news/1"
    redirected_url = None
    status_code = 200
    response_headers = {"content-type": "text/html; charset=utf-8"}
    metadata = {"datePublished": "2026-08-16T10:00:00+00:00"}
    error_message = None


def _config(**overrides) -> Crawl4AIConfig:
    values = {
        "name": "Public page",
        "url": "https://public.example/news/1",
        "allowed_hosts": ("public.example",),
        "allowed_path_prefixes": ("/news/",),
        "license_status": "open",
        "rights_reference": "https://public.example/open-data-license",
        "min_delay_seconds": 0,
        # Browser runners in this module are deterministic fixture seams.  A
        # test-only declaration is required by the production adapter and is
        # paired with the explicit central-policy ALLOW fixture below.
        "test_only": True,
    }
    values.update(overrides)
    return Crawl4AIConfig(**values)


@pytest.fixture(autouse=True)
def _allow_explicit_test_only_rights(monkeypatch):
    """Allow plumbing tests without weakening production source admission.

    The central v260 policy intentionally BLOCKs the Crawl4AI candidate IDs
    used by these parser/archival tests.  Every in-memory ``_config`` is
    marked ``test_only=True`` and this fixture returns an ALLOW decision only
    for that explicit seam.  Config-file tests omit the marker unless they
    are deliberately exercising a test-only lane, so their real BLOCK path
    remains covered.
    """

    real_require = crawl4ai_module.source_rights.require_source_rights
    allow = decide_source_rights("openfootball_current", UseCase.NETWORK_FETCH)

    def require(source_id, use_case, *args, **kwargs):
        config = kwargs.get("config")
        if isinstance(config, dict) and config.get("test_only") is True:
            return replace(allow, source_id=str(source_id or ""))
        return real_require(source_id, use_case, *args, **kwargs)

    monkeypatch.setattr(crawl4ai_module.source_rights, "require_source_rights", require)


def test_config_requires_https_and_explicit_path_boundary():
    try:
        Crawl4AIConfig(name="bad", url="http://public.example/news/1")
    except Crawl4AIConfigError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("HTTP source was accepted")

    try:
        _config(url="https://public.example/other/1")
    except Crawl4AIConfigError as exc:
        assert "path" in str(exc)
    else:
        raise AssertionError("out-of-scope path was accepted")

    try:
        _config(source_id="Not a registry id")
    except Crawl4AIConfigError as exc:
        assert "source_id" in str(exc)
    else:
        raise AssertionError("invalid fact-source identity was accepted")


def test_operator_config_gives_seriea_page_a_bounded_four_mebibyte_budget():
    config_path = Path(__file__).parents[1] / "config" / "crawl4ai_operator_public.json"
    rows = json.loads(config_path.read_text(encoding="utf-8"))["sources"]
    seriea = next(row for row in rows if row["source_id"] == "seriea_public_pages")
    # The last natural run isolated a 2,219,827-byte public page as too large
    # for the generic 2 MiB limit.  This is still far below the adapter's
    # 16 MiB hard ceiling and is a source-specific resource bound, not a
    # request to bypass robots, TLS, or access controls.
    assert seriea["max_bytes"] == 4 * 1024 * 1024


def test_environment_proxy_is_explicit_validated_and_scheme_aware(monkeypatch):
    keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("HTTPS_PROXY", "not-a-proxy")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    assert crawl4ai_module._environment_proxy() == "http://proxy.example:8080"

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:99999")
    monkeypatch.setenv("ALL_PROXY", "socks5h://proxy.example:1080")
    assert crawl4ai_module._environment_proxy(supported_schemes=("http", "https")) is None
    assert crawl4ai_module._environment_proxy() == "socks5h://proxy.example:1080"


def test_premier_league_contract_declares_a_bounded_match_card_wait():
    assert crawl4ai_module.PREMIER_LEAGUE_MATCH_CARD_SELECTOR == "css:[data-testid='matchCard']"


def test_football_data_archive_parser_indexes_only_bounded_same_host_csv_links():
    markup = """
    <a href="/mmz4281/2627/E0.csv">Premier League</a>
    <a href="https://www.football-data.co.uk/mmz4281/2526/E1.csv#fragment">Championship</a>
    <a href="https://other.example/mmz4281/2627/E0.csv">other host</a>
    <a href="/download/2627/E0.csv">wrong path</a>
    <a href="/mmz4281/2627/E0.csv">duplicate</a>
    """
    parser_name, records = crawl4ai_module._source_specific_records(
        source_name="Football-Data",
        final_url="https://www.football-data.co.uk/englandm.php",
        raw_html=markup,
        parser_contract="football_data_archive_v1",
    )
    assert parser_name == "football_data_archive_v1"
    assert len(records) == 2
    assert records[0]["data_url"] == "https://www.football-data.co.uk/mmz4281/2627/E0.csv"
    assert records[0]["season_code"] == "2627"
    assert records[0]["competition_code"] == "E0"
    assert records[0]["historical_only"] is True
    assert records[0]["enters_model"] is False
    assert "historical_archive" in records[0]["model_exclusion_reason"]


def test_operator_config_uses_historical_archive_parser_without_model_admission():
    config_path = Path(__file__).parents[1] / "config" / "crawl4ai_operator_public.json"
    rows = json.loads(config_path.read_text(encoding="utf-8"))["sources"]
    archive = next(row for row in rows if row["source_id"] == "football_data_historical")
    assert archive["parser"] == "football_data_archive_v1"
    assert archive["model_eligible"] is False


def test_fixture_card_browser_context_pins_london_timezone():
    assert (
        crawl4ai_module._browser_timezone_for_config(
            _config(parser="premier_league_public_fixtures_v1")
        )
        == "Europe/London"
    )
    assert (
        crawl4ai_module._browser_timezone_for_config(
            _config(parser="whoscored_public_fixtures_v1")
        )
        == "Europe/London"
    )
    assert crawl4ai_module._browser_timezone_for_config(_config(parser="laliga_public_sports_events_v1")) is None


def test_official_news_cards_keep_headline_provenance_and_display_boundary():
    markup = """
    <main>
      <article><a href="/news/injury-update"><h2>Captain returns to team training</h2>
        <p>Club statement with no inferred match or injury label.</p>
        <time datetime="2026-08-16T10:30:00Z">16 Aug</time></a></article>
      <article><a href="https://other.example/news/ignored"><h2>Other host</h2></a></article>
      <article><a href="/news/injury-update"><h2>Captain returns to team training</h2></a></article>
    </main>
    """
    parser = crawl4ai_module._OfficialNewsCardParser(base_url="https://public.example/news")
    parser.feed(markup)
    records = parser.output()
    assert len(records) == 1
    row = records[0]
    assert row["article_url"] == "https://public.example/news/injury-update"
    assert row["headline"] == "Captain returns to team training"
    assert row["published_at"] == "2026-08-16T10:30:00Z"
    assert row["enters_model"] is False
    assert "fixture_or_team_join" in row["model_exclusion_reason"]


def test_official_news_jsonld_flattens_nested_item_list_without_inventing_urls():
    markup = """
    <script type="application/ld+json">
    {
      "@type": "ItemList",
      "itemListElement": [
        {"@type": "ListItem", "position": 1, "item": {
          "@type": "NewsArticle",
          "headline": "Official squad update",
          "description": "Source-owned announcement.",
          "datePublished": "2026-08-16T10:30:00Z",
          "publisher": {"@type": "Organization", "name": "LALIGA"},
          "mainEntityOfPage": {"@type": "WebPage", "@id": "https://public.example"}
        }},
        {"@type": "ListItem", "position": 2, "item": {
          "@type": "NewsArticle",
          "headline": "Article with explicit URL",
          "datePublished": "2026-08-15T10:30:00Z",
          "url": "/en-GB/news/article-2"
        }}
      ]
    }
    </script>
    """
    records = crawl4ai_module._structured_news_article_records(
        markup,
        base_url="https://public.example/en-GB",
    )
    assert len(records) == 2
    assert records[0]["headline"] == "Official squad update"
    assert records[0]["article_url"] is None
    assert records[0]["source_article_id"].startswith("jsonld:")
    assert records[1]["article_url"] == "https://public.example/en-GB/news/article-2"
    assert records[0]["enters_model"] is False
    assert "fixture_or_team_join" in records[0]["model_exclusion_reason"]


def test_same_url_multi_parser_reuses_one_browser_response_and_keeps_source_identity():
    calls = {"runner": 0, "sleep": 0}

    class SharedPage(FakeCrawlResult):
        url = "https://www.laliga.com/en-GB"
        html = """
        <script type="application/ld+json">
        {"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":{
          "@type":"NewsArticle","headline":"Shared page article",
          "datePublished":"2026-08-16T10:30:00Z"}}]}
        </script>
        """

    def runner(_config):
        calls["runner"] += 1
        return SharedPage()

    result = fetch_crawl4ai(
        now=NOW,
        configs=[
            _config(
                source_id="laliga_public_pages",
                parser="laliga_public_sports_events_v1",
                url="https://www.laliga.com/en-GB",
                allowed_hosts=("www.laliga.com",),
                allowed_path_prefixes=("/en-GB",),
                min_delay_seconds=900,
            ),
            _config(
                source_id="laliga_official_news",
                parser="official_news_jsonld_v1",
                url="https://www.laliga.com/en-GB",
                allowed_hosts=("www.laliga.com",),
                allowed_path_prefixes=("/en-GB",),
                min_delay_seconds=900,
            ),
        ],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=runner,
        archive_dir=None,
        sleep_fn=lambda _seconds: calls.__setitem__("sleep", calls["sleep"] + 1),
    )
    assert calls["runner"] == 1
    assert calls["sleep"] == 0
    assert [page["source_id"] for page in result["pages"]] == [
        "laliga_public_pages",
        "laliga_official_news",
    ]
    assert result["pages"][1]["extraction"]["record_count"] == 1


def test_different_hosts_are_bounded_parallel_but_output_stays_deterministic(tmp_path: Path):
    barrier = threading.Barrier(2, timeout=2)
    entered: list[str] = []

    def runner(config):
        entered.append(config.source_id or "")
        barrier.wait()
        return SimpleNamespace(
            success=True,
            html="<html><body><h1>Public page</h1></body></html>",
            markdown="# Public page",
            url=config.url,
            redirected_url=None,
            status_code=200,
            response_headers={"content-type": "text/html; charset=utf-8"},
            metadata={},
            error_message=None,
        )

    configs = [
        _config(
            source_id="source_one",
            url="https://one.example/news/1",
            allowed_hosts=("one.example",),
        ),
        _config(
            source_id="source_two",
            url="https://two.example/news/1",
            allowed_hosts=("two.example",),
        ),
    ]
    result = fetch_crawl4ai(
        now=NOW,
        configs=configs,
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=runner,
        archive_dir=tmp_path / "crawl4ai",
        sleep_fn=lambda _seconds: None,
    )

    assert sorted(entered) == ["source_one", "source_two"]
    assert [page["source_id"] for page in result["pages"]] == ["source_one", "source_two"]
    assert result["execution"]["strategy"] == "bounded_cross_host_parallel_per_host_serial"
    assert result["execution"]["host_group_count"] == 2
    assert result["execution"]["max_host_workers"] == 2
    throttle_state = json.loads((tmp_path / "crawl4ai" / "host_throttle.json").read_text(encoding="utf-8"))
    assert sorted(throttle_state) == ["one.example", "two.example"]


def test_runner_deadline_is_bounded_even_when_vendor_call_never_returns():
    async def runner(_config):
        await asyncio.sleep(5)
        return FakeCrawlResult()

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(timeout_seconds=0.1)],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=runner,
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["pages"] == []
    assert result["status"] == "unavailable"
    assert result["errors"][0]["error_code"] == "timeout"
    assert "bounded deadline" in result["errors"][0]["error"]


def test_seriea_match_cards_keep_source_identity_without_canonical_fixture_promotion():
    markup = """
    <a aria-label="Inter - Monza" href="/serie-a/match/1cc7b922e8d44f93a52fb4bf8858e454/inter-vs-monza"></a>
    <a aria-label="Udinese - Como" href="/serie-a/match/50b79ed0dfd04af4804eb12f69123766/udinese-vs-como"></a>
    <a aria-label="External" href="https://other.example/serie-a/match/ffffffffffffffffffffffffffffffff/x"></a>
    """
    parser = crawl4ai_module._SerieAFixtureLinkParser(base_url="https://en.legaseriea.it/serie-a")
    parser.feed(markup)
    records = parser.output()
    assert len(records) == 2
    assert records[0]["source_match_id"] == "1cc7b922e8d44f93a52fb4bf8858e454"
    assert records[0]["home_team"] == "Inter"
    assert records[0]["away_team"] == "Monza"
    assert records[0]["enters_model"] is False


def test_seriea_match_cards_attach_only_source_declared_utc_kickoff():
    markup = r'''
    <a aria-label="Inter - Monza" href="/serie-a/match/1cc7b922e8d44f93a52fb4bf8858e454/inter-vs-monza"></a>
    <script>
    {\"matchId\":\"serie-a::Football_Match::1cc7b922e8d44f93a52fb4bf8858e454\",\"status\":\"UPCOMING\",\"matchDateUtc\":\"2026-08-22T16:30:00Z\",\"isUnknownKickOffTime\":false}
    </script>
    '''
    parser_name, records = crawl4ai_module._source_specific_records(
        source_name="seriea_public_pages",
        final_url="https://en.legaseriea.it/serie-a",
        raw_html=markup,
        parser_contract="seriea_public_fixtures_v1",
    )
    assert parser_name == "seriea_public_fixtures_v1"
    assert len(records) == 1
    assert records[0]["kickoff_at"] == "2026-08-22T16:30:00+00:00"
    assert records[0]["time_semantics"] == "source_declared_matchDateUtc"
    assert records[0]["source_timezone"] == "UTC"
    assert records[0]["enters_model"] is False


def test_seriea_unknown_kickoff_is_not_guessed():
    markup = r'''
    <a aria-label="Inter - Monza" href="/serie-a/match/1cc7b922e8d44f93a52fb4bf8858e454/inter-vs-monza"></a>
    <script>
    {\"matchId\":\"serie-a::Football_Match::1cc7b922e8d44f93a52fb4bf8858e454\",\"matchDateUtc\":\"2026-08-22T16:30:00Z\",\"isUnknownKickOffTime\":true}
    </script>
    '''
    _parser_name, records = crawl4ai_module._source_specific_records(
        source_name="seriea_public_pages",
        final_url="https://en.legaseriea.it/serie-a",
        raw_html=markup,
        parser_contract="seriea_public_fixtures_v1",
    )
    assert len(records) == 1
    assert "kickoff_at" not in records[0]
    assert records[0]["time_semantics"] == "source_match_card_kickoff_unavailable"


def test_declared_fact_source_identity_is_separate_from_crawl_engine(tmp_path: Path):
    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(source_id="whoscored_public_pages")],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: FakeCrawlResult(),
        archive_dir=tmp_path / "crawl4ai",
        sleep_fn=lambda _seconds: None,
    )

    page = result["pages"][0]
    assert page["source_id"] == "whoscored_public_pages"
    assert page["fact_source_id"] == "whoscored_public_pages"
    assert page["capture_engine"] == "Crawl4AI"
    assert page["capture_role"] == "fetch_runtime"
    assert page["enters_model"] is False
    manifest = json.loads((tmp_path / "crawl4ai" / "pages.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert manifest["fact_source_id"] == "whoscored_public_pages"
    assert manifest["capture_engine"] == "Crawl4AI"

    rows = snapshot_observations({"as_of": NOW.isoformat(), "espn": {"fixtures": []}, "crawl4ai": result})
    assert rows[0].payload["fact_source_id"] == "whoscored_public_pages"
    assert rows[0].payload["capture_role"] == "fetch_runtime"
    assert rows[0].enters_model is False


def test_explicit_parser_contract_can_project_a_source_on_an_unrelated_host():
    class GenericHostSportsEvent(FakeCrawlResult):
        url = "https://public.example/news/1"

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(parser="sports_event_jsonld_v1", source_id="public_fixture_source")],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: GenericHostSportsEvent(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    page = result["pages"][0]
    assert page["parser_contract"] == "sports_event_jsonld_v1"
    assert page["extraction"]["parser"] == "sports_event_jsonld_v1"
    assert page["extraction"]["record_count"] == 1
    assert page["extracted_records"][0]["home_team"] == "A"
    assert page["enters_model"] is False


def test_source_declared_follow_up_captures_only_allowlisted_match_pages(tmp_path: Path):
    root_url = "https://public.example/news/1"
    detail_url = "https://public.example/news/1/match-42"
    calls: list[str] = []
    sleeps: list[float] = []

    root_html = f"""
    <html><head><script type="application/ld+json">
    {{"@type":"SportsEvent","url":"{detail_url}","startDate":"2026-08-16T17:00:00Z",
      "homeTeam":{{"name":"A"}},"awayTeam":{{"name":"B"}}}}
    </script></head><body></body></html>
    """
    detail_html = """
    <html><head><script type="application/ld+json">
    {"@type":"SportsEvent","url":"https://public.example/news/1/match-42",
      "startDate":"2026-08-16T17:00:00Z","homeTeam":{"name":"A"},"awayTeam":{"name":"B"},
      "eventStatus":"EventScheduled"}
    </script></head><body><p>Official match detail</p></body></html>
    """

    def runner(config):
        calls.append(config.url)
        return SimpleNamespace(
            success=True,
            html=root_html if config.url == root_url else detail_html,
            markdown="# Match",
            url=config.url,
            redirected_url=None,
            status_code=200,
            response_headers={"content-type": "text/html; charset=utf-8"},
            metadata={},
            error_message=None,
        )

    result = fetch_crawl4ai(
        now=NOW,
        configs=[
            _config(
                source_id="public_fixture_source",
                parser="sports_event_jsonld_v1",
                url=root_url,
                follow_links=True,
                max_follow_up_pages=2,
                follow_record_fields=("match_url",),
                follow_up_min_delay_seconds=5,
            )
        ],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=runner,
        archive_dir=tmp_path / "crawl4ai",
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert calls == [root_url, detail_url]
    assert sleeps and sleeps[0] == 5
    assert result["status"] == "ok"
    assert result["execution"]["follow_up_candidate_count"] == 1
    assert result["execution"]["follow_up_requested_count"] == 1
    assert result["execution"]["detail_page_count"] == 1
    assert [page["crawl_stage"] for page in result["pages"]] == ["index", "detail"]
    assert all(page["extracted_record_count"] == len(page["extracted_records"]) for page in result["pages"])
    detail = result["pages"][1]
    assert detail["url"] == detail_url
    assert detail["parent_url"] == root_url
    assert len(detail["parent_content_sha256"]) == 64
    assert detail["follow_reason"] == "match_url"
    assert detail["enters_model"] is False
    manifest_rows = [
        json.loads(line)
        for line in (tmp_path / "crawl4ai" / "pages.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["crawl_stage"] for row in manifest_rows} == {"index", "detail"}


def test_follow_up_rejects_external_and_unlisted_paths_without_crawling_them():
    class UnsafeSportsEvent(FakeCrawlResult):
        html = """
        <html><head><script type="application/ld+json">
        {"@type":"SportsEvent","url":"https://outside.example/match/1?token=secret","startDate":"2026-08-16T17:00:00Z",
          "homeTeam":{"name":"A"},"awayTeam":{"name":"B"}}
        </script></head></html>
        """

    calls: list[str] = []
    result = fetch_crawl4ai(
        now=NOW,
        configs=[
            _config(
                source_id="public_fixture_source",
                parser="sports_event_jsonld_v1",
                follow_links=True,
                max_follow_up_pages=2,
                follow_record_fields=("match_url",),
                follow_up_min_delay_seconds=0,
            )
        ],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda config: calls.append(config.url) or UnsafeSportsEvent(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert len(calls) == 1
    assert result["execution"]["follow_up_candidate_count"] == 1
    assert result["execution"]["follow_up_requested_count"] == 0
    assert result["execution"]["follow_up_rejected_count"] == 1
    assert result["execution"]["detail_page_count"] == 0
    assert result["execution"]["follow_up_rejections"][0]["enters_model"] is False
    assert "secret" not in result["execution"]["follow_up_rejections"][0]["value"]


def test_follow_up_configuration_is_explicitly_opt_in_and_bounded():
    try:
        _config(max_follow_up_pages=1)
    except Crawl4AIConfigError as exc:
        assert "follow_links" in str(exc)
    else:
        raise AssertionError("follow-up pages must not activate implicitly")

    try:
        _config(follow_links=True, max_follow_up_pages=21)
    except Crawl4AIConfigError as exc:
        assert "max_follow_up_pages" in str(exc)
    else:
        raise AssertionError("follow-up page cap exceeded the hard bound")

    try:
        _config(follow_links=True, max_follow_up_pages=1, follow_record_fields=("href",))
    except Crawl4AIConfigError as exc:
        assert "follow_record_fields" in str(exc)
    else:
        raise AssertionError("arbitrary DOM link fields must not be accepted")

    try:
        _config(follow_links=True, max_follow_up_pages=1, follow_up_min_delay_seconds=True)
    except Crawl4AIConfigError as exc:
        assert "follow_up_min_delay_seconds" in str(exc)
    else:
        raise AssertionError("boolean follow-up delay must not be coerced into a rate")


def test_unsupported_parser_contract_is_rejected_before_crawl():
    try:
        _config(parser="invented_parser_v9")
    except Crawl4AIConfigError as exc:
        assert "parser" in str(exc)
    else:
        raise AssertionError("unsupported parser contract was accepted")


def test_laliga_jsonld_sports_events_are_bounded_display_only_records():
    class LaLigaJsonLd(FakeCrawlResult):
        url = "https://www.laliga.com/en-GB"
        html = """
        <html><head><script type="application/ld+json">
        {"@type":"SportsEvent","@id":"https://www.laliga.com/partido/1",
         "url":"https://www.laliga.com/partido/1","startDate":"2026-08-19T19:00:00+00:00",
         "homeTeam":{"name":"Atlético de Madrid"},"awayTeam":{"name":"Málaga CF"},
         "location":{"name":"Riyadh Air Metropolitano"},"eventStatus":"EventScheduled"}
        </script></head><body></body></html>
        """

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(
            name="LaLiga official public match pages operator source",
            source_id="laliga_public_pages",
            url="https://www.laliga.com/en-GB",
            allowed_hosts=("www.laliga.com",),
            allowed_path_prefixes=("/en-GB",),
        )],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: LaLigaJsonLd(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    page = result["pages"][0]
    assert page["extraction"]["parser"] == "laliga_public_sports_events_v1"
    assert page["extraction"]["record_count"] == 1
    record = page["extracted_records"][0]
    assert record["source_event_id"].endswith("/1")
    assert record["home_team"] == "Atlético de Madrid"
    assert record["away_team"] == "Málaga CF"
    assert record["venue"] == "Riyadh Air Metropolitano"
    assert record["enters_model"] is False


def test_bundesliga_jsonld_source_keeps_declared_identity_and_display_boundary():
    class BundesligaJsonLd(FakeCrawlResult):
        url = "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1"
        html = """
        <html><head><script type="application/ld+json">
        {"@type":"SportsEvent","@id":"https://www.bundesliga.com/en/bundesliga/matchday/1",
         "url":"https://www.bundesliga.com/en/bundesliga/matchday/1",
         "startDate":"2026-08-22T18:30:00+0000",
         "homeTeam":{"name":"Borussia Dortmund"},
         "awayTeam":{"name":"FC Bayern München"},
         "location":{"name":"SIGNAL IDUNA PARK"},
         "eventStatus":"https://schema.org/EventScheduled"}
        </script></head><body></body></html>
        """

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(
            name="Bundesliga official public match pages operator source",
            source_id="bundesliga_public_pages",
            parser="sports_event_jsonld_v1",
            url="https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1",
            allowed_hosts=("www.bundesliga.com",),
            allowed_path_prefixes=("/en/bundesliga/",),
        )],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: BundesligaJsonLd(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    page = result["pages"][0]
    assert page["source_id"] == "bundesliga_public_pages"
    assert page["fact_source_id"] == "bundesliga_public_pages"
    assert page["capture_engine"] == "Crawl4AI"
    assert page["parser_contract"] == "sports_event_jsonld_v1"
    assert page["extraction"]["record_count"] == 1
    assert page["extracted_records"][0]["home_team"] == "Borussia Dortmund"
    assert page["extracted_records"][0]["away_team"] == "FC Bayern München"
    assert page["enters_model"] is False


def test_bundesliga_round_page_extracts_multiple_events_without_collapsing_source_rows():
    events = [
        {
            "@type": "SportsEvent",
            "@id": f"https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1/{index}",
            "startDate": f"2026-08-{28 + index:02d}T13:30:00+0000",
            "homeTeam": {"name": home},
            "awayTeam": {"name": away},
        }
        for index, (home, away) in enumerate(
            [("FC Bayern München", "VfB Stuttgart"), ("RB Leipzig", "Hamburger SV")]
        )
    ]
    markup = "".join(
        f'<script type="application/ld+json">{json.dumps(event, ensure_ascii=False)}</script>'
        for event in events
    )

    class BundesligaRoundJsonLd(FakeCrawlResult):
        url = "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1"
        html = f"<html><head>{markup}</head><body></body></html>"

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(
            name="Bundesliga official public match pages operator source",
            source_id="bundesliga_public_pages",
            parser="sports_event_jsonld_v1",
            url="https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1",
            allowed_hosts=("www.bundesliga.com",),
            allowed_path_prefixes=("/en/bundesliga/",),
        )],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: BundesligaRoundJsonLd(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    page = result["pages"][0]
    assert page["extraction"]["record_count"] == 2
    assert [row["home_team"] for row in page["extracted_records"]] == ["FC Bayern München", "RB Leipzig"]
    assert all(row["enters_model"] is False for row in page["extracted_records"])


def test_premier_league_match_cards_are_bounded_display_only_records():
    class PremierLeagueCards(FakeCrawlResult):
        url = "https://www.premierleague.com/en/matches"
        html = """
        <div data-testid="dayDate">Sat 22 Aug</div>
        <a data-testid="matchCard" href="/en/match/2645195/arsenal-vs-coventry-city/overview">
          <span data-testid="matchCardTeamFullName">Arsenal</span>
          <span data-testid="matchCardKickoffTime">03:00</span>
          <span data-testid="matchCardTeamFullName">Coventry City</span>
        </a>
        """

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(
            name="Premier League official public match pages operator source",
            source_id="premier_league_public_pages",
            url="https://www.premierleague.com/en/matches",
            allowed_hosts=("www.premierleague.com",),
            allowed_path_prefixes=("/en/",),
        )],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: PremierLeagueCards(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    page = result["pages"][0]
    assert page["extraction"]["parser"] == "premier_league_public_fixtures_v1"
    assert page["extraction"]["record_count"] == 1
    record = page["extracted_records"][0]
    assert record["source_match_id"] == "2645195"
    assert record["date_label"] == "Sat 22 Aug"
    assert record["home_team"] == "Arsenal"
    assert record["away_team"] == "Coventry City"
    assert record["time_semantics"] == "source_display_label_in_browser_context_timezone"
    assert record["display_timezone"] == "Europe/London"
    assert record["enters_model"] is False


def test_robots_denial_is_fail_closed_and_does_not_start_browser():
    called = False

    def runner(_config):
        nonlocal called
        called = True
        return FakeCrawlResult()

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": False, "status": "disallowed"},
        runner=runner,
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "unavailable"
    assert result["pages"] == []
    assert result["errors"][0]["stage"] == "robots"
    assert result["errors"][0]["error_code"] == "robots_denied"
    assert called is False


def test_crawl_result_records_strict_browser_security_policy():
    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": False, "status": "disallowed"},
        runner=lambda _config: FakeCrawlResult(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert result["security_policy"] == {
        "ignore_https_errors": False,
        "robots_check": "required",
        "redirects": "same_host_and_allowlisted_path_only",
        "login_captcha_bypass": False,
        "access_control_bypass": False,
    }


def test_external_redirect_is_quarantined():
    class Redirected(FakeCrawlResult):
        redirected_url = "https://outside.example/news/1"

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: Redirected(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["pages"] == []
    assert result["errors"][0]["stage"] == "redirect"
    assert result["status"] == "unavailable"


def test_same_host_lowercase_path_redirect_is_allowed_when_explicitly_allowlisted():
    class CanonicalCaseRedirect(FakeCrawlResult):
        redirected_url = "https://public.example/news/1"

    config = _config(allowed_path_prefixes=("/News/", "/news/"))
    result = fetch_crawl4ai(
        now=NOW,
        configs=[config],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: CanonicalCaseRedirect(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "ok"
    assert result["pages"][0]["final_url"] == "https://public.example/news/1"


def test_oversized_result_is_quarantined():
    class Oversized(FakeCrawlResult):
        html = "x" * 101

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(max_bytes=100)],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: Oversized(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["pages"] == []
    assert result["errors"][0]["stage"] == "size"


def test_json_response_is_parsed_as_bounded_data_not_html():
    class JsonResult(FakeCrawlResult):
        html = '{"events":[{"id":"1","status":"scheduled"}]}'
        response_headers = {"content-type": "application/json"}

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: JsonResult(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    page = result["pages"][0]
    assert page["raw_kind"] == "json"
    assert page["structured_json"]["events"][0]["status"] == "scheduled"
    assert page["structured_facts"] == []


def test_json_content_type_mismatch_is_quarantined():
    class InvalidJsonResult(FakeCrawlResult):
        html = "<html>not-json</html>"
        response_headers = {"content-type": "application/json"}

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: InvalidJsonResult(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["pages"] == []
    assert result["errors"][0]["stage"] == "content_type"
    assert result["errors"][0]["error_code"] == "content_type_mismatch"


def test_html_content_type_with_json_body_is_quarantined():
    class HtmlJsonMismatch(FakeCrawlResult):
        html = '{"error":"temporarily unavailable"}'
        response_headers = {"content-type": "text/html; charset=utf-8"}

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: HtmlJsonMismatch(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["pages"] == []
    assert result["errors"][0]["stage"] == "content_type"
    assert result["errors"][0]["error_code"] == "content_type_mismatch"
    assert "declared HTML" in result["errors"][0]["error"]


def test_http_403_result_is_recorded_as_source_failure():
    class Forbidden(FakeCrawlResult):
        success = False
        status_code = 403
        error_message = "HTTP 403 from public source"

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: Forbidden(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["pages"] == []
    assert result["status"] == "unavailable"
    assert result["errors"][0]["stage"] == "crawl"
    assert result["errors"][0]["status_code"] == 403
    assert result["errors"][0]["error_code"] == "http_403"


def test_tls_or_network_exception_is_isolated_without_greenwashing():
    def runner(_config):
        raise TimeoutError("TLS handshake timed out")

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=runner,
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["pages"] == []
    assert result["status"] == "unavailable"
    assert result["errors"][0]["stage"] == "crawl"
    assert result["errors"][0]["error_code"] == "tls_error"
    assert "TLS handshake timed out" in result["errors"][0]["error"]


def test_default_browser_does_not_ignore_tls_errors(monkeypatch):
    captured: dict[str, object] = {}
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(
        crawl4ai_module,
        "strict_tls_runtime_status",
        lambda: {"status": "supported", "reason": "test", "unsafe_flags": []},
    )

    class FakeBrowserConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeRunConfig:
        def __init__(self, **_kwargs):
            pass

    class FakeCacheMode:
        BYPASS = "bypass"

    class FakeCrawler:
        def __init__(self, *, config):
            assert config is not None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def arun(self, _url, *, config):
            assert config is not None
            return FakeCrawlResult()

    monkeypatch.setitem(
        sys.modules,
        "crawl4ai",
        SimpleNamespace(
            AsyncWebCrawler=FakeCrawler,
            BrowserConfig=FakeBrowserConfig,
            CacheMode=FakeCacheMode,
            CrawlerRunConfig=FakeRunConfig,
        ),
    )
    class FakeManagedBrowser:
        @staticmethod
        def build_browser_flags(_config):
            return ["--ignore-certificate-errors", "--safe-flag"]

    class FakeBrowserManager:
        def __init__(self, _config):
            pass

        def _build_browser_args(self):
            return {"args": ["--ignore-certificate-errors-spki-list", "--safe-flag"]}

    monkeypatch.setitem(
        sys.modules,
        "crawl4ai.browser_manager",
        SimpleNamespace(BrowserManager=FakeBrowserManager, ManagedBrowser=FakeManagedBrowser),
    )
    monkeypatch.setattr(crawl4ai_module.shutil, "which", lambda _name: None)

    result = crawl4ai_module._run_async(crawl4ai_module._default_crawler_runner(_config()))

    assert result.success is True
    assert captured["ignore_https_errors"] is False
    assert captured["use_persistent_context"] is False
    assert captured["proxy_config"] == {"server": "http://127.0.0.1:7890"}


def test_default_browser_fails_closed_when_runtime_emits_tls_bypass_flags(monkeypatch):
    monkeypatch.setattr(
        crawl4ai_module,
        "strict_tls_runtime_status",
        lambda: {
            "status": "unsupported",
            "reason": "certificate bypass flags",
            "unsafe_flags": ["--ignore-certificate-errors"],
        },
    )
    try:
        crawl4ai_module._run_async(crawl4ai_module._default_crawler_runner(_config()))
    except RuntimeError as exc:
        assert "strict TLS runtime unavailable" in str(exc)
    else:
        raise AssertionError("unsafe Crawl4AI runtime must not start a browser")


def test_strict_tls_compat_patch_filters_vendor_flags_and_restores_methods(monkeypatch):
    class FakeManagedBrowser:
        @staticmethod
        def build_browser_flags(_config):
            return ["--ignore-certificate-errors", "--safe-managed"]

    class FakeBrowserManager:
        def __init__(self, _config):
            pass

        def _build_browser_args(self):
            return {"args": ["--ignore-certificate-errors-spki-list", "--safe-native"]}

    monkeypatch.setitem(
        sys.modules,
        "crawl4ai.browser_manager",
        SimpleNamespace(BrowserManager=FakeBrowserManager, ManagedBrowser=FakeManagedBrowser),
    )
    original_managed = FakeManagedBrowser.__dict__["build_browser_flags"]
    original_manager = FakeBrowserManager.__dict__["_build_browser_args"]

    with crawl4ai_module._strict_tls_compat_patch():
        assert FakeManagedBrowser.build_browser_flags(None) == ["--safe-managed"]
        assert FakeBrowserManager(None)._build_browser_args() == {"args": ["--safe-native"]}

    assert FakeManagedBrowser.__dict__["build_browser_flags"] is original_managed
    assert FakeBrowserManager.__dict__["_build_browser_args"] is original_manager


def test_fetch_blocks_before_robots_when_default_runtime_tls_is_unverified(monkeypatch):
    monkeypatch.setattr(
        crawl4ai_module,
        "strict_tls_runtime_status",
        lambda: {
            "status": "unsupported",
            "reason": "certificate bypass flags",
            "unsafe_flags": ["--ignore-certificate-errors"],
        },
    )
    calls: list[str] = []
    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True, "status": "allowed"},
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "blocked_tls_policy"
    assert result["pages"] == []
    assert result["tls_runtime"]["status"] == "unsupported"
    assert result["errors"][0]["error_code"] == "tls_policy_unsupported"
    assert calls == []


def test_success_is_hashed_archived_and_display_only(tmp_path: Path):
    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: FakeCrawlResult(),
        archive_dir=tmp_path / "crawl4ai",
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "ok"
    page = result["pages"][0]
    assert page["enters_model"] is False
    assert page["model_eligible"] is False
    assert page["structured_facts_count"] == 1
    assert page["structured_facts"][0]["@type"] == "SportsEvent"
    assert page["metadata"]["structured_facts"][0]["name"] == "A v B"
    assert page["effective_at"] == "2026-08-16T10:00:00+00:00"
    assert len(page["raw_sha256"]) == 64
    assert (tmp_path / "crawl4ai" / page["raw_path"]).is_file()
    manifest = tmp_path / "crawl4ai" / "pages.jsonl"
    row = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    assert row["content_sha256"] == page["raw_sha256"]
    assert row["enters_model"] is False


def test_whoscored_fixture_cards_are_bounded_display_evidence_only():
    class WhoScoredResult(FakeCrawlResult):
        url = "https://www.whoscored.com/regions/252/tournaments/2/england-premier-league"
        html = """
        <div class="Accordion-module_header__abc"><span>Friday, Aug 21 2026</span></div>
        <div class="Match-module_match__abc">
          <div><span class="Match-module_startTime__abc">20:00</span></div>
          <a id="scoresBtn-1983546" href="/matches/1983546/show/england-premier-league-2026-2027-arsenal-coventry"><span>-</span><span>-</span></a>
          <a class="Match-module_teamNameText__abc" href="/teams/13/show/england-arsenal">Arsenal</a>
          <a class="Match-module_teamNameText__abc" href="/teams/17/show/england-coventry">Coventry</a>
          <span class="OddsButton-module_oddsText__abc">1.17</span>
          <span class="OddsButton-module_oddsText__abc">7.50</span>
          <span class="OddsButton-module_oddsText__abc">15.00</span>
        </div>
        """

    result = fetch_crawl4ai(
        now=NOW,
        configs=[
            _config(
                name="WhoScored public match centre",
                url="https://www.whoscored.com/regions/252/tournaments/2/england-premier-league",
                allowed_hosts=("www.whoscored.com",),
                allowed_path_prefixes=("/regions/",),
            )
        ],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: WhoScoredResult(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    page = result["pages"][0]
    assert page["extraction"]["parser"] == "whoscored_public_fixtures_v1"
    assert page["extraction"]["record_count"] == 1
    record = page["extracted_records"][0]
    assert record["source_match_id"] == "1983546"
    assert record["home_team"] == "Arsenal"
    assert record["away_team"] == "Coventry"
    assert record["date_label"] == "Friday, Aug 21 2026"
    assert record["one_x_two_odds"] == {"home": 1.17, "draw": 7.5, "away": 15.0}
    assert record["enters_model"] is False
    assert "fixture_join" in record["model_exclusion_reason"]


def test_string_compatible_markdown_is_plain_text_before_ledger(tmp_path: Path):
    class MarkdownGenerationResult:
        pass

    class StringCompatibleMarkdown(str):
        def __deepcopy__(self, _memo):
            # Mirrors Crawl4AI's 0.9.x deepcopy behavior that exposed the
            # wrapper object to dataclasses.asdict().
            return MarkdownGenerationResult()

    class CrawlResultWithWrappedMarkdown(FakeCrawlResult):
        markdown = StringCompatibleMarkdown("# Match report")

    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config()],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: CrawlResultWithWrappedMarkdown(),
        archive_dir=tmp_path / "crawl4ai",
        sleep_fn=lambda _seconds: None,
    )
    page = result["pages"][0]
    assert type(page["markdown"]) is str
    rows = snapshot_observations({"as_of": NOW.isoformat(), "espn": {"fixtures": []}, "crawl4ai": result})
    assert len(rows) == 1
    json.dumps(rows[0].as_dict())


def test_first_request_is_not_delayed_but_repeated_same_host_requests_are_rate_limited():
    first = _config(name="first", min_delay_seconds=30)
    second = _config(name="second", url="https://public.example/news/2", min_delay_seconds=30)
    sleeps: list[float] = []
    clock_calls: list[None] = []

    def monotonic() -> float:
        clock_calls.append(None)
        return 100.0 if len(clock_calls) == 1 else 101.0

    import league_platform.live_sources.crawl4ai as crawl4ai

    original_monotonic = crawl4ai.time.monotonic
    crawl4ai.time.monotonic = monotonic
    try:
        result = fetch_crawl4ai(
            now=NOW,
            configs=[first, second],
            robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
            runner=lambda _config: FakeCrawlResult(),
            archive_dir=None,
            sleep_fn=sleeps.append,
        )
    finally:
        crawl4ai.time.monotonic = original_monotonic

    assert result["status"] == "ok"
    assert sleeps == [29.0]


def test_persistent_host_throttle_defers_a_new_process_without_starting_browser(tmp_path: Path):
    archive = tmp_path / "crawl4ai"
    calls: list[str] = []
    config = _config(min_delay_seconds=60)

    first = fetch_crawl4ai(
        now=NOW,
        configs=[config],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: calls.append("first") or FakeCrawlResult(),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )
    assert first["status"] == "ok"
    assert calls == ["first"]

    second = fetch_crawl4ai(
        now=NOW.replace(minute=0, second=30),
        configs=[config],
        robots_checker=lambda *_: (_ for _ in ()).throw(AssertionError("robots must not be called before due time")),
        runner=lambda _config: calls.append("second") or FakeCrawlResult(),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )
    assert second["status"] == "not_due"
    assert len(second["pages"]) == 1
    assert second["fresh_page_count"] == 0
    assert second["stale_page_count"] == 1
    assert second["pages"][0]["stale"] is True
    assert second["pages"][0]["source_state"] == "stale"
    assert second["pages"][0]["stale_reason"] == "host_throttled"
    assert second["pages"][0]["capture_engine"] == "Crawl4AI"
    assert second["pages"][0]["source_role"] == "fact_source_capture"
    assert second["pages"][0]["observed_at"] == NOW.isoformat()
    assert second["pages"][0]["enters_model"] is False
    assert second["errors"][0]["error_code"] == "rate_limited_wait"
    assert calls == ["first"]


def test_throttled_poll_reprojects_generic_archive_page_after_parser_upgrade(tmp_path: Path):
    archive = tmp_path / "crawl4ai"
    # An unregistered fixture identity is intentional here: the test covers
    # archive parser reprojection, while the production policy keeps the real
    # ``football_data_historical`` lane central-BLOCKed until rights are
    # granted.  The module fixture supplies an explicit test-only ALLOW.
    source_id = "archive_test_source"
    old_config = _config(
        source_id=source_id,
        parser="generic_public_page_v1",
        min_delay_seconds=60,
    )
    html = "<a href='/mmz4281/2627/E0.csv'>Premier League</a>"
    first = fetch_crawl4ai(
        now=NOW,
        configs=[old_config],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: SimpleNamespace(
            success=True,
            html=html,
            markdown="# archive",
            url=old_config.url,
            redirected_url=None,
            status_code=200,
            response_headers={"content-type": "text/html; charset=utf-8"},
            metadata={},
            error_message=None,
        ),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )
    assert first["pages"][0]["parser_contract"] == "generic_public_page_v1"

    upgraded = _config(
        source_id=source_id,
        parser="football_data_archive_v1",
        min_delay_seconds=60,
    )
    second = fetch_crawl4ai(
        now=NOW.replace(minute=0, second=30),
        configs=[upgraded],
        robots_checker=lambda *_: (_ for _ in ()).throw(AssertionError("robots must not run before due time")),
        runner=lambda _config: (_ for _ in ()).throw(AssertionError("browser must not start before due time")),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )
    page = second["pages"][0]
    assert second["stale_page_count"] == 1
    assert page["parser_contract"] == "football_data_archive_v1"
    assert page["parser_reprojection"] == {
        "from": "generic_public_page_v1",
        "to": "football_data_archive_v1",
        "reason": "stale_raw_page_reparsed_with_current_source_contract",
    }
    assert page["extracted_record_count"] == 1
    assert page["extracted_records"][0]["historical_only"] is True
    assert page["stale"] is True
    assert page["observed_at"] == NOW.isoformat()
    assert page["enters_model"] is False
    assert (archive / "host_throttle.json").is_file()


def test_throttled_poll_does_not_relabel_cached_page_after_source_identity_change(tmp_path: Path):
    archive = tmp_path / "crawl4ai"
    first_config = _config(source_id="source_alpha", min_delay_seconds=60)
    fetch_crawl4ai(
        now=NOW,
        configs=[first_config],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: FakeCrawlResult(),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )

    replacement_config = _config(source_id="source_beta", min_delay_seconds=60)
    second = fetch_crawl4ai(
        now=NOW.replace(minute=0, second=30),
        configs=[replacement_config],
        robots_checker=lambda *_: (_ for _ in ()).throw(AssertionError("robots must not run before due time")),
        runner=lambda _config: (_ for _ in ()).throw(AssertionError("browser must not start before due time")),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )

    assert second["status"] == "not_due"
    assert second["pages"] == []
    assert second["stale_page_count"] == 0
    assert second["errors"][0]["error_code"] == "rate_limited_wait"


def test_throttled_poll_does_not_reuse_corrupted_last_verified_page(tmp_path: Path):
    archive = tmp_path / "crawl4ai"
    config = _config(min_delay_seconds=60)

    first = fetch_crawl4ai(
        now=NOW,
        configs=[config],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: FakeCrawlResult(),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )
    raw_path = archive / first["pages"][0]["raw_path"]
    raw_path.write_text("tampered", encoding="utf-8")

    second = fetch_crawl4ai(
        now=NOW.replace(minute=0, second=30),
        configs=[config],
        robots_checker=lambda *_: (_ for _ in ()).throw(AssertionError("robots must not be called before due time")),
        runner=lambda _config: (_ for _ in ()).throw(AssertionError("browser must not start before due time")),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )

    assert second["status"] == "not_due"
    assert second["pages"] == []
    assert second["fresh_page_count"] == 0
    assert second["stale_page_count"] == 0
    assert second["errors"][0]["error_code"] == "rate_limited_wait"


def test_successful_page_plus_peer_throttle_remains_healthy_and_audited(tmp_path: Path):
    archive = tmp_path / "crawl4ai"
    archive.mkdir()
    (archive / "host_throttle.json").write_text(
        json.dumps({"other.example": NOW.isoformat()}),
        encoding="utf-8",
    )
    robots_calls: list[str] = []
    result = fetch_crawl4ai(
        now=NOW,
        configs=[
            _config(name="ready", min_delay_seconds=0),
            _config(
                name="deferred",
                url="https://other.example/news/1",
                allowed_hosts=("other.example",),
                min_delay_seconds=60,
            ),
        ],
        robots_checker=lambda url, *_: robots_calls.append(url) or {"allowed": True, "status": "allowed"},
        runner=lambda _config: FakeCrawlResult(),
        archive_dir=archive,
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "ok"
    assert len(result["pages"]) == 1
    assert result["errors"][0]["stage"] == "throttle"
    assert result["errors"][0]["error_code"] == "rate_limited_wait"
    assert robots_calls == ["https://public.example/news/1"]


def test_config_loader_never_enables_an_implicit_source():
    configs, errors = load_crawl4ai_configs()
    assert configs == []
    assert errors == []
    configs, errors = load_crawl4ai_configs(raw={"sources": [{"name": "bad", "url": "http://x.example"}]})
    assert configs == []
    assert errors and errors[0]["stage"] == "config"
    configs, errors = load_crawl4ai_configs(raw={"sources": [{"name": "candidate", "url": "https://x.example/", "enabled": False}]})
    assert configs == []
    assert errors == []

    configs, errors = load_crawl4ai_configs(raw={"sources": [{"name": "denied", "url": "https://x.example/", "license_status": "denied"}]})
    assert configs == []
    assert errors and errors[0]["status"] == "rights_blocked"
    assert errors[0]["error_code"] == "source_rights_not_verified"
    assert errors[0]["network_opened"] is False
    assert errors[0]["model_eligible"] is False


def test_operator_sources_with_unknown_rights_are_disabled_before_any_network(tmp_path: Path):
    config_path = Path(__file__).parents[1] / "config" / "crawl4ai_operator_public.json"
    configs, errors = load_crawl4ai_configs(config_path=config_path)
    assert configs == []
    assert errors == []

    calls: list[str] = []
    result = fetch_crawl4ai(
        now=NOW,
        config_path=config_path,
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True},
        runner=lambda _config: calls.append("browser") or FakeCrawlResult(),
        archive_dir=tmp_path / "crawl4ai",
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "not_configured"
    assert result["configured_count"] == 0
    assert calls == []


def test_enabled_unknown_rights_config_is_rejected_before_robots(tmp_path: Path):
    config_path = tmp_path / "operator.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "Operator public page",
                        "url": "https://public.example/news/1",
                        "enabled": True,
                        "allowed_hosts": ["public.example"],
                        "allowed_path_prefixes": ["/news/"],
                        "license_status": "unknown",
                        "min_delay_seconds": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    calls: list[str] = []
    result = fetch_crawl4ai(
        now=NOW,
        config_path=config_path,
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True},
        runner=lambda _config: calls.append("browser") or FakeCrawlResult(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "rights_blocked"
    assert result["configured_count"] == 0
    assert result["pages"] == []
    assert result["rights_blocked_count"] == 1
    assert result["network_opened"] is False
    error = result["errors"][0]
    assert error["stage"] == "rights_gate"
    assert error["status"] == "rights_blocked"
    assert error["error_code"] == "source_rights_not_verified"
    assert error["source_id"] is None
    assert error["network_opened"] is False
    assert error["model_eligible"] is False
    assert error["rights"]["decision"] == "block"
    assert error["rights"]["source_id"] == ""
    blocked_source = result["rights_blocked_sources"][0]
    assert blocked_source["source_id"] is None
    assert blocked_source["name"] == "Operator public page"
    assert blocked_source["url"] == "https://public.example/news/1"
    assert blocked_source["license_status"] == "unknown"
    assert blocked_source["rights_reference"] is None
    assert blocked_source["network_opened"] is False
    assert blocked_source["rights_status"] == "unknown_source_fail_closed"
    assert blocked_source["decision"] == "block"
    assert blocked_source["model_eligible"] is False
    assert calls == []
    assert result["provider_role"] == "execution_layer"
    assert result["execution_layer"] == {
        "id": "crawl4ai_allowlisted_pages",
        "name": "Crawl4AI",
        "role": "fetch_runtime",
        "fact_source": False,
        "model_eligible": False,
    }
    assert result["fact_source_ids"] == []
    assert result["fact_source_count"] == 0
    assert result["unidentified_page_count"] == 0


def test_mixed_operator_config_crawls_only_rights_approved_source(tmp_path: Path):
    config_path = tmp_path / "operator.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "Unknown-rights page",
                        "source_id": "whoscored_public_pages",
                        "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
                        "enabled": True,
                        "allowed_hosts": ["www.whoscored.com"],
                        "allowed_path_prefixes": ["/Regions/"],
                        "license_status": "unknown",
                        "min_delay_seconds": 0,
                    },
                    {
                        "name": "Approved page",
                        "source_id": "public_news",
                        "url": "https://public.example/news/1",
                        "enabled": True,
                        "allowed_hosts": ["public.example"],
                        "allowed_path_prefixes": ["/news/"],
                        "license_status": "open",
                        "rights_reference": "https://public.example/open-data-license",
                        "test_only": True,
                        "min_delay_seconds": 0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    calls: list[str] = []
    result = fetch_crawl4ai(
        now=NOW,
        config_path=config_path,
        robots_checker=lambda url, *_: calls.append(f"robots:{url}") or {"allowed": True},
        runner=lambda config: calls.append(f"browser:{config.source_id}") or FakeCrawlResult(),
        archive_dir=None,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "degraded"
    assert result["configured_count"] == 1
    assert result["rights_blocked_count"] == 1
    assert result["rights_blocked_sources"][0]["source_id"] == "whoscored_public_pages"
    assert result["rights_blocked_sources"][0]["network_opened"] is False
    assert calls == [
        "robots:https://public.example/news/1",
        "browser:public_news",
    ]


def test_mixed_rights_diagnostic_survives_tls_fail_closed_return(
    tmp_path: Path, monkeypatch
):
    config_path = tmp_path / "operator.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "Unknown-rights page",
                        "source_id": "whoscored_public_pages",
                        "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
                        "enabled": True,
                        "allowed_hosts": ["www.whoscored.com"],
                        "allowed_path_prefixes": ["/Regions/"],
                        "license_status": "unknown",
                    },
                    {
                        "name": "Approved page",
                        "source_id": "public_news",
                        "url": "https://public.example/news/1",
                        "enabled": True,
                        "allowed_hosts": ["public.example"],
                        "allowed_path_prefixes": ["/news/"],
                        "license_status": "open",
                        "rights_reference": "https://public.example/open-data-license",
                        "test_only": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        crawl4ai_module,
        "strict_tls_runtime_status",
        lambda: {"status": "unsupported", "reason": "test TLS block"},
    )
    calls: list[str] = []

    result = fetch_crawl4ai(
        now=NOW,
        config_path=config_path,
        robots_checker=lambda *_: calls.append("robots") or {"allowed": True},
        archive_dir=None,
    )

    assert result["status"] == "blocked_tls_policy"
    assert result["rights_blocked_count"] == 1
    assert result["rights_blocked_sources"][0]["source_id"] == "whoscored_public_pages"
    assert result["network_opened"] is False
    assert [error["stage"] for error in result["errors"]] == [
        "rights_gate",
        "tls_policy",
    ]
    assert calls == []


def test_crawl4ai_fanout_counts_declared_sources_separately_from_engine(tmp_path: Path):
    result = fetch_crawl4ai(
        now=NOW,
        configs=[_config(source_id="public_news")],
        robots_checker=lambda *_: {"allowed": True, "status": "allowed"},
        runner=lambda _config: FakeCrawlResult(),
        archive_dir=tmp_path / "crawl4ai",
        sleep_fn=lambda _seconds: None,
    )

    assert result["fact_source_ids"] == ["public_news"]
    assert result["fact_source_count"] == 1
    assert result["execution_layer"]["fact_source"] is False
    assert result["source_fanout"] == [{
        "source_id": "public_news",
        "source_name": "Public page",
        "configured_page_count": 1,
        "page_count": 1,
        "fresh_page_count": 1,
        "stale_page_count": 0,
        "extracted_record_count": 0,
        "error_count": 0,
        "status": "fresh",
        "model_eligible_count": 0,
        "enters_model": False,
    }]
    assert result["pages"][0]["provider_role"] == "execution_layer"


def test_crawl_pages_are_ledger_observations_but_not_model_features():
    raw_hash = "a" * 64
    snapshot = {
        "as_of": NOW.isoformat(),
        "crawl4ai": {
            "provider": "Crawl4AI",
            "status": "ok",
            "retrieved_at": NOW.isoformat(),
            "pages": [
                {
                    "source": "Public page",
                    "source_tier": "reliable_public_provider",
                    "url": "https://public.example/news/1",
                    "final_url": "https://public.example/news/1",
                    "observed_at": NOW.isoformat(),
                        "raw_sha256": raw_hash,
                        "markdown": "# report",
                        "metadata": {"structured_facts": []},
                        "structured_facts": [],
                    "robots": {"allowed": True},
                }
            ],
            "errors": [],
        },
    }
    rows = snapshot_observations(snapshot)
    assert len(rows) == 1
    assert rows[0].kind == "crawl4ai_page"
    assert rows[0].enters_model is False
    assert rows[0].model_exclusion_reason == "unstructured_browser_capture_requires_source_parser"
    assert rows[0].payload["metadata"]["structured_facts"] == []


def test_crawl_page_without_declared_source_is_quarantined_under_unknown_identity():
    snapshot = {
        "as_of": NOW.isoformat(),
        "crawl4ai": {
            "provider": "Crawl4AI",
            "status": "ok",
            "retrieved_at": NOW.isoformat(),
            "pages": [{
                "url": "https://public.example/news/1",
                "final_url": "https://public.example/news/1",
                "observed_at": NOW.isoformat(),
                "raw_sha256": "b" * 64,
                "robots": {"allowed": True},
            }],
            "errors": [],
        },
    }
    rows = snapshot_observations(snapshot)
    assert len(rows) == 1
    assert rows[0].source_name == "unidentified_public_page"
    assert rows[0].enters_model is False
    assert rows[0].model_exclusion_reason == "missing_declared_source_identity"


def test_snapshot_archive_counts_browser_pages():
    payload = {
        "as_of": NOW.isoformat(),
        "espn": {"provider": "ESPN", "fixtures": [], "errors": []},
        "crawl4ai": {
            "provider": "Crawl4AI",
            "status": "ok",
            "pages": [{"url": "https://public.example/news/1"}],
            "errors": [],
        },
    }
    summary = summarize_snapshot(payload)
    assert summary["provider_status"]["crawl4ai"] == "ok"
    assert summary["provider_counts"]["crawl4ai"] == 1
