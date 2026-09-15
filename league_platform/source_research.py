"""Reproducible, fail-closed research inventory for every Matchline source.

This module is deliberately an *audit* layer, not a crawler.  It consumes the
already captured runtime snapshot and optional operator-supplied probe receipts;
it never opens a network connection.  A source with no observation is reported
as ``not_attempted``/``not_configured``/``rights_unverified`` with an explicit
reason rather than being treated as healthy or as a zero-valued source.

The report covers the union of the declarative source registry and the closed
``SourceId`` policy inventory.  That catches policy-only adapters which are not
yet visible in the current snapshot registry (for example historical or
future-disabled adapters).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from league_platform.source_registry import get_source_registry
from league_platform.source_rights import SourceId

SCHEMA_VERSION = "matchline.source_research.v1"
PROBE_SCHEMA_VERSION = "matchline.source_probe_evidence.v1"

REQUIRED_SOURCE_FIELDS = (
    "id",
    "name",
    "adapter",
    "hosts",
    "rights_basis",
    "terms_or_license_urls",
    "robots_and_access",
    "allowlist_redirect_and_response_limits",
    "rate_limit",
    "parser_contract",
    "field_coverage",
    "entity_join",
    "time_semantics",
    "raw_hash_policy",
    "observed_http",
    "failure_or_status",
    "alternatives",
    "eligibility",
)

_ALLOWED_OBSERVATION_STATES = frozenset(
    {"success", "failed", "not_attempted", "not_configured", "training_only", "not_observed"}
)
_BLOCKED_STATUSES = frozenset(
    {
        "blocked",
        "blocked_by_robots",
        "blocked_tls_policy",
        "forbidden",
        "rights_blocked",
        "quarantined",
        "future_disabled",
        "not_configured",
        "opt_in",
        "training_only",
    }
)

# SourceId values which have no independent current registry row still need a
# stable adapter/module identity in the audit.  These are descriptive paths,
# not dynamic imports and never authorize execution.
_ADAPTERS: dict[str, str] = {
    "openfootball_current": "league_platform.live_sources.openfootball_live",
    "openfootball_historical": "league_platform.sources.openfootball_verified",
    "openligadb_secondary_results": "league_platform.live_sources.openligadb",
    "cfl_official_current": "league_platform.live_sources.cfl_official",
    "espn_schedule_summary": "league_platform.live_sources.espn",
    "espn_market_summary": "league_platform.live_sources.espn_market",
    "espn_team_rosters": "league_platform.live_sources.espn_roster",
    "espn_injury_reports": "league_platform.live_sources.espn_injuries",
    "understat_xg": "league_platform.live_sources.understat",
    "official_league_lineups": "league_platform.live_sources.{premier_league,laliga,bundesliga,seriea,ligue1}",
    "official_premier_league_lineups": "league_platform.live_sources.premier_league",
    "official_laliga_lineups": "league_platform.live_sources.laliga",
    "official_bundesliga_lineups": "league_platform.live_sources.bundesliga",
    "official_serie_a_lineups": "league_platform.live_sources.seriea",
    "official_ligue1_lineups": "league_platform.live_sources.ligue1",
    "public_rss_news": "league_platform.live_sources.news",
    "open_meteo_geocoding": "league_platform.live_sources.geocoding",
    "open_meteo_weather": "league_platform.live_sources.open_meteo",
    "sofascore_prematch": "league_platform.live_sources.sofascore",
    "sports_lottery_official": "league_platform.live_sources.sports_lottery",
    "oddstorm_market_comparison": "league_platform.live_sources.oddstorm",
    "oddstorm_market_history": "league_platform.live_sources.oddstorm",
    "football_data_historical": "league_platform.catalog/data_manifest.json",
    "fbref_public_stats": "league_platform.live_sources.crawl4ai",
    "whoscored_public_pages": "league_platform.live_sources.crawl4ai",
    "premier_league_public_pages": "league_platform.live_sources.crawl4ai",
    "laliga_public_pages": "league_platform.live_sources.crawl4ai",
    "laliga_official_news": "league_platform.live_sources.crawl4ai",
    "bundesliga_public_pages": "league_platform.live_sources.crawl4ai",
    "seriea_public_pages": "league_platform.live_sources.crawl4ai",
    "ligue1_official_news": "league_platform.live_sources.crawl4ai",
    "clubelo_public_ratings": "league_platform.live_sources.crawl4ai",
    "sofifa_public_reference": "league_platform.live_sources.crawl4ai",
    "fotmob_public_api": "league_platform.live_sources.fotmob",
    "wikidata_entities": "league_platform.live_sources.wikidata",
    "met_norway_weather": "league_platform.live_sources.met_norway",
    "osm_geodata": "future adapter; no executable module in current tree",
    "pappalardo_wyscout_historical": "future adapter; no executable module in current tree",
    "five_hundred_league_public_pages": "league_platform.live_sources.five_hundred_league",
    "lazq_public_mirror": "league_platform.live_sources.lazq",
    "football_data_china_public_csv": "league_platform.sources.football_data_china",
    "sevenm_csl_public_fixture_script": "league_platform.sources.sevenm_csl",
    "checkbestodds_public_pages": "league_platform.sources.checkbestodds",
}

_HOSTS: dict[str, list[str]] = {
    "openfootball_current": ["raw.githubusercontent.com"],
    "openfootball_historical": ["raw.githubusercontent.com"],
    "openligadb_secondary_results": ["api.openligadb.de"],
    "cfl_official_current": ["api.cfl-china.cn"],
    "espn_schedule_summary": ["site.api.espn.com", "site.web.api.espn.com"],
    "espn_market_summary": ["site.api.espn.com"],
    "espn_team_rosters": ["site.web.api.espn.com"],
    "espn_injury_reports": ["site.api.espn.com", "site.web.api.espn.com"],
    "understat_xg": ["understat.com"],
    "official_league_lineups": [
        "premierleague.com",
        "laliga.com",
        "bundesliga.com",
        "api-sdp.legaseriea.it",
        "ma-api.ligue1.fr",
    ],
    "official_premier_league_lineups": ["premierleague.com"],
    "official_laliga_lineups": ["apim.laliga.com", "laliga.com"],
    "official_bundesliga_lineups": ["bundesliga.com"],
    "official_serie_a_lineups": ["api-sdp.legaseriea.it", "en.legaseriea.it"],
    "official_ligue1_lineups": ["ma-api.ligue1.fr", "ligue1.com"],
    "public_rss_news": ["feeds.bbci.co.uk", "www.skysports.com", "news.google.com"],
    "open_meteo_geocoding": ["geocoding-api.open-meteo.com"],
    "open_meteo_weather": ["api.open-meteo.com"],
    "sofascore_prematch": ["api.sofascore.com"],
    "sports_lottery_official": ["webapi.sporttery.cn"],
    "oddstorm_market_comparison": ["www.oddstorm.com"],
    "oddstorm_market_history": ["www.oddstorm.com"],
    "football_data_historical": ["www.football-data.co.uk"],
    "fbref_public_stats": ["fbref.com"],
    "whoscored_public_pages": ["www.whoscored.com"],
    "premier_league_public_pages": ["www.premierleague.com"],
    "laliga_public_pages": ["www.laliga.com"],
    "laliga_official_news": ["www.laliga.com"],
    "bundesliga_public_pages": ["www.bundesliga.com"],
    "seriea_public_pages": ["en.legaseriea.it"],
    "ligue1_official_news": ["ligue1.com"],
    "clubelo_public_ratings": ["www.clubelo.com", "api.clubelo.com"],
    "sofifa_public_reference": ["sofifa.com"],
    "fotmob_public_api": ["www.fotmob.com"],
    "wikidata_entities": ["www.wikidata.org"],
    "met_norway_weather": ["api.met.no"],
    "osm_geodata": ["www.openstreetmap.org", "nominatim.openstreetmap.org"],
    "pappalardo_wyscout_historical": ["figshare.com"],
    "five_hundred_league_public_pages": ["500league.com"],
    "lazq_public_mirror": ["api.lazq.com"],
    "football_data_china_public_csv": ["football-data.cn"],
    "sevenm_csl_public_fixture_script": ["data.7m.com.cn"],
    "checkbestodds_public_pages": ["checkbestodds.com"],
}

_TERMS: dict[str, list[str]] = {
    "openfootball_current": [
        "https://github.com/openfootball/football.json/blob/master/LICENSE.md",
        "https://github.com/openfootball/football.json",
    ],
    "openfootball_historical": [
        "https://github.com/openfootball/football.json/blob/master/LICENSE.md",
        "https://github.com/openfootball/football.json",
    ],
    "openligadb_secondary_results": [
        "https://www.openligadb.de/lizenz",
        "https://api.openligadb.de/index.html",
    ],
    "wikidata_entities": [
        "https://www.wikidata.org/wiki/Wikidata:Licensing",
        "https://www.wikidata.org/wiki/Wikidata:Data_access/en",
    ],
    "met_norway_weather": [
        "https://creativecommons.org/licenses/by/4.0/",
        "https://api.met.no/doc/TermsOfService",
    ],
    "open_meteo_geocoding": [
        "https://open-meteo.com/en/terms",
        "https://open-meteo.com/en/licence",
    ],
    "open_meteo_weather": [
        "https://open-meteo.com/en/terms",
        "https://open-meteo.com/en/licence",
    ],
    "football_data_historical": [
        "https://www.football-data.co.uk/data.php",
        "https://www.football-data.co.uk/downloadm.php",
    ],
    "espn_schedule_summary": ["https://disneytermsofuse.com/english/"],
    "espn_market_summary": ["https://disneytermsofuse.com/english/"],
    "espn_team_rosters": ["https://disneytermsofuse.com/english/"],
    "espn_injury_reports": ["https://disneytermsofuse.com/english/"],
    "oddstorm_market_comparison": ["https://www.oddstorm.com/terms"],
    "oddstorm_market_history": ["https://www.oddstorm.com/terms"],
    "fbref_public_stats": ["https://www.sports-reference.com/data_use.html"],
    "whoscored_public_pages": ["https://www.whoscored.com/termsofuse"],
    "premier_league_public_pages": [
        "https://www.premierleague.com/en/terms-and-conditions",
        "https://www.premierleague.com/robots.txt",
    ],
    "official_premier_league_lineups": [
        "https://www.premierleague.com/en/terms-and-conditions",
        "https://www.premierleague.com/robots.txt",
    ],
    "laliga_public_pages": ["https://www.laliga.com/en-GB/legal/legal-web"],
    "official_laliga_lineups": ["https://www.laliga.com/en-GB/legal/legal-web"],
    "laliga_official_news": ["https://www.laliga.com/en-GB/legal/legal-web"],
    "bundesliga_public_pages": [
        "https://www.bundesliga.com/en/bundesliga/info/legal-notices",
        "https://www.bundesliga.com/robots.txt",
    ],
    "official_bundesliga_lineups": [
        "https://www.bundesliga.com/en/bundesliga/info/legal-notices",
        "https://www.bundesliga.com/robots.txt",
    ],
    "seriea_public_pages": ["https://en.legaseriea.it/terms-and-conditions"],
    "official_serie_a_lineups": ["https://en.legaseriea.it/terms-and-conditions"],
    "ligue1_official_news": ["https://ligue1.com/en/legal/cgu"],
    "official_ligue1_lineups": ["https://ligue1.com/en/legal/cgu"],
    "sofifa_public_reference": ["https://sofifa.com/robots.txt"],
    "fotmob_public_api": ["https://www.fotmob.com/robots.txt"],
    "osm_geodata": [
        "https://opendatacommons.org/licenses/odbl/1-0/",
        "https://www.openstreetmap.org/copyright",
    ],
    "pappalardo_wyscout_historical": [
        "https://creativecommons.org/licenses/by/4.0/",
        "https://figshare.com/collections/Soccer_match_event_dataset/4415000",
    ],
}

_ROBOTS: dict[str, str] = {
    "openfootball_current": "fixed raw URLs; no robots traversal; same-host HTTPS only",
    "openfootball_historical": "fixed raw URLs; no robots traversal; same-host HTTPS only",
    "openligadb_secondary_results": "API allowlist; no redirect; no arbitrary endpoint discovery",
    "wikidata_entities": "Wikidata access guidance: identifying User-Agent, bounded concurrency, stop on 429",
    "met_norway_weather": "identified User-Agent, HTTPS, max four-decimal coordinates, cache/Expires, stop on 429",
    "open_meteo_geocoding": "HTTPS API allowlist; free-tier limits and commercial plan boundary apply",
    "open_meteo_weather": "HTTPS API allowlist; free-tier limits and commercial plan boundary apply",
    "sofifa_public_reference": "robots/API path blocked; no request is opened",
    "fotmob_public_api": "robots policy blocked; no request is opened",
    "bundesliga_public_pages": "robots/legal notice reserves text-and-data-mining rights; no automated fetch",
    "official_bundesliga_lineups": "robots/legal notice reserves text-and-data-mining rights; no automated fetch",
}

_LINEUP_IDS = frozenset(
    {
        "official_league_lineups",
        "official_premier_league_lineups",
        "official_laliga_lineups",
        "official_bundesliga_lineups",
        "official_serie_a_lineups",
        "official_ligue1_lineups",
    }
)
_OFFICIAL_PAGE_IDS = frozenset(
    {
        "premier_league_public_pages",
        "laliga_public_pages",
        "laliga_official_news",
        "bundesliga_public_pages",
        "seriea_public_pages",
        "ligue1_official_news",
    }
)
_ESPN_IDS = frozenset(
    {
        "espn_schedule_summary",
        "espn_market_summary",
        "espn_team_rosters",
        "espn_injury_reports",
    }
)
_ODDSTORM_IDS = frozenset({"oddstorm_market_comparison", "oddstorm_market_history"})
_ROBOTS_BLOCKED_IDS = frozenset({"sofifa_public_reference", "fotmob_public_api"})
_FUTURE_IDS = frozenset({"osm_geodata", "pappalardo_wyscout_historical"})
_NETWORK_ALLOWED_SOURCE_IDS = frozenset(
    {
        "openfootball_current",
        "openfootball_historical",
        "openligadb_secondary_results",
        "wikidata_entities",
        "met_norway_weather",
    }
)


def _registry_union() -> list[dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    for raw in get_source_registry():
        source_id = raw.get("id")
        if isinstance(source_id, str) and source_id.strip():
            rows_by_id[source_id] = dict(raw)
    for source_id in SourceId:
        value = source_id.value
        if value not in rows_by_id:
            rows_by_id[value] = {
                "id": value,
                "name": value,
                "adapter": _ADAPTERS.get(value, "no executable adapter declared"),
                "hosts": _HOSTS.get(value, []),
                "status": "policy_only",
                "access_policy": "policy inventory requires explicit adapter contract",
                "model_policy": "not eligible until explicit source contract passes",
            }
    return [rows_by_id[key] for key in sorted(rows_by_id)]
def _inventory_digest(source_ids: list[str]) -> str:
    canonical = json.dumps(source_ids, ensure_ascii=False, sort_keys=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _name(source_id: str, row: Mapping[str, Any]) -> str:
    value = row.get("name")
    if isinstance(value, str) and value.strip() and value.strip() != source_id:
        return value.strip()
    return source_id


def _rights_profile(source_id: str) -> tuple[str, str, dict[str, Any]]:
    if source_id in {"openfootball_current", "openfootball_historical"}:
        return (
            "verified_cc0_public_domain",
            "OpenFootball public-domain/CC0 data; only fixed source files and durable raw admission are eligible.",
            {"display": "conditional_after_admission", "model": "conditional_after_causal_gate", "publication": "conditional_after_maturity_gate"},
        )
    if source_id == "openligadb_secondary_results":
        return (
            "verified_odbl_isolated_display_current",
            "OpenLigaDB publishes an ODbL database/API; this project isolates it to exact-join current/post-match display and attribution.",
            {"display": "allowed_isolated", "model": "blocked", "publication": "blocked"},
        )
    if source_id == "wikidata_entities":
        return (
            "verified_cc0_structured_data",
            "Wikidata structured data is CC0; entity/venue use remains bounded and exact-join only.",
            {"display": "allowed_with_attribution_preference", "model": "conditional_exact_venue_chain", "publication": "conditional_with_provenance"},
        )
    if source_id == "met_norway_weather":
        return (
            "verified_cc_by_forecast_with_identifying_user_agent",
            "MET Norway publishes CC-BY weather with identifying User-Agent, attribution, cache and traffic obligations; forecast is not historical training data.",
            {"display": "allowed_with_attribution", "model": "conditional_exact_coordinates_and_time", "publication": "conditional_with_attribution"},
        )
    if source_id in {"open_meteo_geocoding", "open_meteo_weather"}:
        return (
            "free_tier_noncommercial_cc_by_only",
            "Open-Meteo free API is non-commercial under its terms; commercial use requires an appropriate subscription/contract.",
            {"display": "blocked_for_current_commercial_release", "model": "blocked_pending_commercial_plan", "publication": "blocked_pending_commercial_plan"},
        )
    if source_id in _ESPN_IDS:
        return (
            "provider_permission_required",
            "Disney/ESPN terms prohibit automated extraction and data collection without an express written permission path.",
            {"display": "blocked", "model": "blocked", "publication": "blocked"},
        )
    if source_id in _ODDSTORM_IDS:
        return (
            "provider_permission_required",
            "OddStorm terms prohibit automated scraping, mirroring, redistribution and resale absent express written permission.",
            {"display": "blocked", "model": "blocked", "publication": "blocked"},
        )
    if source_id in _ROBOTS_BLOCKED_IDS:
        return (
            "robots_policy_blocked",
            "The current policy records a robots/API restriction; no network request is opened until that policy changes.",
            {"display": "blocked", "model": "blocked", "publication": "blocked"},
        )
    if source_id in _FUTURE_IDS:
        return (
            "rights_metadata_present_adapter_contract_pending",
            "A possible license/terms reference exists, but the adapter, attribution and publication contract is not implemented in the current inventory.",
            {"display": "blocked_until_adapter_contract", "model": "blocked", "publication": "blocked"},
        )
    if source_id == "football_data_historical":
        return (
            "public_download_rights_not_stated_for_product_use",
            "The official site offers downloadable quantitative-testing files, but this inventory has no explicit commercial redistribution grant; keep training use isolated and re-verify before product use.",
            {"display": "blocked_current", "model": "blocked_current", "publication": "blocked_current"},
        )
    if source_id == "legacy_anti_waf_relay":
        return (
            "forbidden_access_control_bypass_artifact",
            "Legacy relay logic would bypass access controls and is represented only as a negative audit record.",
            {"display": "blocked", "model": "blocked", "publication": "blocked"},
        )
    if source_id == "crawl4ai_allowlisted_pages":
        return (
            "execution_layer_not_fact_source",
            "Crawl4AI is an execution layer; each page needs an independently verified source identity, rights and provenance.",
            {"display": "conditional_per_declared_page", "model": "blocked_by_default", "publication": "blocked_by_default"},
        )
    if source_id in _LINEUP_IDS or source_id in _OFFICIAL_PAGE_IDS:
        return (
            "first_party_access_reuse_terms_not_verified",
            "The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.",
            {"display": "blocked_pending_terms_review", "model": "blocked", "publication": "blocked"},
        )
    return (
        "rights_unverified",
        "No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.",
        {"display": "blocked_pending_rights_review", "model": "blocked", "publication": "blocked"},
    )


def _fields(source_id: str) -> list[str]:
    if source_id in {"openfootball_current", "openfootball_historical"}:
        return ["fixture_id", "provider_team_id", "competition", "season", "round", "kickoff_at", "score", "halftime_score"]
    if source_id == "openligadb_secondary_results":
        return ["provider_match_id", "provider_team_id", "kickoff_at", "finished_score", "halftime_score", "goals", "venue"]
    if source_id == "wikidata_entities":
        return ["team_entity_id", "preferred_venue_entity_id", "venue_name", "latitude", "longitude"]
    if source_id == "met_norway_weather":
        return ["forecast_at", "temperature_c", "humidity_percent", "wind_speed_mps", "precipitation_mm", "symbol_code"]
    if source_id == "open_meteo_geocoding":
        return ["place_query", "geocoded_coordinates", "provider_place_id", "retrieved_at"]
    if source_id == "open_meteo_weather":
        return ["fixture_id", "forecast_hour", "temperature", "precipitation", "wind", "retrieved_at"]
    if source_id in _LINEUP_IDS:
        return ["native_match_id", "team_id", "player_id", "shirt_number", "starter_or_bench", "observed_at"]
    if source_id in _ESPN_IDS:
        return ["native_event_or_team_id", "team", "player", "status", "score_or_market_fields", "observed_at"]
    if source_id in {"understat_xg", "fbref_public_stats", "clubelo_public_ratings"}:
        return ["team_or_match_id", "observed_at", "xg_or_rating", "source_payload_hash"]
    if source_id in {"sports_lottery_official", "oddstorm_market_comparison", "oddstorm_market_history", "lazq_public_mirror", "checkbestodds_public_pages"}:
        return ["native_match_id", "market", "line", "price", "observed_at", "settlement_rule"]
    if source_id == "public_rss_news" or source_id in {"laliga_official_news", "ligue1_official_news"}:
        return ["article_url", "title", "published_at", "team_or_fixture_reference", "content_hash"]
    if source_id in {"open_meteo_geocoding", "osm_geodata"}:
        return ["place_query", "coordinates", "provider_place_id", "retrieved_at"]
    if source_id in {"football_data_historical", "pappalardo_wyscout_historical", "football_data_china_public_csv", "five_hundred_league_public_pages", "sevenm_csl_public_fixture_script"}:
        return ["fixture_or_match_id", "teams", "kickoff_or_date", "score", "historical_market_fields"]
    if source_id == "crawl4ai_allowlisted_pages":
        return ["declared_source_id", "final_url", "parser_contract", "raw_hash", "observed_at"]
    return ["provider_native_id", "teams_or_entities", "observed_fields", "observed_at"]


def _entity_join(source_id: str) -> str:
    if source_id in {"openfootball_current", "openfootball_historical"}:
        return "canonical OpenFootball fixture ID; provider-scoped team IDs; exact competition/team/time; no fuzzy join"
    if source_id == "openligadb_secondary_results":
        return "unique exact Bundesliga competition + UTC kickoff + home/away team join; unmatched/conflict rows isolated"
    if source_id == "wikidata_entities":
        return "team name search -> one senior club entity -> preferred home venue -> valid coordinates; ties quarantined"
    if source_id == "met_norway_weather":
        return "requires already verified venue coordinates; no team-name or home-ground guessing"
    if source_id in _LINEUP_IDS:
        return "same competition + exact kickoff + explicit canonical home/away pair + native match ID; complete bilateral XI required"
    if source_id in _ESPN_IDS:
        return "provider-native event/team identity only; no name-only or cross-provider fuzzy join"
    if source_id in {"sports_lottery_official", "oddstorm_market_comparison", "oddstorm_market_history", "lazq_public_mirror", "checkbestodds_public_pages"}:
        return "exact competition + UTC kickoff + canonical home/away identity; low-confidence rows remain display-only"
    if source_id in {"public_rss_news", "laliga_official_news", "ligue1_official_news"}:
        return "explicit fixture/team reference and article timestamp; otherwise global source-health evidence only"
    if source_id == "crawl4ai_allowlisted_pages":
        return "declared source_id is mandatory; final URL must remain same-host allowlist; no aggregate page-to-source relabeling"
    return "provider-native identity plus exact competition/team/time where available; ambiguity quarantined"
def _time_semantics(source_id: str) -> str:
    if source_id in {"openfootball_current", "openfootball_historical", "openligadb_secondary_results"}:
        return "preserve effective_at when supplied and observed_at/retrieved_at; only exact timezone-aware kickoff rows enter causal fixture/result lanes"
    if source_id in _LINEUP_IDS or source_id in _ESPN_IDS:
        return "observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time"
    if source_id in {"wikidata_entities", "osm_geodata"}:
        return "retrieved_at is the observation clock; coordinates are not time-varying match facts and remain field-level provenance"
    if source_id == "open_meteo_geocoding":
        return "retrieved_at is the observation clock for place resolution; coordinates require exact query/response provenance"
    if source_id in {"met_norway_weather", "open_meteo_weather"}:
        return "forecast_at is the target hour; retrieved_at is observation time; forecast must be within provider horizon and before freeze"
    if source_id in {"sports_lottery_official", "oddstorm_market_comparison", "oddstorm_market_history", "lazq_public_mirror"}:
        return "quote observed_at and settlement line are mandatory; stale/history rows cannot become current odds"
    if source_id in {"football_data_historical", "pappalardo_wyscout_historical"}:
        return "historical source timestamp/season boundary is retained; rows cannot become current or future observations"
    return "observed_at is mandatory; missing effective time is explicitly non-model and non-publication"


def _parser_contract(source_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    parser = row.get("parser")
    if not isinstance(parser, str) or not parser.strip():
        parser = row.get("adapter") or _ADAPTERS.get(source_id) or "no parser contract declared"
    return {
        "name": str(parser),
        "input": "bounded bytes/JSON/CSV or explicitly allowlisted page",
        "validation": ["content type/size", "schema shape", "finite values", "provider identity", "timezone-aware clocks"],
        "failure": "isolate source and preserve structured error; never convert malformed payload to zero",
    }


def _limits(source_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    access_policy = row.get("access_policy")
    if source_id in {"openfootball_current", "openfootball_historical"}:
        return {
            "host_allowlist": _HOSTS.get(source_id, []),
            "path_policy": "fixed operator URL set only",
            "redirects": "reject",
            "max_response_bytes": 10 * 1024 * 1024,
            "timeout_seconds": 30,
            "concurrency": "bounded per host",
        }
    if source_id == "openligadb_secondary_results":
        return {
            "host_allowlist": _HOSTS[source_id],
            "path_policy": "/getmatchdata/bl1/<season> or /getmatchdata/<numeric-match-id>",
            "redirects": "reject",
            "max_response_bytes": 10 * 1024 * 1024,
            "match_max_response_bytes": 1024 * 1024,
            "timeout_seconds": 20,
            "concurrency": "bounded and lookback capped",
        }
    if source_id == "met_norway_weather":
        return {
            "host_allowlist": _HOSTS[source_id],
            "path_policy": "/weatherapi/locationforecast/2.0/compact only",
            "redirects": "reject through allowlist validation",
            "max_response_bytes": 5 * 1024 * 1024,
            "timeout_seconds": 30,
            "coordinates": "truncate to four decimals",
            "concurrency": "bounded; comply with 20 requests/second application ceiling",
        }
    if source_id == "wikidata_entities":
        return {
            "host_allowlist": _HOSTS[source_id],
            "path_policy": "/w/api.php only",
            "redirects": "reject",
            "max_response_bytes": 2 * 1024 * 1024,
            "timeout_seconds": 30,
            "max_requests_per_call": 96,
            "default_requests_per_call": 16,
            "concurrency": "serial bounded entity chain",
        }
    if source_id in {"open_meteo_geocoding", "open_meteo_weather"}:
        return {
            "host_allowlist": _HOSTS.get(source_id, []),
            "path_policy": "documented HTTPS API only",
            "redirects": "reject or fail closed",
            "max_response_bytes": 5 * 1024 * 1024,
            "timeout_seconds": 30,
            "free_tier_budget": "10,000/day; 5,000/hour; 600/minute per terms",
            "concurrency": "bounded and cached",
        }
    return {
        "host_allowlist": row.get("hosts") if isinstance(row.get("hosts"), list) else _HOSTS.get(source_id, []),
        "path_policy": str(access_policy or "explicit path contract required"),
        "redirects": "reject unless exact same-host redirect is explicitly contracted",
        "max_response_bytes": 2 * 1024 * 1024,
        "timeout_seconds": 45,
        "concurrency": "serial per host; bounded cross-host fan-out",
    }


def _rate_limit(source_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    polling = row.get("polling") if isinstance(row.get("polling"), Mapping) else {}
    normal = polling.get("normal_minutes")
    near = polling.get("near_kickoff_minutes")
    if source_id == "met_norway_weather":
        return {"normal_minutes": normal or 180, "near_kickoff_minutes": near or 30, "provider_rule": "<=20 req/s/application; honor Expires/Last-Modified; stop on 429"}
    if source_id == "wikidata_entities":
        return {"normal_minutes": normal or 360, "near_kickoff_minutes": near or 60, "provider_rule": "meaningful User-Agent; <=3 concurrent recommended; honor Retry-After"}
    if source_id in {"open_meteo_geocoding", "open_meteo_weather"}:
        return {"normal_minutes": normal or 180, "near_kickoff_minutes": near or 30, "provider_rule": "free-tier daily/hourly/minute budgets; cache repeated coordinates"}
    if source_id in _ESPN_IDS or source_id in _ODDSTORM_IDS or source_id in _ROBOTS_BLOCKED_IDS:
        return {"normal_minutes": 0, "near_kickoff_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}
    return {
        "normal_minutes": normal if isinstance(normal, int) and normal >= 0 else None,
        "near_kickoff_minutes": near if isinstance(near, int) and near >= 0 else None,
        "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm",
    }


def _runtime_aliases(source_id: str) -> tuple[str, ...]:
    aliases = {
        "openfootball_current": ("openfootball_current", "fixture_feed"),
        "openfootball_historical": (),
        "openligadb_secondary_results": ("openligadb",),
        "cfl_official_current": ("cfl_official",),
        "espn_schedule_summary": ("espn",),
        "espn_market_summary": ("espn_markets",),
        "espn_team_rosters": ("espn_rosters",),
        "espn_injury_reports": ("espn_injuries",),
        "understat_xg": ("understat",),
        "official_premier_league_lineups": ("premier_league_official",),
        "official_laliga_lineups": ("laliga_official",),
        "official_bundesliga_lineups": ("bundesliga_official",),
        "official_serie_a_lineups": ("serie_a_official",),
        "official_ligue1_lineups": ("ligue1_official",),
        "official_league_lineups": ("official_lineups",),
        "laliga_official_match_directory": ("laliga_official",),
        "public_rss_news": ("news",),
        "open_meteo_geocoding": ("geocoding",),
        "open_meteo_weather": ("weather",),
        "sofascore_prematch": ("sofascore",),
        "sports_lottery_official": ("sports_lottery",),
        "oddstorm_market_comparison": ("oddstorm",),
        "oddstorm_market_history": ("oddstorm",),
        "wikidata_entities": ("wikidata_entities",),
        "met_norway_weather": ("met_norway_weather",),
        "crawl4ai_allowlisted_pages": ("crawl4ai",),
        "premier_league_public_pages": ("crawl4ai_premier_league_public_pages",),
        "laliga_public_pages": ("crawl4ai_laliga_public_pages",),
        "laliga_official_news": ("crawl4ai_laliga_official_news",),
        "bundesliga_public_pages": ("crawl4ai_bundesliga_public_pages",),
        "seriea_public_pages": ("crawl4ai_seriea_public_pages",),
        "ligue1_official_news": ("crawl4ai_ligue1_official_news",),
        "whoscored_public_pages": ("crawl4ai_whoscored",),
        "fbref_public_stats": ("crawl4ai_fbref_public_stats",),
        "clubelo_public_ratings": ("crawl4ai_clubelo_public_ratings",),
        "sofifa_public_reference": ("crawl4ai_sofifa_public_reference",),
        "fotmob_public_api": ("fotmob",),
        "football_data_historical": (),
    }
    return aliases.get(source_id, (source_id,))


def _find_runtime(snapshot: Mapping[str, Any], source_id: str) -> tuple[Mapping[str, Any] | None, str | None]:
    for alias in _runtime_aliases(source_id):
        value = snapshot.get(alias)
        if isinstance(value, Mapping):
            return value, alias
    return None, None


def _collect_hash_stats(value: Any, *, limit: int = 8) -> dict[str, Any]:
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in {"raw_sha256", "content_sha256", "wire_sha256", "record_sha256"} and isinstance(child, str):
                    candidate = child.removeprefix("sha256:")
                    if len(candidate) == 64:
                        try:
                            int(candidate, 16)
                        except ValueError:
                            pass
                        else:
                            found.add(candidate)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    hashes = sorted(found)
    return {"hashes": hashes[:limit], "count": len(hashes), "truncated": len(hashes) > limit}


def _collect_hashes(value: Any, *, limit: int = 8) -> list[str]:
    return list(_collect_hash_stats(value, limit=limit)["hashes"])


def _collect_http_details(
    runtime: Mapping[str, Any] | None,
    probe: Mapping[str, Any] | None,
    *,
    source_id: str,
    runtime_key: str | None = None,
) -> dict[str, Any]:
    if isinstance(probe, Mapping):
        payload = dict(probe)
        payload.setdefault("source_id", source_id)
        payload.setdefault("status_codes", [])
        payload.setdefault("content_types", [])
        payload.setdefault("raw_hashes", [])
        payload.setdefault("raw_hash_count", len(payload["raw_hashes"]) if isinstance(payload["raw_hashes"], list) else 0)
        payload.setdefault("raw_hashes_truncated", False)
        payload.setdefault("evidence", ["operator_probe_evidence"])
        return payload
    if runtime is None:
        return {
            "attempted": False,
            "state": "not_observed",
            "status": "not_observed_for_this_snapshot",
            "network_opened": False,
            "status_codes": [],
            "content_types": [],
            "raw_hashes": [],
            "raw_hash_count": 0,
            "raw_hashes_truncated": False,
            "evidence": ["no runtime section for this policy-only source"],
        }
    status = str(runtime.get("status") or "not_observed_for_this_snapshot")
    explicit_network = runtime.get("network_opened")
    record_count = runtime.get("record_count")
    network_opened = (
        explicit_network
        if isinstance(explicit_network, bool)
        else status in {"fresh", "ok", "degraded", "stale"}
        and (isinstance(record_count, int) and record_count > 0 or runtime.get("retrieved_at") is not None)
    )
    errors = runtime.get("errors") if isinstance(runtime.get("errors"), list) else []
    status_codes: set[int] = set()
    content_types: set[str] = set()
    for page in runtime.get("page_summaries", []) if isinstance(runtime.get("page_summaries"), list) else []:
        if not isinstance(page, Mapping):
            continue
        code = page.get("status_code")
        if isinstance(code, int) and not isinstance(code, bool):
            status_codes.add(code)
        content_type = page.get("content_type")
        if isinstance(content_type, str) and content_type.strip():
            content_types.add(content_type.strip())
    if network_opened or status in {"fresh", "ok", "degraded", "stale", "outside_horizon"}:
        state = "success" if status in {"fresh", "ok", "outside_horizon"} and not errors else "failed"
        attempted = True
    elif status == "training_only":
        state = "training_only"
        attempted = False
    elif status in {"not_configured", "opt_in"}:
        state = "not_configured"
        attempted = False
    elif status in _BLOCKED_STATUSES or status.startswith("blocked"):
        state = "not_attempted"
        attempted = False
    elif not network_opened and status in {"unavailable", "blocked_storage"}:
        state = "not_attempted"
        attempted = False
    else:
        state = "failed" if errors else "not_observed"
        attempted = network_opened
    hash_stats = _collect_hash_stats(runtime)
    return {
        "attempted": attempted,
        "state": state,
        "status": status,
        "network_opened": network_opened,
        "status_codes": sorted(status_codes),
        "content_types": sorted(content_types),
        "content_type_observation": "adapter_exported_headers" if content_types else "not_recorded_by_adapter",
        "raw_hashes": hash_stats["hashes"],
        "raw_hash_count": hash_stats["count"],
        "raw_hashes_truncated": hash_stats["truncated"],
        "record_count": record_count if attempted and isinstance(record_count, int) else None,
        "reported_record_count": record_count if isinstance(record_count, int) else None,
        "error_count": runtime.get("error_count") if isinstance(runtime.get("error_count"), int) else len(errors),
        "errors": [dict(item) for item in errors[:5] if isinstance(item, Mapping)],
        "evidence": [f"snapshot.runtime.{runtime_key or source_id}"],
    }




def _status(runtime: Mapping[str, Any] | None, profile: str, source_id: str) -> tuple[str, str]:
    if runtime is None:
        if source_id in _FUTURE_IDS:
            return "future_disabled", "policy inventory has no executable adapter/attribution contract"
        if source_id == "openfootball_historical":
            return "training_only", "historical CC0 source is consumed through verified archive training lane"
        if source_id == "football_data_historical":
            return "training_only", "historical download is not a current runtime source"
        return "not_observed", "no source section was present in this runtime snapshot"
    raw_status = str(runtime.get("status") or "not_observed")
    if profile.startswith("verified_") and raw_status in {"ok", "fresh"}:
        return "fresh", "runtime snapshot contains an admitted source payload"
    if raw_status in _BLOCKED_STATUSES or raw_status.startswith("blocked"):
        return raw_status, "runtime preserved an explicit policy/quarantine block"
    if raw_status in {"not_configured", "opt_in"}:
        return "not_configured", "operator did not enable this source in the captured cycle"
    if raw_status == "training_only":
        return "training_only", "source is intentionally isolated from current serving"
    if raw_status in {"ok", "fresh"}:
        return "fresh", "runtime source payload was captured"
    if raw_status in {"degraded", "unavailable", "stale", "outside_horizon", "observed_empty"}:
        return raw_status, "runtime payload or diagnostics are present; see errors and coverage"
    return raw_status, "runtime reported an explicit non-success state"
def _parse_iso(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    return parsed.astimezone(timezone.utc)


def _probe_map(probe_evidence: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(probe_evidence, Mapping):
        return {}
    if probe_evidence.get("schema_version") != PROBE_SCHEMA_VERSION:
        raise ValueError("probe evidence schema version is invalid")
    rows = probe_evidence.get("probes")
    if not isinstance(rows, list):
        raise ValueError("probe evidence probes must be a list")
    allowed_ids = set(str(row["id"]) for row in _registry_union())
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("probe evidence row must be an object")
        source_id = row.get("source_id")
        if not isinstance(source_id, str) or source_id not in allowed_ids:
            raise ValueError(f"probe evidence source is not registered: {source_id}")
        if source_id in result:
            raise ValueError(f"duplicate probe evidence source: {source_id}")
        state = row.get("state")
        if state not in _ALLOWED_OBSERVATION_STATES:
            raise ValueError(f"probe evidence state is invalid: {source_id}")
        for field in ("attempted", "network_opened"):
            if not isinstance(row.get(field), bool):
                raise ValueError(f"probe evidence {field} must be bool: {source_id}")
        attempted = row.get("attempted")
        network_opened = row.get("network_opened")
        if attempted is True and network_opened is not True:
            raise ValueError(f"probe attempted without an opened network: {source_id}")
        if network_opened is True and attempted is not True:
            raise ValueError(f"probe opened a network without an attempt: {source_id}")
        if state in {"success", "failed"} and not (attempted and network_opened):
            raise ValueError(f"probe outcome lacks a real attempt: {source_id}")
        if row.get("network_opened") is True and source_id not in _NETWORK_ALLOWED_SOURCE_IDS:
            raise ValueError(f"probe evidence opens a blocked source: {source_id}")
        codes = row.get("status_codes", [])
        if not isinstance(codes, list) or any(isinstance(code, bool) or not isinstance(code, int) or code < 100 or code > 599 for code in codes):
            raise ValueError(f"probe evidence status_codes are invalid: {source_id}")
        hashes = row.get("raw_hashes", [])
        if not isinstance(hashes, list):
            raise ValueError(f"probe evidence raw_hashes must be a list: {source_id}")
        for digest in hashes:
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError(f"probe evidence hash is invalid: {source_id}")
            try:
                int(digest, 16)
            except ValueError as exc:
                raise ValueError(f"probe evidence hash is invalid: {source_id}") from exc
        for field in ("request_started_at", "response_observed_at", "observed_at"):
            if field in row:
                _parse_iso(row[field], field=f"probe.{source_id}.{field}")
        if "request_started_at" in row and "response_observed_at" in row and _parse_iso(row["response_observed_at"], field="probe.response_observed_at") < _parse_iso(row["request_started_at"], field="probe.request_started_at"):
            raise ValueError(f"probe response precedes request: {source_id}")
        result[source_id] = row
    return result
def _available_keys(value: Any) -> set[str]:
    keys: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                keys.add(str(key).casefold())
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return keys


def _observed_fields(
    source_id: str,
    raw_payload: Mapping[str, Any] | None,
    probe: Mapping[str, Any] | None,
    observed_http: Mapping[str, Any],
) -> dict[str, Any]:
    declared = _fields(source_id)
    if observed_http.get("state") != "success":
        return {
            "declared": declared,
            "observed": [],
            "missing": declared,
            "status": "not_observed",
            "missing_reason": "source response was not admitted as a successful observation",
        }
    available = _available_keys(raw_payload)
    available.update(_available_keys(probe))
    aliases: dict[str, set[str]] = {
        "fixture_id": {"fixture_id", "id"},
        "provider_match_id": {"provider_match_id", "matchid", "match_id"},
        "provider_team_id": {"provider_team_id", "team_id", "team1", "team2"},
        "competition": {"competition", "competition_id", "league", "leaguename"},
        "season": {"season", "leagueseason"},
        "round": {"round", "roundname", "grouporderid"},
        "kickoff_at": {"kickoff_at", "matchdatetimeutc", "matchdatetime"},
        "score": {"score", "finished_score", "matchresults", "pointsteam1", "pointsteam2"},
        "finished_score": {"score", "finished_score", "matchresults"},
        "halftime_score": {"halftime_score", "resulttypeid"},
        "goals": {"goals", "goal"},
        "venue": {"venue", "location", "stadium"},
        "team_entity_id": {"team_entity_id", "team_wikidata_id", "team_entity"},
        "preferred_venue_entity_id": {"preferred_venue_entity_id", "venue_wikidata_id", "venue_entities"},
        "venue_name": {"venue_name", "stadium", "name"},
        "latitude": {"latitude", "lat"},
        "longitude": {"longitude", "lon"},
        "forecast_at": {"forecast_at", "forecast_hour"},
        "temperature_c": {"temperature_c", "air_temperature", "temperature"},
        "humidity_percent": {"humidity_percent", "relative_humidity"},
        "wind_speed_mps": {"wind_speed_mps", "wind_speed"},
        "precipitation_mm": {"precipitation_mm", "precipitation_amount", "precipitation"},
        "symbol_code": {"symbol_code"},
        "source_payload_hash": {"source_payload_hash", "raw_sha256", "content_sha256", "wire_sha256", "raw_hashes"},
        "observed_at": {"observed_at", "retrieved_at", "response_observed_at"},
        "native_match_id": {"native_match_id", "match_id", "matchid", "provider_match_id"},
        "team": {"team", "home_team", "away_team", "team_name"},
        "player": {"player", "player_id", "player_name"},
        "status": {"status"},
        "market": {"market", "markets", "odds", "lines"},
        "line": {"line", "handicap_line", "total_over_under_line"},
        "price": {"price", "odds"},
        "settlement_rule": {"settlement_rule", "result_scope"},
        "article_url": {"article_url", "url", "link"},
        "title": {"title", "headline"},
        "published_at": {"published_at", "published", "effective_at"},
        "team_or_fixture_reference": {"team_or_fixture_reference", "fixture_id", "team"},
        "content_hash": {"content_hash", "raw_sha256", "content_sha256"},
        "place_query": {"place_query", "query", "team_name"},
        "geocoded_coordinates": {"coordinates", "latitude", "longitude"},
        "provider_place_id": {"provider_place_id", "wikidata_id", "location_id"},
        "retrieved_at": {"retrieved_at", "response_observed_at"},
        "declared_source_id": {"declared_source_id", "source_id", "fact_source_id"},
        "final_url": {"final_url", "url"},
        "parser_contract": {"parser_contract", "parser"},
        "raw_hash": {"raw_hash", "raw_sha256", "content_sha256"},
        "native_event_or_team_id": {"native_event_or_team_id", "event_id", "team_id"},
        "score_or_market_fields": {"score_or_market_fields", "score", "market", "markets"},
        "team_or_match_id": {"team_or_match_id", "team_id", "match_id", "fixture_id"},
        "xg_or_rating": {"xg_or_rating", "xg", "rating"},
        "historical_market_fields": {"historical_market_fields", "market", "odds"},
        "fixture_or_match_id": {"fixture_or_match_id", "fixture_id", "match_id"},
        "teams": {"teams", "home_team", "away_team", "team1", "team2"},
        "kickoff_or_date": {"kickoff_or_date", "kickoff_at", "date", "matchdatetime"},
        "coordinates": {"coordinates", "latitude", "longitude"},
        "provider_native_id": {"provider_native_id", "provider_match_id", "match_id", "fixture_id"},
        "teams_or_entities": {"teams_or_entities", "team", "team_name", "entity"},
        "observed_fields": {"observed_fields", "fields"},
        "player_id": {"player_id", "player"},
        "shirt_number": {"shirt_number", "shirtno", "number"},
        "starter_or_bench": {"starter_or_bench", "starter", "bench"},
    }
    observed = sorted(field for field in declared if available & aliases.get(field, {field.casefold()}))
    return {
        "declared": declared,
        "observed": observed,
        "missing": [field for field in declared if field not in observed],
        "status": "partial" if len(observed) < len(declared) else "complete",
        "available_key_count": len(available),
    }


def build_source_research_report(
    snapshot: Mapping[str, Any] | None = None,
    *,
    snapshot_sha256: str | None = None,
    snapshot_path: str | None = None,
    observed_at: str,
    probe_evidence: Mapping[str, Any] | None = None,
    probe_evidence_path: str | None = None,
    probe_evidence_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic report from a captured snapshot and probe receipts.

    ``observed_at`` is mandatory so a caller cannot accidentally bake the wall
    clock into an otherwise reproducible report.  The function never reads or
    writes a URL and never imports an adapter dynamically.
    """

    if not isinstance(observed_at, str) or not observed_at.strip():
        raise ValueError("observed_at must be explicitly supplied")
    rows = _registry_union()
    by_registry = {str(row["id"]): row for row in rows}
    source_ids = sorted(by_registry)
    runtime_registry: dict[str, Mapping[str, Any]] = {}
    if isinstance(snapshot, Mapping):
        raw_registry = snapshot.get("source_registry")
        if isinstance(raw_registry, list):
            for row in raw_registry:
                if isinstance(row, Mapping) and isinstance(row.get("id"), str):
                    runtime_registry[str(row["id"])] = row
    probes = _probe_map(probe_evidence)
    source_reports: list[dict[str, Any]] = []
    for source_id in source_ids:
        row = by_registry[source_id]
        profile, rights_basis, eligibility = _rights_profile(source_id)
        raw_runtime, runtime_key = _find_runtime(snapshot or {}, source_id)
        runtime = raw_runtime
        # Keep the raw provider section for hashes and payload evidence even
        # when the compact runtime registry has intentionally dropped detail.
        raw_source_payload = raw_runtime
        derived_runtime = runtime_registry.get(source_id)
        if isinstance(derived_runtime, Mapping) and isinstance(derived_runtime.get("runtime"), Mapping):
            runtime = derived_runtime["runtime"]
        state, state_reason = _status(runtime, profile, source_id)
        probe = probes.get(source_id)
        if isinstance(probe, Mapping) and (
            probe.get("network_opened") is True or probe.get("attempted") is True
        ) and source_id not in _NETWORK_ALLOWED_SOURCE_IDS:
            raise ValueError(f"probe attempted for source without network rights: {source_id}")
        observed_http = _collect_http_details(
            runtime,
            probe,
            source_id=source_id,
            runtime_key=runtime_key,
        )
        raw_payload_hashes = _collect_hash_stats(raw_source_payload)
        merged_hashes = set(observed_http.get("raw_hashes") or []) | set(raw_payload_hashes["hashes"])
        observed_http["raw_hashes"] = sorted(merged_hashes)[:8]
        observed_http["raw_hash_count"] = max(
            int(observed_http.get("raw_hash_count") or 0),
            int(raw_payload_hashes["count"]),
            len(merged_hashes),
        )
        observed_http["raw_hashes_truncated"] = bool(
            observed_http.get("raw_hashes_truncated")
            or raw_payload_hashes["truncated"]
            or len(merged_hashes) > 8
        )
        # A dated operator probe is a separate observation from the current
        # snapshot. Prefer its explicit outcome for the report's observed
        # HTTP state, while retaining the snapshot runtime key and status in
        # the evidence payload.
        if isinstance(probe, Mapping):
            probe_state = str(probe.get("state") or "")
            if probe_state == "success":
                state, state_reason = "fresh", "dated operator probe admitted a valid response"
            elif probe_state == "failed":
                state, state_reason = "unavailable", "dated operator probe reached the source but parser/contract admission failed"
        if state in {"rights_blocked", "forbidden", "blocked_by_robots", "quarantined", "future_disabled"} or state.startswith("blocked"):
            observed_http.setdefault("attempted", False)
            if observed_http.get("network_opened") is True and not isinstance(probe, Mapping):
                raise ValueError(f"blocked source has network_opened=true: {source_id}")
        terms = list(_TERMS.get(source_id, []))
        row_terms = row.get("terms_url")
        row_license = row.get("license_url")
        for value in (row_terms, row_license):
            if isinstance(value, str) and value.strip() and value.strip() not in terms:
                terms.append(value.strip())
        hosts = row.get("hosts") if isinstance(row.get("hosts"), list) else _HOSTS.get(source_id, [])
        source_report = {
            "id": source_id,
            "name": _name(source_id, row),
            "adapter": str(row.get("adapter") or _ADAPTERS.get(source_id) or "no executable adapter declared"),
            "hosts": sorted({str(value) for value in hosts if isinstance(value, str) and value.strip()}),
            "rights_basis": {
                "classification": profile,
                "conclusion": rights_basis,
                "license_or_terms_reviewed": terms,
            },
            "terms_or_license_urls": sorted(set(terms)),
            "robots_and_access": {
                "policy": _ROBOTS.get(source_id, "source-specific robots/access proof is not independently archived; central rights gate applies before network"),
                "access_policy": str(row.get("access_policy") or "explicit source contract required"),
                "network_decision": "not_attempted" if not observed_http.get("attempted") else "attempted_under_declared_policy",
            },
            "allowlist_redirect_and_response_limits": _limits(source_id, row),
            "rate_limit": _rate_limit(source_id, row),
            "parser_contract": _parser_contract(source_id, row),
            "field_coverage": _observed_fields(source_id, raw_source_payload, probe, observed_http),
            "entity_join": _entity_join(source_id),
            "time_semantics": _time_semantics(source_id),
            "raw_hash_policy": {
                "required": True,
                "algorithm": "SHA-256",
                "deduplicate": "same raw bytes deduplicated; conflicting payloads retained",
                "observed_hashes": observed_http.get("raw_hashes", []),
                "observed_hash_count": observed_http.get("raw_hash_count", 0),
                "observed_hashes_truncated": observed_http.get("raw_hashes_truncated", False),
                "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash",
            },
            "observed_http": observed_http,
            "failure_or_status": {
                "state": state,
                "reason": state_reason,
                "runtime_key": runtime_key,
                "runtime_error_count": observed_http.get("error_count", 0),
                "runtime_errors": observed_http.get("errors", []),
            },
            "alternatives": _alternatives(source_id),
            "eligibility": eligibility,
        }
        source_reports.append(source_report)

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": observed_at,
        "snapshot": {
            "path": snapshot_path,
            "as_of": snapshot.get("as_of") if isinstance(snapshot, Mapping) else None,
            "sha256": snapshot_sha256,
            "source_registry_count": len(runtime_registry),
        },
        "source_inventory": {
            "schema_version": "matchline.source_inventory.v1",
            "ids_sha256": _inventory_digest(source_ids),
            "ids": source_ids,
        },
        "probe_evidence": {
            "schema_version": PROBE_SCHEMA_VERSION,
            "path": probe_evidence_path,
            "sha256": probe_evidence_sha256,
            "row_count": len(probes),
        },
        "source_id_count": len(source_reports),
        "source_ids": source_ids,
        "sources": source_reports,
        "method": {
            "network_performed_by_report_generator": False,
            "source_inventory": "registry_union_SourceId",
            "missing_values": "explicit non-success semantics; no false zero or fresh inference",
            "probe_evidence_schema": PROBE_SCHEMA_VERSION,
        }
    }
    validate_source_research_report(report)
    return report


def _alternatives(source_id: str) -> list[str]:
    if source_id in {"openfootball_current", "openfootball_historical"}:
        return ["OpenFootball fixed CC0 source files are the canonical schedule/result and historical fallback"]
    if source_id == "openligadb_secondary_results":
        return ["OpenFootball exact-kickoff canonical result; OpenLigaDB remains isolated display/post-match cross-check"]
    if source_id in {"wikidata_entities", "met_norway_weather"}:
        return ["Use the other permitted CC0/CC-BY lane only for its own fields; keep missing venue/weather explicit"]
    if source_id in {"open_meteo_geocoding", "open_meteo_weather"}:
        return ["MET Norway with attribution and exact coordinates; self-hosted/licensed Open-Meteo plan for commercial use"]
    if source_id in _LINEUP_IDS:
        return ["Operator-provided licensed official feed; until then no lineup confirmation and no inferred XI"]
    if source_id in _ESPN_IDS:
        return ["Licensed sports-data provider or first-party permission; OpenFootball remains fixture/result authority"]
    if source_id in _ODDSTORM_IDS or source_id in {"sports_lottery_official", "lazq_public_mirror", "checkbestodds_public_pages"}:
        return ["Licensed market feed with explicit redistribution/settlement rights; otherwise market baseline stays unavailable"]
    if source_id in _ROBOTS_BLOCKED_IDS or source_id in {"fbref_public_stats", "whoscored_public_pages", "clubelo_public_ratings", "understat_xg"}:
        return ["Use OpenFootball historical/current facts or obtain provider permission; do not bypass robots/WAF/TLS policy"]
    if source_id == "legacy_anti_waf_relay":
        return ["Remove dependency and use an explicitly licensed/allowlisted provider"]
    return ["OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable"]


def _contains_forbidden_unknown(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        return normalized == "unknown" or normalized.startswith("unknown_")
    if isinstance(value, Mapping):
        return any(_contains_forbidden_unknown(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_unknown(child) for child in value)
    return False


def _valid_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def validate_source_research_report(report: Mapping[str, Any]) -> None:
    """Validate the audit contract and fail closed on unsafe observations."""

    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("source research schema version is invalid")
    generated_at = _parse_iso(report.get("generated_at"), field="generated_at")
    if _contains_forbidden_unknown(report):
        raise ValueError("source research contains an unclassified unknown value")
    source_rows = report.get("sources")
    if not isinstance(source_rows, list) or not source_rows:
        raise ValueError("source research sources must be a non-empty list")
    expected = sorted({str(row["id"]) for row in _registry_union()})
    declared_ids = report.get("source_ids")
    if declared_ids != expected:
        raise ValueError("source research source_ids do not match the live inventory")
    actual = sorted(str(row.get("id")) for row in source_rows if isinstance(row, Mapping))
    if actual != expected or len(actual) != len(set(actual)):
        raise ValueError("source research must cover the registry/SourceId union exactly once")
    if report.get("source_id_count") != len(expected):
        raise ValueError("source_id_count does not match source rows")
    inventory = report.get("source_inventory")
    if not isinstance(inventory, Mapping) or inventory.get("schema_version") != "matchline.source_inventory.v1":
        raise ValueError("source inventory metadata is missing")
    if inventory.get("ids") != expected or inventory.get("ids_sha256") != _inventory_digest(expected):
        raise ValueError("source inventory digest does not match source rows")
    snapshot = report.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot metadata is missing")
    snapshot_as_of = snapshot.get("as_of")
    snapshot_time = _parse_iso(snapshot_as_of, field="snapshot.as_of") if snapshot_as_of is not None else None
    if snapshot_time is not None and snapshot_time > generated_at:
        raise ValueError("snapshot as_of is newer than report generated_at")
    snapshot_path = snapshot.get("path")
    if snapshot_path is not None and not isinstance(snapshot_path, str):
        raise ValueError("snapshot path must be a string")
    if snapshot_path is not None and not _valid_digest(snapshot_sha := snapshot.get("sha256")):
        raise ValueError("snapshot path requires a valid snapshot SHA-256")
    if isinstance(snapshot_path, str):
        if not Path(snapshot_path).is_file() or hashlib.sha256(Path(snapshot_path).read_bytes()).hexdigest() != snapshot_sha:
            raise ValueError("snapshot file is missing or bytes do not match snapshot SHA-256")
    probe_meta = report.get("probe_evidence")
    if not isinstance(probe_meta, Mapping) or probe_meta.get("schema_version") != PROBE_SCHEMA_VERSION:
        raise ValueError("probe evidence metadata is missing")
    probe_path = probe_meta.get("path")
    if probe_path is not None and not isinstance(probe_path, str):
        raise ValueError("probe evidence path must be a string")
    if probe_path is not None and not _valid_digest(probe_meta.get("sha256")):
        raise ValueError("probe evidence path requires a valid SHA-256")
    if isinstance(probe_path, str):
        if not Path(probe_path).is_file() or hashlib.sha256(Path(probe_path).read_bytes()).hexdigest() != probe_meta.get("sha256"):
            raise ValueError("probe evidence file is missing or bytes do not match probe SHA-256")
    if not isinstance(probe_meta.get("row_count"), int) or probe_meta["row_count"] < 0:
        raise ValueError("probe evidence row_count is invalid")
    for row in source_rows:
        if not isinstance(row, Mapping):
            raise ValueError("source research row must be an object")
        missing = [key for key in REQUIRED_SOURCE_FIELDS if key not in row]
        if missing:
            raise ValueError(f"source research row missing field: {missing[0]}")
        source_id = row.get("id")
        observed = row.get("observed_http")
        status_meta = row.get("failure_or_status")
        if not isinstance(observed, Mapping) or observed.get("state") not in _ALLOWED_OBSERVATION_STATES:
            raise ValueError(f"source research HTTP observation state is invalid: {source_id}")
        if not isinstance(status_meta, Mapping) or not isinstance(status_meta.get("state"), str):
            raise ValueError(f"source research status metadata is invalid: {source_id}")
        network_opened = observed.get("network_opened")
        attempted = observed.get("attempted")
        if not isinstance(network_opened, bool) or not isinstance(attempted, bool):
            raise ValueError(f"source research network/attempt flags must be bool: {source_id}")
        if attempted and not network_opened:
            raise ValueError(f"source research attempted without opened network: {source_id}")
        if network_opened and not attempted:
            raise ValueError(f"source research opened network without attempt: {source_id}")
        status = str(status_meta["state"])
        if (status in _BLOCKED_STATUSES or status.startswith("blocked")) and network_opened:
            raise ValueError(f"blocked source claims an opened network: {source_id}")
        if network_opened and source_id not in _NETWORK_ALLOWED_SOURCE_IDS:
            raise ValueError(f"source outside allowed network inventory was opened: {source_id}")
        if observed["state"] in {"success", "failed"} and not (attempted and network_opened):
            raise ValueError(f"source HTTP outcome lacks a real attempt: {source_id}")
        if not attempted and observed.get("record_count") is not None:
            raise ValueError(f"unattempted source claims an observed record count: {source_id}")
        coverage = row.get("field_coverage")
        if not isinstance(coverage, Mapping):
            raise ValueError(f"source field coverage is missing: {source_id}")
        declared = coverage.get("declared")
        observed_fields = coverage.get("observed")
        missing_fields = coverage.get("missing")
        if not all(isinstance(value, list) for value in (declared, observed_fields, missing_fields)):
            raise ValueError(f"source field coverage lists are invalid: {source_id}")
        if not isinstance(source_id, str) or declared != _fields(source_id):
            raise ValueError(f"source field declaration does not match adapter contract: {source_id}")
        if any(not isinstance(value, str) for value in (*declared, *observed_fields, *missing_fields)):
            raise ValueError(f"source field coverage values are invalid: {source_id}")
        if (
            len(set(declared)) != len(declared)
            or len(set(observed_fields)) != len(observed_fields)
            or len(set(missing_fields)) != len(missing_fields)
            or set(declared) != set(observed_fields) | set(missing_fields)
            or set(observed_fields) & set(missing_fields)
        ):
            raise ValueError(f"source field coverage is inconsistent: {source_id}")
        eligibility = row.get("eligibility")
        if not isinstance(eligibility, Mapping) or set(eligibility) != {"display", "model", "publication"}:
            raise ValueError(f"source research eligibility is incomplete: {source_id}")
        hashes_meta = row.get("raw_hash_policy")
        hashes = hashes_meta.get("observed_hashes") if isinstance(hashes_meta, Mapping) else None
        if not isinstance(hashes_meta, Mapping) or not isinstance(hashes, list):
            raise ValueError(f"source research observed hashes are missing: {source_id}")
        if not isinstance(hashes_meta.get("observed_hash_count"), int) or hashes_meta["observed_hash_count"] < len(hashes):
            raise ValueError(f"source research observed hash count is invalid: {source_id}")
        if not isinstance(hashes_meta.get("observed_hashes_truncated"), bool):
            raise ValueError(f"source research hash truncation flag is invalid: {source_id}")
        for digest in hashes:
            if not _valid_digest(digest):
                raise ValueError(f"source research hash is invalid: {source_id}")
        for field in ("request_started_at", "response_observed_at", "observed_at"):
            if field in observed:
                timestamp = _parse_iso(observed[field], field=f"source.{source_id}.observed_http.{field}")
                if timestamp > generated_at:
                    raise ValueError(f"probe is newer than report generated_at: {source_id}")
        if "request_started_at" in observed and "response_observed_at" in observed and _parse_iso(observed["response_observed_at"], field="probe.response_observed_at") < _parse_iso(observed["request_started_at"], field="probe.request_started_at"):
            raise ValueError(f"probe response precedes request: {source_id}")


def load_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("snapshot must be a JSON object")
    return decoded, digest


def load_probe_evidence(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict) or decoded.get("schema_version") != PROBE_SCHEMA_VERSION:
        raise ValueError("probe evidence schema version is invalid")
    return decoded


def render_markdown(report: Mapping[str, Any]) -> str:
    validate_source_research_report(report)
    snapshot = report.get("snapshot") if isinstance(report.get("snapshot"), Mapping) else {}
    report_date = datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00")).date().isoformat()
    lines = [
        f"# Matchline 逐源抓取可行性审计（{report_date}）",
        "",
        "> 本报告由 `league_platform.source_research` 从 registry 与 `SourceId` 并集生成。报告生成器不联网；所有无证据项均保留为明确的未尝试、未配置、权利未验证或隔离状态。",
        "",
        f"- Schema：`{report.get('schema_version')}`",
        f"- 生成时间：`{report.get('generated_at')}`（由调用方显式传入）",
        f"- 快照 `as_of`：`{snapshot.get('as_of')}`",
        f"- 快照 SHA-256：`{snapshot.get('sha256')}`",
        f"- 覆盖来源数：`{report.get('source_id_count')}`",
        "- 合规边界：不绕过 robots、WAF、验证码、登录、TLS、速率限制或授权；受限来源不进入模型/正式发布。",
        "",
        "## 判定字段",
        "",
        "每行都包含权利依据、terms/license URL、robots/access、allowlist/redirect/size/timeout、限速、解析、字段、实体联结、双时钟、SHA-256、实际 HTTP/运行状态、替代方案和 display/model/publication eligibility。`not_recorded_by_adapter` 表示适配器没有导出该 HTTP header，不表示响应成功。",
        "",
        "| 来源 | 权利结论 | HTTP/运行状态 | 网络 | 记录数 | display | model | publication |",
        "|---|---|---|---:|---:|---|---|---|",
    ]
    for row in report["sources"]:
        observed = row["observed_http"]
        eligibility = row["eligibility"]
        record_count = observed.get("record_count")
        lines.append(
            "| {id} | {rights} | {status} | {network} | {records} | {display} | {model} | {publication} |".format(
                id=row["id"],
                rights=row["rights_basis"]["classification"],
                status=row["failure_or_status"]["state"],
                network="opened" if observed.get("network_opened") else "not opened",
                records="—" if record_count is None else record_count,
                display=eligibility["display"],
                model=eligibility["model"],
                publication=eligibility["publication"],
            )
        )
    lines.extend(["", "## 逐源详细记录", ""])
    for row in report["sources"]:
        coverage = row["field_coverage"]
        lines.extend(
            [
                f"### `{row['id']}` — {row['name']}",
                "",
                f"- Adapter：`{row['adapter']}`；主机：`{', '.join(row['hosts']) or '无（执行层/未来适配器）'}`",
                f"- 权利：{row['rights_basis']['classification']}；{row['rights_basis']['conclusion']}",
                f"- 条款/许可证：{', '.join(row['terms_or_license_urls']) or '未声明；因此不放行' }",
                f"- Robots/access：{row['robots_and_access']['policy']}；网络判定 `{row['robots_and_access']['network_decision']}`",
                f"- Allowlist/限制：`{json.dumps(row['allowlist_redirect_and_response_limits'], ensure_ascii=False, sort_keys=True)}`",
                f"- 限速：`{json.dumps(row['rate_limit'], ensure_ascii=False, sort_keys=True)}`",
                f"- Parser：`{row['parser_contract']['name']}`；声明字段：`{', '.join(coverage['declared'])}`；已观测：`{', '.join(coverage['observed'])}`；缺失：`{', '.join(coverage['missing']) or '无'}`",
                f"- 实体联结：{row['entity_join']}",
                f"- 时间：{row['time_semantics']}",
                f"- 原始哈希：`{json.dumps(row['raw_hash_policy'], ensure_ascii=False, sort_keys=True)}`",
                f"- 实际 HTTP/运行：`{json.dumps(row['observed_http'], ensure_ascii=False, sort_keys=True)}`",
                f"- 状态/失败：`{json.dumps(row['failure_or_status'], ensure_ascii=False, sort_keys=True)}`",
                f"- 替代方案：{'；'.join(row['alternatives'])}",
                f"- Eligibility：display=`{row['eligibility']['display']}`，model=`{row['eligibility']['model']}`，publication=`{row['eligibility']['publication']}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("/dev/shm/matchline-live-runtime/current.json"))
    parser.add_argument("--probe-evidence", type=Path)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    args = parser.parse_args()
    snapshot, snapshot_sha256 = load_snapshot(args.snapshot)
    probe_evidence = load_probe_evidence(args.probe_evidence)
    probe_evidence_sha256 = (
        hashlib.sha256(args.probe_evidence.read_bytes()).hexdigest()
        if args.probe_evidence is not None
        else None
    )
    report = build_source_research_report(
        snapshot,
        snapshot_sha256=snapshot_sha256,
        snapshot_path=str(args.snapshot),
        observed_at=args.observed_at,
        probe_evidence=probe_evidence,
        probe_evidence_path=(str(args.probe_evidence) if args.probe_evidence is not None else None),
        probe_evidence_sha256=probe_evidence_sha256,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output_markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"schema_version": SCHEMA_VERSION, "sources": len(report["sources"]), "snapshot_sha256": snapshot_sha256}, ensure_ascii=False, sort_keys=True))


__all__ = [
    "PROBE_SCHEMA_VERSION",
    "REQUIRED_SOURCE_FIELDS",
    "SCHEMA_VERSION",
    "build_source_research_report",
    "load_probe_evidence",
    "load_snapshot",
    "main",
    "render_markdown",
    "validate_source_research_report",
]


if __name__ == "__main__":
    main()
