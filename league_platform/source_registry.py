"""Explicit public-source registry for Matchline's pre-match polling plan.

The registry is deliberately declarative.  It does not discover arbitrary
websites and it does not turn a source into a model feature by itself.  Each
adapter still owns its URL/robots/response contract; this module gives the
polling operator and the Sites audit page one small source-of-truth for what
is allowed, what is only a comparison, and what must remain quarantined.

The ``github_reference`` fields document which parts of the user's
``rikfish163-rgb/worldcup-predicter`` project informed an adapter.  The old
relay/anti-WAF scripts are intentionally represented as forbidden rather than
executable sources.
"""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Mapping
from urllib.parse import urlparse


_SOURCE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "id": "sports_lottery_official",
        "name": "中国体育彩票公开赛程与赔率",
        "adapter": "league_platform.live_sources.sports_lottery",
        "hosts": ["webapi.sporttery.cn"],
        "source_tier": "official",
        "access_policy": "public_https_no_redirect_no_proxy",
        "polling": {"normal_minutes": 30, "near_kickoff_minutes": 5},
        "model_policy": "eligible_for_purchase_odds_baseline_after_strict_join",
        "status": "enabled",
        "github_reference": "wc_analysis/scrape_sporttery.py parser and multi-market shape only",
    },
    {
        "id": "openfootball_current",
        "name": "OpenFootball 当前赛程与赛果",
        "adapter": "league_platform.live_sources.openfootball_live",
        "hosts": ["raw.githubusercontent.com"],
        "source_tier": "open_data_provider",
        "access_policy": "fixed_cc0_urls_https_no_redirect_bounded_parallelism",
        "polling": {"normal_minutes": 30, "near_kickoff_minutes": 10},
        "model_policy": "eligible_for_fixture_identity_and_causal_results_with_exact_kickoff_only",
        "status": "enabled",
        "license": "CC0-1.0",
        "license_url": "https://github.com/openfootball/football.json/blob/master/LICENSE.md",
        "attribution_required": False,
        "github_reference": "Six fixed 2026-27 OpenFootball JSON/TXT source URLs; date-only rows remain quarantined",
    },
    {
        "id": "cfl_official_current",
        "name": "中足联官方中超当前赛程与赛果",
        "adapter": "league_platform.live_sources.cfl_official",
        "hosts": ["api.cfl-china.cn"],
        "source_tier": "official_first_party_public_interface",
        "access_policy": "official_public_frontend_api_https_no_redirect_bounded_reads",
        "polling": {"normal_minutes": 30, "near_kickoff_minutes": 10},
        "model_policy": "eligible_for_fixture_identity_and_causal_results_under_project_public_fact_policy; commercial_reuse_gate_remains_blocked",
        "status": "enabled",
        "license": "not_published",
        "rights_status": "public_first_party_access_reuse_terms_unverified",
        "commercial_reuse_verified": False,
        "official_page_url": "https://www.cfl-china.cn/zh/fixtures/list.html?competition_code=CSL",
        "github_reference": "None; first-party CFL web interface discovered and independently contract-tested",
    },
    {
        "id": "espn_schedule_summary",
        "name": "ESPN 赛程、赛果与事件摘要",
        "adapter": "league_platform.live_sources.espn",
        "hosts": ["site.api.espn.com", "site.web.api.espn.com"],
        "source_tier": "public_provider",
        "access_policy": "provider_terms_block_unlicensed_automated_access_and_commercial_use",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "never_fetch_or_publish_without_express_written_permission",
        "status": "forbidden",
        "terms_url": "https://disneytermsofuse.com/english/",
        "github_reference": "Legacy parser and archives are retained only for authorized fixtures and audit replay",
    },
    {
        "id": "espn_team_rosters",
        "name": "ESPN 公开球队名单",
        "adapter": "league_platform.live_sources.espn_roster",
        "hosts": ["site.web.api.espn.com"],
        "source_tier": "public_provider",
        "access_policy": "provider_terms_block_unlicensed_automated_access_and_commercial_use",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "never_fetch_or_publish_without_express_written_permission",
        "status": "forbidden",
        "terms_url": "https://disneytermsofuse.com/english/",
        "github_reference": "Legacy parser retained for authorized/test fixtures only; no current polling",
    },
    {
        "id": "espn_market_summary",
        "name": "ESPN 事件摘要市场与名单",
        "adapter": "league_platform.live_sources.espn_market",
        "hosts": ["site.api.espn.com"],
        "source_tier": "public_provider",
        "access_policy": "provider_terms_block_unlicensed_automated_access_and_commercial_use",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "never_fetch_or_publish_without_express_written_permission",
        "status": "forbidden",
        "terms_url": "https://disneytermsofuse.com/english/",
        "github_reference": "Legacy parser retained for authorized/test fixtures only; no current polling",
    },
    {
        "id": "espn_injury_reports",
        "name": "ESPN 公开联盟伤停报告",
        "adapter": "league_platform.live_sources.espn_injuries",
        "hosts": ["site.api.espn.com", "site.web.api.espn.com"],
        "source_tier": "public_provider",
        "access_policy": "provider_terms_block_unlicensed_automated_access_and_commercial_use",
        "polling": {"normal_minutes": 30, "near_kickoff_minutes": 5},
        "model_policy": "never_fetch_or_publish_without_express_written_permission",
        "status": "forbidden",
        "terms_url": "https://disneytermsofuse.com/english/",
        "github_reference": "Parser retained for authorized/test fixtures only; endpoint accessibility is not an access license",
    },
    {
        "id": "understat_xg",
        "name": "Understat 近期 xG 与球队状态",
        "adapter": "league_platform.live_sources.understat",
        "hosts": ["understat.com"],
        "source_tier": "public_provider",
        "access_policy": "public_https_bounded_requests",
        "polling": {"normal_minutes": 360, "near_kickoff_minutes": 60},
        "model_policy": "eligible_when_observed_before_freeze",
        "status": "enabled",
        "github_reference": "project README source catalog; no hard-coded xG defaults adopted",
    },
    {
        "id": "official_league_lineups",
        "name": "英超/西甲/德甲/意甲/法甲官方比赛中心与首发",
        "adapter": "league_platform.live_sources.{premier_league,laliga,bundesliga,seriea,ligue1}",
        "hosts": ["premierleague.com", "laliga.com", "bundesliga.com", "api-sdp.legaseriea.it", "ma-api.ligue1.fr"],
        "source_tier": "official",
        "access_policy": "public_https_strict_fixture_join",
        "polling": {"normal_minutes": 30, "near_kickoff_minutes": 2},
        "model_policy": "eligible_only_complete_pre_kickoff_xi",
        "status": "enabled",
        "github_reference": "project lineup/source separation retained; official feeds supersede generic pages; Ligue 1 API is the public interface referenced by the official web client",
    },
    {
        "id": "laliga_official_match_directory",
        "name": "西甲官方公开比赛目录",
        "adapter": "league_platform.live_sources.laliga",
        "hosts": ["apim.laliga.com"],
        "source_tier": "official",
        "access_policy": "public_https_bounded_no_redirect",
        "polling": {"normal_minutes": 30, "near_kickoff_minutes": 2},
        "model_policy": "eligible_for_fixture_identity_and_official_page_discovery_no_lineup_inference",
        "status": "enabled",
        "github_reference": "LaLiga public-service match directory discovers exact official match pages; it never infers a lineup",
    },
    {
        "id": "sofascore_prematch",
        "name": "SofaScore 赛前事件、伤停与阵容线索",
        "adapter": "league_platform.live_sources.sofascore",
        "hosts": ["api.sofascore.com"],
        "source_tier": "public_provider",
        "access_policy": "public_https_bounded_requests",
        "polling": {"normal_minutes": 30, "near_kickoff_minutes": 5},
        "model_policy": "eligible_only_high_confidence_provider_facts",
        "status": "enabled",
        "github_reference": "project README source catalog; provider-reported fields remain non-authoritative",
    },
    {
        "id": "public_rss_news",
        "name": "BBC/Sky/Google News RSS 事实线索",
        "adapter": "league_platform.live_sources.news",
        "hosts": ["feeds.bbci.co.uk", "www.skysports.com", "news.google.com"],
        "source_tier": "reliable_media_and_lead_source",
        "access_policy": "public_https_rss_only",
        "polling": {"normal_minutes": 30, "near_kickoff_minutes": 10},
        "model_policy": "display_and_structured_fact_after_verification",
        "status": "enabled",
        "github_reference": "project README RSS/Google News catalog; sentiment is not model input",
    },
    {
        "id": "open_meteo",
        "name": "Open-Meteo 天气与场地环境",
        "adapter": "league_platform.live_sources.open_meteo",
        "hosts": ["geocoding-api.open-meteo.com", "api.open-meteo.com"],
        "source_tier": "public_provider",
        "access_policy": "public_https_bounded_requests",
        "polling": {"normal_minutes": 180, "near_kickoff_minutes": 30},
        "model_policy": "eligible_when_venue_join_and_time_bound_pass",
        "status": "enabled",
        "github_reference": "project environmental-feature idea retained with explicit venue/time join",
    },
    {
        "id": "wikidata_entities",
        "name": "Wikidata 结构化球队与场馆实体",
        "adapter": "league_platform.live_sources.wikidata",
        "hosts": ["www.wikidata.org"],
        "source_tier": "open_structured_data_cc0",
        "access_policy": "public_https_identified_cached_bounded_requests",
        "polling": {"normal_minutes": 360, "near_kickoff_minutes": 60},
        "model_policy": "eligible_only_exact_team_preferred_venue_coordinate_chain",
        "status": "enabled",
        "license": "CC0-1.0",
        "license_url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
        "attribution_required": False,
        "github_reference": "Wikibase API entity search and claims; ambiguous joins remain quarantined",
    },
    {
        "id": "met_norway_weather",
        "name": "MET Norway Locationforecast 天气",
        "adapter": "league_platform.live_sources.met_norway",
        "hosts": ["api.met.no"],
        "source_tier": "public_weather_cc_by",
        "access_policy": "public_https_identified_cached_bounded_requests",
        "polling": {"normal_minutes": 180, "near_kickoff_minutes": 30},
        "model_policy": "eligible_only_with_exact_coordinates_and_time_bound",
        "status": "enabled",
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "terms_url": "https://api.met.no/doc/TermsOfService",
        "attribution_required": True,
        "github_reference": "official MET Norway global Locationforecast; requires identifying User-Agent, caching, rate limits and attribution",
    },
    {
        "id": "oddstorm_market_comparison",
        "name": "OddStorm 市场对照（授权阻断）",
        "adapter": "league_platform.live_sources.oddstorm",
        "hosts": ["www.oddstorm.com"],
        "source_tier": "independent_market_comparison",
        "access_policy": "provider_terms_forbid_automated_scraping_mirroring_redistribution_and_resale",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "never_fetch_or_publish_without_express_written_permission",
        "status": "forbidden",
        "terms_url": "https://www.oddstorm.com/terms",
        "rights_status": "blocked_pending_express_written_permission",
        "terms_reviewed_at": "2026-08-24",
        "github_reference": "Parser retained for authorized fixtures only; public visibility is not permission to automate or redistribute",
    },
    {
        "id": "openligadb_secondary_results",
        "name": "OpenLigaDB 赛果二次核验",
        "adapter": "league_platform.live_sources.openligadb",
        "hosts": ["api.openligadb.de"],
        "source_tier": "public_secondary_result",
        "access_policy": "public_https_bounded_requests",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "result_enrichment_only_after_exact_join",
        "status": "enabled",
        "license": "ODbL-1.0",
        "license_url": "https://www.openligadb.de/lizenz",
        "attribution_required": True,
        "share_alike_database_obligation": True,
        "github_reference": "project historical-result catalog; never replaces canonical fixture identity",
    },
    {
        "id": "football_data_historical",
        "name": "Football-Data.co.uk 历史赛果与赔率 CSV",
        "adapter": "league_platform.data_manifest",
        "hosts": ["www.football-data.co.uk"],
        "source_tier": "public_historical_archive",
        "access_policy": "public_https_csv_training_only",
        "polling": {"normal_minutes": 1440, "near_kickoff_minutes": 0},
        "model_policy": "training_and_backtest_only_never_current_pre_match",
        "status": "enabled",
        "github_reference": "project league_platform/data_manifest.json; historical rows never become future observations",
    },
    {
        "id": "fbref_public_stats",
        "name": "FBref 公共球队与球员统计",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["fbref.com"],
        "source_tier": "public_stats_candidate",
        "access_policy": "public_https_current_probe_403",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "display_only_until_access_and_parser_are_verified",
        "status": "quarantined",
        "github_reference": "soccerdata/fbref.py and wc_analysis/fetch_fbref_form.py; current page probe returned 403",
    },
    {
        "id": "whoscored_public_pages",
        "name": "WhoScored 公开比赛预览页",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["www.whoscored.com"],
        "source_tier": "public_page_candidate",
        "access_policy": "explicit_path_robots_allowlist_required",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "display_only_until_source_parser_terms_and_entity_time_gates_pass",
        "status": "opt_in",
        "github_reference": "soccerdata/whoscored.py; only robots-allowed paths may be considered",
    },
    {
        "id": "premier_league_public_pages",
        "name": "英超官方公开比赛页",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["www.premierleague.com"],
        "source_tier": "official",
        "access_policy": "explicit_path_robots_allowlist_required",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "display_only_until_source_parser_terms_and_entity_time_gates_pass",
        "status": "opt_in",
        "github_reference": "official public match pages are captured by Crawl4AI only after explicit robots/path checks",
    },
    {
        "id": "laliga_public_pages",
        "name": "西甲官方公开比赛页",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["www.laliga.com"],
        "source_tier": "official",
        "access_policy": "explicit_path_robots_allowlist_required",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "display_only_until_source_parser_terms_and_entity_time_gates_pass",
        "status": "opt_in",
        "github_reference": "official public match pages are captured by Crawl4AI only after explicit robots/path checks",
    },
    {
        "id": "laliga_official_news",
        "name": "西甲官方公开新闻页",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["www.laliga.com"],
        "source_tier": "official",
        "access_policy": "explicit_path_robots_allowlist_required",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "display_only_until_article_parser_fixture_or_team_join_and_time_gates_pass",
        "status": "opt_in",
        "github_reference": "LaLiga first-party NewsArticle JSON-LD projected by Crawl4AI; headline evidence is not a lineup or injury fact",
    },
    {
        "id": "bundesliga_public_pages",
        "name": "德甲官方公开比赛页",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["bundesliga.com"],
        "source_tier": "official",
        "access_policy": "explicit_path_robots_allowlist_required",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "display_only_until_source_parser_terms_and_entity_time_gates_pass",
        "status": "opt_in",
        "github_reference": "Bundesliga SportsEvent JSON-LD is captured by Crawl4AI only after explicit robots/path checks",
    },
    {
        "id": "seriea_public_pages",
        "name": "意甲官方公开比赛页",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["en.legaseriea.it"],
        "source_tier": "official",
        "access_policy": "explicit_path_robots_allowlist_required",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "display_only_until_source_parser_fixture_join_license_and_time_gates_pass",
        "status": "opt_in",
        "github_reference": "Lega Serie A public match-card links captured by Crawl4AI; source records remain display-only until strict fixture/time join",
    },
    {
        "id": "ligue1_official_news",
        "name": "法甲官方公开新闻页",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["ligue1.com"],
        "source_tier": "official",
        "access_policy": "explicit_path_robots_allowlist_required",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "display_only_until_article_parser_fixture_or_team_join_and_time_gates_pass",
        "status": "opt_in",
        "github_reference": "Ligue 1 public news surface captured by Crawl4AI; headline evidence is not a lineup or injury fact",
    },
    {
        "id": "clubelo_public_ratings",
        "name": "ClubElo 公共评级",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["www.clubelo.com"],
        "source_tier": "public_rating_candidate",
        "access_policy": "public_https_bounded_probe_only",
        "polling": {"normal_minutes": 360, "near_kickoff_minutes": 60},
        "model_policy": "display_only_until_source_health_and_time_contract_pass",
        "status": "quarantined",
        "github_reference": "soccerdata/clubelo.py; current TLS probe timed out",
    },
    {
        "id": "sofifa_public_reference",
        "name": "SoFIFA 公共球员参考",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": ["sofifa.com"],
        "source_tier": "public_reference_candidate",
        "access_policy": "robots_api_disallowed",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "forbidden_until_policy_and_model_input_rights_change",
        "status": "blocked_by_robots",
        "github_reference": "soccerdata/sofifa.py; robots disallows /api/ and reserves AI training",
    },
    {
        "id": "crawl4ai_allowlisted_pages",
        "name": "Crawl4AI 抓取引擎（白名单网页运行时）",
        "adapter": "league_platform.live_sources.crawl4ai",
        "hosts": [],
        "source_tier": "fetch_runtime",
        "role": "fetch_runtime",
        "fact_source": False,
        "access_policy": "explicit_allowlist_robots_fail_closed_same_host_redirect",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "runtime_only_never_a_fact_source; page source identity is required",
        "status": "opt_in",
        "github_reference": "project scraper architecture considered; no arbitrary-site discovery",
    },
    {
        "id": "lazq_public_mirror",
        "name": "Lazq 公开镜像赔率",
        "adapter": "league_platform.live_sources.lazq",
        "hosts": ["api.lazq.com"],
        "source_tier": "unverified_mirror",
        "access_policy": "public_https_bounded_requests",
        "polling": {"normal_minutes": 60, "near_kickoff_minutes": 10},
        "model_policy": "display_only_not_official_not_purchase_eligible",
        "status": "quarantined",
        "github_reference": "project mirror/odds idea; lineage is not sufficient for model use",
    },
    {
        "id": "fotmob_public_api",
        "name": "FotMob 公共 API",
        "adapter": "league_platform.live_sources.fotmob",
        "hosts": ["www.fotmob.com"],
        "source_tier": "public_provider",
        "access_policy": "robots_disallowed",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "forbidden_until_robots_policy_changes",
        "status": "blocked_by_robots",
        "github_reference": "project schedule/lineup source catalog; no robots bypass",
    },
    {
        "id": "legacy_anti_waf_relay",
        "name": "旧项目 relay_sporttery.sh / 4090 中转",
        "adapter": "external-archive:cleanup-v276-2026-08-26/legacy-entrypoints/wc_analysis/relay_sporttery.sh",
        "hosts": [],
        "source_tier": "forbidden",
        "access_policy": "would_bypass_access_controls",
        "polling": {"normal_minutes": 0, "near_kickoff_minutes": 0},
        "model_policy": "never_execute",
        "status": "forbidden",
        "github_reference": "cleanup-v276 external archive; retained only as a negative audit record",
    },
)


def get_source_registry() -> list[dict[str, Any]]:
    """Return a defensive copy suitable for API/UI serialization."""

    return deepcopy(list(_SOURCE_ROWS))


def validate_source_registry(rows: list[dict[str, Any]] | None = None) -> None:
    """Fail closed on duplicate IDs or unsafe registry declarations."""

    selected = rows if rows is not None else get_source_registry()
    ids: set[str] = set()
    for row in selected:
        source_id = row.get("id")
        if not isinstance(source_id, str) or not source_id or source_id in ids:
            raise ValueError("source registry IDs must be unique non-empty strings")
        ids.add(source_id)
        if row.get("status") == "enabled" and row.get("access_policy") in {
            "robots_disallowed",
            "would_bypass_access_controls",
        }:
            raise ValueError(f"unsafe access policy marked enabled: {source_id}")
        policy = str(row.get("model_policy") or "")
        if row.get("status") in {"forbidden", "blocked_by_robots"} and "never" not in policy and "forbidden" not in policy:
            raise ValueError(f"blocked source lacks an explicit model prohibition: {source_id}")
        role = row.get("role", "fact_source")
        if role not in {"fact_source", "fetch_runtime"}:
            raise ValueError(f"source registry row has an unknown role: {source_id}")
        if role == "fetch_runtime" and row.get("fact_source") is not False:
            raise ValueError(f"fetch runtime must not be declared as a fact source: {source_id}")


validate_source_registry()


_RUNTIME_BINDINGS: dict[str, tuple[str, str]] = {
    "sports_lottery_official": ("sports_lottery", "matches"),
    "openfootball_current": ("fixture_feed_openfootball", "fixtures"),
    "cfl_official_current": ("fixture_feed_cfl", "fixtures"),
    "espn_schedule_summary": ("espn", "fixtures"),
    "espn_team_rosters": ("espn_rosters", "rosters"),
    "espn_market_summary": ("espn_markets", "markets"),
    "espn_injury_reports": ("espn_injuries", "reports"),
    "understat_xg": ("understat", "team_features"),
    "laliga_official_match_directory": ("laliga_match_directory", "fixtures"),
    "official_league_lineups": ("official_lineups", "lineups"),
    "sofascore_prematch": ("sofascore", "events"),
    "public_rss_news": ("news", "items"),
    "open_meteo": ("weather", "weather"),
    "met_norway_weather": ("met_norway_weather", "weather"),
    "wikidata_entities": ("wikidata_entities", "venues"),
    "oddstorm_market_comparison": ("oddstorm", "lines"),
    "openligadb_secondary_results": ("openligadb", "matches"),
    "crawl4ai_allowlisted_pages": ("crawl4ai", "pages"),
    # Keep the browser edge aggregate while also projecting explicitly
    # allowlisted pages to their declared source row.  A successful Crawl4AI
    # page must not make every candidate site look healthy.
    "whoscored_public_pages": ("crawl4ai_whoscored", "pages"),
    "premier_league_public_pages": ("crawl4ai_premier_league_public_pages", "pages"),
    "laliga_public_pages": ("crawl4ai_laliga_public_pages", "pages"),
    "laliga_official_news": ("crawl4ai_laliga_official_news", "pages"),
    "bundesliga_public_pages": ("crawl4ai_bundesliga_public_pages", "pages"),
    "seriea_public_pages": ("crawl4ai_seriea_public_pages", "pages"),
    "ligue1_official_news": ("crawl4ai_ligue1_official_news", "pages"),
}


def _provider_runtime(snapshot: Mapping[str, Any], provider_key: str, records_key: str) -> dict[str, Any]:
    """Return bounded, serialisable runtime health for one registry row.

    The source registry describes policy; this function only projects the
    already captured snapshot into a small health record. It never probes a
    URL, follows a redirect, or promotes a source into model input.
    """

    if provider_key in {"fixture_feed_openfootball", "fixture_feed_cfl"}:
        explicit_key = (
            "openfootball_current"
            if provider_key == "fixture_feed_openfootball"
            else "cfl_official"
        )
        explicit = snapshot.get(explicit_key)
        if isinstance(explicit, Mapping):
            payload = dict(explicit)
        else:
            aggregate = snapshot.get("fixture_feed")
            if not isinstance(aggregate, Mapping):
                payload = None
            else:
                expected_name = (
                    "OpenFootball"
                    if provider_key == "fixture_feed_openfootball"
                    else "Chinese Professional Football League official"
                )
                records = [
                    dict(row)
                    for row in (aggregate.get(records_key) or [])
                    if isinstance(row, Mapping)
                    and isinstance(row.get("source"), Mapping)
                    and row["source"].get("name") == expected_name
                ]
                # OpenFootball-only snapshots predate the explicit sub-feed
                # section. Their fixtures still carry full row lineage, so the
                # runtime projection can remain accurate without rewriting the
                # append-only snapshot.
                if (
                    not records
                    and provider_key == "fixture_feed_openfootball"
                    and aggregate.get("provider") == "OpenFootball"
                ):
                    records = [
                        dict(row)
                        for row in (aggregate.get(records_key) or [])
                        if isinstance(row, Mapping)
                    ]
                errors = [
                    dict(error)
                    for error in (aggregate.get("errors") or [])
                    if isinstance(error, Mapping)
                    and (
                        error.get("provider") == expected_name
                        or (
                            provider_key == "fixture_feed_openfootball"
                            and aggregate.get("provider") == "OpenFootball"
                            and error.get("provider") is None
                        )
                    )
                ]
                payload = dict(aggregate)
                payload[records_key] = records
                payload["errors"] = errors
                payload["status"] = (
                    "ok"
                    if records and not errors
                    else "degraded"
                    if records
                    else "unavailable"
                )
        if payload is None:
            return {
                "status": "unavailable",
                "retrieved_at": None,
                "record_count": 0,
                "error_count": 1,
                "errors": [{"error": "provider_payload_missing"}],
            }
    elif provider_key == "espn_injuries":
        payload = snapshot.get(provider_key)
        if not isinstance(payload, Mapping):
            return {
                "status": "unavailable",
                "retrieved_at": None,
                "record_count": 0,
                "observed_competition_count": 0,
                "observed_empty_competition_count": 0,
                "reported_player_count": 0,
                "healthy_team_count": None,
                "error_count": 1,
                "errors": [{"error": "provider_payload_missing"}],
            }

        def count(value: object) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        reports = payload.get(records_key)
        report_count = len(reports) if isinstance(reports, list) else 0
        errors = [
            dict(error)
            for error in (payload.get("errors") or [])[:5]
            if isinstance(error, Mapping)
        ]
        observed = count(payload.get("observed_competitions"))
        observed_empty = count(payload.get("observed_empty_competitions"))
        payload_status = str(payload.get("status") or "")
        if payload_status == "rights_blocked":
            status = "rights_blocked"
        elif report_count and not errors:
            status = "fresh"
        elif observed and observed_empty == observed and not errors:
            status = "observed_empty"
        elif (report_count or observed) and errors:
            status = "degraded"
        elif payload_status == "not_requested":
            status = "not_requested"
        else:
            status = "unavailable"
        return {
            "status": status,
            "retrieved_at": payload.get("retrieved_at"),
            "checked_at": payload.get("checked_at"),
            "record_count": report_count,
            "observed_competition_count": observed,
            "observed_empty_competition_count": observed_empty,
            "reported_player_count": count(payload.get("reported_player_count")),
            # An omitted provider team bucket is unknown.  Never project an
            # inferred count of healthy teams into the operational dashboard.
            "healthy_team_count": None,
            "coverage_semantics": "missing_team_bucket_is_unknown_not_healthy",
            "rights_status": payload.get("rights_status"),
            "terms_url": payload.get("terms_url"),
            "network_opened": payload.get("network_opened"),
            "access_allowed": payload.get("access_allowed"),
            "authorization_required": payload.get("authorization_required"),
            "error_count": len(errors),
            "errors": errors,
        }

    if provider_key == "laliga_match_directory":
        payload = snapshot.get("laliga_official")
        if not isinstance(payload, Mapping):
            return {
                "status": "unavailable",
                "retrieved_at": None,
                "record_count": 0,
                "match_discovery": {
                    "status": "unavailable",
                    "matched_count": 0,
                    "page_count": 0,
                    "records_seen": 0,
                    "truncated": None,
                },
                "error_count": 1,
                "errors": [{"error": "provider_payload_missing"}],
            }
        discovery = payload.get("match_discovery")
        if not isinstance(discovery, Mapping):
            idle_status = str(payload.get("status") or "unavailable")
            return {
                "status": "not_requested" if idle_status == "not_requested" else "unavailable",
                "retrieved_at": payload.get("retrieved_at"),
                "record_count": 0,
                "match_discovery": {
                    "status": "not_requested" if idle_status == "not_requested" else "unavailable",
                    "matched_count": 0,
                    "page_count": 0,
                    "records_seen": 0,
                    "truncated": None,
                },
                "error_count": 0 if idle_status == "not_requested" else 1,
                "errors": [] if idle_status == "not_requested" else [{"error": "match_directory_diagnostic_missing"}],
            }
        raw_errors = discovery.get("errors")
        errors = [dict(error) for error in raw_errors[:5] if isinstance(error, Mapping)] if isinstance(raw_errors, list) else []

        def non_negative(value: object) -> int:
            if isinstance(value, bool):
                return 0
            if isinstance(value, int):
                return value if value >= 0 else 0
            if isinstance(value, float) and value.is_integer() and value >= 0:
                return int(value)
            return 0

        discovery_status = str(discovery.get("status") or "unavailable")
        if errors and discovery_status == "ok":
            status = "degraded"
        elif discovery_status == "ok":
            status = "fresh"
        elif discovery_status in {"not_requested", "not_needed", "not_due", "unavailable"}:
            status = discovery_status
        else:
            status = "unavailable"
        return {
            "status": status,
            "retrieved_at": payload.get("retrieved_at"),
            "record_count": non_negative(discovery.get("records_seen")),
            "match_discovery": {
                "status": status,
                "matched_count": non_negative(discovery.get("matched_count")),
                "page_count": non_negative(discovery.get("pages")),
                "records_seen": non_negative(discovery.get("records_seen")),
                "truncated": discovery.get("truncated") if isinstance(discovery.get("truncated"), bool) else None,
                "endpoint": discovery.get("endpoint") if isinstance(discovery.get("endpoint"), str) else None,
                "raw_sha256": discovery.get("raw_sha256") if isinstance(discovery.get("raw_sha256"), str) else None,
            },
            "error_count": len(errors),
            "errors": errors,
        }

    if provider_key == "official_lineups":
        payloads = [
            snapshot.get("premier_league_official"),
            snapshot.get("laliga_official"),
            snapshot.get("bundesliga_official"),
            snapshot.get("serie_a_official"),
            snapshot.get("ligue1_official"),
        ]
        payloads = [item for item in payloads if isinstance(item, Mapping)]
        retrieved = [item.get("retrieved_at") for item in payloads if item.get("retrieved_at")]
        errors = [error for item in payloads for error in (item.get("errors") or []) if isinstance(error, Mapping)]
        lineup_rows = [
            row
            for item in payloads
            for row in (item.get(records_key) or [])
            if isinstance(row, Mapping)
        ]
        records = len(lineup_rows)
        fixture_count = sum(
            len(item.get("fixtures") or [])
            for item in payloads
            if isinstance(item.get("fixtures"), list)
        )
        available_count = 0
        confirmed_count = 0
        model_eligible_count = 0
        not_published_count = 0
        for row in lineup_rows:
            lineups = row.get("lineups") if isinstance(row.get("lineups"), Mapping) else {}
            players = sum(
                len(lineups.get(side, {}).get("players") or [])
                for side in ("home", "away")
                if isinstance(lineups.get(side), Mapping)
            )
            available = lineups.get("available") is True or lineups.get("confirmed") is True or players > 0
            if available:
                available_count += 1
            if lineups.get("confirmed") is True:
                confirmed_count += 1
            if lineups.get("model_eligible") is True:
                model_eligible_count += 1
            diagnostic = lineups.get("diagnostic") if isinstance(lineups.get("diagnostic"), Mapping) else {}
            if (
                lineups.get("available") is False
                or diagnostic.get("status") == "not_published"
                or diagnostic.get("reason") == "official_feed_returned_no_lineup_rows"
            ):
                not_published_count += 1
        statuses = {str(item.get("status") or "") for item in payloads}
        if errors and (records or fixture_count):
            status = "degraded"
        elif confirmed_count and not errors:
            status = "fresh"
        elif available_count and not_published_count == records:
            status = "not_published"
        elif available_count:
            status = "partial"
        elif not_published_count or fixture_count:
            status = "not_published"
        elif records:
            status = "observed_empty"
        elif "not_requested" in statuses:
            status = "not_requested"
        else:
            status = "unavailable"
        return {
            "status": status,
            "retrieved_at": max(retrieved, default=None),
            "record_count": records,
            "fixture_count": fixture_count,
            "available_count": available_count,
            "confirmed_count": confirmed_count,
            "model_eligible_count": model_eligible_count,
            "not_published_count": not_published_count,
            "error_count": len(errors),
            "errors": [dict(error) for error in errors[:5]],
        }

    if provider_key.startswith("crawl4ai_") and provider_key != "crawl4ai":
        aggregate = snapshot.get("crawl4ai")
        declared_source_id = {
            # The first implementation used the shorter provider binding;
            # keep it readable while projecting the full declared identity.
            "whoscored": "whoscored_public_pages",
        }.get(provider_key.removeprefix("crawl4ai_"), provider_key.removeprefix("crawl4ai_"))
        legacy_host = {
            "whoscored_public_pages": {"www.whoscored.com", "whoscored.com"},
        }.get(declared_source_id, set())

        def matches_declared_source(value: Mapping[str, Any], *, error: bool = False) -> bool:
            identity = str(value.get("source_id") or value.get("fact_source_id") or "")
            if identity:
                return identity == declared_source_id
            # Preserve the historical WhoScored projection for raw pages
            # written before source_id was added. New source lanes require an
            # explicit identity and therefore fail closed instead of guessing.
            if error:
                identity = str(value.get("source_id") or "")
                if identity:
                    return identity == declared_source_id
                host = str(urlparse(str(value.get("url") or "")).hostname or "").lower()
                return host in legacy_host
            host = str(urlparse(str(value.get("url") or value.get("final_url") or "")).hostname or "").lower()
            return host in legacy_host

        if not isinstance(aggregate, Mapping):
            payload = None
        else:
            pages = [
                page
                for page in aggregate.get("pages", [])
                if isinstance(page, Mapping)
                and matches_declared_source(page)
            ]
            errors = [
                error
                for error in aggregate.get("errors", [])
                if isinstance(error, Mapping)
                and matches_declared_source(error, error=True)
            ]
            payload = dict(aggregate)
            payload["pages"] = pages
            payload["errors"] = errors
            aggregate_status = str(aggregate.get("status") or "")
            payload["status"] = (
                "ok"
                if pages and not errors
                else aggregate_status
                if aggregate_status and aggregate_status != "ok"
                else "unavailable"
                if errors
                else "not_configured"
            )
            # The aggregate Crawl4AI payload carries one cross-source join
            # summary.  A fact-source binding must not inherit that total (for
            # example, LaLiga must not display Bundesliga's joined cards).
            # Recompute the summary from the pages already filtered to this
            # declared source ID; if a page has no join contract, omit the
            # field rather than inventing a zero-valued join.
            if provider_key != "crawl4ai":
                page_joins = [
                    page.get("fixture_join")
                    for page in pages
                    if isinstance(page.get("fixture_join"), Mapping)
                ]
                if page_joins:
                    joined_count = sum(
                        int(join.get("joined_count") or 0)
                        for join in page_joins
                        if isinstance(join.get("joined_count"), (int, float))
                    )
                    unmatched_count = sum(
                        int(join.get("unmatched_count") or 0)
                        for join in page_joins
                        if isinstance(join.get("unmatched_count"), (int, float))
                    )
                    admission_counts: dict[str, int] = {}
                    for join in page_joins:
                        counts = join.get("admission_counts")
                        if not isinstance(counts, Mapping):
                            continue
                        for state, count in counts.items():
                            if isinstance(count, (int, float)) and int(count) >= 0:
                                key = str(state)
                                admission_counts[key] = admission_counts.get(key, 0) + int(count)
                    payload["fixture_join"] = {
                        "status": (
                            "exact"
                            if joined_count and not unmatched_count
                            else "partial"
                            if joined_count
                            else "unavailable"
                        ),
                        "joined_count": joined_count,
                        "unmatched_count": unmatched_count,
                        "model_admission": "display_only_until_source_license_and_time_review",
                        "admission_counts": admission_counts,
                    }
                else:
                    payload.pop("fixture_join", None)
    elif provider_key not in {"fixture_feed_openfootball", "fixture_feed_cfl"}:
        payload = snapshot.get(provider_key)
    if not isinstance(payload, Mapping):
        return {"status": "unavailable", "retrieved_at": None, "record_count": 0, "error_count": 1, "errors": [{"error": "provider_payload_missing"}]}
    errors = [error for error in (payload.get("errors") or []) if isinstance(error, Mapping)]
    declared_error_count = payload.get("error_count")
    error_count = (
        declared_error_count
        if isinstance(declared_error_count, int)
        and not isinstance(declared_error_count, bool)
        and declared_error_count >= len(errors)
        else len(errors)
    )
    security_policy = None
    tls_runtime = None
    if provider_key.startswith("crawl4ai") and isinstance(payload.get("security_policy"), Mapping):
        allowed_policy_keys = (
            "ignore_https_errors",
            "robots_check",
            "redirects",
            "login_captcha_bypass",
            "access_control_bypass",
        )
        security_policy = {
            key: payload["security_policy"][key]
            for key in allowed_policy_keys
            if key in payload["security_policy"]
        }
    if provider_key.startswith("crawl4ai"):
        if isinstance(payload.get("tls_runtime"), Mapping):
            tls_runtime = dict(payload["tls_runtime"])
        else:
            # Older browser pages predate the launch-argument attestation.
            # Keep them visible as historical evidence, but never call them
            # a healthy current source until strict TLS is proven.
            tls_runtime = {
                "status": "unverified",
                "reason": "snapshot has no Crawl4AI TLS launch-argument attestation",
            }
    if provider_key == "weather":
        horizon_skips = [error for error in errors if error.get("stage") == "horizon"]
        operational_errors = [error for error in errors if error.get("stage") != "horizon"]
        records = len(payload.get(records_key) or []) if isinstance(payload.get(records_key), list) else 0
        if horizon_skips and not operational_errors and not records:
            return {
                "status": "outside_horizon",
                "retrieved_at": payload.get("retrieved_at"),
                "record_count": 0,
                "error_count": 0,
                "skipped_count": len(horizon_skips),
                "skipped_reasons": ["fixture kickoff is outside the public forecast horizon"],
                "errors": [],
            }
    if provider_key == "news":
        records = int(payload.get("item_count") or sum(len(feed.get("items") or []) for feed in payload.get("feeds") or [] if isinstance(feed, Mapping)))
    else:
        records = len(payload.get(records_key) or []) if isinstance(payload.get(records_key), list) else 0
    extracted_record_count = None
    if provider_key.startswith("crawl4ai"):
        extracted_record_count = sum(
            len(page.get("extracted_records") or [])
            for page in (payload.get(records_key) or [])
            if isinstance(page, Mapping) and isinstance(page.get("extracted_records"), list)
        )
    stale_records = sum(
        1
        for page in (payload.get(records_key) or [])
        if isinstance(page, Mapping) and page.get("stale") is True
    ) if provider_key.startswith("crawl4ai") else 0
    fresh_records = max(0, records - stale_records)
    payload_status = str(payload.get("status") or "")
    if provider_key.startswith("crawl4ai") and not records:
        dependency_errors = [
            error
            for error in errors
            if "No module named 'crawl4ai'" in str(error.get("error", ""))
            or "crawl4ai package is not installed" in str(error.get("error", ""))
        ]
        if dependency_errors:
            return {
                "status": "dependency_missing",
                "retrieved_at": payload.get("retrieved_at"),
                "record_count": 0,
                "error_count": len(errors),
                "errors": [dict(error) for error in errors[:5]],
                "dependency": "crawl4ai",
                **({"security_policy": security_policy} if security_policy else {}),
            }
    # A disabled/optional adapter is operationally different from a source
    # that was attempted and failed. Preserve those explicit states so the
    # Sites health panel does not turn an intentionally empty Crawl4AI lane
    # into a misleading outage.
    throttle_only = bool(errors) and all(error.get("stage") == "throttle" for error in errors)
    tls_policy_blocked = provider_key.startswith("crawl4ai") and records and str((tls_runtime or {}).get("status")) != "supported"
    if tls_policy_blocked:
        status = "blocked_tls_policy"
    elif records and stale_records == records:
        # A throttled poll may expose the last verified page so the evidence
        # panel remains useful. It must not be reported as fresh source data.
        status = "stale"
    elif not records and payload_status in {
        "not_configured",
        "invalid_config",
        "not_requested",
        "outside_horizon",
        "not_due",
        "blocked_tls_policy",
        "rights_blocked",
    }:
        status = payload_status
    else:
        # A successful page plus a persisted cross-process throttle deferral
        # is still a fresh cycle. Keep the rate-limit row in `errors` for
        # audit and retry scheduling, but do not report a source outage.
        status = "fresh" if records and (not errors or throttle_only) else "degraded" if records else "unavailable"
    runtime = {
        "status": status,
        "retrieved_at": payload.get("retrieved_at"),
        "record_count": records,
        "fresh_record_count": fresh_records,
        "stale_record_count": stale_records,
        "error_count": error_count,
        "errors": [dict(error) for error in errors[:5]],
    }
    if payload_status == "rights_blocked":
        runtime.update(
            {
                "checked_at": payload.get("checked_at"),
                "rights_status": payload.get("rights_status"),
                "terms_url": payload.get("terms_url"),
                "network_opened": payload.get("network_opened"),
            }
        )
    if extracted_record_count is not None:
        runtime["extracted_record_count"] = extracted_record_count
    if provider_key == "crawl4ai" and isinstance(payload.get("execution"), Mapping):
        # This is runtime scheduling evidence, not a source fact. Keep it on
        # the shared execution-layer row so operators can measure fan-out
        # latency without making every declared source look like a separate
        # browser process.
        execution = payload["execution"]
        bounded_execution: dict[str, Any] = {}
        strategy = execution.get("strategy")
        if isinstance(strategy, str) and strategy:
            bounded_execution["strategy"] = strategy[:120]
        for key in (
            "host_group_count",
            "max_host_workers",
            "configured_source_count",
            "index_page_count",
            "detail_page_count",
            "follow_up_candidate_count",
            "follow_up_requested_count",
            "follow_up_rejected_count",
            "follow_up_capped_count",
        ):
            value = execution.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                bounded_execution[key] = value
        follow_rejections = execution.get("follow_up_rejections")
        if isinstance(follow_rejections, list):
            bounded_execution["follow_up_rejections"] = [
                dict(item)
                for item in follow_rejections[:20]
                if isinstance(item, Mapping)
            ]
        duration_ms = execution.get("duration_ms")
        if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool) and math.isfinite(float(duration_ms)) and duration_ms >= 0:
            bounded_execution["duration_ms"] = round(float(duration_ms), 3)
        if bounded_execution:
            runtime["execution"] = bounded_execution
    if security_policy:
        runtime["security_policy"] = security_policy
    if provider_key.startswith("crawl4ai"):
        runtime["tls_runtime"] = tls_runtime
        fixture_join = payload.get("fixture_join")
        if isinstance(fixture_join, Mapping):
            runtime["fixture_join"] = {
                key: fixture_join[key]
                for key in (
                    "status",
                    "joined_count",
                    "unmatched_count",
                    "model_admission",
                    "admission_counts",
                )
                if key in fixture_join
            }
        # Page details are intentionally capped for the Sites payload, but
        # fan-out accounting must inspect the complete current-cycle page
        # list. Otherwise a large Crawl4AI run would silently drop sources
        # after the first 20 pages from its health/readiness counts.
        all_pages = [
            page
            for page in (payload.get(records_key) or [])
            if isinstance(page, Mapping)
        ]
        page_summaries = [
            {
                key: page.get(key)
                for key in (
                    "source",
                    "source_id",
                    "fact_source_id",
                    "url",
                    "final_url",
                    "capture_engine",
                    "capture_role",
                    "source_role",
                    "parser_contract",
                    "crawl_stage",
                    "parent_url",
                    "parent_content_sha256",
                    "follow_reason",
                    "status_code",
                    "content_type",
                    "content_size",
                    "raw_sha256",
                    "content_sha256",
                    "observed_at",
                    "effective_at",
                    "stale",
                    "fresh",
                    "source_state",
                    "stale_reason",
                    "stale_checked_at",
                    "robots",
                    "extraction",
                    "extracted_records",
                    "fixture_join",
                    "enters_model",
                    "model_exclusion_reason",
                )
                if key in page
            }
            for page in all_pages[:20]
        ]
        runtime["page_summaries"] = page_summaries
        # The browser edge currently emits an explicit ``enters_model`` flag
        # for every page and the adapter keeps that flag false.  When the
        # complete boolean ledger is present, expose the exact admission
        # count (normally zero) instead of leaving the dashboard with an
        # ambiguous null.  Older snapshots without the flag stay unknown and
        # are not silently converted to zero.
        entry_flags = [page.get("enters_model") for page in all_pages]
        if all(isinstance(value, bool) for value in entry_flags):
            runtime["model_eligible_count"] = sum(1 for value in entry_flags if value is True)
        # Crawl4AI is a shared capture runtime.  Make its fan-out explicit in
        # the read model so a page count can never be mistaken for a source
        # count, and so operators can see which declared fact-source IDs were
        # actually served by this runtime during the current cycle.
        if provider_key == "crawl4ai":
            source_counts: dict[str, int] = {}
            unidentified_page_count = 0
            for page in all_pages:
                identity = str(page.get("fact_source_id") or page.get("source_id") or "").strip()
                if not identity:
                    unidentified_page_count += 1
                    continue
                source_counts[identity] = source_counts.get(identity, 0) + 1
            declared_source_ids = sorted(source_counts)
            runtime["fanout"] = {
                "capture_engine": "Crawl4AI",
                "declared_source_count": len(declared_source_ids),
                "declared_source_ids": declared_source_ids,
                "pages_by_source": source_counts,
                "unidentified_page_count": unidentified_page_count,
                "rule": "one_runtime_to_many_declared_source_ids",
            }
            registry_rows = {
                str(row.get("id")): row
                for row in get_source_registry()
                if row.get("id")
            }
            runtime["execution_layer_count"] = 1
            runtime["fact_source_count"] = sum(
                1
                for source_id in declared_source_ids
                if source_id in registry_rows
                and registry_rows[source_id].get("role", "fact_source") != "fetch_runtime"
                and registry_rows[source_id].get("fact_source") is not False
            )
            runtime["fanout_source_count"] = len(declared_source_ids)
    return runtime


def build_runtime_source_registry(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Attach current-cycle health to each declarative source row.

    Disabled/quarantined/forbidden entries retain their policy status. The
    returned list is safe for the append-only snapshot and Sites display; it
    does not alter the source's model policy or eligibility.
    """

    rows: list[dict[str, Any]] = []
    for row in get_source_registry():
        item = deepcopy(row)
        source_id = str(item["id"])
        if source_id in _RUNTIME_BINDINGS:
            provider_key, records_key = _RUNTIME_BINDINGS[source_id]
            item["runtime"] = _provider_runtime(snapshot, provider_key, records_key)
        elif source_id == "football_data_historical":
            item["runtime"] = {
                "status": "training_only",
                "retrieved_at": None,
                "record_count": 0,
                "error_count": 0,
                "errors": [],
            }
        elif item.get("status") == "opt_in":
            item["runtime"] = {
                "status": "not_configured",
                "retrieved_at": None,
                "record_count": 0,
                "error_count": 0,
                "errors": [],
            }
        else:
            item["runtime"] = {
                "status": str(item.get("status") or "unavailable"),
                "retrieved_at": None,
                "record_count": 0,
                "error_count": 0,
                "errors": [],
            }
        rows.append(item)
    return rows


__all__ = ["build_runtime_source_registry", "get_source_registry", "validate_source_registry"]
