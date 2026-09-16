"""Validate and attach as-of current-source snapshots to historical state."""

from __future__ import annotations

import json
import hashlib
import math
import re
import string
import unicodedata
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from league_platform.fixture_feed import (
    MULTI_SOURCE_FIXTURE_PROVIDER,
    fixture_feed_key,
    fixture_feed_section,
    fixture_rows,
)
from league_platform.identity import canonical_team_name, team_id
from league_platform.live_sources.news import ALLOWED_NEWS_HOSTS
from league_platform.live_sources.geocoding import GEOCODING_HOST, GEOCODING_PATH
from league_platform.live_sources.open_meteo import OPEN_METEO_HOST, OPEN_METEO_PATH
from league_platform.live_sources.met_norway import (
    MET_HOST,
    MET_LICENSE,
    MET_PATH,
    MET_SOURCE_ID,
    MET_SOURCE_NAME,
)
from league_platform.live_sources.wikidata import (
    WIKIDATA_HOST,
    WIKIDATA_LICENSE,
    WIKIDATA_PATH,
    WIKIDATA_SOURCE_ID,
    WIKIDATA_SOURCE_NAME,
)
from league_platform.live_sources.openligadb import OPENLIGADB_HOST, OPENLIGADB_PATH
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES
from league_platform.live_sources.cfl_official import (
    CFL_API_HOST,
    CFL_OFFICIAL_PAGE_URL,
    MATCHES_PATH as CFL_MATCHES_PATH,
    SOURCE_ACCESS_BASIS as CFL_SOURCE_ACCESS_BASIS,
    SOURCE_NAME as CFL_SOURCE_NAME,
    SOURCE_RIGHTS_STATUS as CFL_SOURCE_RIGHTS_STATUS,
    validate_cfl_venue,
)
from league_platform.live_sources.oddstorm import (
    ODDSTORM_RIGHTS_STATUS,
    ODDSTORM_TERMS_URL,
)
from league_platform.live_sources.premier_league import (
    PREMIER_LEAGUE_HOST,
    PREMIER_LEAGUE_LINEUPS_PATH,
    PREMIER_LEAGUE_MATCHWEEK_PATH,
)
from league_platform.live_sources.laliga import (
    LALIGA_HOST,
    LALIGA_MATCH_PATH,
)
from league_platform.live_sources.bundesliga import (
    BUNDESLIGA_HOST,
    BUNDESLIGA_MATCH_PATH,
)
from league_platform.live_sources.seriea import (
    SERIE_A_API_HOST,
    SERIE_A_API_PATH,
)
from league_platform.live_sources.ligue1 import (
    LIGUE1_API_HOST,
    LIGUE1_MATCH_PATH,
)
from league_platform.live_sources.sofascore import SOFASCORE_HOST
from league_platform.live_sources.fotmob import (
    FOTMOB_DETAILS_PATH,
    FOTMOB_HOST,
    FOTMOB_SOURCE_NAME,
)
from league_platform.live_sources.sports_lottery import SPORTS_LOTTERY_ALLOWED_PATHS
from league_platform.live_sources.espn import ESPN_ALLOWED_HOSTS, ESPN_CODES
from league_platform.live_sources.espn_injuries import ESPN_INJURY_PATH_PREFIX
from league_platform.source_registry import build_runtime_source_registry, validate_source_registry
from league_platform.source_rights import SourceId, UseCase, decide_source_rights

_BASE_SOURCE_ROLES = {
    "fixtures_and_results": "ESPN",
    "recent_xg_and_form": "Understat",
    "historical_training": ["football-data.co.uk", "OpenFootball"],
    "current_market": "ESPN event summary / named bookmaker",
    "injuries_and_lineups": "SofaScore provider-reported; not authoritative",
}
_LEGACY_REQUIRED_SOURCE_ROLES = {
    key: _BASE_SOURCE_ROLES[key]
    for key in (
        "fixtures_and_results",
        "recent_xg_and_form",
        "historical_training",
        "current_market",
    )
}
EXPECTED_SOURCE_ROLES = {
    **_BASE_SOURCE_ROLES,
    "independent_asian_market": "OddStorm public comparison; never labeled as official Sports Lottery odds",
    "official_premier_league_lineups": "Premier League official; strict ID/time/team join only",
    "official_laliga_lineups": "LaLiga official; strict kickoff/team join, observed_at fallback",
    "official_bundesliga_lineups": "Bundesliga official; strict kickoff/team join, observed_at fallback",
    "official_serie_a_lineups": "Serie A official SDP API; strict kickoff/team join, observed_at fallback",
    "official_ligue1_lineups": "Ligue 1 official public match API; strict native ID/time/team join, observed_at fallback",
    "player_rosters": "ESPN public team roster; display/audit only until fixture-specific confirmed XI",
    "injury_reports": "ESPN injury endpoint disabled pending express written permission; no automated access",
    # Crawl4AI is an optional, explicitly allowlisted multi-source capture
    # engine. Its raw captures remain display/audit-only until each declared
    # page source proves a strict fixture join; the engine itself is never a
    # fact source or model feature.
    "browser_rendered_public_pages": "Crawl4AI capture engine; declared page sources and append-only display evidence",
}
_OPENFOOTBALL_BASE_SOURCE_ROLES = {
    "fixtures_and_results": "OpenFootball CC0 current schedules and results",
    "recent_xg_and_form": "Understat",
    "historical_training": ["football-data.co.uk", "OpenFootball"],
    "current_market": "Sports Lottery official sale data; independent market baseline unavailable",
}
_CURRENT_BASE_SOURCE_ROLES = {
    **_OPENFOOTBALL_BASE_SOURCE_ROLES,
    "fixtures_and_results": (
        "OpenFootball CC0 + CFL official public current schedules and results; "
        "CFL commercial reuse rights unverified"
    ),
}
ODDSTORM_BLOCKED_SOURCE_ROLE = (
    "OddStorm authorization blocked; independent Asian market baseline unavailable"
)
CURRENT_SOURCE_ROLES = {
    **EXPECTED_SOURCE_ROLES,
    **_CURRENT_BASE_SOURCE_ROLES,
    "independent_asian_market": ODDSTORM_BLOCKED_SOURCE_ROLE,
    "player_rosters": "ESPN roster endpoint disabled pending express written permission",
}

# Exact role contract emitted by ``sync_live`` after the v260 rights cut-over.
# Keep this literal here: the producer and consumer must agree on the wire
# value, while neither side imports the other and silently masks schema drift.
V260_SOURCE_ROLES = {
    "fixtures_and_results": (
        "OpenFootball CC0 current-cycle rows rebuilt from durable raw evidence only; "
        "stale replay remains disabled"
    ),
    "secondary_current_results": (
        "OpenLigaDB ODbL isolated serving lane; excluded from fixture_feed "
        "and every model/training/redistribution lane"
    ),
    "historical_training": ("OpenFootball durable raw archive verified consumer required"),
    "blocked_current_sources": ("All other v260 sources are canonical pre-network rights blocks"),
    "raw_archive_replay": "current_cycle_reparse_only_no_stale_replay",
}

_BLOCKED_CURRENT_PROJECTION_SOURCES = {
    "cfl_official": SourceId.CFL_OFFICIAL_CURRENT,
    "espn": SourceId.ESPN_SCHEDULE_SUMMARY,
    "espn_markets": SourceId.ESPN_MARKET_SUMMARY,
    "espn_rosters": SourceId.ESPN_TEAM_ROSTERS,
    "espn_injuries": SourceId.ESPN_INJURY_REPORTS,
    "understat": SourceId.UNDERSTAT_XG,
    "news": SourceId.PUBLIC_RSS_NEWS,
    "geocoding": SourceId.OPEN_METEO_GEOCODING,
    "weather": SourceId.OPEN_METEO_WEATHER,
    "sofascore": SourceId.SOFASCORE_PREMATCH,
    "fotmob": SourceId.FOTMOB_PUBLIC_API,
    "premier_league_official": SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS,
    "laliga_official": SourceId.OFFICIAL_LALIGA_LINEUPS,
    "bundesliga_official": SourceId.OFFICIAL_BUNDESLIGA_LINEUPS,
    "serie_a_official": SourceId.OFFICIAL_SERIE_A_LINEUPS,
    "ligue1_official": SourceId.OFFICIAL_LIGUE1_LINEUPS,
    "sports_lottery": SourceId.SPORTS_LOTTERY_OFFICIAL,
    "oddstorm": SourceId.ODDSTORM_MARKET_COMPARISON,
    # Crawl4AI is an execution layer, not an independently admissible fact
    # source.  An unknown stable ID therefore deliberately fails closed.
    "crawl4ai": "crawl4ai_execution_layer",
}

_BLOCKED_CURRENT_TOP_LEVEL_KEYS = frozenset(_BLOCKED_CURRENT_PROJECTION_SOURCES)
_RECEIPT_CLAIM_KEYS = frozenset(
    {
        "producer_receipt",
        "raw_archive_receipt",
        "raw_archive_receipts",
        "raw_archive_producer_receipt",
        "raw_archive_admission",
    }
)
# Accept snapshots written by the previous source schema while the append-only
# archive is migrated.  A snapshot with new providers but without the official
# lineup role is also valid: it was produced after OddStorm landed but before
# the first-party lineup source was added.
LEGACY_SOURCE_ROLES = {**_BASE_SOURCE_ROLES, "injuries_and_lineups": None}
# Raw snapshots written before the independent-market and official-lineup
# roles were introduced remain part of the append-only archive.  They are
# trusted only as historical observations and must still pass every provider
# and timestamp validation below; accepting this exact role shape lets the
# as-of replay consume those observations without rewriting archive history.
HISTORICAL_SOURCE_ROLES = {**_BASE_SOURCE_ROLES}
INTERMEDIATE_SOURCE_ROLES = {
    **_BASE_SOURCE_ROLES,
    "independent_asian_market": "OddStorm public comparison; never labeled as official Sports Lottery odds",
}
# Snapshots created after the Premier League source landed but before the
# LaLiga adapter was added remain immutable and must still be readable.
PRE_LALIGA_SOURCE_ROLES = {
    **INTERMEDIATE_SOURCE_ROLES,
    "official_premier_league_lineups": "Premier League official; strict ID/time/team join only",
}
# Snapshots created after Bundesliga landed but before the Serie A adapter was
# added remain immutable and must stay replayable.
PRE_SERIE_A_SOURCE_ROLES = {**EXPECTED_SOURCE_ROLES}
PRE_SERIE_A_SOURCE_ROLES.pop("official_serie_a_lineups")
# Snapshots written after LaLiga landed but before the Bundesliga adapter was
# added remain immutable and must still be readable during replay.
PRE_BUNDESLIGA_SOURCE_ROLES = {**PRE_SERIE_A_SOURCE_ROLES}
PRE_BUNDESLIGA_SOURCE_ROLES.pop("official_bundesliga_lineups")

# Source roles were added one provider at a time.  Historical snapshots can
# therefore contain a legitimate *combination* that is not one of the named
# milestones above (for example LaLiga plus Premier League before Bundesliga
# landed).  Keep the exact milestone dictionaries for documentation, but use
# this allowlisted compatibility check for replay so an old snapshot is not
# discarded merely because another optional role had not been introduced yet.
_OPTIONAL_SOURCE_ROLE_VALUES: dict[str, set[object]] = {
    "injuries_and_lineups": {
        None,
        "SofaScore provider-reported; not authoritative",
    },
    "independent_asian_market": {
        "OddStorm public comparison; never labeled as official Sports Lottery odds",
        ODDSTORM_BLOCKED_SOURCE_ROLE,
    },
    "official_premier_league_lineups": {
        "Premier League official; strict ID/time/team join only",
    },
    "official_laliga_lineups": {
        "LaLiga official; strict kickoff/team join, observed_at fallback",
    },
    "official_bundesliga_lineups": {
        "Bundesliga official; strict kickoff/team join, observed_at fallback",
    },
    "official_serie_a_lineups": {
        "Serie A official SDP API; strict kickoff/team join, observed_at fallback",
    },
    "official_ligue1_lineups": {
        "Ligue 1 official public match API; strict native ID/time/team join, observed_at fallback",
    },
    "browser_rendered_public_pages": {
        "Crawl4AI capture engine; declared page sources and append-only display evidence",
        # A short-lived v69 writer used this equivalent label before the
        # runtime-only/fact-source boundary was made explicit.  Keep the
        # append-only raw archive replayable; this value never authorizes
        # model entry by itself.
        "Crawl4AI; explicit allowlist and append-only display evidence",
    },
    "player_rosters": {
        "ESPN public team roster; display/audit only until fixture-specific confirmed XI",
        "ESPN roster endpoint disabled pending express written permission",
    },
    "injury_reports": {
        "ESPN public league injury reports; display/validation only and missing team buckets remain unknown",
        "ESPN injury endpoint disabled pending express written permission; no automated access",
    },
}


def _compatible_source_roles(value: object) -> bool:
    """Validate an allowlisted historical/current role combination."""

    if not isinstance(value, dict):
        return False
    if value == V260_SOURCE_ROLES:
        return True
    required_candidates = (
        _LEGACY_REQUIRED_SOURCE_ROLES,
        _OPENFOOTBALL_BASE_SOURCE_ROLES,
        _CURRENT_BASE_SOURCE_ROLES,
    )
    required = next(
        (
            candidate
            for candidate in required_candidates
            if all(value.get(key) == expected for key, expected in candidate.items())
        ),
        None,
    )
    if required is None:
        return False
    if any(key not in required and key not in _OPTIONAL_SOURCE_ROLE_VALUES for key in value):
        return False
    return all(
        key in required or any(item == allowed for allowed in _OPTIONAL_SOURCE_ROLE_VALUES[key])
        for key, item in value.items()
    )


def _source_role_generation(value: object) -> str:
    """Classify a role dictionary already accepted by the compatibility gate."""

    return "v260" if value == V260_SOURCE_ROLES else "legacy"


def _role_contract(generation: str) -> dict[str, object]:
    return {
        "generation": generation,
        "model_admission": False,
        "purpose": "research_display_only",
    }


def _blocked_model_admission(reason: str) -> dict[str, object]:
    return {
        "eligible": False,
        "enters_model": False,
        "reason": reason,
        "status": "unverified",
    }


def _section_fact_count(section: object) -> int:
    """Count quarantined rows without copying their facts into the read-model."""

    if not isinstance(section, dict):
        return 0
    fact_fields = (
        "competition_reports",
        "events",
        "feeds",
        "fixtures",
        "geocodes",
        "injuries",
        "lineups",
        "lines",
        "markets",
        "matches",
        "news",
        "observations",
        "pages",
        "reports",
        "rosters",
        "team_features",
        "team_status",
        "weather",
    )
    return sum(
        len(value) for field in fact_fields if isinstance((value := section.get(field)), list)
    )


def _rights_quarantine(live: dict) -> list[dict[str, object]]:
    """Build metadata-only quarantine rows from the central v260 policy."""

    rows: list[dict[str, object]] = []
    for section_key, source_id in _BLOCKED_CURRENT_PROJECTION_SOURCES.items():
        if section_key not in live:
            continue
        decision = decide_source_rights(source_id, UseCase.MODEL_INPUT)
        rows.append(
            {
                "decision": decision.decision.value,
                "fact_count": _section_fact_count(live.get(section_key)),
                "model_eligible": False,
                "rights_status": decision.rights_status,
                "section": section_key,
                "source_id": decision.source_id,
                "use_case": decision.use_case,
            }
        )
    feed = fixture_feed_section(live)
    fixtures = feed.get("fixtures") if isinstance(feed, dict) else None
    if isinstance(fixtures, list):
        blocked_fixture_counts: dict[str, int] = {}
        for fixture in fixtures:
            source = fixture.get("source") if isinstance(fixture, dict) else None
            provider = source.get("name") if isinstance(source, dict) else None
            if provider == "OpenFootball":
                continue
            source_id = (
                SourceId.CFL_OFFICIAL_CURRENT.value
                if provider == CFL_SOURCE_NAME
                else "unknown_fixture_source"
            )
            blocked_fixture_counts[source_id] = blocked_fixture_counts.get(source_id, 0) + 1
        for source_id, count in sorted(blocked_fixture_counts.items()):
            decision = decide_source_rights(source_id, UseCase.MODEL_INPUT)
            rows.append(
                {
                    "decision": decision.decision.value,
                    "fact_count": count,
                    "model_eligible": False,
                    "rights_status": decision.rights_status,
                    "section": "fixture_feed",
                    "source_id": decision.source_id,
                    "use_case": decision.use_case,
                }
            )
    return rows


def _without_receipt_claims(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: deepcopy(item) for key, item in value.items() if key not in _RECEIPT_CLAIM_KEYS}


def _research_openfootball_fixture(fixture: dict) -> dict:
    """Project a validated fixture for local research, never for model input.

    The current JSON can only *claim* a raw hash or producer receipt.  This
    consumer has not replayed the immutable raw bytes, so every fixture stays
    explicitly unverified even when the producer wrote a successful-looking
    admission object.
    """

    competition_id = str(fixture["competition_id"])
    home_provider_name = str(fixture["home_team"])
    away_provider_name = str(fixture["away_team"])
    home_canonical = canonical_team_name(competition_id, home_provider_name)
    away_canonical = canonical_team_name(competition_id, away_provider_name)
    home_id = team_id(competition_id, home_provider_name)
    away_id = team_id(competition_id, away_provider_name)
    source = _without_receipt_claims(fixture.get("source"))
    source["enters_model"] = False
    source["model_eligible"] = False
    source["verification_status"] = "unverified_consumer_raw_replay_required"
    lineage = _without_receipt_claims(fixture.get("lineage"))
    lineage["verification_status"] = "unverified_consumer_raw_replay_required"

    # Closed projection: material fields derived from blocked schedule,
    # lineup, market, weather or geocoding overlays never cross this boundary.
    row = {
        key: deepcopy(fixture.get(key))
        for key in (
            "away_provider_team_id",
            "away_team",
            "away_team_en",
            "competition_id",
            "effective_at",
            "halftime_score",
            "home_provider_team_id",
            "home_team",
            "home_team_en",
            "id",
            "kickoff_at",
            "kickoff_date",
            "kickoff_time_quality",
            "kickoff_timezone",
            "native_fixture_id",
            "observed_at",
            "provider_competition_id",
            "provider_fixture_ids",
            "provider_team_ids",
            "result_scope",
            "round",
            "score",
            "season",
            "status",
            "time_semantics",
        )
        if key in fixture
    }
    row.update(
        {
            "away_team_canonical": away_canonical,
            "away_team_id": away_id,
            "current_features": {},
            "home_team_canonical": home_canonical,
            "home_team_id": home_id,
            "identity_resolution": {
                "scheme": "competition_scoped_explicit_alias_v1",
                "home": {
                    "provider_name": home_provider_name,
                    "canonical_name": home_canonical,
                    "canonical_team_id": home_id,
                    "resolution": (
                        "explicit_alias" if home_canonical != home_provider_name else "exact_name"
                    ),
                },
                "away": {
                    "provider_name": away_provider_name,
                    "canonical_name": away_canonical,
                    "canonical_team_id": away_id,
                    "resolution": (
                        "explicit_alias" if away_canonical != away_provider_name else "exact_name"
                    ),
                },
            },
            "lineage": lineage,
            "market_probability": None,
            "model_admission": _blocked_model_admission("consumer_raw_replay_required"),
            "source": source,
            "source_verification": {
                "status": "unverified",
                "verified_by_consumer": False,
                "reason": "consumer_raw_replay_required",
            },
        }
    )
    return row


def _validation_projection(
    live: dict,
    *,
    allowed_competition_ids: set[str] | None = None,
) -> tuple[dict, list[dict]]:
    """Select model-catalog OpenFootball rows before fact projection.

    OpenFootball can carry additional competitions that are valid CC0 facts
    but are not yet part of the trained model catalog.  Those rows remain in
    the immutable producer snapshot and are surfaced as display-only metadata;
    they must not enter the strict v260 validator, which intentionally knows
    only the model's registered competitions.
    """

    feed = fixture_feed_section(live)
    if not isinstance(feed, dict):
        raise ValueError("live snapshot fixture feed is invalid")
    provider = feed.get("provider")
    fixtures = feed.get("fixtures")
    errors = feed.get("errors")
    if (
        provider not in {"OpenFootball", MULTI_SOURCE_FIXTURE_PROVIDER}
        or not isinstance(fixtures, list)
        or any(not isinstance(row, dict) for row in fixtures)
        or not isinstance(errors, list)
    ):
        raise ValueError("live snapshot fixture feed is invalid")
    selected = [
        deepcopy(row)
        for row in fixtures
        if (
            isinstance(row.get("source"), dict)
            and row["source"].get("name") == "OpenFootball"
            and (
                allowed_competition_ids is None
                or row.get("competition_id") in allowed_competition_ids
            )
        )
    ]
    validation_feed = deepcopy(feed)
    validation_feed["provider"] = "OpenFootball"
    validation_feed["fixtures"] = selected
    validation_feed.pop("provider_status", None)
    validation_feed.pop("source_contract", None)
    validation_live: dict = {"fixture_feed": validation_feed}
    if "openligadb" in live:
        validation_live["openligadb"] = deepcopy(live["openligadb"])
    if "met_norway_weather" in live:
        validation_live["met_norway_weather"] = deepcopy(live["met_norway_weather"])
    if "wikidata_entities" in live:
        validation_live["wikidata_entities"] = deepcopy(live["wikidata_entities"])
    return validation_live, selected


def _display_only_openfootball_rows(
    live: dict,
    allowed_competition_ids: set[str],
) -> list[dict[str, object]]:
    """Describe valid-but-unregistered OpenFootball rows without facts.

    The model catalog is deliberately narrower than the source registry.  A
    newly added league should be visible to operators instead of becoming an
    opaque ``unknown competition`` failure, while its team/score/source
    payload remains outside the model/read projection until an explicit catalog
    and training admission change is made.
    """

    feed = fixture_feed_section(live)
    if not isinstance(feed, dict):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for field in ("fixtures", "date_only_fixtures"):
        candidates = feed.get(field)
        if not isinstance(candidates, list):
            continue
        for row in candidates:
            if not isinstance(row, dict):
                continue
            source = row.get("source")
            competition_id = row.get("competition_id")
            fixture_id = row.get("id")
            if (
                not isinstance(source, dict)
                or source.get("name") != "OpenFootball"
                or not isinstance(competition_id, str)
                or not competition_id.strip()
                or competition_id in allowed_competition_ids
                or not isinstance(fixture_id, str)
                or not fixture_id.strip()
                or fixture_id in seen
            ):
                continue
            seen.add(fixture_id)
            item: dict[str, object] = {
                "competition_id": competition_id,
                "fixture_id": fixture_id,
                "status": "display_only",
                "reason": "competition_not_in_model_catalog",
            }
            for key in ("kickoff_at", "kickoff_date", "home_team", "away_team"):
                value = row.get(key)
                if value is not None:
                    item[key] = value
            rows.append(item)
    rows.sort(
        key=lambda item: (
            str(item.get("kickoff_at") or item.get("kickoff_date") or ""),
            str(item["fixture_id"]),
        )
    )
    return rows


def _isolated_openligadb_projection(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    payload = deepcopy(value)
    payload.update(
        {
            "display_lane": "isolated_current_only",
            "fixture_feed_eligible": False,
            "local_display_only": True,
            "material_feature_eligible": False,
            "model_eligible": False,
            "redistribution_allowed": False,
            "training_eligible": False,
        }
    )
    payload["matches"] = [
        {
            **deepcopy(row),
            "display_lane": "isolated_current_only",
            "enters_model": False,
            "model_eligible": False,
            "redistribution_allowed": False,
            "training_eligible": False,
        }
        for row in payload.get("matches", [])
        if isinstance(row, dict)
    ]
    return payload


def _attach_research_current_readmodel(
    payload: dict,
    live: dict,
    *,
    as_of: datetime,
    checked_at: datetime,
    freshness_limit: timedelta,
    role_generation: str,
) -> dict:
    """Attach the v260 local research lane with no model-admitted features."""

    competition_ids = {
        str(item["id"])
        for item in payload.get("competitions", [])
        if isinstance(item, dict) and item.get("id")
    }
    expected = live.get("expected_competitions")
    if (
        not isinstance(expected, list)
        or any(not isinstance(item, str) for item in expected)
        or not expected
        or not set(expected).issubset(competition_ids)
    ):
        raise ValueError("live snapshot expected_competitions are invalid")

    validation_live, selected = _validation_projection(
        live,
        allowed_competition_ids=competition_ids,
    )
    display_only_rows = _display_only_openfootball_rows(live, competition_ids)
    _validate_live_snapshot(validation_live, as_of, competition_ids)
    fixtures = [_research_openfootball_fixture(row) for row in selected]
    existing_ids = {
        row.get("id")
        for row in payload.get("matches", [])
        if isinstance(row, dict) and row.get("id")
    }
    duplicate_ids = sorted(str(row["id"]) for row in fixtures if row.get("id") in existing_ids)
    if duplicate_ids:
        raise ValueError("duplicate current fixture IDs")

    payload.setdefault("matches", []).extend(fixtures)
    payload["matches"].sort(
        key=lambda item: str(item.get("kickoff_at") or ""),
        reverse=True,
    )
    payload.setdefault("summary", {})["current_fixture_count"] = len(fixtures)
    payload["summary"]["current_xg_team_count"] = 0

    age = checked_at.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)
    status = (
        "research_only"
        if fixtures and age <= freshness_limit
        else "stale_research_only"
        if fixtures
        else "unavailable"
    )
    for competition in payload.get("competitions", []):
        if not isinstance(competition, dict):
            continue
        fixture_count = sum(row.get("competition_id") == competition.get("id") for row in fixtures)
        competition["current_source_status"] = status if fixture_count else "unavailable"
        competition["current_fixture_count"] = fixture_count
        competition["current_xg_team_count"] = 0

    for key in _BLOCKED_CURRENT_TOP_LEVEL_KEYS:
        payload.pop(key, None)
    openligadb = _isolated_openligadb_projection(live.get("openligadb"))
    if openligadb is not None:
        payload["openligadb"] = openligadb

    payload["current_data"] = {
        "age_seconds": max(0, int(age.total_seconds())),
        "as_of": as_of.isoformat(),
        "canonical_fixture_count": len(fixtures),
        "canonical_fixture_provider": "OpenFootball",
        "display_only_competitions": sorted(
            {str(row["competition_id"]) for row in display_only_rows}
        ),
        "display_only_fixture_count": len(display_only_rows),
        "display_only_fixtures": display_only_rows,
        "fixture_count": len(fixtures),
        "market_count": 0,
        "message": (
            "当前赛程仅供本地研究显示；未经本消费端原始归档重放，不进入模型。"
            if fixtures
            else "当前没有通过本地研究显示边界的赛程。"
        ),
        "model_admission": {
            "eligible": False,
            "reason": "consumer_raw_replay_required",
            "status": "blocked",
        },
        "openligadb_local_display_count": (
            len(openligadb.get("matches", [])) if openligadb is not None else 0
        ),
        "rights_quarantine": _rights_quarantine(live),
        "role_contract": _role_contract(role_generation),
        "roles": deepcopy(live.get("roles", {})),
        "source_registry": [],
        "status": status,
        "xg_team_count": 0,
    }
    return payload


_LOTTERY_COMPETITION_ALIASES = {
    "英超": "premier-league",
    "英格兰超级联赛": "premier-league",
    "premier league": "premier-league",
    "英冠": "championship",
    "英格兰冠军联赛": "championship",
    "efl championship": "championship",
    "championship": "championship",
    "西甲": "la-liga",
    "西班牙甲级联赛": "la-liga",
    "la liga": "la-liga",
    "德甲": "bundesliga",
    "德国甲级联赛": "bundesliga",
    "bundesliga": "bundesliga",
    "意甲": "serie-a",
    "意大利甲级联赛": "serie-a",
    "serie a": "serie-a",
    "法甲": "ligue-1",
    "法国甲级联赛": "ligue-1",
    "ligue 1": "ligue-1",
    "中超": "csl",
    "中国足球超级联赛": "csl",
    "chinese super league": "csl",
}

# The official Sports Lottery feed uses a mixture of Chinese display names
# and short provider abbreviations (for example ``ALA``/``阿拉维斯``).  Keep
# these aliases explicit and scoped to a competition; never use fuzzy string
# similarity for a market join.  The mapped value is the provider-neutral
# English team name emitted by the current ESPN fixture feed.
_LOTTERY_TEAM_ALIASES = {
    "premier-league": {
        "阿森纳": "Arsenal",
        "ars": "Arsenal",
        "考文垂": "Coventry City",
        "cov": "Coventry City",
        "赫尔城": "Hull City",
        "hul": "Hull City",
        "埃弗顿": "Everton",
        "eve": "Everton",
        "水晶宫": "Crystal Palace",
        "cry": "Crystal Palace",
        "伊普斯维奇": "Ipswich Town",
        "ips": "Ipswich Town",
        "桑德兰": "Sunderland",
        "sun": "Sunderland",
        "布伦特福德": "Brentford",
        "brn": "Brentford",
        "曼彻斯特城": "Manchester City",
        "mci": "Manchester City",
        "曼彻斯特联": "Manchester United",
        "mun": "Manchester United",
        "利物浦": "Liverpool",
        "liv": "Liverpool",
        "切尔西": "Chelsea",
        "che": "Chelsea",
        "托特纳姆热刺": "Tottenham Hotspur",
        "tot": "Tottenham Hotspur",
        "纽卡斯尔联": "Newcastle United",
        "new": "Newcastle United",
        "布莱顿": "Brighton & Hove Albion",
        "bha": "Brighton & Hove Albion",
        "伯恩茅斯": "AFC Bournemouth",
        "bou": "AFC Bournemouth",
        "利兹联": "Leeds United",
        "lee": "Leeds United",
        "诺丁汉森林": "Nottingham Forest",
        "nfo": "Nottingham Forest",
        "富勒姆": "Fulham",
    },
    "championship": {
        "西布罗姆维奇": "West Bromwich Albion",
        "wba": "West Bromwich Albion",
        "伯恩利": "Burnley",
        "bur": "Burnley",
    },
    "la-liga": {
        "阿拉维斯": "Alavés",
        "ala": "Alavés",
        "赫塔费": "Getafe",
        "get": "Getafe",
        "塞维利亚": "Sevilla",
        "sev": "Sevilla",
        "巴列卡诺": "Rayo Vallecano",
        "rva": "Rayo Vallecano",
        "rvl": "Rayo Vallecano",
        "桑坦德竞技": "Racing Santander",
        "san": "Racing Santander",
        "比利亚雷亚尔": "Villarreal",
        "vil": "Villarreal",
        "西班牙人": "Espanyol",
        "esp": "Espanyol",
        "莱万特": "Levante",
        "lev": "Levante",
        "拉科鲁尼亚": "La Coruna",
        "dep": "Deportivo",
        "埃尔切": "Elche",
        "elc": "Elche",
        "巴塞罗那": "Barcelona",
        "bar": "Barcelona",
        "皇家马德里": "Real Madrid",
        "rma": "Real Madrid",
        "马德里竞技": "Atlético Madrid",
        # The Sports Lottery feed uses both Chinese names and the compact
        # three-letter codes ``ATM``/``MAL`` for this fixture.  Keep the
        # bridge explicit so the official market attaches to the canonical
        # ESPN fixture instead of creating a duplicate display-only row.
        "atl": "Atlético Madrid",
        "atm": "Atlético Madrid",
        "马拉加": "Málaga",
        "mal": "Málaga",
        "皇家贝蒂斯": "Real Betis",
        "bet": "Real Betis",
        "皇家社会": "Real Sociedad",
        "soc": "Real Sociedad",
        "毕尔巴鄂竞技": "Athletic Club",
        "ath": "Athletic Club",
        "塞尔塔": "Celta Vigo",
        "cel": "Celta Vigo",
        "维戈塞尔塔": "Celta Vigo",
        "cvo": "Celta Vigo",
        "瓦伦西亚": "Valencia",
        "val": "Valencia",
        "vca": "Valencia",
        "奥萨苏纳": "Osasuna",
        "osa": "Osasuna",
    },
    "bundesliga": {
        "拜仁慕尼黑": "Bayern Munich",
        "bay": "Bayern Munich",
        "斯图加特": "VfB Stuttgart",
        "stu": "VfB Stuttgart",
        "勒沃库森": "Bayer Leverkusen",
        "b04": "Bayer Leverkusen",
        "多特蒙德": "Borussia Dortmund",
        "dor": "Borussia Dortmund",
        "莱比锡": "RB Leipzig",
        "rbl": "RB Leipzig",
        "法兰克福": "Eintracht Frankfurt",
        "sge": "Eintracht Frankfurt",
    },
    "serie-a": {
        "ac米兰": "AC Milan",
        "ac": "AC Milan",
        "acm": "AC Milan",
        "mil": "AC Milan",
        "乌迪内斯": "Udinese",
        "udi": "Udinese",
        "科莫": "Como",
        "cmo": "Como",
        "国际米兰": "Internazionale",
        "int": "Internazionale",
        "inm": "Internazionale",
        "蒙扎": "Monza",
        "moz": "Monza",
        "热那亚": "Genoa",
        "gna": "Genoa",
        "帕尔马": "Parma",
        "par": "Parma",
        "卡利亚里": "Cagliari",
        "cag": "Cagliari",
        "尤文图斯": "Juventus",
        "juv": "Juventus",
        "弗洛西诺内": "Frosinone",
        "fsn": "Frosinone",
        "威尼斯": "Venezia",
        "vez": "Venezia",
        "莱切": "Lecce",
        "lcc": "Lecce",
        "都灵": "Torino",
        "act": "Torino",
        "罗马": "AS Roma",
        "rom": "AS Roma",
        "那不勒斯": "Napoli",
        "nap": "Napoli",
        "拉齐奥": "Lazio",
        "laz": "Lazio",
        "亚特兰大": "Atalanta",
        "萨索洛": "Sassuolo",
        "博洛尼亚": "Bologna",
        "佛罗伦萨": "Fiorentina",
    },
    "ligue-1": {
        "巴黎圣日耳曼": "Paris Saint-Germain",
        "psg": "Paris Saint-Germain",
        "马赛": "Marseille",
        "om": "Marseille",
        "斯特拉斯堡": "Strasbourg",
        "stb": "Strasbourg",
        "朗斯": "Lens",
        "len": "Lens",
        "欧塞尔": "AJ Auxerre",
        "aux": "AJ Auxerre",
        "尼斯": "Nice",
        "nic": "Nice",
        "洛里昂": "Lorient",
        "lor": "Lorient",
        "图卢兹": "Toulouse",
        "tou": "Toulouse",
        "昂热": "Angers",
        "soa": "Angers",
        "里昂": "Lyon",
        "lyo": "Lyon",
        "摩纳哥": "AS Monaco",
        "mon": "AS Monaco",
        "里尔": "Lille",
        "lil": "Lille",
        "勒阿弗尔": "Le Havre AC",
    },
    "csl": {
        "北京国安": "Beijing Guoan",
        "bgu": "Beijing Guoan",
        "成都蓉城": "Chengdu Rongcheng",
        "cdr": "Chengdu Rongcheng",
        "山东泰山": "Shandong Taishan",
        "sd": "Shandong Taishan",
        "上海海港": "Shanghai Port",
        "shp": "Shanghai Port",
        "上海申花": "Shanghai Shenhua",
        "shs": "Shanghai Shenhua",
        "浙江队": "Zhejiang Professional FC",
        "zhe": "Zhejiang Professional FC",
    },
}

_LOTTERY_PROBABILITY_KEYS = {
    "had_probability": {"h", "d", "a"},
    "hhad_probability": {"h", "d", "a"},
    "ttg_probability": {f"s{index}" for index in range(8)},
    "hafu_probability": {"hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa"},
    "crs_probability": {"s00", "s10", "s20", "s01", "s02", "s11", "s21", "s12", "s22"},
}


def _validate_lottery_probability_map(values: object, field: str) -> None:
    """Reject malformed Sports Lottery distributions without fabricating gaps."""

    if values is None:
        return
    if not isinstance(values, dict):
        raise ValueError("current Sports Lottery probability contract is invalid")
    if not values:
        return
    allowed = _LOTTERY_PROBABILITY_KEYS[field]
    if not set(values).issubset(allowed):
        raise ValueError(f"current Sports Lottery {field} contains an unknown outcome key")
    try:
        numbers = [float(value) for value in values.values()]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"current Sports Lottery {field} contains a non-numeric value") from exc
    if any(not math.isfinite(value) or value < 0 for value in numbers):
        raise ValueError(f"current Sports Lottery {field} contains an invalid value")
    total = sum(numbers)
    if total <= 0 or abs(total - 1.0) > 1e-5:
        raise ValueError(f"current Sports Lottery {field} must sum to one")


def _validate_source(
    source: dict,
    as_of: datetime,
    *,
    hash_keys: tuple[str, ...],
    expected_name: str,
    allowed_hosts: set[str],
    require_all_hashes: bool = False,
) -> None:
    observed = datetime.fromisoformat(source["retrieved_at"])
    if observed.tzinfo is None or observed.astimezone(timezone.utc) > as_of.astimezone(
        timezone.utc
    ):
        raise ValueError(
            "current source retrieved_at must be timezone-aware and no later than as_of"
        )
    parsed_url = urlparse(source.get("url", ""))
    if parsed_url.scheme != "https" or parsed_url.hostname not in allowed_hosts:
        raise ValueError("current source URL or host is not allowlisted")
    if source.get("name") != expected_name:
        raise ValueError("current source name does not match its data role")
    hashes = [source.get(key) for key in hash_keys]
    valid_hashes = [
        isinstance(value, str)
        and len(value) == 64
        and all(character in string.hexdigits for character in value)
        for value in hashes
    ]
    if (require_all_hashes and not all(valid_hashes)) or (
        not require_all_hashes and not any(valid_hashes)
    ):
        raise ValueError("current source is missing a 64-character content hash")


def _validate_lineup_poll_diagnostics(value: object) -> None:
    """Validate optional lineup polling audit metadata without trusting it as a feature."""

    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("lineup poll diagnostics must be an object")
    allowed_states = {
        "lineups_observed",
        "partial",
        "awaiting_publication",
        "source_unavailable",
        "not_due",
        "not_requested",
        "not_configured",
    }
    for competition_id, row in value.items():
        if not isinstance(competition_id, str) or not isinstance(row, dict):
            raise ValueError("lineup poll diagnostics row is invalid")
        if row.get("state") not in allowed_states:
            raise ValueError("lineup poll diagnostics state is invalid")
        if not isinstance(row.get("requested"), bool):
            raise ValueError("lineup poll diagnostics requested flag is invalid")
        for key in (
            "candidate_count",
            "invalid_kickoff_count",
            "source_lineup_count",
            "confirmed_lineup_count",
            "model_eligible_lineup_count",
            "error_count",
        ):
            count = row.get(key)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("lineup poll diagnostics count is invalid")
        available_count = row.get("available_lineup_count")
        if available_count is not None and (
            not isinstance(available_count, int)
            or isinstance(available_count, bool)
            or available_count < 0
        ):
            raise ValueError("lineup poll diagnostics available count is invalid")
        fixture_plans = row.get("fixture_plans")
        if fixture_plans is None:
            continue
        if not isinstance(fixture_plans, list) or len(fixture_plans) > 64:
            raise ValueError("lineup poll fixture plans are invalid or oversized")
        for plan in fixture_plans:
            if not isinstance(plan, dict):
                raise ValueError("lineup poll fixture plan is invalid")
            fixture_id = plan.get("fixture_id")
            kickoff_at = plan.get("kickoff_at")
            if not isinstance(fixture_id, str) or not fixture_id.strip() or len(fixture_id) > 160:
                raise ValueError("lineup poll fixture plan id is invalid")
            if not isinstance(kickoff_at, str) or not kickoff_at.strip():
                raise ValueError("lineup poll fixture plan kickoff is invalid")
            try:
                parsed_kickoff = datetime.fromisoformat(kickoff_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("lineup poll fixture plan kickoff is invalid") from exc
            if parsed_kickoff.tzinfo is None or parsed_kickoff.utcoffset() is None:
                raise ValueError("lineup poll fixture plan kickoff must be timezone-aware")
            for key in (
                "source_lineup_count",
                "available_lineup_count",
                "confirmed_lineup_count",
                "model_eligible_lineup_count",
            ):
                count = plan.get(key)
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError("lineup poll fixture plan count is invalid")
            stage_plan = plan.get("stage_plan")
            if not isinstance(stage_plan, list) or len(stage_plan) > 4:
                raise ValueError("lineup poll fixture plan stages are invalid")
            for stage in stage_plan:
                if not isinstance(stage, dict) or not isinstance(stage.get("stage"), str):
                    raise ValueError("lineup poll fixture plan stage is invalid")
                if stage.get("state") not in {"closed", "due", "upcoming"}:
                    raise ValueError("lineup poll fixture plan stage state is invalid")
                cutoff_at = stage.get("cutoff_at")
                if not isinstance(cutoff_at, str) or not cutoff_at.strip():
                    raise ValueError("lineup poll fixture plan cutoff is invalid")
                try:
                    parsed_cutoff = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("lineup poll fixture plan cutoff is invalid") from exc
                if parsed_cutoff.tzinfo is None or parsed_cutoff.utcoffset() is None:
                    raise ValueError("lineup poll fixture plan cutoff must be timezone-aware")
            if not isinstance(plan.get("poll_due"), bool):
                raise ValueError("lineup poll fixture plan poll_due flag is invalid")


def _validate_official_fixture_join_diagnostics(value: object) -> None:
    """Validate bounded official-schedule join diagnostics as audit metadata."""

    if value is None:
        return
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("official fixture join diagnostics are invalid or oversized")
    allowed_statuses = {"exact", "partial", "unavailable", "not_due"}
    count_keys = {
        "canonical_candidate_count",
        "official_fixture_count",
        "matched_count",
        "ambiguous_count",
        "team_pair_time_mismatch_count",
        "unmatched_canonical_count",
        "unmatched_official_count",
    }
    for competition_id, row in value.items():
        if not isinstance(competition_id, str) or not isinstance(row, dict):
            raise ValueError("official fixture join diagnostic row is invalid")
        if row.get("status") not in allowed_statuses:
            raise ValueError("official fixture join diagnostic status is invalid")
        if row.get("join_basis") != "strict_kickoff_and_normalized_team_pair":
            raise ValueError("official fixture join diagnostic basis is invalid")
        for key in count_keys:
            count = row.get(key)
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
                or count > 100000
            ):
                raise ValueError("official fixture join diagnostic count is invalid")
        samples = row.get("samples")
        if not isinstance(samples, list) or len(samples) > 12:
            raise ValueError("official fixture join diagnostic samples are invalid")
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("outcome") not in {
                "ambiguous_exact_match",
                "team_pair_time_mismatch",
                "no_official_fixture_match",
            }:
                raise ValueError("official fixture join diagnostic sample is invalid")


def _validate_official_schedule_overlay(fixture: dict, as_of: datetime) -> None:
    """Validate a first-party field overlay without changing row ownership."""

    source_contracts: dict[str, dict[str, object]] = {
        "premier-league": {
            "provider": "Premier League official",
            "provider_id_key": "premier_league_official",
            "allowed_hosts": {PREMIER_LEAGUE_HOST},
            "path_pattern": PREMIER_LEAGUE_MATCHWEEK_PATH,
        },
        "serie-a": {
            "provider": "Serie A official",
            "provider_id_key": "serie_a_official",
            "allowed_hosts": {SERIE_A_API_HOST},
            "path_pattern": SERIE_A_API_PATH,
        },
    }
    overlay = fixture.get("schedule_overlay")
    field_sources = fixture.get("field_sources")
    official_provider_names = {
        str(contract.get("provider")) for contract in source_contracts.values()
    }
    has_official_field_source = isinstance(field_sources, dict) and any(
        isinstance(value, dict) and value.get("name") in official_provider_names
        for value in field_sources.values()
    )
    uses_overlay = (
        any(
            value is not None
            for value in (
                overlay,
                fixture.get("kickoff_time_observed_at"),
            )
        )
        or fixture.get("kickoff_time_source") in official_provider_names
        or has_official_field_source
    )
    if not uses_overlay:
        return
    competition_id = fixture.get("competition_id")
    contract = source_contracts.get(competition_id) if isinstance(competition_id, str) else None
    provider = contract.get("provider") if contract is not None else None
    allowed_hosts = contract.get("allowed_hosts") if contract is not None else None
    path_pattern = contract.get("path_pattern") if contract is not None else None
    provider_id_key = contract.get("provider_id_key") if contract is not None else None
    if (
        contract is None
        or not isinstance(provider, str)
        or not isinstance(allowed_hosts, set)
        or any(not isinstance(host, str) for host in allowed_hosts)
        or not isinstance(path_pattern, (str, re.Pattern))
        or not isinstance(provider_id_key, str)
        or fixture.get("kickoff_time_source") != provider
        or not isinstance(overlay, dict)
        or not isinstance(field_sources, dict)
        or overlay.get("provider") != provider
        or overlay.get("join_basis") != "exact_competition_and_explicit_canonical_team_pair"
    ):
        raise ValueError("current official schedule overlay contract is invalid")
    fields = overlay.get("fields")
    if (
        not isinstance(fields, list)
        or "kickoff_at" not in fields
        or len(fields) != len(set(fields))
        or not set(fields).issubset({"kickoff_at", "venue"})
    ):
        raise ValueError("current official schedule overlay fields are invalid")
    kickoff_source = field_sources.get("kickoff_at")
    if not isinstance(kickoff_source, dict):
        raise ValueError("current official schedule overlay source is invalid")
    try:
        _validate_source(
            kickoff_source,
            as_of,
            hash_keys=("raw_sha256",),
            expected_name=str(provider),
            allowed_hosts=allowed_hosts,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("current official schedule overlay source is invalid") from exc
    source_path = unquote(urlparse(kickoff_source.get("url", "")).path)
    if not re.fullmatch(path_pattern, source_path):
        raise ValueError("current official schedule overlay URL path is not allowlisted")
    native_fixture_id = str(kickoff_source.get("native_fixture_id") or "")
    provider_ids = fixture.get("provider_fixture_ids")
    if (
        not native_fixture_id
        or overlay.get("provider_fixture_id") != native_fixture_id
        or not isinstance(provider_ids, dict)
        or provider_ids.get(provider_id_key) != native_fixture_id
        or overlay.get("observed_at") != kickoff_source.get("retrieved_at")
        or fixture.get("kickoff_time_observed_at") != kickoff_source.get("retrieved_at")
    ):
        raise ValueError("current official schedule overlay identity or clock is invalid")
    try:
        kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]))
        prior = datetime.fromisoformat(str(overlay["prior_kickoff_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("current official schedule overlay kickoff is invalid") from exc
    if kickoff.tzinfo is None or prior.tzinfo is None:
        raise ValueError("current official schedule overlay kickoff is invalid")
    delta = int(
        (kickoff.astimezone(timezone.utc) - prior.astimezone(timezone.utc)).total_seconds()
    )
    if (
        isinstance(overlay.get("kickoff_delta_seconds"), bool)
        or not isinstance(overlay.get("kickoff_delta_seconds"), int)
        or overlay.get("kickoff_delta_seconds") != delta
        or abs(delta) > int(timedelta(days=7).total_seconds())
    ):
        raise ValueError("current official schedule overlay delta is invalid")
    if "venue" in fields:
        venue_source = field_sources.get("venue")
        venue = fixture.get("venue")
        if (
            venue_source != kickoff_source
            or not isinstance(venue, dict)
            or venue.get("source") != kickoff_source
            or not venue.get("name")
        ):
            raise ValueError("current official schedule overlay venue is invalid")


def _validate_live_snapshot(live: dict, as_of: datetime, competition_ids: set[str]) -> None:
    valid_statuses = {"upcoming", "live", "finished", "postponed", "cancelled"}
    selected_feed_key = fixture_feed_key(live)
    selected_feed = fixture_feed_section(live)
    fixtures = fixture_rows(live)
    fixture_ids = {fixture.get("id") for fixture in fixtures}
    if selected_feed_key == "fixture_feed":
        aggregate_provider = selected_feed.get("provider")
        if aggregate_provider not in {"OpenFootball", MULTI_SOURCE_FIXTURE_PROVIDER}:
            raise ValueError("current fixture feed aggregation provider is invalid")
        fixture_source_names = {
            fixture.get("source", {}).get("name")
            for fixture in fixtures
            if isinstance(fixture.get("source"), dict)
        }
        if aggregate_provider == "OpenFootball" and fixture_source_names - {"OpenFootball"}:
            raise ValueError("OpenFootball fixture feed contains a relabelled provider row")
        if aggregate_provider == MULTI_SOURCE_FIXTURE_PROVIDER:
            contract = selected_feed.get("source_contract")
            fact_sources = contract.get("fact_sources") if isinstance(contract, dict) else None
            provider_status = selected_feed.get("provider_status")
            if (
                not isinstance(contract, dict)
                or contract.get("aggregation_role")
                != "provider-neutral union; row source remains authoritative"
                or not isinstance(fact_sources, list)
                or not fact_sources
                or any(
                    not isinstance(provider, str) or not provider.strip()
                    for provider in fact_sources
                )
                or len(set(fact_sources)) != len(fact_sources)
                or not fixture_source_names.issubset(set(fact_sources))
            ):
                raise ValueError("current multi-source fixture aggregation contract is invalid")
            if provider_status is None:
                # Replay snapshots written before per-provider availability
                # was materialized only when every declared source contributed
                # at least one exact row.
                if set(fact_sources) != fixture_source_names:
                    raise ValueError(
                        "current multi-source fixture aggregation contract is invalid"
                    )
            else:
                if not isinstance(provider_status, list) or len(provider_status) != len(
                    fact_sources
                ):
                    raise ValueError("current multi-source fixture provider status is invalid")
                fixture_counts = {
                    provider: sum(
                        fixture.get("source", {}).get("name") == provider
                        for fixture in fixtures
                        if isinstance(fixture.get("source"), dict)
                    )
                    for provider in fact_sources
                }
                status_by_provider: dict[str, dict] = {}
                for row in provider_status:
                    if not isinstance(row, dict):
                        raise ValueError("current multi-source fixture provider status is invalid")
                    provider = row.get("provider")
                    count = row.get("fixture_count")
                    error_count = row.get("error_count")
                    if (
                        not isinstance(provider, str)
                        or provider not in fact_sources
                        or provider in status_by_provider
                        or row.get("status") not in {"ok", "stale", "degraded", "unavailable"}
                        or isinstance(count, bool)
                        or not isinstance(count, int)
                        or count < 0
                        or count != fixture_counts[provider]
                        or isinstance(error_count, bool)
                        or not isinstance(error_count, int)
                        or error_count < 0
                    ):
                        raise ValueError("current multi-source fixture provider status is invalid")
                    status_by_provider[provider] = row
                if set(status_by_provider) != set(fact_sources):
                    raise ValueError("current multi-source fixture provider status is invalid")
    for fixture in fixtures:
        if fixture.get("competition_id") not in competition_ids:
            raise ValueError("current fixture has unknown competition")
        if fixture.get("status") not in valid_statuses:
            raise ValueError("current fixture has invalid status")
        kickoff = datetime.fromisoformat(fixture["kickoff_at"])
        if kickoff.tzinfo is None:
            raise ValueError("current fixture kickoff_at must be timezone-aware")
        source = fixture["source"]
        if selected_feed_key == "fixture_feed":
            lineage = fixture.get("lineage")
            if source.get("name") == "OpenFootball":
                _validate_source(
                    source,
                    as_of,
                    hash_keys=("raw_sha256",),
                    expected_name="OpenFootball",
                    allowed_hosts={"raw.githubusercontent.com"},
                )
                source_id = source.get("source_id")
                contract = OPENFOOTBALL_SOURCES.get(str(source_id))
                provider_fixture_ids = fixture.get("provider_fixture_ids")
                if (
                    source.get("license") != "CC0-1.0"
                    or not str(fixture.get("id") or "").startswith("openfootball:")
                    or (
                        isinstance(provider_fixture_ids, dict)
                        and "OpenFootball" in provider_fixture_ids
                        and provider_fixture_ids.get("OpenFootball") != fixture.get("id")
                    )
                    or fixture.get("kickoff_time_quality") != "exact"
                    or contract is None
                    or contract.get("url") != source.get("url")
                    or contract.get("competition_id") != fixture.get("competition_id")
                    or not isinstance(lineage, dict)
                    or lineage.get("source_id") != source_id
                    or lineage.get("raw_sha256") != source.get("raw_sha256")
                    or not lineage.get("source_row_id")
                ):
                    raise ValueError("current OpenFootball fixture source contract is invalid")
                _validate_official_schedule_overlay(fixture, as_of)
            elif source.get("name") == CFL_SOURCE_NAME:
                _validate_source(
                    source,
                    as_of,
                    hash_keys=("raw_sha256",),
                    expected_name=CFL_SOURCE_NAME,
                    allowed_hosts={CFL_API_HOST},
                )
                native_fixture_id = fixture.get("native_fixture_id")
                source_id = source.get("source_id")
                parsed_url = urlparse(str(source.get("url") or ""))
                query = parse_qs(parsed_url.query, keep_blank_values=True)
                try:
                    observed_at = datetime.fromisoformat(str(fixture["observed_at"]))
                    effective_at = datetime.fromisoformat(str(fixture["effective_at"]))
                    source_observed_at = datetime.fromisoformat(str(source["retrieved_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "current CFL official fixture time semantics are invalid"
                    ) from exc
                time_semantics = fixture.get("time_semantics")
                if (
                    parsed_url.path != CFL_MATCHES_PATH
                    or fixture.get("competition_id") != "csl"
                    or fixture.get("kickoff_time_quality") != "exact"
                    or fixture.get("kickoff_timezone") != "Asia/Shanghai"
                    or fixture.get("kickoff_time_source") != "official_local_date_time"
                    or not isinstance(native_fixture_id, str)
                    or not native_fixture_id
                    or fixture.get("id") != f"cfl-official:csl:{native_fixture_id}"
                    or fixture.get("provider_fixture_ids") != {"cfl_official": native_fixture_id}
                    or not fixture.get("home_provider_team_id")
                    or not fixture.get("away_provider_team_id")
                    or source_id != f"cfl-official:csl:{fixture.get('season')}"
                    or source.get("license") != "not_published"
                    or source.get("rights_status") != CFL_SOURCE_RIGHTS_STATUS
                    or source.get("commercial_reuse_verified") is not False
                    or source.get("access_basis") != CFL_SOURCE_ACCESS_BASIS
                    or source.get("official_page_url") != CFL_OFFICIAL_PAGE_URL
                    or observed_at.tzinfo is None
                    or effective_at.tzinfo is None
                    or observed_at.astimezone(timezone.utc)
                    != source_observed_at.astimezone(timezone.utc)
                    or observed_at.astimezone(timezone.utc) > as_of.astimezone(timezone.utc)
                    or effective_at.astimezone(timezone.utc)
                    > observed_at.astimezone(timezone.utc) + timedelta(minutes=5)
                    or not isinstance(time_semantics, dict)
                    or time_semantics.get("effective_at_basis")
                    not in {"provider_update_time", "capture_retrieved_at"}
                    or time_semantics.get("observed_at_basis") != "capture_retrieved_at"
                    or query.get("competition_code") != ["CSL"]
                    or query.get("curPage") != ["1"]
                    or query.get("pageSize") != ["400"]
                    or not query.get("tournament_calendar_id", [""])[0]
                    or not query.get("stage_id", [""])[0]
                    or not isinstance(lineage, dict)
                    or lineage.get("source_id") != source_id
                    or lineage.get("url") != source.get("url")
                    or lineage.get("raw_sha256") != source.get("raw_sha256")
                    or lineage.get("native_fixture_id") != native_fixture_id
                    or lineage.get("effective_at") != fixture.get("effective_at")
                    or lineage.get("observed_at") != fixture.get("observed_at")
                    or not lineage.get("source_row_id")
                ):
                    raise ValueError("current CFL official fixture source contract is invalid")
                validate_cfl_venue(fixture.get("venue"), fixture_source=source)
            else:
                raise ValueError("current fixture source provider is not allowlisted")
        else:
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="ESPN",
                allowed_hosts=ESPN_ALLOWED_HOSTS,
            )
            if (
                not all(
                    fixture.get(key)
                    for key in ("id", "home_provider_team_id", "away_provider_team_id")
                )
                or source.get("native_fixture_id") is None
            ):
                raise ValueError("current fixture is missing provider-native IDs")
        score = fixture.get("score")
        if fixture["status"] == "finished" and (
            not isinstance(score, dict)
            or not all(
                isinstance(score.get(key), int) and score[key] >= 0 for key in ("home", "away")
            )
        ):
            raise ValueError("finished current fixture requires a non-negative integer score")
    for feature in live.get("understat", {}).get("team_features", []):
        if feature.get("competition_id") not in competition_ids:
            raise ValueError("current feature has unknown competition")
        _validate_source(
            feature["source"],
            as_of,
            hash_keys=("wire_sha256", "content_sha256"),
            expected_name="Understat",
            allowed_hosts={"understat.com"},
            require_all_hashes=True,
        )
        for key in ("xg_for", "xg_against"):
            if (
                key not in feature
                or not math.isfinite(float(feature[key]))
                or float(feature[key]) < 0
            ):
                raise ValueError("current xG feature must be present, finite and non-negative")
    for market in live.get("espn_markets", {}).get("markets", []):
        if selected_feed_key == "fixture_feed":
            raise ValueError("unlicensed ESPN markets cannot enrich OpenFootball fixtures")
        if market.get("fixture_id") not in fixture_ids or not market.get("provider"):
            raise ValueError("current market is missing a known fixture or provider")
        market_source = {**market["source"], "retrieved_at": market["retrieved_at"]}
        _validate_source(
            market_source,
            as_of,
            hash_keys=("raw_sha256",),
            expected_name="ESPN event summary",
            allowed_hosts=ESPN_ALLOWED_HOSTS,
        )
        values = market.get("probability", {}).values()
        if set(market.get("probability", {})) != {"home", "draw", "away"} or not all(
            math.isfinite(float(value)) and 0 <= float(value) <= 1 for value in values
        ):
            raise ValueError("current market probabilities must be finite and bounded")
        if abs(sum(float(value) for value in values) - 1) > 1e-5:
            raise ValueError("current market probabilities must sum to one")

    team_status_rows = live.get("espn_markets", {}).get("team_status", [])
    if not isinstance(team_status_rows, list):
        raise ValueError("current ESPN team status contract is invalid")
    for status in team_status_rows:
        if selected_feed_key == "fixture_feed":
            raise ValueError("unlicensed ESPN team status cannot enrich OpenFootball fixtures")
        if (
            not isinstance(status, dict)
            or status.get("fixture_id") not in fixture_ids
            or status.get("provider") != "ESPN event summary"
            or status.get("status") not in {"roster_evidence_only", "confirmed_lineup"}
            or not isinstance(status.get("confirmed"), bool)
            or not isinstance(status.get("model_eligible"), bool)
            or not isinstance(status.get("teams"), dict)
            or not isinstance(status.get("source"), dict)
        ):
            raise ValueError("current ESPN team status contract is invalid")
        if status["status"] == "confirmed_lineup" and status["confirmed"] is not True:
            raise ValueError("current ESPN confirmed lineup must be marked confirmed")
        if status["status"] == "roster_evidence_only" and status["confirmed"] is True:
            raise ValueError("current ESPN roster evidence cannot be marked confirmed")
        if status["model_eligible"] and (
            status["confirmed"] is not True or status["status"] != "confirmed_lineup"
        ):
            raise ValueError("current ESPN model eligibility requires confirmed lineup")
        source = {**status["source"], "retrieved_at": status.get("retrieved_at")}
        _validate_source(
            source,
            as_of,
            hash_keys=("raw_sha256",),
            expected_name="ESPN event summary",
            allowed_hosts=ESPN_ALLOWED_HOSTS,
        )
        for side in ("home", "away"):
            team = status["teams"].get(side)
            if team is None:
                continue
            if not isinstance(team, dict) or not isinstance(team.get("players"), list):
                raise ValueError("current ESPN team status team contract is invalid")
            starter_count = team.get("starter_count")
            if starter_count is not None and (
                not isinstance(starter_count, int) or starter_count < 0
            ):
                raise ValueError("current ESPN team status starter count is invalid")
            for player in team["players"]:
                if (
                    not isinstance(player, dict)
                    or not player.get("player_id")
                    or not isinstance(player.get("name"), str)
                ):
                    raise ValueError("current ESPN team status player contract is invalid")
        if status["model_eligible"]:
            fixture = next(
                (item for item in fixtures if item.get("id") == status.get("fixture_id")),
                {},
            )
            if not fixture:
                raise ValueError(
                    "current ESPN model-eligible lineup references an unknown fixture"
                )
            try:
                kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]))
                observed = datetime.fromisoformat(
                    str(status.get("retrieved_at") or status["source"].get("retrieved_at"))
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "current ESPN model-eligible lineup has invalid time semantics"
                ) from exc
            if kickoff.tzinfo is None or observed.tzinfo is None or observed >= kickoff:
                raise ValueError("current ESPN model-eligible lineup must precede kickoff")
            if any(
                status["teams"].get(side, {}).get("starter_count") != 11
                for side in ("home", "away")
            ):
                raise ValueError(
                    "current ESPN model-eligible lineup requires eleven starters per side"
                )

    roster_payload = live.get("espn_rosters")
    if roster_payload is not None:
        if (
            not isinstance(roster_payload, dict)
            or roster_payload.get("provider") != "ESPN team roster"
            or not isinstance(roster_payload.get("rosters"), list)
            or not isinstance(roster_payload.get("errors"), list)
        ):
            raise ValueError("current ESPN roster provider contract is invalid")
        for roster in roster_payload["rosters"]:
            if (
                not isinstance(roster, dict)
                or not roster.get("competition_id")
                or not roster.get("provider_team_id")
            ):
                raise ValueError("current ESPN roster row is invalid")
            if roster.get("competition_id") not in competition_ids:
                raise ValueError("current ESPN roster has unknown competition")
            if roster.get("status") not in {"ok", "empty_roster"}:
                raise ValueError("current ESPN roster status is invalid")
            source = roster.get("source")
            if not isinstance(source, dict):
                raise ValueError("current ESPN roster source is invalid")
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="ESPN team roster",
                allowed_hosts=ESPN_ALLOWED_HOSTS,
            )
            athletes = roster.get("athletes")
            if not isinstance(athletes, list) or len(athletes) > 80:
                raise ValueError("current ESPN roster athlete list is invalid")
            if roster.get("athlete_count") != len(athletes):
                raise ValueError("current ESPN roster athlete count is invalid")
            if (
                roster.get("enters_model") is not False
                or roster.get("model_eligible") is not False
            ):
                raise ValueError("current ESPN team roster cannot enter the model")
            for athlete in athletes:
                if (
                    not isinstance(athlete, dict)
                    or not athlete.get("provider_player_id")
                    or not isinstance(athlete.get("name"), str)
                ):
                    raise ValueError("current ESPN roster athlete row is invalid")

    injury_payload = live.get("espn_injuries")
    if injury_payload is not None:
        if (
            not isinstance(injury_payload, dict)
            or injury_payload.get("provider") != "ESPN injury report"
            or injury_payload.get("coverage_semantics")
            != "missing_team_bucket_is_unknown_not_healthy"
            or not isinstance(injury_payload.get("reports"), list)
            or not isinstance(injury_payload.get("competition_reports"), list)
            or not isinstance(injury_payload.get("errors"), list)
        ):
            raise ValueError("current ESPN injury provider contract is invalid")
        if (
            injury_payload.get("model_eligible") is not False
            or injury_payload.get("enters_model") is not False
        ):
            raise ValueError("current ESPN injury provider must remain display-only")
        seen_report_keys: set[tuple[str, str]] = set()
        for report in injury_payload["reports"]:
            if not isinstance(report, dict):
                raise ValueError("current ESPN injury report row is invalid")
            competition_id = report.get("competition_id")
            provider_team_id = report.get("provider_team_id")
            injuries = report.get("injuries")
            if (
                competition_id not in competition_ids
                or competition_id not in ESPN_CODES
                or not isinstance(provider_team_id, str)
                or re.fullmatch(r"[1-9][0-9]{0,15}", provider_team_id) is None
                or report.get("report_status") != "published"
                or not isinstance(injuries, list)
                or len(injuries) > 80
                or report.get("reported_player_count") != len(injuries)
            ):
                raise ValueError("current ESPN injury report row is invalid")
            report_key = (competition_id, provider_team_id)
            if report_key in seen_report_keys:
                raise ValueError("current ESPN injury report has duplicate team bucket")
            seen_report_keys.add(report_key)
            if (
                report.get("model_eligible") is not False
                or report.get("enters_model") is not False
            ):
                raise ValueError("current ESPN injury report must remain display-only")
            scheduled_fixture_ids = report.get("scheduled_fixture_ids", [])
            if not isinstance(scheduled_fixture_ids, list) or any(
                item not in fixture_ids for item in scheduled_fixture_ids
            ):
                raise ValueError("current ESPN injury report fixture links are invalid")
            source = report.get("source")
            if not isinstance(source, dict):
                raise ValueError("current ESPN injury report source is invalid")
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="ESPN injury report",
                allowed_hosts=ESPN_ALLOWED_HOSTS,
            )
            expected_path = f"{ESPN_INJURY_PATH_PREFIX}{ESPN_CODES[competition_id]}/injuries"
            if urlparse(source.get("url", "")).path != expected_path:
                raise ValueError("current ESPN injury report source path is not allowlisted")
            policy = source.get("policy")
            if not isinstance(policy, dict) or policy.get("allow_model") is not False:
                raise ValueError("current ESPN injury report source policy is invalid")
            for player in injuries:
                if (
                    not isinstance(player, dict)
                    or re.fullmatch(
                        r"[1-9][0-9]{0,15}",
                        str(player.get("provider_player_id") or ""),
                    )
                    is None
                    or not isinstance(player.get("name"), str)
                    or not player["name"].strip()
                    or not isinstance(player.get("status"), str)
                    or not player["status"].strip()
                ):
                    raise ValueError("current ESPN injury player row is invalid")

    news = live.get("news")
    if news is not None:
        if (
            not isinstance(news, dict)
            or news.get("provider") != "Public RSS"
            or not isinstance(news.get("feeds"), list)
            or not isinstance(news.get("errors"), list)
            or not isinstance(news.get("item_count"), int)
        ):
            raise ValueError("current news provider contract is invalid")
        observed_items = 0
        for feed in news["feeds"]:
            if (
                not isinstance(feed, dict)
                or not isinstance(feed.get("name"), str)
                or not isinstance(feed.get("items"), list)
            ):
                raise ValueError("current news feed contract is invalid")
            parsed_url = urlparse(feed.get("url", ""))
            if parsed_url.hostname not in ALLOWED_NEWS_HOSTS:
                raise ValueError("current news URL or host is not allowlisted")
            _validate_source(
                feed,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name=feed["name"],
                allowed_hosts=ALLOWED_NEWS_HOSTS,
            )
            for item in feed["items"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("title"), str)
                    or not isinstance(item.get("link"), str)
                    or not item["link"].startswith("https://")
                ):
                    raise ValueError("current news item contract is invalid")
            observed_items += len(feed["items"])
        if news["item_count"] != observed_items:
            raise ValueError("current news item_count is inconsistent")

    weather = live.get("weather")
    if weather is not None:
        if (
            not isinstance(weather, dict)
            or weather.get("provider") != "Open-Meteo"
            or not isinstance(weather.get("weather"), list)
            or not isinstance(weather.get("errors"), list)
        ):
            raise ValueError("current weather provider contract is invalid")
        for observation in weather["weather"]:
            if not isinstance(observation, dict) or not observation.get("fixture_id"):
                raise ValueError("current weather observation contract is invalid")
            if observation["fixture_id"] not in fixture_ids:
                raise ValueError("current weather references an unknown fixture")
            source = observation.get("source")
            if not isinstance(source, dict):
                raise ValueError("current weather observation is missing provenance")
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Open-Meteo",
                allowed_hosts={OPEN_METEO_HOST},
            )
            if urlparse(source.get("url", "")).path != OPEN_METEO_PATH:
                raise ValueError("current weather URL path is not allowlisted")

    met_weather = live.get("met_norway_weather")
    if met_weather is not None:
        if (
            not isinstance(met_weather, dict)
            or met_weather.get("provider") != MET_SOURCE_NAME
            or met_weather.get("source_id") != MET_SOURCE_ID
            or met_weather.get("status") not in {"ok", "partial", "unavailable"}
            or not isinstance(met_weather.get("weather"), list)
            or not isinstance(met_weather.get("errors"), list)
            or met_weather.get("license") != MET_LICENSE
            or met_weather.get("attribution_required") is not True
            or not isinstance(met_weather.get("network_opened"), bool)
        ):
            raise ValueError("MET Norway weather provider contract is invalid")
        for observation in met_weather["weather"]:
            if not isinstance(observation, dict) or not observation.get("fixture_id"):
                raise ValueError("MET Norway weather observation contract is invalid")
            if observation["fixture_id"] not in fixture_ids:
                raise ValueError("MET Norway weather references an unknown fixture")
            try:
                forecast_at = datetime.fromisoformat(str(observation["forecast_at"]))
                latitude = float(observation["latitude"])
                longitude = float(observation["longitude"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("MET Norway weather observation values are invalid") from exc
            if (
                forecast_at.tzinfo is None
                or forecast_at.utcoffset() is None
                or forecast_at.minute != 0
                or forecast_at.second != 0
                or forecast_at.microsecond != 0
            ):
                raise ValueError("MET Norway forecast_at must be an exact timezone-aware hour")
            if not (
                math.isfinite(latitude)
                and math.isfinite(longitude)
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                raise ValueError("MET Norway weather coordinates are invalid")
            source = observation.get("source")
            if not isinstance(source, dict):
                raise ValueError("MET Norway weather observation is missing provenance")
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name=MET_SOURCE_NAME,
                allowed_hosts={MET_HOST},
            )
            parsed_source_url = urlparse(source.get("url", ""))
            if (
                parsed_source_url.path != MET_PATH
                or source.get("source_id") != MET_SOURCE_ID
                or source.get("license") != MET_LICENSE
                or source.get("attribution_required") is not True
            ):
                raise ValueError("MET Norway weather source contract is invalid")
            coordinate_confidence = observation.get("coordinate_confidence")
            coordinate_model_eligible = observation.get("coordinate_model_eligible")
            if coordinate_confidence is not None or coordinate_model_eligible is not None:
                if coordinate_confidence not in {"high", "medium", "unverified"} or not isinstance(
                    coordinate_model_eligible, bool
                ):
                    raise ValueError("MET Norway coordinate confidence contract is invalid")
                if coordinate_model_eligible is not (coordinate_confidence == "high"):
                    raise ValueError("MET Norway coordinate confidence contract is invalid")

    wikidata = live.get("wikidata_entities")
    if wikidata is not None:
        if (
            not isinstance(wikidata, dict)
            or wikidata.get("provider") != WIKIDATA_SOURCE_NAME
            or wikidata.get("source_id") != WIKIDATA_SOURCE_ID
            or wikidata.get("status") not in {"ok", "partial", "unavailable", "not_configured"}
            or not isinstance(wikidata.get("venues"), list)
            or not isinstance(wikidata.get("errors"), list)
            or not isinstance(wikidata.get("network_opened"), bool)
            or wikidata.get("license") != WIKIDATA_LICENSE
        ):
            raise ValueError("Wikidata venue provider contract is invalid")
        seen_wikidata_fixture_ids: set[str] = set()
        for item in wikidata["venues"]:
            if not isinstance(item, dict):
                raise ValueError("Wikidata venue observation contract is invalid")
            fixture_id = item.get("fixture_id")
            venue = item.get("venue")
            if not isinstance(fixture_id, str) or fixture_id not in fixture_ids:
                raise ValueError("Wikidata venue references an unknown fixture")
            if fixture_id in seen_wikidata_fixture_ids:
                raise ValueError("Wikidata venue has a duplicate fixture")
            seen_wikidata_fixture_ids.add(fixture_id)
            if not isinstance(venue, dict) or not isinstance(venue.get("name"), str):
                raise ValueError("Wikidata venue fields are invalid")
            wikidata_id = venue.get("wikidata_id")
            if (
                not isinstance(wikidata_id, str)
                or re.fullmatch(r"Q[1-9][0-9]*", wikidata_id) is None
            ):
                raise ValueError("Wikidata venue entity ID is invalid")
            claim_rank = venue.get("claim_rank")
            confidence = venue.get("confidence")
            model_eligible = venue.get("model_eligible")
            if (
                claim_rank not in {"preferred", "normal"}
                or confidence not in {"high", "medium"}
                or not isinstance(model_eligible, bool)
            ):
                raise ValueError("Wikidata venue confidence contract is invalid")
            if (
                claim_rank == "preferred" and (confidence != "high" or model_eligible is not True)
            ) or (
                claim_rank == "normal" and (confidence != "medium" or model_eligible is not False)
            ):
                raise ValueError("Wikidata venue confidence contract is invalid")
            try:
                latitude = float(venue["latitude"])
                longitude = float(venue["longitude"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Wikidata venue coordinates are invalid") from exc
            if not (
                math.isfinite(latitude)
                and math.isfinite(longitude)
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                raise ValueError("Wikidata venue coordinates are invalid")
            source = venue.get("source")
            if not isinstance(source, dict):
                raise ValueError("Wikidata venue source is missing")
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name=WIKIDATA_SOURCE_NAME,
                allowed_hosts={WIKIDATA_HOST},
            )
            parsed_source_url = urlparse(source.get("url", ""))
            if (
                parsed_source_url.path != WIKIDATA_PATH
                or source.get("source_id") != WIKIDATA_SOURCE_ID
                or source.get("license") != WIKIDATA_LICENSE
                or source.get("attribution_required") is not False
            ):
                raise ValueError("Wikidata venue source contract is invalid")
            fixture = next(row for row in fixtures if row.get("id") == fixture_id)
            attached = fixture.get("venue")
            if not isinstance(attached, dict):
                raise ValueError("Wikidata venue is not attached to its fixture")
            if (
                attached.get("wikidata_id") != wikidata_id
                or attached.get("latitude") != latitude
                or attached.get("longitude") != longitude
                or attached.get("source") != source
            ):
                raise ValueError("Wikidata fixture venue projection is inconsistent")
        if wikidata.get("status") == "ok" and not wikidata["venues"]:
            raise ValueError("Wikidata ok status requires venue rows")
        if wikidata.get("status") == "not_configured" and wikidata.get("network_opened"):
            raise ValueError("Wikidata not_configured lane cannot open the network")

    geocoding = live.get("geocoding")
    if geocoding is not None:
        if (
            not isinstance(geocoding, dict)
            or geocoding.get("provider") != "Open-Meteo Geocoding"
            or not isinstance(geocoding.get("geocodes"), list)
            or not isinstance(geocoding.get("errors"), list)
        ):
            raise ValueError("current geocoding provider contract is invalid")
        for item in geocoding["geocodes"]:
            if not isinstance(item, dict) or not item.get("fixture_id"):
                raise ValueError("current geocoding observation contract is invalid")
            if item["fixture_id"] not in fixture_ids:
                raise ValueError("current geocoding references an unknown fixture")
            try:
                latitude = float(item["latitude"])
                longitude = float(item["longitude"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("current geocoding coordinates are invalid") from exc
            if not (
                math.isfinite(latitude)
                and math.isfinite(longitude)
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                raise ValueError("current geocoding coordinates are out of range")
            _validate_source(
                item["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Open-Meteo Geocoding",
                allowed_hosts={GEOCODING_HOST},
            )
            if urlparse(item["source"].get("url", "")).path != GEOCODING_PATH:
                raise ValueError("current geocoding URL path is not allowlisted")

    openligadb = live.get("openligadb")
    if openligadb is not None:
        if (
            not isinstance(openligadb, dict)
            or openligadb.get("provider") != "OpenLigaDB"
            or not isinstance(openligadb.get("matches"), list)
            or not isinstance(openligadb.get("errors"), list)
        ):
            raise ValueError("current OpenLigaDB provider contract is invalid")
        for match in openligadb["matches"]:
            if (
                not isinstance(match, dict)
                or not match.get("id")
                or match.get("competition_id") != "bundesliga"
                or match.get("status") not in {"upcoming", "finished"}
                or not isinstance(match.get("source"), dict)
            ):
                raise ValueError("current OpenLigaDB match contract is invalid")
            kickoff = datetime.fromisoformat(str(match.get("kickoff_at")))
            if kickoff.tzinfo is None or kickoff.utcoffset() is None:
                raise ValueError("current OpenLigaDB kickoff must be timezone-aware")
            _validate_source(
                match["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="OpenLigaDB",
                allowed_hosts={OPENLIGADB_HOST},
            )
            if not OPENLIGADB_PATH.fullmatch(urlparse(match["source"].get("url", "")).path):
                raise ValueError("current OpenLigaDB URL path is not allowlisted")
            if (
                match["source"].get("license") != "ODbL-1.0"
                or match["source"].get("license_url") != "https://www.openligadb.de/lizenz"
                or match["source"].get("attribution_required") is not True
            ):
                raise ValueError("current OpenLigaDB license contract is invalid")
            provider_match_id = match.get("provider_match_id")
            if (
                not isinstance(provider_match_id, str)
                or not provider_match_id.isdigit()
                or match.get("id") != f"openligadb:{provider_match_id}"
            ):
                raise ValueError("current OpenLigaDB match identity is invalid")
            score = match.get("score")
            if score is not None and (
                not isinstance(score, dict)
                or any(
                    isinstance(score.get(key), bool)
                    or not isinstance(score.get(key), int)
                    or score[key] < 0
                    for key in ("home", "away")
                )
            ):
                raise ValueError("current OpenLigaDB score contract is invalid")
            halftime_score = match.get("halftime_score")
            if halftime_score is not None and (
                not isinstance(halftime_score, dict)
                or any(
                    isinstance(halftime_score.get(key), bool)
                    or not isinstance(halftime_score.get(key), int)
                    or halftime_score[key] < 0
                    for key in ("home", "away")
                )
                or (
                    isinstance(score, dict)
                    and any(halftime_score[key] > score[key] for key in ("home", "away"))
                )
            ):
                raise ValueError("current OpenLigaDB halftime score contract is invalid")
            provider_updated_at = match.get("provider_updated_at")
            if provider_updated_at is not None:
                try:
                    provider_updated = datetime.fromisoformat(str(provider_updated_at))
                except ValueError as exc:
                    raise ValueError("current OpenLigaDB provider update time is invalid") from exc
                if (
                    provider_updated.tzinfo is None
                    or provider_updated.utcoffset() is None
                    or provider_updated.astimezone(timezone.utc)
                    > as_of.astimezone(timezone.utc) + timedelta(minutes=5)
                ):
                    raise ValueError("current OpenLigaDB provider update time is invalid")
            venue = match.get("venue")
            if venue is not None:
                if not isinstance(venue, dict) or set(venue) - {
                    "provider_location_id",
                    "city",
                    "stadium",
                }:
                    raise ValueError("current OpenLigaDB venue contract is invalid")
                for key, limit in (
                    ("provider_location_id", 20),
                    ("city", 160),
                    ("stadium", 200),
                ):
                    value = venue.get(key)
                    if value is not None and (
                        not isinstance(value, str)
                        or not value.strip()
                        or len(value) > limit
                        or (key == "provider_location_id" and not value.isdigit())
                    ):
                        raise ValueError("current OpenLigaDB venue contract is invalid")
            goals = match.get("goals", [])
            if not isinstance(goals, list) or len(goals) > 100:
                raise ValueError("current OpenLigaDB goal contract is invalid")
            goal_ids: set[str] = set()
            for goal in goals:
                if not isinstance(goal, dict):
                    raise ValueError("current OpenLigaDB goal contract is invalid")
                goal_id = goal.get("provider_goal_id")
                if not isinstance(goal_id, str) or not goal_id.isdigit() or goal_id in goal_ids:
                    raise ValueError("current OpenLigaDB goal identity is invalid")
                goal_ids.add(goal_id)
                if any(
                    isinstance(goal.get(key), bool)
                    or not isinstance(goal.get(key), int)
                    or goal[key] < 0
                    for key in ("home_score", "away_score")
                ):
                    raise ValueError("current OpenLigaDB goal score is invalid")
                minute = goal.get("minute")
                if minute is not None and (
                    isinstance(minute, bool)
                    or not isinstance(minute, int)
                    or minute < 0
                    or minute > 240
                ):
                    raise ValueError("current OpenLigaDB goal minute is invalid")
                for key in ("penalty", "own_goal", "overtime"):
                    if goal.get(key) is not None and not isinstance(goal.get(key), bool):
                        raise ValueError("current OpenLigaDB goal flag is invalid")
                for key, limit in (
                    ("provider_scorer_id", 20),
                    ("provider_scoring_team_id", 20),
                    ("scorer", 160),
                    ("comment", 240),
                ):
                    value = goal.get(key)
                    if value is not None and (
                        not isinstance(value, str)
                        or len(value) > limit
                        or (
                            key in {"provider_scorer_id", "provider_scoring_team_id"}
                            and not value.isdigit()
                        )
                    ):
                        raise ValueError("current OpenLigaDB goal metadata is invalid")

    official = live.get("premier_league_official")
    if official is not None:
        if (
            not isinstance(official, dict)
            or official.get("provider") != "Premier League official"
            or not isinstance(official.get("fixtures"), list)
            or not isinstance(official.get("lineups"), list)
            or not isinstance(official.get("errors"), list)
        ):
            raise ValueError("current Premier League official provider contract is invalid")
        for fixture in official["fixtures"]:
            if (
                not isinstance(fixture, dict)
                or not fixture.get("id")
                or fixture.get("competition_id") != "premier-league"
                or fixture.get("status") not in {"upcoming", "finished"}
                or not isinstance(fixture.get("source"), dict)
            ):
                raise ValueError("current Premier League official fixture contract is invalid")
            kickoff = datetime.fromisoformat(str(fixture.get("kickoff_at")))
            if kickoff.tzinfo is None or kickoff.utcoffset() is None:
                raise ValueError("current Premier League official kickoff must be timezone-aware")
            _validate_source(
                fixture["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Premier League official",
                allowed_hosts={PREMIER_LEAGUE_HOST},
            )
            if not re.fullmatch(
                PREMIER_LEAGUE_MATCHWEEK_PATH, urlparse(fixture["source"].get("url", "")).path
            ):
                raise ValueError(
                    "current Premier League official fixture URL path is not allowlisted"
                )
        for row in official["lineups"]:
            if (
                not isinstance(row, dict)
                or not row.get("match_id")
                or not isinstance(row.get("lineups"), dict)
                or not isinstance(row.get("source"), dict)
            ):
                raise ValueError("current Premier League official lineup contract is invalid")
            _validate_source(
                row["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Premier League official",
                allowed_hosts={PREMIER_LEAGUE_HOST},
            )
            if not re.fullmatch(
                PREMIER_LEAGUE_LINEUPS_PATH, urlparse(row["source"].get("url", "")).path
            ):
                raise ValueError(
                    "current Premier League official lineup URL path is not allowlisted"
                )
            lineups = row["lineups"]
            if not isinstance(lineups.get("confirmed"), bool) or not isinstance(
                lineups.get("model_eligible"), bool
            ):
                raise ValueError("current Premier League official lineup eligibility is invalid")
            if lineups["model_eligible"] and not lineups["confirmed"]:
                raise ValueError(
                    "current Premier League model eligibility requires confirmed lineup"
                )
            if lineups["model_eligible"]:
                # The parser applies this rule, but the snapshot is an
                # untrusted boundary too.  Re-check the exact official
                # fixture and source observation here so a tampered or stale
                # snapshot cannot move a post-kickoff lineup into the model.
                fixture_id = row.get("fixture_id")
                official_fixture = next(
                    (
                        fixture
                        for fixture in official["fixtures"]
                        if fixture.get("id") == fixture_id
                    ),
                    None,
                )
                if official_fixture is None:
                    raise ValueError(
                        "current Premier League model-eligible lineup must reference an official fixture"
                    )
                try:
                    kickoff = datetime.fromisoformat(str(official_fixture["kickoff_at"]))
                    observed = datetime.fromisoformat(str(row["source"]["retrieved_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "current Premier League model-eligible lineup has invalid time semantics"
                    ) from exc
                if kickoff.tzinfo is None or observed.tzinfo is None:
                    raise ValueError(
                        "current Premier League model-eligible lineup times must be timezone-aware"
                    )
                if observed >= kickoff:
                    raise ValueError(
                        "current Premier League model-eligible lineup must precede kickoff"
                    )

    laliga_official = live.get("laliga_official")
    if laliga_official is not None:
        if (
            not isinstance(laliga_official, dict)
            or laliga_official.get("provider") != "LaLiga official"
            or not isinstance(laliga_official.get("fixtures"), list)
            or not isinstance(laliga_official.get("lineups"), list)
            or not isinstance(laliga_official.get("errors"), list)
        ):
            raise ValueError("current LaLiga official provider contract is invalid")
        for fixture in laliga_official["fixtures"]:
            if (
                not isinstance(fixture, dict)
                or not fixture.get("id")
                or fixture.get("competition_id") != "la-liga"
                or fixture.get("status") not in {"upcoming", "finished"}
                or not isinstance(fixture.get("source"), dict)
            ):
                raise ValueError("current LaLiga official fixture contract is invalid")
            kickoff = datetime.fromisoformat(str(fixture.get("kickoff_at")))
            if kickoff.tzinfo is None or kickoff.utcoffset() is None:
                raise ValueError("current LaLiga official kickoff must be timezone-aware")
            _validate_source(
                fixture["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="LaLiga official",
                allowed_hosts={LALIGA_HOST},
            )
            if not LALIGA_MATCH_PATH.fullmatch(urlparse(fixture["source"].get("url", "")).path):
                raise ValueError("current LaLiga official fixture URL path is not allowlisted")
        official_fixture_by_id = {
            fixture["id"]: fixture
            for fixture in laliga_official["fixtures"]
            if isinstance(fixture, dict) and fixture.get("id")
        }
        for row in laliga_official["lineups"]:
            if (
                not isinstance(row, dict)
                or row.get("fixture_id") not in official_fixture_by_id
                or not isinstance(row.get("lineups"), dict)
                or not isinstance(row.get("source"), dict)
            ):
                raise ValueError("current LaLiga official lineup contract is invalid")
            _validate_source(
                row["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="LaLiga official",
                allowed_hosts={LALIGA_HOST},
            )
            if not LALIGA_MATCH_PATH.fullmatch(urlparse(row["source"].get("url", "")).path):
                raise ValueError("current LaLiga official lineup URL path is not allowlisted")
            lineups = row["lineups"]
            if not isinstance(lineups.get("confirmed"), bool) or not isinstance(
                lineups.get("model_eligible"), bool
            ):
                raise ValueError("current LaLiga official lineup eligibility is invalid")
            if lineups["model_eligible"] and not lineups["confirmed"]:
                raise ValueError("current LaLiga model eligibility requires confirmed lineup")
            if lineups["model_eligible"]:
                try:
                    kickoff = datetime.fromisoformat(
                        str(official_fixture_by_id[row["fixture_id"]]["kickoff_at"])
                    )
                    observed = datetime.fromisoformat(str(row["source"]["retrieved_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "current LaLiga model-eligible lineup has invalid time semantics"
                    ) from exc
                if kickoff.tzinfo is None or observed.tzinfo is None:
                    raise ValueError(
                        "current LaLiga model-eligible lineup times must be timezone-aware"
                    )
                if observed >= kickoff:
                    raise ValueError("current LaLiga model-eligible lineup must precede kickoff")

    bundesliga_official = live.get("bundesliga_official")
    if bundesliga_official is not None:
        if (
            not isinstance(bundesliga_official, dict)
            or bundesliga_official.get("provider") != "Bundesliga official"
            or not isinstance(bundesliga_official.get("fixtures"), list)
            or not isinstance(bundesliga_official.get("lineups"), list)
            or not isinstance(bundesliga_official.get("errors"), list)
        ):
            raise ValueError("current Bundesliga official provider contract is invalid")
        official_fixture_by_id = {}
        for fixture in bundesliga_official["fixtures"]:
            if (
                not isinstance(fixture, dict)
                or not fixture.get("id")
                or fixture.get("competition_id") != "bundesliga"
                or fixture.get("status") not in {"upcoming", "finished"}
                or not isinstance(fixture.get("source"), dict)
            ):
                raise ValueError("current Bundesliga official fixture contract is invalid")
            kickoff = datetime.fromisoformat(str(fixture.get("kickoff_at")))
            if kickoff.tzinfo is None or kickoff.utcoffset() is None:
                raise ValueError("current Bundesliga official kickoff must be timezone-aware")
            _validate_source(
                fixture["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Bundesliga official",
                allowed_hosts={BUNDESLIGA_HOST},
            )
            if not BUNDESLIGA_MATCH_PATH.fullmatch(
                urlparse(fixture["source"].get("url", "")).path
            ):
                raise ValueError("current Bundesliga official fixture URL path is not allowlisted")
            official_fixture_by_id[fixture["id"]] = fixture
        for row in bundesliga_official["lineups"]:
            if (
                not isinstance(row, dict)
                or row.get("fixture_id") not in official_fixture_by_id
                or not isinstance(row.get("lineups"), dict)
                or not isinstance(row.get("source"), dict)
            ):
                raise ValueError("current Bundesliga official lineup contract is invalid")
            _validate_source(
                row["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Bundesliga official",
                allowed_hosts={BUNDESLIGA_HOST},
            )
            if not BUNDESLIGA_MATCH_PATH.fullmatch(urlparse(row["source"].get("url", "")).path):
                raise ValueError("current Bundesliga official lineup URL path is not allowlisted")
            lineups = row["lineups"]
            if not isinstance(lineups.get("confirmed"), bool) or not isinstance(
                lineups.get("model_eligible"), bool
            ):
                raise ValueError("current Bundesliga official lineup eligibility is invalid")
            if lineups["model_eligible"] and not lineups["confirmed"]:
                raise ValueError("current Bundesliga model eligibility requires confirmed lineup")
            if lineups["model_eligible"]:
                fixture = official_fixture_by_id[row["fixture_id"]]
                try:
                    kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]))
                    observed = datetime.fromisoformat(str(row["source"]["retrieved_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "current Bundesliga model-eligible lineup has invalid time semantics"
                    ) from exc
                if kickoff.tzinfo is None or observed.tzinfo is None or observed >= kickoff:
                    raise ValueError(
                        "current Bundesliga model-eligible lineup must precede kickoff"
                    )
                starter_counts = []
                for side in ("home", "away"):
                    players = (lineups.get(side) or {}).get("players", [])
                    if not isinstance(players, list) or any(
                        not isinstance(player, dict) for player in players
                    ):
                        raise ValueError("current Bundesliga lineup players contract is invalid")
                    starter_counts.append(
                        sum(1 for player in players if player.get("starter") is True)
                    )
                if any(count != 11 for count in starter_counts):
                    raise ValueError(
                        "current Bundesliga model-eligible lineup requires eleven starters per side"
                    )

    serie_a_official = live.get("serie_a_official")
    if serie_a_official is not None:
        if (
            not isinstance(serie_a_official, dict)
            or serie_a_official.get("provider") != "Serie A official"
            or not isinstance(serie_a_official.get("fixtures"), list)
            or not isinstance(serie_a_official.get("lineups"), list)
            or not isinstance(serie_a_official.get("errors"), list)
        ):
            raise ValueError("current Serie A official provider contract is invalid")
        official_fixture_by_id = {}
        for fixture in serie_a_official["fixtures"]:
            if (
                not isinstance(fixture, dict)
                or not fixture.get("id")
                or fixture.get("competition_id") != "serie-a"
                or fixture.get("status") not in {"upcoming", "finished"}
                or not isinstance(fixture.get("source"), dict)
            ):
                raise ValueError("current Serie A official fixture contract is invalid")
            kickoff = datetime.fromisoformat(str(fixture.get("kickoff_at")))
            if kickoff.tzinfo is None or kickoff.utcoffset() is None:
                raise ValueError("current Serie A official kickoff must be timezone-aware")
            _validate_source(
                fixture["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Serie A official",
                allowed_hosts={SERIE_A_API_HOST},
            )
            if not SERIE_A_API_PATH.fullmatch(
                unquote(urlparse(fixture["source"].get("url", "")).path)
            ):
                raise ValueError("current Serie A official fixture URL path is not allowlisted")
            official_fixture_by_id[fixture["id"]] = fixture
        for row in serie_a_official["lineups"]:
            if (
                not isinstance(row, dict)
                or row.get("fixture_id") not in official_fixture_by_id
                or not isinstance(row.get("lineups"), dict)
                or not isinstance(row.get("source"), dict)
            ):
                raise ValueError("current Serie A official lineup contract is invalid")
            _validate_source(
                row["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Serie A official",
                allowed_hosts={SERIE_A_API_HOST},
            )
            if not SERIE_A_API_PATH.fullmatch(
                unquote(urlparse(row["source"].get("url", "")).path)
            ):
                raise ValueError("current Serie A official lineup URL path is not allowlisted")
            lineups = row["lineups"]
            if not isinstance(lineups.get("confirmed"), bool) or not isinstance(
                lineups.get("model_eligible"), bool
            ):
                raise ValueError("current Serie A official lineup eligibility is invalid")
            if lineups["model_eligible"] and not lineups["confirmed"]:
                raise ValueError("current Serie A model eligibility requires confirmed lineup")
            if lineups["model_eligible"]:
                fixture = official_fixture_by_id[row["fixture_id"]]
                try:
                    kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]))
                    observed = datetime.fromisoformat(str(row["source"]["retrieved_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "current Serie A model-eligible lineup has invalid time semantics"
                    ) from exc
                if kickoff.tzinfo is None or observed.tzinfo is None or observed >= kickoff:
                    raise ValueError("current Serie A model-eligible lineup must precede kickoff")
                starter_counts = []
                for side in ("home", "away"):
                    players = (lineups.get(side) or {}).get("players", [])
                    if not isinstance(players, list) or any(
                        not isinstance(player, dict) for player in players
                    ):
                        raise ValueError("current Serie A lineup players contract is invalid")
                    starter_counts.append(
                        sum(1 for player in players if player.get("starter") is True)
                    )
                if any(count != 11 for count in starter_counts):
                    raise ValueError(
                        "current Serie A model-eligible lineup requires eleven starters per side"
                    )

    ligue1_official = live.get("ligue1_official")
    if ligue1_official is not None:
        if (
            not isinstance(ligue1_official, dict)
            or ligue1_official.get("provider") != "Ligue 1 official"
            or not isinstance(ligue1_official.get("fixtures"), list)
            or not isinstance(ligue1_official.get("lineups"), list)
            or not isinstance(ligue1_official.get("errors"), list)
        ):
            raise ValueError("current Ligue 1 official provider contract is invalid")
        official_fixture_by_id = {}
        for fixture in ligue1_official["fixtures"]:
            if (
                not isinstance(fixture, dict)
                or not fixture.get("id")
                or fixture.get("competition_id") != "ligue-1"
                or fixture.get("status") not in {"upcoming", "finished"}
                or not isinstance(fixture.get("source"), dict)
            ):
                raise ValueError("current Ligue 1 official fixture contract is invalid")
            kickoff = datetime.fromisoformat(str(fixture.get("kickoff_at")))
            if kickoff.tzinfo is None or kickoff.utcoffset() is None:
                raise ValueError("current Ligue 1 official kickoff must be timezone-aware")
            _validate_source(
                fixture["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Ligue 1 official",
                allowed_hosts={LIGUE1_API_HOST},
            )
            if not LIGUE1_MATCH_PATH.fullmatch(urlparse(fixture["source"].get("url", "")).path):
                raise ValueError("current Ligue 1 official fixture URL path is not allowlisted")
            official_fixture_by_id[fixture["id"]] = fixture
        for row in ligue1_official["lineups"]:
            if (
                not isinstance(row, dict)
                or row.get("fixture_id") not in official_fixture_by_id
                or not row.get("match_id")
                or not isinstance(row.get("lineups"), dict)
                or not isinstance(row.get("source"), dict)
            ):
                raise ValueError("current Ligue 1 official lineup contract is invalid")
            _validate_source(
                row["source"],
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Ligue 1 official",
                allowed_hosts={LIGUE1_API_HOST},
            )
            if not LIGUE1_MATCH_PATH.fullmatch(urlparse(row["source"].get("url", "")).path):
                raise ValueError("current Ligue 1 official lineup URL path is not allowlisted")
            if urlparse(row["source"].get("url", "")).path.rsplit("/", 1)[-1] != str(
                row["match_id"]
            ):
                raise ValueError("current Ligue 1 official lineup native ID does not match URL")
            lineups = row["lineups"]
            if not isinstance(lineups.get("confirmed"), bool) or not isinstance(
                lineups.get("model_eligible"), bool
            ):
                raise ValueError("current Ligue 1 official lineup eligibility is invalid")
            if lineups["model_eligible"] and not lineups["confirmed"]:
                raise ValueError("current Ligue 1 model eligibility requires confirmed lineup")
            if lineups["model_eligible"]:
                fixture = official_fixture_by_id[row["fixture_id"]]
                try:
                    kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]))
                    observed = datetime.fromisoformat(str(row["source"]["retrieved_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "current Ligue 1 model-eligible lineup has invalid time semantics"
                    ) from exc
                if kickoff.tzinfo is None or observed.tzinfo is None or observed >= kickoff:
                    raise ValueError("current Ligue 1 model-eligible lineup must precede kickoff")
                starter_counts = []
                for side in ("home", "away"):
                    players = (lineups.get(side) or {}).get("players", [])
                    if not isinstance(players, list) or any(
                        not isinstance(player, dict) for player in players
                    ):
                        raise ValueError("current Ligue 1 lineup players contract is invalid")
                    starter_counts.append(
                        sum(1 for player in players if player.get("starter") is True)
                    )
                if any(count != 11 for count in starter_counts):
                    raise ValueError(
                        "current Ligue 1 model-eligible lineup requires eleven starters per side"
                    )

    sofascore = live.get("sofascore")
    if sofascore is not None:
        if (
            not isinstance(sofascore, dict)
            or sofascore.get("provider") != "SofaScore"
            or not isinstance(sofascore.get("events"), list)
            or not isinstance(sofascore.get("errors"), list)
        ):
            raise ValueError("current SofaScore provider contract is invalid")
        for observation in sofascore["events"]:
            if not isinstance(observation, dict) or not observation.get("fixture_id"):
                raise ValueError("current SofaScore observation contract is invalid")
            if observation["fixture_id"] not in fixture_ids:
                raise ValueError("current SofaScore references an unknown fixture")
            for source_key in ("source", "lineups_source"):
                source = observation.get(source_key)
                if source is None:
                    continue
                if not isinstance(source, dict):
                    raise ValueError("current SofaScore provenance is invalid")
                _validate_source(
                    source,
                    as_of,
                    hash_keys=("raw_sha256",),
                    expected_name="SofaScore",
                    allowed_hosts={SOFASCORE_HOST},
                )
                if not urlparse(source.get("url", "")).path.startswith("/api/v1/"):
                    raise ValueError("current SofaScore URL path is not allowlisted")

    fotmob = live.get("fotmob")
    if fotmob is not None:
        if (
            not isinstance(fotmob, dict)
            or fotmob.get("provider") != FOTMOB_SOURCE_NAME
            or not isinstance(fotmob.get("lineups"), list)
            or not isinstance(fotmob.get("errors"), list)
        ):
            raise ValueError("current FotMob provider contract is invalid")
        for observation in fotmob["lineups"]:
            if (
                not isinstance(observation, dict)
                or observation.get("fixture_id") not in fixture_ids
                or not isinstance(observation.get("lineups"), dict)
                or not isinstance(observation.get("source"), dict)
            ):
                raise ValueError("current FotMob lineup contract is invalid")
            source = observation["source"]
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name=FOTMOB_SOURCE_NAME,
                allowed_hosts={FOTMOB_HOST},
            )
            if urlparse(source.get("url", "")).path != FOTMOB_DETAILS_PATH:
                raise ValueError("current FotMob URL path is not allowlisted")
            lineups = observation["lineups"]
            if not isinstance(lineups.get("confirmed"), bool) or not isinstance(
                lineups.get("model_eligible"), bool
            ):
                raise ValueError("current FotMob lineup eligibility is invalid")
            if lineups["model_eligible"] and not lineups["confirmed"]:
                raise ValueError("current FotMob model eligibility requires confirmed lineup")
            if lineups["model_eligible"]:
                raise ValueError(
                    "FotMob lineups are quarantined because its public API path is disallowed by robots.txt"
                )
            fixture = next(
                item for item in fixtures if item.get("id") == observation["fixture_id"]
            )
            kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]))
            observed = datetime.fromisoformat(str(source["retrieved_at"]))
            if lineups["model_eligible"] and observed >= kickoff:
                raise ValueError("current FotMob model-eligible lineup must precede kickoff")

    lottery = live.get("sports_lottery")
    if lottery is not None:
        if (
            not isinstance(lottery, dict)
            or lottery.get("provider") != "Sports Lottery"
            or not isinstance(lottery.get("matches"), list)
            or not isinstance(lottery.get("errors"), list)
        ):
            raise ValueError("current Sports Lottery provider contract is invalid")
        for match in lottery["matches"]:
            if not isinstance(match, dict) or not isinstance(match.get("source"), dict):
                raise ValueError("current Sports Lottery match contract is invalid")
            _validate_source(
                {**match["source"], "retrieved_at": lottery.get("retrieved_at")},
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Sports Lottery",
                allowed_hosts={"webapi.sporttery.cn"},
            )
            if urlparse(match["source"].get("url", "")).path not in SPORTS_LOTTERY_ALLOWED_PATHS:
                raise ValueError("current Sports Lottery URL path is not allowlisted")
            for key in _LOTTERY_PROBABILITY_KEYS:
                _validate_lottery_probability_map(match.get(key), key)

    oddstorm = live.get("oddstorm")
    if oddstorm is not None:
        if (
            not isinstance(oddstorm, dict)
            or oddstorm.get("provider") != "OddStorm public bookmaker comparison"
            or not isinstance(oddstorm.get("lines"), list)
            or not isinstance(oddstorm.get("errors"), list)
        ):
            raise ValueError("current OddStorm provider contract is invalid")
        if oddstorm.get("status") == "rights_blocked":
            if (
                oddstorm["lines"]
                or oddstorm.get("network_opened") is not False
                or oddstorm.get("access_allowed") is not False
                or oddstorm.get("rights_status") != ODDSTORM_RIGHTS_STATUS
                or oddstorm.get("terms_url") != ODDSTORM_TERMS_URL
            ):
                raise ValueError("current OddStorm rights-block contract is invalid")
        else:
            raise ValueError("current OddStorm facts are blocked by the v260 source policy")


def _lottery_competition_id(value: object) -> str | None:
    """Map only explicit allowlisted competition labels; never infer from teams."""

    text = " ".join(str(value or "").strip().casefold().split())
    return _LOTTERY_COMPETITION_ALIASES.get(text)


def _lottery_fixture_id(item: dict) -> str:
    material = "|".join(
        str(item.get(key) or "")
        for key in ("match_id", "match_num", "kickoff_at", "home_team", "away_team")
    )
    digest = hashlib.sha1(material.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return f"sporttery:{item.get('match_id') or digest}"


def _normalise_join_name(value: object) -> str:
    """Normalize provider names while preserving non-Latin scripts."""

    raw = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    # Keep CJK names intact: ASCII transliteration would erase the whole
    # value.  Latin names still use the accent-insensitive form used by the
    # historical identity layer.
    latin = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    text = latin if latin else raw
    return "".join(character for character in text if character.isalnum())


# First-party league pages sometimes expose a club's legal/display name while
# ESPN uses the common broadcast name.  Keep this map explicit and scoped to
# the provider/competition: it is an identity bridge, not fuzzy similarity.
# The LaLiga adapter has already validated each official page against the
# canonical fixture before writing these rows; this second map lets the
# current-snapshot join retain that high-confidence identity.
_OFFICIAL_TEAM_JOIN_ALIASES = {
    "premier-league": {
        "brightonandhovealbion": "brighton",
        "brightonhovealbion": "brighton",
        "brighton": "brighton",
        "afcbournemouth": "bournemouth",
        "bournemouth": "bournemouth",
    },
    "la-liga": {
        "rracingclub": "racingsantander",
        "racingclub": "racingsantander",
        "villarrealcf": "villarreal",
        "rcdespanoldebarcelona": "espanyol",
        "rcdespanyoldebarcelona": "espanyol",
        "espanol": "espanyol",
        "levanteud": "levante",
        "rcdeportivo": "deportivo",
        "elchecf": "elche",
        "deportivoalaves": "alaves",
        "alaves": "alaves",
        "getafecf": "getafe",
        "sevillafc": "sevilla",
        "rcdmallorca": "mallorca",
        "girona": "girona",
        "gironafc": "girona",
        "rcelta": "celtavigo",
        "celta": "celtavigo",
        "celtavigo": "celtavigo",
        "valenciacf": "valencia",
        "valencia": "valencia",
        "caosasuna": "osasuna",
        "athleticclub": "athleticclub",
        "clubatleticodemadrid": "atleticomadrid",
        "atleticodemadrid": "atleticomadrid",
    },
    "serie-a": {
        "inter": "internazionale",
        "internazionale": "internazionale",
        "acmilan": "acmilan",
        "milan": "acmilan",
        "asroma": "asroma",
        "roma": "asroma",
    },
    "ligue-1": {
        "olympiquedemarseille": "marseille",
        "marseille": "marseille",
        "rcstrasbourgalsace": "strasbourg",
        "strasbourg": "strasbourg",
        "asmonaco": "monaco",
        "monaco": "monaco",
        "staderennais": "rennes",
        "rennes": "rennes",
        "parissaintgermain": "parissaintgermain",
        "psg": "parissaintgermain",
        "ogcnice": "nice",
        "nice": "nice",
        "lehavreac": "lehavre",
        "lehavre": "lehavre",
    },
}


def _official_join_name(competition_id: object, value: object) -> str:
    canonical = (
        canonical_team_name(str(competition_id), value) if isinstance(value, str) else value
    )
    normalized = _normalise_join_name(canonical)
    aliases = _OFFICIAL_TEAM_JOIN_ALIASES.get(str(competition_id), {})
    return aliases.get(normalized, normalized)


def _official_fixture_join_diagnostics(
    canonical_fixtures: list[dict],
    official_fixtures: object,
    *,
    competition_id: str,
    reference_time: datetime,
    horizon_hours: int = 48,
    sample_limit: int = 12,
) -> dict:
    """Describe whether an official schedule can identify the active fixtures.

    A provider can return a healthy-looking schedule while none of its native
    rows identify the same upcoming matches as the canonical ESPN feed.  That
    distinction matters for lineup polling: fetching a page without a unique
    fixture join must never become model evidence.  This bounded projection is
    audit metadata only; it does not relax the strict join used below.
    """

    reference = reference_time.astimezone(timezone.utc)
    horizon = reference + timedelta(hours=max(0, horizon_hours))

    def row_key(row: object) -> tuple[str, str, str] | None:
        if not isinstance(row, dict):
            return None
        kickoff = _fixture_time(row.get("kickoff_at"))
        home = _official_join_name(competition_id, row.get("home_team"))
        away = _official_join_name(competition_id, row.get("away_team"))
        if kickoff is None or not home or not away:
            return None
        return kickoff.astimezone(timezone.utc).isoformat(), home, away

    candidates: list[dict] = []
    for row in canonical_fixtures:
        if not isinstance(row, dict) or row.get("competition_id") != competition_id:
            continue
        if row.get("status") != "upcoming":
            continue
        kickoff = _fixture_time(row.get("kickoff_at"))
        if kickoff is None or not reference <= kickoff.astimezone(timezone.utc) <= horizon:
            continue
        candidates.append(row)

    official_rows = (
        [row for row in official_fixtures if isinstance(row, dict)]
        if isinstance(official_fixtures, list)
        else []
    )
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    by_team_pair: dict[tuple[str, str], list[dict]] = {}
    for row in official_rows:
        key = row_key(row)
        if key is None:
            continue
        by_key.setdefault(key, []).append(row)
        by_team_pair.setdefault((key[1], key[2]), []).append(row)

    matched_ids: set[str] = set()
    samples: list[dict] = []
    matched_count = 0
    ambiguous_count = 0
    time_mismatch_count = 0
    unmatched_count = 0
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        key = row_key(candidate)
        pair = (key[1], key[2]) if key is not None else None
        exact = by_key.get(key, []) if key is not None else []
        if len(exact) == 1:
            matched_count += 1
            official_id = str(exact[0].get("id") or "")
            if official_id:
                matched_ids.add(official_id)
            continue
        if len(exact) > 1:
            outcome = "ambiguous_exact_match"
            ambiguous_count += 1
            candidates_for_sample = exact
        elif pair is not None and by_team_pair.get(pair):
            outcome = "team_pair_time_mismatch"
            time_mismatch_count += 1
            candidates_for_sample = by_team_pair[pair]
        else:
            outcome = "no_official_fixture_match"
            unmatched_count += 1
            candidates_for_sample = []
        if len(samples) < sample_limit:
            samples.append(
                {
                    "canonical_fixture_id": candidate_id or None,
                    "canonical_kickoff_at": candidate.get("kickoff_at"),
                    "canonical_home_team": candidate.get("home_team"),
                    "canonical_away_team": candidate.get("away_team"),
                    "outcome": outcome,
                    "official_candidates": [
                        {
                            "fixture_id": item.get("id"),
                            "kickoff_at": item.get("kickoff_at"),
                            "home_team": item.get("home_team"),
                            "away_team": item.get("away_team"),
                        }
                        for item in candidates_for_sample[:sample_limit]
                    ],
                }
            )

    official_unmatched_count = sum(
        1 for row in official_rows if str(row.get("id") or "") not in matched_ids
    )
    if matched_count and not (ambiguous_count or time_mismatch_count or unmatched_count):
        status = "exact"
    elif matched_count:
        status = "partial"
    elif candidates:
        status = "unavailable"
    else:
        status = "not_due"
    return {
        "schema_version": "matchline.fixture-join-diagnostics.v1",
        "competition_id": competition_id,
        "status": status,
        "join_basis": "strict_kickoff_and_normalized_team_pair",
        "reference_at": reference.isoformat(),
        "horizon_hours": max(0, horizon_hours),
        "canonical_candidate_count": len(candidates),
        "official_fixture_count": len(official_rows),
        "matched_count": matched_count,
        "ambiguous_count": ambiguous_count,
        "team_pair_time_mismatch_count": time_mismatch_count,
        "unmatched_canonical_count": unmatched_count,
        "unmatched_official_count": official_unmatched_count,
        "samples": samples,
        "model_admission": "display_only_until_exact_fixture_join",
    }


def _lottery_name_variants(item: dict, side: str, competition_id: str | None) -> set[str]:
    """Return only explicit, competition-scoped market team identities."""

    values = {item.get(f"{side}_team"), item.get(f"{side}_team_en")}
    variants = {normalized for value in values if (normalized := _normalise_join_name(value))}
    aliases = _LOTTERY_TEAM_ALIASES.get(competition_id or "", {})
    for value in tuple(variants):
        mapped = aliases.get(value)
        if mapped:
            variants.add(_normalise_join_name(mapped))
    return variants


def _fixture_name_variants(
    fixture: dict,
    side: str,
    competition_id: str,
) -> set[str]:
    """Return provider display names plus explicit canonical identities.

    The join remains exact after competition-scoped alias resolution.  This
    deliberately avoids suffix stripping or fuzzy matching, which could link
    the official market to the wrong club.
    """

    values = {
        fixture.get(f"{side}_team"),
        fixture.get(f"{side}_team_en"),
    }
    canonical_values = {
        canonical_team_name(competition_id, value)
        for value in values
        if isinstance(value, str) and value
    }
    return {
        normalized
        for value in values | canonical_values
        if (normalized := _normalise_join_name(value))
    }


def _lottery_only_fixture(item: dict, competition_id: str) -> dict:
    """Convert a lottery-only row into a visible, deliberately quarantined fixture."""

    source = item.get("source")
    if not isinstance(source, dict):
        raise ValueError("Sports Lottery fixture is missing source provenance")
    kickoff = str(item.get("kickoff_at") or "")
    return {
        "id": _lottery_fixture_id(item),
        "competition_id": competition_id,
        "season": kickoff[:4],
        "kickoff_at": kickoff,
        "home_team": str(item.get("home_team") or ""),
        "away_team": str(item.get("away_team") or ""),
        "home_team_id": team_id(competition_id, str(item.get("home_team") or "")),
        "away_team_id": team_id(competition_id, str(item.get("away_team") or "")),
        "status": "upcoming",
        "score": None,
        "market_probability": None,
        "entity_match_confidence": 0.55,
        "current_features": {
            "home": None,
            "away": None,
            "market": None,
            "team_status": None,
            "weather": None,
            "sofascore": None,
            "lottery_market": {
                "provider": "Sports Lottery",
                "probability": item.get("had_probability"),
                "hhad_probability": item.get("hhad_probability"),
                "ttg_probability": item.get("ttg_probability"),
                "hafu_probability": item.get("hafu_probability"),
                "crs_probability": item.get("crs_probability"),
                "hhad_line": item.get("hhad_line"),
                "retrieved_at": source.get("retrieved_at"),
                "source": source,
            },
            "lottery_match_conflict": False,
            "lottery_match_confidence": 0.55,
            "fixture_origin": "sports_lottery_only",
        },
        "source": source,
    }


def _fixture_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _crawl4ai_whoscored_features(live: dict, *, as_of: datetime) -> dict[str, dict]:
    """Project exact WhoScored joins into display-only current features.

    The browser source is never allowed to replace the canonical market or
    become a model feature here.  This projection only gives the match center
    and forecast evidence panel a canonical fixture ID, source odds and
    conflict state after ``crawl4ai_join`` has already proved the team/time
    join in the immutable snapshot.
    """

    crawl = live.get("crawl4ai")
    if not isinstance(crawl, dict) or not isinstance(crawl.get("pages"), list):
        return {}
    candidates_by_fixture: dict[str, list[dict]] = {}
    as_of_utc = as_of.astimezone(timezone.utc)
    for page in crawl["pages"]:
        if not isinstance(page, dict):
            continue
        joined = page.get("joined_fixture_records")
        if not isinstance(joined, list):
            continue
        observed = _fixture_time(page.get("observed_at"))
        raw_hash = page.get("raw_sha256") or page.get("content_sha256")
        source_url = page.get("url")
        source_name = page.get("source") or "WhoScored"
        if observed is None or observed.astimezone(timezone.utc) > as_of_utc:
            continue
        if not isinstance(raw_hash, str) or len(raw_hash) != 64 or not isinstance(source_url, str):
            continue
        source = {
            "name": str(source_name),
            "url": source_url,
            "raw_sha256": raw_hash,
            "retrieved_at": observed.astimezone(timezone.utc).isoformat(),
        }
        for record in joined:
            if not isinstance(record, dict) or record.get("join_status") != "exact":
                continue
            fixture_id = record.get("fixture_id")
            if not isinstance(fixture_id, str) or not fixture_id:
                continue
            candidates_by_fixture.setdefault(fixture_id, []).append(
                {
                    "provider": "WhoScored",
                    "fixture_id": fixture_id,
                    "source_match_id": record.get("source_match_id"),
                    "match_url": record.get("match_url"),
                    "home_team": record.get("home_team"),
                    "away_team": record.get("away_team"),
                    "canonical_home_team": record.get("canonical_home_team"),
                    "canonical_away_team": record.get("canonical_away_team"),
                    "source_kickoff_at": record.get("source_kickoff_at"),
                    "canonical_kickoff_at": record.get("canonical_kickoff_at"),
                    "kickoff_delta_seconds": record.get("kickoff_delta_seconds"),
                    "source_timezone": record.get("source_timezone"),
                    "one_x_two_odds": record.get("one_x_two_odds"),
                    "observed_at": observed.astimezone(timezone.utc).isoformat(),
                    "source": source,
                    "enters_model": False,
                    "model_eligible": False,
                    "display_only_reason": "public_source_market_comparison_requires_license_review",
                }
            )
    result: dict[str, dict] = {}
    for fixture_id, candidates in candidates_by_fixture.items():
        candidates.sort(
            key=lambda item: (str(item.get("observed_at")), str(item.get("source_match_id")))
        )
        selected = dict(candidates[-1])
        odds_signatures = {
            json.dumps(item.get("one_x_two_odds"), ensure_ascii=False, sort_keys=True)
            for item in candidates
        }
        conflict = len(odds_signatures) > 1
        selected["conflict"] = conflict
        if conflict:
            selected["display_only_reason"] = "public_source_market_conflict"
            selected["candidates"] = candidates[:4]
        result[fixture_id] = selected
    return result


def _build_match_center(live: dict, fixtures: list[dict], *, as_of: datetime) -> dict:
    """Build a display-only live/recent match center from ESPN summaries.

    Incidents and boxscore statistics are intentionally attached only to the
    match-center projection.  They remain absent from ``current_features``
    and therefore cannot become pre-match model inputs by accident.
    """

    summary = live.get("espn_markets") or {}
    if not isinstance(summary, dict):
        summary = {}
    fixture_by_id = {
        row.get("id"): row for row in fixtures if isinstance(row, dict) and row.get("id")
    }
    incidents_by_id = {
        row.get("fixture_id"): row
        for row in summary.get("incidents", [])
        if isinstance(row, dict) and row.get("fixture_id")
    }
    stats_by_id = {
        row.get("fixture_id"): row
        for row in summary.get("match_stats", [])
        if isinstance(row, dict) and row.get("fixture_id")
    }
    rows: list[dict] = []
    recent_cutoff = as_of.astimezone(timezone.utc) - timedelta(hours=24)
    for update in summary.get("fixture_updates", []):
        if not isinstance(update, dict):
            continue
        fixture_id = update.get("fixture_id")
        fixture = fixture_by_id.get(fixture_id)
        if not isinstance(fixture, dict):
            continue
        status = update.get("status")
        incident = incidents_by_id.get(fixture_id)
        stats = stats_by_id.get(fixture_id)
        fixture_time = _fixture_time(fixture.get("kickoff_at"))
        if status == "live" or (
            status == "finished"
            and (incident is not None or stats is not None)
            and fixture_time is not None
            and fixture_time.astimezone(timezone.utc) >= recent_cutoff
        ):
            evidence_source = None
            for candidate in (incident, stats):
                if isinstance(candidate, dict) and isinstance(candidate.get("source"), dict):
                    evidence_source = deepcopy(candidate["source"])
                    break
            if evidence_source is None:
                evidence_source = {
                    "name": summary.get("provider") or "ESPN event summary",
                    "retrieved_at": (
                        (incident or {}).get("retrieved_at")
                        or (stats or {}).get("retrieved_at")
                        or update.get("observed_at")
                        or summary.get("retrieved_at")
                    ),
                }
            rows.append(
                {
                    "fixture_id": fixture_id,
                    "competition_id": fixture.get("competition_id"),
                    "kickoff_at": fixture.get("kickoff_at"),
                    "home_team": fixture.get("home_team"),
                    "away_team": fixture.get("away_team"),
                    "status": status,
                    "score": update.get("score") or fixture.get("score"),
                    "halftime_score": update.get("halftime_score")
                    or fixture.get("halftime_score"),
                    "observed_at": update.get("observed_at"),
                    "clock": update.get("clock"),
                    "period": update.get("period"),
                    "status_text": update.get("status_text"),
                    "incidents": incident,
                    "match_stats": stats,
                    "source": evidence_source,
                    "enters_model": False,
                    "model_exclusion_reason": "live_or_postmatch_event",
                }
            )
    rows.sort(
        key=lambda row: (
            row.get("status") != "live",
            str(row.get("kickoff_at") or ""),
            str(row.get("fixture_id") or ""),
        )
    )
    live_rows = [row for row in rows if row.get("status") == "live"]
    recent_rows = [row for row in rows if row.get("status") == "finished"]
    return {
        "provider": "ESPN event summary",
        "retrieved_at": summary.get("retrieved_at"),
        "status": "live" if live_rows else "quiet",
        "live": live_rows,
        "recent_finished": recent_rows[-12:],
        "live_match_count": len(live_rows),
        "recent_finished_count": len(recent_rows),
        "incident_observation_count": len(incidents_by_id),
        "stats_observation_count": len(stats_by_id),
        "enters_model": False,
        "model_exclusion_reason": "live_or_postmatch_event",
    }


def _espn_injury_feature_for_fixture(
    fixture: dict,
    reports_by_team: dict[tuple[str, str], dict],
) -> dict | None:
    """Join league reports only by exact competition and ESPN team IDs."""

    competition_id = str(fixture.get("competition_id") or "")
    home_team_id = str(fixture.get("home_provider_team_id") or "")
    away_team_id = str(fixture.get("away_provider_team_id") or "")
    home = reports_by_team.get((competition_id, home_team_id))
    away = reports_by_team.get((competition_id, away_team_id))
    if home is None and away is None:
        return None

    def players(report: dict | None) -> list[dict]:
        if not isinstance(report, dict) or not isinstance(report.get("injuries"), list):
            return []
        return [
            {
                **deepcopy(row),
                # This remains an ESPN provider ID, not a canonical D1 ID.
                "player_id": row.get("provider_player_id"),
            }
            for row in report["injuries"]
            if isinstance(row, dict) and row.get("provider_player_id")
        ]

    complete = isinstance(home, dict) and isinstance(away, dict)
    partial = (isinstance(home, dict) or isinstance(away, dict)) and not complete
    present_reports = [report for report in (home, away) if isinstance(report, dict)]
    observed_times: list[str] = []
    for report in present_reports:
        retrieved_at = report.get("retrieved_at")
        if isinstance(retrieved_at, str):
            observed_times.append(retrieved_at)
    source = next(
        (
            deepcopy(report["source"])
            for report in present_reports
            if isinstance(report.get("source"), dict)
        ),
        None,
    )
    injuries = {
        "home": players(home),
        "away": players(away),
        "available": complete,
        "complete": complete,
        "partial": partial,
        "healthy_team_claim": False,
        "coverage_semantics": (
            "both_provider_team_buckets_required;empty_bucket_means_no_listed_report_not_proof_of_health"
        ),
    }
    if isinstance(source, dict):
        injuries["source"] = source
    return {
        "provider": "ESPN injury report",
        "coverage_status": "complete" if complete else "partial",
        "retrieved_at": max(observed_times, default=None),
        "injuries": injuries,
        "team_reports": {
            "home": (
                {
                    "provider_team_id": home.get("provider_team_id"),
                    "team_name": home.get("team_name"),
                    "reported_player_count": home.get("reported_player_count"),
                }
                if isinstance(home, dict)
                else None
            ),
            "away": (
                {
                    "provider_team_id": away.get("provider_team_id"),
                    "team_name": away.get("team_name"),
                    "reported_player_count": away.get("reported_player_count"),
                }
                if isinstance(away, dict)
                else None
            ),
        },
        "source": source,
        "model_eligible": False,
        "enters_model": False,
        "model_exclusion_reason": (
            "league_report_not_fixture_complete_and_provider_terms_require_review"
        ),
    }


def attach_current_data(
    snapshot: dict,
    live_path: Path,
    *,
    now: datetime | None = None,
    freshness_limit: timedelta = timedelta(hours=6),
) -> dict:
    payload = deepcopy(snapshot)
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if not live_path.exists():
        payload["current_data"] = {
            "status": "unavailable",
            "message": "尚未运行多源当前数据同步。",
            "as_of": None,
            "roles": {},
            "role_contract": _role_contract("none"),
            "model_admission": {
                "eligible": False,
                "reason": "current_snapshot_unavailable",
                "status": "blocked",
            },
        }
        return payload

    live = json.loads(live_path.read_text(encoding="utf-8"))
    roles = live.get("roles")
    if live.get("schema_version") != "1.0.0" or not _compatible_source_roles(roles):
        raise ValueError("live snapshot schema_version or roles are invalid")
    role_generation = _source_role_generation(roles)
    source_registry = live.get("source_registry")
    if source_registry is not None:
        if not isinstance(source_registry, list):
            raise ValueError("live snapshot source registry must be a list")
        # Runtime health is derived metadata. Validate the immutable policy
        # rows separately so a tampered status cannot silently authorize a
        # source, while still permitting bounded runtime fields.
        policy_rows = []
        for row in source_registry:
            if not isinstance(row, dict):
                raise ValueError("live snapshot source registry row is invalid")
            policy_rows.append({key: value for key, value in row.items() if key != "runtime"})
        validate_source_registry(policy_rows)
        # Runtime health is derived from the provider payload, not trusted
        # from a previously materialized status. This replays new safety
        # gates (including Crawl4AI TLS attestation) when an older snapshot is
        # read without rewriting its append-only raw history.
        source_registry = build_runtime_source_registry(live)
    try:
        as_of = datetime.fromisoformat(str(live["as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live snapshot as_of must be timezone-aware") from exc
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("live snapshot as_of must be timezone-aware")
    if as_of > checked_at + timedelta(minutes=5):
        raise ValueError("live snapshot as_of is in the future")
    if fixture_feed_key(live) == "espn":
        # A cached legacy role document remains auditable, but neither a
        # payload boolean nor a self-issued authorization string can reopen an
        # ESPN serving/model lane under the central v260 policy.
        age = checked_at.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)
        espn_decision = decide_source_rights(
            SourceId.ESPN_SCHEDULE_SUMMARY,
            UseCase.MODEL_INPUT,
        )
        payload.setdefault("summary", {})["current_fixture_count"] = 0
        payload["summary"]["current_xg_team_count"] = 0
        payload["current_data"] = {
            "status": "rights_blocked",
            "message": "旧来源角色仅保留研究审计；其事实不进入当前模型。",
            "as_of": as_of.isoformat(),
            "age_seconds": max(0, int(age.total_seconds())),
            "roles": deepcopy(roles),
            "role_contract": _role_contract("legacy"),
            "model_admission": {
                "eligible": False,
                "reason": "legacy_source_roles_not_model_admitted",
                "status": "blocked",
            },
            "source_registry": [],
            "fixture_count": 0,
            "canonical_fixture_count": 0,
            "espn_fixture_count": 0,
            "market_count": 0,
            "network_opened": False,
            "rights_status": espn_decision.rights_status,
            "terms_url": espn_decision.terms_url,
        }
        return payload
    return _attach_research_current_readmodel(
        payload,
        live,
        as_of=as_of,
        checked_at=checked_at,
        freshness_limit=freshness_limit,
        role_generation=role_generation,
    )
