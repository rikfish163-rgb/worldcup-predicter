"""Fetch diverse current sources and atomically publish one as-of snapshot."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import unicodedata

from league_platform.catalog import LEAGUES
from league_platform.identity import canonical_team_name
from league_platform.ingestion import archive_snapshot_intelligence
from league_platform.fixture_feed import (
    fixture_feed_section,
    fixture_rows,
)
from league_platform.source_archive import DEFAULT_ARCHIVE_DIR, archive_source_snapshot
from league_platform.source_registry import build_runtime_source_registry
from league_platform.live_sources import (
    fetch_bundesliga_lineups,
    fetch_crawl4ai,
    fetch_laliga_lineups,
    fetch_ligue1_lineups,
    fetch_met_norway_weather,
    fetch_openligadb_fixtures,
    fetch_serie_a_lineups,
    fetch_wikidata_venues,
    load_crawl4ai_configs,
)
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_CURRENT_SOURCE_IDS,
    fetch_openfootball_current,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawArchiveError,
    load_verified_openfootball_archive,
    verify_openfootball_raw_receipt,
)
from league_platform.source_rights import (
    POLICY_VERSION,
    SourceId,
    UseCase,
    decide_source_rights,
    rights_blocked_envelope,
)


DEFAULT_OUTPUT = Path("data/live/current.json")
DEFAULT_INTELLIGENCE_LEDGER = Path("data/live/intelligence/observations.jsonl")
DEFAULT_OPENFOOTBALL_RAW_ARCHIVE_DIR = DEFAULT_OUTPUT.parent / "openfootball-raw"
OPENFOOTBALL_RAW_ARCHIVE_ENV = "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"
MET_NORWAY_USER_AGENT = "MatchlineResearch/1.0 (+https://predict.hetaisheng.ccwu.cc)"
WIKIDATA_USER_AGENT = "MatchlineResearch/1.0 (+https://predict.hetaisheng.ccwu.cc)"
WIKIDATA_MAX_FIXTURES = 96
OPENFOOTBALL_RAW_ADMISSION_SCHEMA = "matchline.openfootball_raw_admission.v1"
PUBLICATION_DIAGNOSTIC_SUFFIX = ".publication.json"
ODDSTORM_STALE_FALLBACK_MAX_AGE = timedelta(hours=2)
FIXTURE_SOURCE_STALE_FALLBACK_MAX_AGE = timedelta(hours=2)

_STALE_REPLAY_SOURCE_IDS = {
    "cfl_official": SourceId.CFL_OFFICIAL_CURRENT,
    "oddstorm": SourceId.ODDSTORM_MARKET_COMPARISON,
}
_CRAWL4AI_FACT_SOURCES = (
    SourceId.FBREF_PUBLIC_STATS,
    SourceId.WHOSCORED_PUBLIC_PAGES,
    SourceId.PREMIER_LEAGUE_PUBLIC_PAGES,
    SourceId.LALIGA_PUBLIC_PAGES,
    SourceId.LALIGA_OFFICIAL_NEWS,
    SourceId.BUNDESLIGA_PUBLIC_PAGES,
    SourceId.SERIEA_PUBLIC_PAGES,
    SourceId.LIGUE1_OFFICIAL_NEWS,
    SourceId.CLUBELO_PUBLIC_RATINGS,
    SourceId.SOFIFA_PUBLIC_REFERENCE,
)


def _blocked_section(
    source_id: SourceId,
    *,
    provider: str,
    reference_time: datetime,
    empty_fields: tuple[str, ...],
    compatibility_lists: tuple[str, ...] = (),
    compatibility_values: dict[str, Any] | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Build the canonical block plus legacy empty-shape compatibility."""

    payload = rights_blocked_envelope(
        source_id,
        provider=provider,
        checked_at=reference_time.astimezone(timezone.utc).isoformat(),
        empty_fields=empty_fields,
        config=config,
    )
    for field in compatibility_lists:
        payload[field] = []
    if compatibility_values:
        payload.update(compatibility_values)
    return {key: payload[key] for key in sorted(payload)}


def _stale_replay_allowed(source_key: str) -> bool:
    """Allow replay only for an explicitly mapped v260 serving contract.

    OpenFootball is intentionally absent: its verified raw admission is bound
    to one current-cycle cutoff and does not authorize carrying a prior row
    into a later cycle.
    """

    source_id = _STALE_REPLAY_SOURCE_IDS.get(source_key)
    return bool(
        source_id is not None and decide_source_rights(source_id, UseCase.SERVE_CURRENT).is_allowed
    )


def _blocked_espn_sections(reference_time: datetime) -> dict[str, dict[str, Any]]:
    """Return canonical provider blocks without opening an ESPN endpoint."""

    return {
        "espn": _blocked_section(
            SourceId.ESPN_SCHEDULE_SUMMARY,
            provider="ESPN",
            reference_time=reference_time,
            empty_fields=("fixtures",),
        ),
        "espn_markets": _blocked_section(
            SourceId.ESPN_MARKET_SUMMARY,
            provider="ESPN event summary",
            reference_time=reference_time,
            empty_fields=(
                "fixture_updates",
                "incidents",
                "markets",
                "match_stats",
                "team_status",
            ),
        ),
        "espn_rosters": _blocked_section(
            SourceId.ESPN_TEAM_ROSTERS,
            provider="ESPN team roster",
            reference_time=reference_time,
            empty_fields=("rosters",),
        ),
    }


def _blocked_crawl4ai_section(
    reference_time: datetime,
    *,
    config: object | None,
) -> dict[str, Any]:
    """Block every declared Crawl4AI fact source before config/browser I/O."""

    source_blocks = {
        source_id.value: _blocked_section(
            source_id,
            provider=f"Crawl4AI / {source_id.value}",
            reference_time=reference_time,
            empty_fields=("pages",),
            config=config,
        )
        for source_id in _CRAWL4AI_FACT_SOURCES
    }
    checked_at = reference_time.astimezone(timezone.utc).isoformat()
    return {
        "access_allowed": False,
        "checked_at": checked_at,
        "configured_count": 0,
        "errors": [],
        "model_eligible": False,
        "network_opened": False,
        "page_count": 0,
        "pages": [],
        "provider": "Crawl4AI",
        "provider_role": "execution_layer",
        "retrieved_at": None,
        "rights_blocked_count": len(source_blocks),
        "schema_version": "matchline.source_rights_aggregate.v1",
        "source_blocks": source_blocks,
        "status": "rights_blocked",
    }


def _crawl4ai_bridge_section(
    openfootball_current: object,
    *,
    reference_time: datetime,
    config_path: Path | str | None,
    archive_root: Path,
    blocked_section: dict[str, Any],
) -> dict[str, Any]:
    """Run the optional Crawl4AI edge only after verified raw admission.

    The config loader is the outer, network-free central-rights seam.  It
    returns no runnable configs for BLOCK/unknown identities, so the browser
    adapter is not even invoked on the default path.  ``fetch_crawl4ai`` then
    repeats its own typed identity/parser/TLS/robots preflight before any page
    request.  Crawl4AI remains an execution/display layer; no result can enter
    the model through this bridge.
    """

    if config_path is None or not isinstance(openfootball_current, dict):
        return blocked_section
    admission = openfootball_current.get("raw_archive_admission")
    if not isinstance(admission, dict):
        return blocked_section
    if (
        admission.get("schema_version") != OPENFOOTBALL_RAW_ADMISSION_SCHEMA
        or admission.get("policy_version") != POLICY_VERSION
        or admission.get("status") != "verified_current_raw"
    ):
        return blocked_section
    if _openfootball_archive_is_volatile(archive_root):
        return {
            **blocked_section,
            "errors": [
                {
                    "stage": "archive_policy",
                    "error": "Crawl4AI evidence archive path is volatile",
                    "network_opened": False,
                    "model_eligible": False,
                    "enters_model": False,
                }
            ],
            "status": "blocked_storage",
        }

    # This read-only loader performs the authoritative central rights and
    # parser/host admission.  A blocked/unknown config must not reach the
    # Crawl4AI function at all (and therefore cannot inspect TLS or a browser).
    try:
        configs, _config_errors = load_crawl4ai_configs(config_path=config_path)
    except Exception:
        return blocked_section
    if not isinstance(configs, list) or not configs:
        return blocked_section

    try:
        result = fetch_crawl4ai(
            now=reference_time,
            config_path=config_path,
            archive_dir=archive_root / "crawl4ai",
        )
    except Exception as exc:  # noqa: BLE001 - isolate optional execution layer
        return {
            **blocked_section,
            "configured_count": len(configs),
            "errors": [
                {
                    "stage": "bridge",
                    "error": f"{type(exc).__name__}: {exc}",
                    "network_opened": False,
                    "model_eligible": False,
                    "enters_model": False,
                }
            ],
            "status": "unavailable",
        }
    if not isinstance(result, dict):
        return {
            **blocked_section,
            "configured_count": len(configs),
            "errors": [
                {
                    "stage": "bridge",
                    "error": "fetch_crawl4ai returned a non-object result",
                    "network_opened": False,
                    "model_eligible": False,
                    "enters_model": False,
                }
            ],
            "status": "unavailable",
        }

    bridged = dict(result)
    bridged["provider"] = "Crawl4AI"
    bridged["provider_role"] = "execution_layer"
    bridged["model_eligible"] = False
    bridged.setdefault("network_opened", False)
    pages = bridged.get("pages")
    if not isinstance(pages, list):
        bridged["pages"] = []
    else:
        # Keep the display-only boundary true even if a test/custom seam
        # returns an over-optimistic page flag.
        bridged["pages"] = [
            {
                **page,
                "model_eligible": False,
                "enters_model": False,
            }
            if isinstance(page, dict)
            else page
            for page in pages
        ]
    return bridged


def _blocked_current_sections(
    reference_time: datetime,
    *,
    crawl4ai_config: object | None,
) -> dict[str, dict[str, Any]]:
    """Materialize every non-admitted v260 source as an empty envelope."""

    sections = _blocked_espn_sections(reference_time)
    sections.update(
        {
            "cfl_official": _blocked_section(
                SourceId.CFL_OFFICIAL_CURRENT,
                provider="Chinese Professional Football League official",
                reference_time=reference_time,
                empty_fields=("fixtures",),
                compatibility_lists=(
                    "date_only_fixtures",
                    "recent_results",
                    "upcoming_3_days",
                    "upcoming_7_days",
                ),
            ),
            "espn_injuries": _blocked_section(
                SourceId.ESPN_INJURY_REPORTS,
                provider="ESPN injury report",
                reference_time=reference_time,
                empty_fields=("competition_reports", "fallbacks", "reports"),
                compatibility_values={
                    "reported_player_count": 0,
                    "requested_competitions": 0,
                    "coverage_semantics": "missing_team_bucket_is_unknown_not_healthy",
                    "enters_model": False,
                },
            ),
            "understat": _blocked_section(
                SourceId.UNDERSTAT_XG,
                provider="Understat",
                reference_time=reference_time,
                empty_fields=("observations",),
                compatibility_lists=("team_features",),
            ),
            "news": _blocked_section(
                SourceId.PUBLIC_RSS_NEWS,
                provider="Public RSS",
                reference_time=reference_time,
                empty_fields=("news",),
                compatibility_lists=("feeds",),
                compatibility_values={"item_count": 0},
            ),
            "geocoding": _blocked_section(
                SourceId.OPEN_METEO_GEOCODING,
                provider="Open-Meteo Geocoding",
                reference_time=reference_time,
                empty_fields=("geocodes",),
                compatibility_values={
                    "cache": {
                        "fresh_count": 0,
                        "requested_fixture_count": 0,
                        "reused_count": 0,
                        "status": "rights_blocked",
                    }
                },
            ),
            "weather": _blocked_section(
                SourceId.OPEN_METEO_WEATHER,
                provider="Open-Meteo",
                reference_time=reference_time,
                empty_fields=("weather",),
            ),
            "sofascore": _blocked_section(
                SourceId.SOFASCORE_PREMATCH,
                provider="SofaScore",
                reference_time=reference_time,
                empty_fields=("events",),
            ),
            "premier_league_official": _blocked_section(
                SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS,
                provider="Premier League official",
                reference_time=reference_time,
                empty_fields=("fixtures", "lineups"),
                compatibility_lists=("fixture_errors", "lineup_errors", "matchweeks"),
                compatibility_values={"poll_schedule": None, "season": None},
            ),
            "laliga_official": _blocked_section(
                SourceId.OFFICIAL_LALIGA_LINEUPS,
                provider="LaLiga official",
                reference_time=reference_time,
                empty_fields=("fixtures", "lineups"),
            ),
            "bundesliga_official": _blocked_section(
                SourceId.OFFICIAL_BUNDESLIGA_LINEUPS,
                provider="Bundesliga official",
                reference_time=reference_time,
                empty_fields=("fixtures", "lineups"),
            ),
            "serie_a_official": _blocked_section(
                SourceId.OFFICIAL_SERIE_A_LINEUPS,
                provider="Serie A official",
                reference_time=reference_time,
                empty_fields=("fixtures", "lineups"),
            ),
            "ligue1_official": _blocked_section(
                SourceId.OFFICIAL_LIGUE1_LINEUPS,
                provider="Ligue 1 official",
                reference_time=reference_time,
                empty_fields=("fixtures", "lineups"),
            ),
            "sports_lottery": _blocked_section(
                SourceId.SPORTS_LOTTERY_OFFICIAL,
                provider="Sports Lottery",
                reference_time=reference_time,
                empty_fields=("fallbacks", "matches"),
            ),
            "oddstorm": _blocked_section(
                SourceId.ODDSTORM_MARKET_COMPARISON,
                provider="OddStorm public bookmaker comparison",
                reference_time=reference_time,
                empty_fields=("lines",),
                compatibility_values={
                    "terms_url": "https://www.oddstorm.com/terms",
                },
            ),
            "fotmob": _blocked_section(
                SourceId.FOTMOB_PUBLIC_API,
                provider="FotMob",
                reference_time=reference_time,
                empty_fields=("lineups",),
            ),
            "crawl4ai": _blocked_crawl4ai_section(
                reference_time,
                config=crawl4ai_config,
            ),
        }
    )
    return sections


def _wikidata_unavailable_section(
    reference_time: datetime,
    *,
    status: str = "not_configured",
    reason: str = "wikidata_fetch_not_enabled_for_this_call",
) -> dict[str, Any]:
    """Return a truthful empty Wikidata lane without fabricating venue facts."""

    return {
        "provider": "Wikidata structured data",
        "source_id": SourceId.WIKIDATA_ENTITIES.value,
        "license": "CC0-1.0",
        "license_url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
        "attribution_required": False,
        "retrieved_at": reference_time.astimezone(timezone.utc).isoformat(),
        "network_opened": False,
        "status": status,
        "venues": [],
        "errors": [{"reason": reason, "count": 0 if status == "not_configured" else 1}],
        "error_count": 0 if status == "not_configured" else 1,
        "record_count": 0,
        "model_eligible": False,
    }


def _attach_wikidata_venues(
    fixture_feed: dict[str, Any],
    wikidata_entities: dict[str, Any],
) -> None:
    """Attach only exact fixture-keyed venue records to the canonical feed."""

    venues = wikidata_entities.get("venues")
    if not isinstance(venues, list):
        return
    by_id = {
        row.get("id"): row
        for row in fixture_feed.get("fixtures", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    for item in venues:
        if not isinstance(item, dict):
            continue
        fixture = by_id.get(item.get("fixture_id"))
        venue = item.get("venue")
        if not isinstance(fixture, dict) or not isinstance(venue, dict):
            continue
        source = venue.get("source")
        if not isinstance(source, dict):
            continue
        # Keep the provider identity nested on the field itself.  This lets
        # the bundle gate reject a contaminated schedule overlay instead of
        # trusting the canonical OpenFootball row name.
        fixture["venue"] = deepcopy(venue)
        field_sources = fixture.get("field_sources")
        if not isinstance(field_sources, dict):
            field_sources = {}
        field_sources["venue"] = deepcopy(source)
        fixture["field_sources"] = field_sources


def _compact_met_weather_diagnostics(payload: object) -> dict[str, Any]:
    """Strip fixture-level error identities before a public snapshot is built."""

    if not isinstance(payload, dict):
        return {
            "provider": "MET Norway Locationforecast",
            "source_id": SourceId.MET_NORWAY_WEATHER.value,
            "status": "unavailable",
            "weather": [],
            "errors": [{"reason": "adapter_result_invalid", "count": 1}],
            "error_count": 1,
            "network_opened": False,
        }
    result = deepcopy(payload)
    raw_errors = payload.get("errors")
    if not isinstance(raw_errors, list):
        result["errors"] = [{"reason": "adapter_errors_invalid", "count": 1}]
        result["error_count"] = 1
        return result
    total = payload.get("error_count")
    total_count = (
        int(total)
        if isinstance(total, int) and not isinstance(total, bool) and total >= len(raw_errors)
        else len(raw_errors)
    )
    counts: dict[str, int] = {}
    for item in raw_errors:
        reason = item.get("reason") if isinstance(item, dict) else None
        reason_text = str(reason).strip() if reason else "unknown_error"
        if reason_text.startswith("errors_truncated:"):
            continue
        counts[reason_text] = counts.get(reason_text, 0) + 1
    summarized = [{"reason": reason, "count": count} for reason, count in sorted(counts.items())]
    summarized_count = sum(counts.values())
    if summarized_count < total_count:
        summarized.append(
            {"reason": "additional_errors_omitted", "count": total_count - summarized_count}
        )
    result["errors"] = summarized[:64]
    result["error_count"] = total_count
    return result


def _empty_openfootball_current(
    reference_time: datetime,
    *,
    error: str,
) -> dict[str, Any]:
    network_rights = decide_source_rights(
        SourceId.OPENFOOTBALL_CURRENT,
        UseCase.NETWORK_FETCH,
    )
    return {
        "date_only_fixtures": [],
        "diagnostics": {"failed_source_count": 1, "parsed_fixture_count": 0},
        "errors": [{"error": error[:500], "stage": "current_cycle_fetch"}],
        "fixtures": [],
        "provider": "OpenFootball",
        "recent_results": [],
        "retrieved_at": reference_time.astimezone(timezone.utc).isoformat(),
        "source_contract": {
            "fact_source": "OpenFootball",
            "rights": network_rights.as_dict(),
        },
        "status": "unavailable",
        "upcoming_3_days": [],
        "upcoming_7_days": [],
    }


def _is_current_cycle_timestamp(value: object, reference_time: datetime) -> bool:
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return bool(
        observed.tzinfo is not None
        and observed.utcoffset() is not None
        and observed.astimezone(timezone.utc) == reference_time.astimezone(timezone.utc)
    )


def _admit_openfootball_current(
    result: object,
    *,
    reference_time: datetime,
) -> dict[str, Any]:
    """Admit only a well-shaped result returned by this exact current cycle."""

    list_fields = (
        "fixtures",
        "date_only_fixtures",
        "recent_results",
        "upcoming_3_days",
        "upcoming_7_days",
        "errors",
    )
    expected_network_rights = decide_source_rights(
        SourceId.OPENFOOTBALL_CURRENT,
        UseCase.NETWORK_FETCH,
    ).as_dict()
    source_contract = result.get("source_contract") if isinstance(result, dict) else None
    if (
        not isinstance(result, dict)
        or result.get("provider") != "OpenFootball"
        or result.get("status") not in {"ok", "degraded", "unavailable"}
        or not _is_current_cycle_timestamp(result.get("retrieved_at"), reference_time)
        or not isinstance(source_contract, dict)
        or source_contract.get("rights") != expected_network_rights
        or any(
            not isinstance(result.get(field), list)
            or any(not isinstance(row, dict) for row in result[field])
            for field in list_fields
        )
    ):
        payload = _empty_openfootball_current(
            reference_time,
            error="OpenFootball current-cycle result failed structural admission",
        )
    else:
        payload = {
            "provider": "OpenFootball",
            "retrieved_at": result["retrieved_at"],
            "status": result["status"],
            **{field: deepcopy(result[field]) for field in list_fields},
            "diagnostics": (
                deepcopy(result["diagnostics"])
                if isinstance(result.get("diagnostics"), dict)
                else {}
            ),
            "source_contract": deepcopy(source_contract),
            "training_admitted": False,
            "raw_archive_receipts": (
                deepcopy(result["raw_archive_receipts"])
                if isinstance(result.get("raw_archive_receipts"), list)
                else []
            ),
        }
    payload["serve_current_rights"] = decide_source_rights(
        SourceId.OPENFOOTBALL_CURRENT,
        UseCase.SERVE_CURRENT,
    ).as_dict()
    return payload


def _openfootball_fixture_feed(source: dict[str, Any]) -> dict[str, Any]:
    """Project the admitted OpenFootball lane without any secondary source."""

    payload = deepcopy(source)
    payload["provider"] = "OpenFootball"
    payload.pop("raw_archive_receipts", None)
    payload.pop("raw_archive_producer_receipt", None)
    payload.pop("serve_current_rights", None)
    return payload


def _valid_nonzero_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value != "0" * 64


def _canonical_openfootball_rows(rows: list[dict[str, Any]]) -> tuple[list[str], str]:
    fixture_ids: list[str] = []
    canonical_rows: list[dict[str, Any]] = []
    for row in rows:
        fixture_id = row.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise ValueError("OpenFootball row id is invalid")
        fixture_ids.append(fixture_id)
        canonical_rows.append(row)
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("OpenFootball rows contain a duplicate fixture id")
    ordered = [
        row
        for _fixture_id, row in sorted(
            zip(fixture_ids, canonical_rows, strict=True),
            key=lambda item: item[0],
        )
    ]
    raw = json.dumps(
        ordered,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sorted(fixture_ids), hashlib.sha256(raw).hexdigest()


def _openfootball_kickoff(row: dict[str, Any]) -> datetime:
    value = row.get("kickoff_at")
    if not isinstance(value, str):
        raise ValueError("exact OpenFootball fixture is missing kickoff_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("OpenFootball fixture kickoff_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("OpenFootball fixture kickoff_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _partition_verified_openfootball_rows(
    rows: list[dict[str, Any]],
    *,
    reference_time: datetime,
) -> dict[str, list[dict[str, Any]]]:
    exact = [
        deepcopy(row)
        for row in rows
        if row.get("kickoff_time_quality") == "exact" and isinstance(row.get("kickoff_at"), str)
    ]
    date_only = [deepcopy(row) for row in rows if row.get("kickoff_time_quality") == "date_only"]
    if len(exact) + len(date_only) != len(rows):
        raise ValueError("OpenFootball reparse returned an unknown kickoff-time quality")
    exact.sort(key=lambda row: (_openfootball_kickoff(row), str(row["id"])))
    date_only.sort(key=lambda row: (str(row.get("kickoff_date")), str(row["id"])))
    recent_start = reference_time - timedelta(days=7)
    upcoming_3_end = reference_time + timedelta(days=3)
    upcoming_7_end = reference_time + timedelta(days=7)
    return {
        "fixtures": exact,
        "date_only_fixtures": date_only,
        "recent_results": [
            row
            for row in exact
            if row.get("status") == "finished"
            and recent_start <= _openfootball_kickoff(row) <= reference_time
        ],
        "upcoming_3_days": [
            row
            for row in exact
            if row.get("status") == "upcoming"
            and reference_time <= _openfootball_kickoff(row) <= upcoming_3_end
        ],
        "upcoming_7_days": [
            row
            for row in exact
            if row.get("status") == "upcoming"
            and reference_time <= _openfootball_kickoff(row) <= upcoming_7_end
        ],
    }


def _openfootball_rows_match(
    fetched_exact: list[dict[str, Any]],
    fetched_date_only: list[dict[str, Any]],
    reparsed_rows: list[dict[str, Any]],
    *,
    reference_time: datetime,
) -> bool:
    try:
        reparsed = _partition_verified_openfootball_rows(
            reparsed_rows,
            reference_time=reference_time,
        )
        fetched_exact_identity = _canonical_openfootball_rows(fetched_exact)
        fetched_date_identity = _canonical_openfootball_rows(fetched_date_only)
        reparsed_exact_identity = _canonical_openfootball_rows(reparsed["fixtures"])
        reparsed_date_identity = _canonical_openfootball_rows(reparsed["date_only_fixtures"])
        fetched_ids = fetched_exact_identity[0] + fetched_date_identity[0]
        reparsed_ids = reparsed_exact_identity[0] + reparsed_date_identity[0]
    except (TypeError, ValueError):
        return False
    return bool(
        len(set(fetched_ids)) == len(fetched_ids)
        and len(set(reparsed_ids)) == len(reparsed_ids)
        and sorted(fetched_ids) == sorted(reparsed_ids)
        and fetched_exact_identity == reparsed_exact_identity
        and fetched_date_identity == reparsed_date_identity
    )


def _openfootball_row_source_id(row: dict[str, Any]) -> str | None:
    source = row.get("source")
    lineage = row.get("lineage")
    if not isinstance(source, dict) or not isinstance(lineage, dict):
        return None
    source_id = source.get("source_id")
    return (
        source_id if isinstance(source_id, str) and lineage.get("source_id") == source_id else None
    )


def _raw_archive_receipts_by_source(
    source: dict[str, Any],
    *,
    reference_time: datetime,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    expected_fields = {
        "status",
        "observation_id",
        "source_id",
        "retrieved_at",
        "raw_sha256",
        "size_bytes",
        "raw_path",
        "record_sha256",
        "manifest_sha256",
        "duplicate",
    }
    receipts = source.get("raw_archive_receipts")
    if not isinstance(receipts, list):
        receipts = []
    accepted: dict[str, dict[str, Any]] = {}
    invalid_source_ids: set[str] = set()
    errors: list[dict[str, Any]] = []
    for receipt in receipts:
        source_id = receipt.get("source_id") if isinstance(receipt, dict) else None
        valid = bool(
            isinstance(receipt, dict)
            and set(receipt) == expected_fields
            and receipt.get("status") == "raw_observation_archived"
            and isinstance(source_id, str)
            and source_id in OPENFOOTBALL_CURRENT_SOURCE_IDS
            and _is_current_cycle_timestamp(receipt.get("retrieved_at"), reference_time)
            and all(
                _valid_nonzero_sha256(receipt.get(field))
                for field in (
                    "observation_id",
                    "raw_sha256",
                    "record_sha256",
                    "manifest_sha256",
                )
            )
            and isinstance(receipt.get("size_bytes"), int)
            and not isinstance(receipt.get("size_bytes"), bool)
            and receipt["size_bytes"] >= 0
            and isinstance(receipt.get("raw_path"), str)
            and bool(receipt["raw_path"])
            and isinstance(receipt.get("duplicate"), bool)
        )
        if not valid or source_id in accepted or source_id in invalid_source_ids:
            errors.append(
                {
                    "source_id": source_id,
                    "stage": "raw_archive_admission",
                    "error": "OpenFootball raw archive receipt is invalid or duplicated",
                }
            )
            if isinstance(source_id, str):
                accepted.pop(source_id, None)
                invalid_source_ids.add(source_id)
            continue
        assert isinstance(source_id, str)
        accepted[source_id] = deepcopy(receipt)
    return accepted, errors


def _raw_archive_admission_blocked(
    reference_time: datetime,
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": OPENFOOTBALL_RAW_ADMISSION_SCHEMA,
        "status": "blocked",
        "policy_version": POLICY_VERSION,
        "observed_before": reference_time.isoformat(),
        "source_ids": [],
        "row_count": 0,
        "reason": reason,
    }


def _openfootball_archive_is_volatile(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
        volatile_root = Path("/dev/shm").resolve(strict=True)
        resolved.relative_to(volatile_root)
    except ValueError:
        return False
    except OSError:
        return True
    return True


def _openfootball_raw_archive_root(
    output: Path,
    configured: Path | None,
) -> Path:
    if configured is not None:
        return Path(configured)
    environment_value = os.environ.get(OPENFOOTBALL_RAW_ARCHIVE_ENV, "").strip()
    return Path(environment_value) if environment_value else output.parent / "openfootball-raw"


def _verify_openfootball_current_from_raw_archive(
    source: dict[str, Any],
    *,
    archive_root: Path,
    reference_time: datetime,
) -> dict[str, Any]:
    receipts, admission_errors = _raw_archive_receipts_by_source(
        source,
        reference_time=reference_time,
    )
    fetched_by_source: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for field in ("fixtures", "date_only_fixtures"):
        for row in source[field]:
            source_id = _openfootball_row_source_id(row)
            if source_id is None or source_id not in receipts:
                admission_errors.append(
                    {
                        "source_id": source_id,
                        "stage": "raw_archive_admission",
                        "error": "OpenFootball fetch row is not bound to a successful raw receipt",
                    }
                )
                continue
            fetched_by_source.setdefault(
                source_id,
                {"fixtures": [], "date_only_fixtures": []},
            )[field].append(row)

    accepted_source_ids: list[str] = []
    for source_id in sorted(receipts):
        receipt = receipts[source_id]
        try:
            # A receipt's hash-shaped fields are producer claims.  Bind them
            # to an actual manifest record/raw object before admitting any
            # fetched rows; do not let a self-attested observation or manifest
            # digest unlock publication.
            receipt_record = verify_openfootball_raw_receipt(archive_root, receipt)
            verified = load_verified_openfootball_archive(
                archive_root,
                source_ids=[source_id],
                observed_before=reference_time,
            )
            selected = verified["selected_records"]
            if len(selected) != 1 or any(
                selected[0].get(field) != receipt.get(field)
                for field in ("source_id", "retrieved_at", "record_sha256", "raw_sha256")
            ):
                raise OpenFootballRawArchiveError(
                    "OpenFootball selected raw record does not match this-cycle receipt"
                )
            if any(
                receipt_record.get(field) != receipt.get(field)
                for field in (
                    "source_id",
                    "retrieved_at",
                    "record_sha256",
                    "raw_sha256",
                    "size_bytes",
                    "raw_path",
                )
            ):
                raise OpenFootballRawArchiveError(
                    "OpenFootball manifest record does not match this-cycle receipt"
                )
            fetched = fetched_by_source.get(
                source_id,
                {"fixtures": [], "date_only_fixtures": []},
            )
            if not _openfootball_rows_match(
                fetched["fixtures"],
                fetched["date_only_fixtures"],
                verified["rows"],
                reference_time=reference_time,
            ):
                raise OpenFootballRawArchiveError(
                    "OpenFootball fetch rows do not match the raw archive reparse"
                )
        except (KeyError, OSError, TypeError, ValueError, OpenFootballRawArchiveError) as exc:
            admission_errors.append(
                {
                    "source_id": source_id,
                    "stage": "raw_archive_admission",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
            continue
        accepted_source_ids.append(source_id)

    verified_aggregate: dict[str, Any] | None = None
    if accepted_source_ids:
        try:
            verified_aggregate = load_verified_openfootball_archive(
                archive_root,
                source_ids=accepted_source_ids,
                observed_before=reference_time,
            )
            accepted_exact = [
                row
                for source_id in accepted_source_ids
                for row in fetched_by_source.get(source_id, {}).get("fixtures", [])
            ]
            accepted_date_only = [
                row
                for source_id in accepted_source_ids
                for row in fetched_by_source.get(source_id, {}).get("date_only_fixtures", [])
            ]
            if not _openfootball_rows_match(
                accepted_exact,
                accepted_date_only,
                verified_aggregate["rows"],
                reference_time=reference_time,
            ):
                raise OpenFootballRawArchiveError(
                    "OpenFootball aggregate raw reparse does not match admitted fetch rows"
                )
        except (KeyError, OSError, TypeError, ValueError, OpenFootballRawArchiveError) as exc:
            admission_errors.append(
                {
                    "source_id": None,
                    "stage": "raw_archive_admission",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
            accepted_source_ids = []
            verified_aggregate = None

    payload = deepcopy(source)
    payload.pop("raw_archive_receipts", None)
    errors = payload.get("errors")
    payload["errors"] = (errors if isinstance(errors, list) else []) + admission_errors
    source_contract = payload.get("source_contract")
    payload["source_contract"] = {
        "fact_source": "OpenFootball",
        "rights": decide_source_rights(
            SourceId.OPENFOOTBALL_CURRENT,
            UseCase.NETWORK_FETCH,
        ).as_dict(),
        "license": "CC0-1.0",
        "source_ids": accepted_source_ids,
        "admission_status": (
            "verified_current_raw" if verified_aggregate is not None else "blocked"
        ),
    }
    if not isinstance(source_contract, dict):
        payload["errors"].append(
            {
                "source_id": None,
                "stage": "raw_archive_admission",
                "error": "OpenFootball source contract is invalid",
            }
        )

    if verified_aggregate is None:
        for field in (
            "fixtures",
            "date_only_fixtures",
            "recent_results",
            "upcoming_3_days",
            "upcoming_7_days",
        ):
            payload[field] = []
        payload["status"] = "unavailable"
        payload["raw_archive_admission"] = _raw_archive_admission_blocked(
            reference_time,
            reason="no_verified_current_raw_observation",
        )
        return payload

    verified_rows = verified_aggregate["rows"]
    payload.update(
        _partition_verified_openfootball_rows(
            verified_rows,
            reference_time=reference_time,
        )
    )
    payload["status"] = (
        "degraded"
        if payload["errors"] and verified_rows
        else "ok"
        if verified_rows
        else "unavailable"
    )
    diagnostics = payload.get("diagnostics")
    payload["diagnostics"] = {
        **(diagnostics if isinstance(diagnostics, dict) else {}),
        "admitted_source_count": len(accepted_source_ids),
        "admitted_fixture_count": len(verified_rows),
        "raw_archive_admission_error_count": len(admission_errors),
    }
    payload["raw_archive_admission"] = {
        "schema_version": OPENFOOTBALL_RAW_ADMISSION_SCHEMA,
        "status": "verified_current_raw",
        "policy_version": POLICY_VERSION,
        "observed_before": verified_aggregate["observed_before"],
        "source_ids": verified_aggregate["source_ids"],
        "row_count": len(verified_rows),
        "selected_records": verified_aggregate["selected_records"],
        "admission_sha256": verified_aggregate["admission_sha256"],
        "source_manifest_sha256": verified_aggregate["source_manifest_sha256"],
        "parser_contract_sha256": verified_aggregate["parser_contract_sha256"],
        "rows_sha256": verified_aggregate["rows_sha256"],
    }
    return payload


def _isolate_openligadb_current(
    result: object,
    *,
    reference_time: datetime,
    error: str | None = None,
) -> dict[str, Any]:
    """Keep ODbL rows in the serving-only lane and mark every model gate off."""

    if (
        isinstance(result, dict)
        and result.get("provider") == "OpenLigaDB"
        and result.get("status") in {None, "ok", "degraded", "unavailable"}
        and _is_current_cycle_timestamp(result.get("retrieved_at"), reference_time)
        and isinstance(result.get("matches"), list)
        and all(isinstance(row, dict) for row in result["matches"])
        and isinstance(result.get("errors", []), list)
    ):
        payload = deepcopy(result)
    else:
        payload = {
            "errors": [
                {
                    "error": (
                        error or "OpenLigaDB current-cycle result failed structural admission"
                    )[:500],
                    "stage": "isolated_current_fetch",
                }
            ],
            "matches": [],
            "provider": "OpenLigaDB",
            "retrieved_at": reference_time.astimezone(timezone.utc).isoformat(),
            "status": "unavailable",
        }
    if payload.get("status") is None:
        payload["status"] = (
            "degraded"
            if payload["matches"] and payload.get("errors")
            else "ok"
            if payload["matches"]
            else "unavailable"
        )
    payload["matches"] = [
        {
            **row,
            "display_lane": "isolated_current_only",
            "enters_model": False,
            "model_eligible": False,
            "redistribution_allowed": False,
            "training_eligible": False,
        }
        for row in payload["matches"]
    ]
    payload.update(
        {
            "display_lane": "isolated_current_only",
            "fixture_feed_eligible": False,
            "material_feature_eligible": False,
            "model_eligible": False,
            "redistribution_allowed": False,
            "training_eligible": False,
            "rights": decide_source_rights(
                SourceId.OPENLIGADB_SECONDARY_RESULTS,
                UseCase.SERVE_CURRENT,
            ).as_dict(),
            "model_rights": decide_source_rights(
                SourceId.OPENLIGADB_SECONDARY_RESULTS,
                UseCase.MODEL_INPUT,
            ).as_dict(),
            "training_rights": decide_source_rights(
                SourceId.OPENLIGADB_SECONDARY_RESULTS,
                UseCase.TRAINING,
            ).as_dict(),
            "redistribution_rights": decide_source_rights(
                SourceId.OPENLIGADB_SECONDARY_RESULTS,
                UseCase.REDISTRIBUTION,
            ).as_dict(),
        }
    )
    return payload


def _fixture_count(snapshot: Any) -> int:
    """Return the canonical fixture count for current or legacy snapshots."""

    if not isinstance(snapshot, dict):
        return 0
    return len(fixture_rows(snapshot))


def _load_previous_snapshot(output: Path) -> tuple[str, dict[str, Any] | None]:
    """Load the previous hand-off defensively for the empty-result guard.

    A corrupt previous pointer is not treated as an empty season.  Keeping it
    in place while a provider returns zero fixtures gives the operator a
    visible failure to repair instead of replacing one unusable artifact with
    another.
    """

    if not output.exists():
        return "missing", None
    try:
        decoded = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "invalid", None
    return ("valid", decoded) if isinstance(decoded, dict) else ("invalid", None)


def _publication_diagnostic_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}{PUBLICATION_DIAGNOSTIC_SUFFIX}")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write a small control artifact without exposing a half-written JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _write_published_diagnostic(
    output: Path,
    snapshot: dict[str, Any],
    *,
    reference_time: datetime,
    allow_empty_snapshot: bool,
) -> None:
    """Mark the bounded hand-off diagnostic healthy after a successful swap."""

    _write_json_atomic(
        _publication_diagnostic_path(output),
        {
            "schema_version": "1.0.0",
            "status": "published",
            "reason": "snapshot_published",
            "detected_at": reference_time.astimezone(timezone.utc).isoformat(),
            "output": str(output),
            "published": {
                "as_of": snapshot.get("as_of"),
                "fixture_count": _fixture_count(snapshot),
                "content_sha256": _snapshot_sha256(snapshot),
                "empty_override": allow_empty_snapshot,
            },
        },
    )


def _guard_empty_snapshot(
    output: Path,
    snapshot: dict[str, Any],
    *,
    reference_time: datetime,
    allow_empty_snapshot: bool,
) -> dict[str, Any] | None:
    """Record an empty transition without ever replaying a whole snapshot.

    A v260 OpenFootball raw admission proves only the selected observations at
    its exact cutoff.  Returning a prior pointer would silently make those
    observations current without a new fetch-and-reparse admission. The caller
    therefore publishes the current empty, fail-closed snapshot and retains
    prior raw archives only as immutable history.
    """

    previous_state, previous = _load_previous_snapshot(output)
    attempted_count = _fixture_count(snapshot)
    previous_count = _fixture_count(previous)
    if (
        attempted_count != 0
        or previous_state == "missing"
        or (previous_state == "valid" and previous_count == 0)
    ):
        return None

    if allow_empty_snapshot:
        reason = "empty_fixture_set_explicitly_allowed"
    else:
        reason = "empty_fixture_set_fail_closed_no_current_raw_admission"
    diagnostic = {
        "schema_version": "1.0.0",
        "status": "allowed",
        "reason": reason,
        "detected_at": reference_time.astimezone(timezone.utc).isoformat(),
        "output": str(output),
        "attempted": {
            "as_of": snapshot.get("as_of"),
            "fixture_count": attempted_count,
            "content_sha256": _snapshot_sha256(snapshot),
            "provider": fixture_feed_section(snapshot).get("provider"),
            "provider_status": fixture_feed_section(snapshot).get("status"),
            "error_count": len(
                fixture_feed_section(snapshot).get("errors", [])
                if isinstance(fixture_feed_section(snapshot).get("errors"), list)
                else []
            ),
        },
        "previous": {
            "as_of": previous.get("as_of") if isinstance(previous, dict) else None,
            "fixture_count": previous_count,
            "content_sha256": _snapshot_sha256(previous) if isinstance(previous, dict) else None,
            "state": previous_state,
        },
    }
    diagnostic_path = _publication_diagnostic_path(output)
    _write_json_atomic(diagnostic_path, diagnostic)
    snapshot["publication"] = {
        "status": "allowed_empty",
        "reason": reason,
        "diagnostic_path": str(diagnostic_path),
        "attempted_as_of": snapshot.get("as_of"),
        "attempted_fixture_count": attempted_count,
        "previous_as_of": previous.get("as_of") if isinstance(previous, dict) else None,
        "previous_fixture_count": previous_count,
    }
    return None


def _apply_espn_event_updates(espn: dict, espn_markets: dict) -> int:
    """Refresh lagging scoreboard statuses from the same-event summary feed."""

    fixtures = espn.get("fixtures")
    updates = espn_markets.get("fixture_updates")
    if not isinstance(fixtures, list) or not isinstance(updates, list):
        return 0
    by_id = {
        fixture.get("id"): fixture
        for fixture in fixtures
        if isinstance(fixture, dict) and isinstance(fixture.get("id"), str)
    }
    applied = 0
    for update in updates:
        if not isinstance(update, dict):
            continue
        fixture = by_id.get(update.get("fixture_id"))
        status = update.get("status")
        if not isinstance(fixture, dict) or status not in {
            "upcoming",
            "live",
            "finished",
            "postponed",
            "cancelled",
        }:
            continue
        score = update.get("score")
        if status == "finished" and (
            not isinstance(score, dict)
            or any(
                isinstance(score.get(key), bool)
                or not isinstance(score.get(key), int)
                or score[key] < 0
                for key in ("home", "away")
            )
        ):
            # A finished status without two valid final scores is not a
            # causally usable result; leave the scoreboard row unchanged.
            continue
        fixture["status"] = status
        if isinstance(score, dict):
            fixture["score"] = score
        result_scope = update.get("result_scope")
        if isinstance(result_scope, str):
            fixture["result_scope"] = result_scope
        halftime_score = update.get("halftime_score")
        if isinstance(halftime_score, dict) and all(
            isinstance(halftime_score.get(key), int)
            and not isinstance(halftime_score.get(key), bool)
            and halftime_score[key] >= 0
            for key in ("home", "away")
        ):
            fixture["halftime_score"] = {
                "home": halftime_score["home"],
                "away": halftime_score["away"],
            }
        clock = update.get("clock")
        if isinstance(clock, (int, float)) and not isinstance(clock, bool):
            fixture["clock"] = clock
        elif isinstance(clock, str) and clock.strip():
            fixture["clock"] = clock.strip()[:40]
        period = update.get("period")
        if isinstance(period, int) and not isinstance(period, bool) and 0 <= period <= 10:
            fixture["period"] = period
        status_text = update.get("status_text")
        if isinstance(status_text, str) and status_text.strip():
            fixture["status_text"] = status_text.strip()[:120]
        source = update.get("source")
        if isinstance(source, dict):
            # The summary is now the provenance for the refreshed status and,
            # for a finished fixture, the final score itself.
            fixture["source"] = {
                **source,
                # Preserve the canonical provider identity expected by the
                # fixture contract while recording the exact status evidence.
                "name": "ESPN",
                "source_kind": "event_summary_status",
            }
        fixture["status_observed_at"] = update.get("observed_at")
        applied += 1
    return applied


def _reuse_recent_oddstorm_snapshot(
    attempted: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    reference_time: datetime,
    max_age: timedelta = ODDSTORM_STALE_FALLBACK_MAX_AGE,
) -> dict[str, Any]:
    """Apply the serving-rights gate before any OddStorm stale fallback.

    Policy v260 blocks this source, so the attempted empty envelope is returned
    before the previous snapshot is inspected. The bounded legacy mechanics
    below are reachable only after a future central serving decision changes.
    """

    if not _stale_replay_allowed("oddstorm"):
        return attempted
    if not isinstance(attempted, dict) or attempted.get("lines"):
        return attempted
    if attempted.get("status") not in {"empty", "unavailable"}:
        return attempted
    if not isinstance(previous, dict):
        return attempted
    previous_oddstorm = previous.get("oddstorm")
    if not isinstance(previous_oddstorm, dict):
        return attempted
    previous_lines = previous_oddstorm.get("lines")
    if not isinstance(previous_lines, list) or not previous_lines:
        return attempted
    try:
        previous_as_of = datetime.fromisoformat(str(previous["as_of"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return attempted
    if previous_as_of.tzinfo is None or previous_as_of.utcoffset() is None:
        return attempted
    previous_as_of = previous_as_of.astimezone(timezone.utc)
    reference = reference_time.astimezone(timezone.utc)
    age = reference - previous_as_of
    if age < timedelta(0) or age > max_age:
        return attempted

    fallback = dict(attempted)
    fallback["lines"] = deepcopy(previous_lines)
    fallback["status"] = "degraded_stale_cache"
    errors = list(attempted.get("errors") or [])
    errors.append(
        {
            "stage": "stale_fallback",
            "reason": "reused_recent_non_empty_snapshot",
            "previous_snapshot_as_of": previous_as_of.isoformat(),
            "age_seconds": int(age.total_seconds()),
            "max_age_seconds": int(max_age.total_seconds()),
        }
    )
    fallback["errors"] = errors
    fallback["stale_fallback"] = {
        "used": True,
        "reason": "empty_or_unavailable_current_response",
        "previous_snapshot_as_of": previous_as_of.isoformat(),
        "age_seconds": int(age.total_seconds()),
        "max_age_seconds": int(max_age.total_seconds()),
        "previous_raw_sha256": previous_oddstorm.get("raw_sha256"),
        "current_attempt_raw_sha256": attempted.get("raw_sha256"),
        "enters_model": False,
        "model_exclusion_reason": "stale_fallback_and_line_timestamp_unavailable",
    }
    return fallback


def _reuse_recent_fixture_source(
    attempted: dict[str, Any],
    *,
    previous_snapshot: dict[str, Any] | None,
    source_key: str,
    reference_time: datetime,
    max_age: timedelta = FIXTURE_SOURCE_STALE_FALLBACK_MAX_AGE,
) -> dict[str, Any]:
    """Apply source policy before carrying a bounded fixture snapshot.

    Unknown keys and every currently blocked source return the attempted
    envelope before prior rows are inspected. OpenFootball is deliberately not
    mapped because a current-cycle raw admission cannot authorize stale replay.
    """

    if not _stale_replay_allowed(source_key):
        return attempted
    if not isinstance(attempted, dict) or attempted.get("fixtures"):
        return attempted
    if attempted.get("status") not in {"unavailable", "degraded"}:
        return attempted
    if not isinstance(previous_snapshot, dict):
        return attempted
    previous = previous_snapshot.get(source_key)
    if not isinstance(previous, dict):
        return attempted
    previous_fixtures = previous.get("fixtures")
    if not isinstance(previous_fixtures, list) or not previous_fixtures:
        return attempted
    if not isinstance(previous.get("source_contract"), dict):
        return attempted
    try:
        retrieved_at = datetime.fromisoformat(str(previous["retrieved_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return attempted
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        return attempted
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware")
    retrieved_at = retrieved_at.astimezone(timezone.utc)
    reference = reference_time.astimezone(timezone.utc)
    age = reference - retrieved_at
    if age < timedelta(0) or age > max_age:
        return attempted

    fallback = deepcopy(previous)
    fallback["status"] = "stale"
    fallback["checked_at"] = reference.isoformat()
    fallback["errors"] = deepcopy(attempted.get("errors") or [])
    fallback["fallback"] = {
        "status": "carried_forward_after_source_failure",
        "source_key": source_key,
        "age_seconds": int(age.total_seconds()),
        "max_age_seconds": int(max_age.total_seconds()),
        "attempted_status": attempted.get("status"),
        "attempted_retrieved_at": attempted.get("retrieved_at"),
    }
    previous_diagnostics = previous.get("diagnostics")
    diagnostics: dict[str, Any] = (
        deepcopy(previous_diagnostics) if isinstance(previous_diagnostics, dict) else {}
    )
    diagnostics["stale_fixture_count"] = len(previous_fixtures)
    fallback["diagnostics"] = diagnostics
    return fallback


def _load_recent_fixture_source_snapshot(
    previous: dict[str, Any] | None,
    archive_dir: Path,
    *,
    source_key: str,
    reference_time: datetime,
    max_age: timedelta = FIXTURE_SOURCE_STALE_FALLBACK_MAX_AGE,
) -> dict[str, Any] | None:
    """Read an archive only after the exact serving source is policy-allowed."""

    if not _stale_replay_allowed(source_key):
        return None

    def usable(snapshot: object) -> bool:
        if not isinstance(snapshot, dict):
            return False
        source = snapshot.get(source_key)
        return (
            isinstance(source, dict)
            and isinstance(source.get("fixtures"), list)
            and bool(source["fixtures"])
            and isinstance(source.get("source_contract"), dict)
        )

    if usable(previous):
        return previous
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware")
    manifest = archive_dir / "snapshots.jsonl"
    try:
        raw_manifest = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    reference = reference_time.astimezone(timezone.utc)
    root = archive_dir.resolve()
    for raw_line in reversed(raw_manifest.splitlines()):
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        try:
            snapshot_at = datetime.fromisoformat(str(record["as_of"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            continue
        age = reference - snapshot_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > max_age:
            continue
        raw_path = record.get("raw_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        candidate = (archive_dir / raw_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        try:
            snapshot = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(snapshot, dict) or snapshot.get("as_of") != record.get("as_of"):
            continue
        if usable(snapshot):
            return snapshot
    return None


def _load_recent_oddstorm_snapshot(
    previous: dict[str, Any] | None,
    archive_dir: Path,
    *,
    reference_time: datetime,
    max_age: timedelta = ODDSTORM_STALE_FALLBACK_MAX_AGE,
) -> dict[str, Any] | None:
    """Read an OddStorm archive only after the serving policy allows it.

    V260 returns before opening the manifest. The legacy bounded reader remains
    available only behind a future central policy release; it never deletes or
    rewrites the append-only raw archive.
    """

    if not _stale_replay_allowed("oddstorm"):
        return None

    def usable(snapshot: object) -> bool:
        return (
            isinstance(snapshot, dict)
            and isinstance(snapshot.get("oddstorm"), dict)
            and isinstance(snapshot["oddstorm"].get("lines"), list)
            and bool(snapshot["oddstorm"]["lines"])
        )

    if usable(previous):
        return previous
    manifest = archive_dir / "snapshots.jsonl"
    try:
        raw_manifest = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    reference = reference_time.astimezone(timezone.utc)
    root = archive_dir.resolve()
    for raw_line in reversed(raw_manifest.splitlines()):
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        try:
            snapshot_at = datetime.fromisoformat(str(record["as_of"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            continue
        snapshot_at = snapshot_at.astimezone(timezone.utc)
        age = reference - snapshot_at
        if age < timedelta(0) or age > max_age:
            continue
        raw_path = record.get("raw_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        candidate = (archive_dir / raw_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        try:
            snapshot = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(snapshot, dict) or snapshot.get("as_of") != record.get("as_of"):
            continue
        if usable(snapshot):
            return snapshot
    return None


def _premier_league_matchweeks_for_horizon(
    fixtures: list[dict],
    *,
    reference_time: datetime,
    horizon: datetime,
) -> tuple[str, tuple[int, ...]]:
    """Choose a bounded official schedule window without a name-only join.

    The first-party service is keyed by matchweek.  We use only the upcoming
    ESPN kickoff dates to select a small surrounding window, then keep the
    official fixtures and IDs in their own source section.  No team-name
    reconciliation is performed here.
    """

    upcoming = []
    for fixture in fixtures:
        if (
            fixture.get("competition_id") != "premier-league"
            or fixture.get("status") != "upcoming"
        ):
            continue
        try:
            kickoff = datetime.fromisoformat(str(fixture["kickoff_at"])).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if reference_time.astimezone(timezone.utc) < kickoff <= horizon.astimezone(timezone.utc):
            upcoming.append(kickoff)
    if not upcoming:
        season = str(reference_time.year if reference_time.month >= 8 else reference_time.year - 1)
        season_start = datetime(int(season), 8, 1, tzinfo=timezone.utc)
        current_week = max(
            1, min(38, ((reference_time.astimezone(timezone.utc) - season_start).days // 7) + 1)
        )
        return season, tuple(range(max(1, current_week - 2), min(38, current_week + 2) + 1))
    first = min(upcoming)
    season_year = first.year if first.month >= 8 else first.year - 1
    season_start = datetime(season_year, 8, 1, tzinfo=timezone.utc)
    first_week = max(1, min(38, ((first - season_start).days // 7) + 1))
    last = max(upcoming)
    last_week = max(1, min(38, ((last - season_start).days // 7) + 1))
    # The public service's matchweek numbering is not guaranteed to align
    # with a simple seven-day calendar estimate (opening rounds can start
    # before/after the nominal season-start anchor).  Keep the request
    # bounded, but include one extra preceding matchweek so a current-round
    # fixture is not silently omitted from the official join window.
    start = max(1, first_week - 3)
    end = min(38, last_week + 2)
    return str(season_year), tuple(range(start, end + 1))


def _sofascore_window(
    fixtures: list[dict], *, reference_time: datetime, hours: int = 48
) -> list[dict]:
    """Select only near-term fixtures for provider-reported player context."""

    horizon = reference_time.astimezone(timezone.utc) + timedelta(hours=hours)
    selected: list[dict] = []
    for fixture in fixtures:
        try:
            kickoff = datetime.fromisoformat(str(fixture["kickoff_at"])).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if reference_time.astimezone(timezone.utc) <= kickoff <= horizon:
            selected.append(fixture)
    return selected


def _geocoding_cache_key(value: object) -> tuple[str, str] | None:
    """Return the stable venue key used to reuse a verified coordinate row."""

    if not isinstance(value, dict):
        return None
    nested_venue = value.get("venue")
    venue = nested_venue if isinstance(nested_venue, dict) else value
    city = venue.get("city")
    country = venue.get("country") or venue.get("requested_country")
    if (
        not isinstance(city, str)
        or not city.strip()
        or not isinstance(country, str)
        or not country.strip()
    ):
        return None
    return city.strip(), country.strip()


def _valid_cached_geocode(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        latitude = float(value["latitude"])
        longitude = float(value["longitude"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    )


def _prepare_geocoding_requests(
    fixtures: list[dict],
    previous_snapshot: dict[str, Any] | None,
) -> tuple[list[dict], list[dict]]:
    """Reuse coordinates only after the central serving policy permits it.

    V260 blocks Open-Meteo geocoding, so previous coordinates are never copied.
    The legacy exact city/country cache remains behind the policy branch for a
    future release rather than acting as an authorization mechanism.
    """

    if not decide_source_rights(
        SourceId.OPEN_METEO_GEOCODING,
        UseCase.SERVE_CURRENT,
    ).is_allowed:
        blocked_missing: list[dict] = []
        blocked_seen_keys: set[tuple[str, str]] = set()
        for fixture in fixtures:
            key = _geocoding_cache_key(fixture)
            if key is None:
                blocked_missing.append(fixture)
            elif key not in blocked_seen_keys:
                blocked_seen_keys.add(key)
                blocked_missing.append(fixture)
        return [], blocked_missing

    previous_geocoding = (
        previous_snapshot.get("geocoding") if isinstance(previous_snapshot, dict) else None
    )
    previous_rows = (
        previous_geocoding.get("geocodes") if isinstance(previous_geocoding, dict) else None
    )
    cached_by_key: dict[tuple[str, str], dict] = {}
    if isinstance(previous_rows, list):
        for row in previous_rows:
            key = _geocoding_cache_key(row)
            if key is None or not _valid_cached_geocode(row) or key in cached_by_key:
                continue
            cached_by_key[key] = row

    cached: list[dict] = []
    missing: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for fixture in fixtures:
        key = _geocoding_cache_key(fixture)
        if key is None:
            # Preserve the adapter's explicit per-fixture "city unavailable"
            # error instead of deduplicating all malformed venues together.
            missing.append(fixture)
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        previous = cached_by_key.get(key)
        if previous is None:
            missing.append(fixture)
            continue
        row = deepcopy(previous)
        fixture_id = fixture.get("id", fixture.get("fixture_id"))
        row["fixture_id"] = str(fixture_id) if fixture_id is not None else ""
        row["cache_reused_from_fixture_id"] = previous.get("fixture_id")
        cached.append(row)
    return cached, missing


def _lineup_poll_diagnostics(
    fixtures: list[dict],
    *,
    reference_time: datetime,
    source_results: dict[str, dict],
    horizon_hours: int = 48,
) -> dict[str, dict]:
    """Explain why each competition has (or has not) lineup coverage.

    A zero lineup count is not one state: the match may be outside the poll
    window, the public source may have been requested but not published an XI,
    the source may have failed, or no permitted adapter may exist.  Keep this
    as snapshot audit metadata so operators can distinguish those cases
    without promoting roster evidence or guessing a lineup.
    """

    reference = reference_time.astimezone(timezone.utc)
    horizon = reference + timedelta(hours=horizon_hours)
    stage_offsets = (
        ("t_minus_24h", timedelta(hours=24)),
        ("t_minus_6h", timedelta(hours=6)),
        ("t_minus_90m", timedelta(minutes=90)),
    )

    def parse_time(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def observation_time(row: dict) -> datetime | None:
        source = row.get("source")
        candidates = [
            row.get("observed_at"),
            row.get("retrieved_at"),
            source.get("observed_at") if isinstance(source, dict) else None,
            source.get("retrieved_at") if isinstance(source, dict) else None,
        ]
        for candidate in candidates:
            parsed = parse_time(candidate)
            if parsed is not None:
                return parsed
        return None

    diagnostics: dict[str, dict] = {}
    configured_sources = {
        "premier-league": ("premier_league_official", "Premier League official"),
        "la-liga": ("laliga_official", "LaLiga official"),
        "bundesliga": ("bundesliga_official", "Bundesliga official"),
        "serie-a": ("serie_a_official", "Serie A official"),
        "ligue-1": ("ligue1_official", "Ligue 1 official"),
    }
    for league in LEAGUES:
        candidates: list[dict] = []
        invalid_kickoff_count = 0
        for fixture in fixtures:
            if (
                not isinstance(fixture, dict)
                or fixture.get("competition_id") != league.id
                or fixture.get("status") != "upcoming"
            ):
                continue
            try:
                kickoff = datetime.fromisoformat(str(fixture["kickoff_at"])).astimezone(
                    timezone.utc
                )
            except (KeyError, TypeError, ValueError):
                invalid_kickoff_count += 1
                continue
            if reference <= kickoff <= horizon:
                candidates.append(fixture)

        source_key, provider = configured_sources.get(
            league.id,
            (None, f"{league.name_en} official lineup adapter"),
        )
        source = source_results.get(source_key, {}) if source_key else {}
        lineups = source.get("lineups") if isinstance(source, dict) else []
        errors = source.get("errors") if isinstance(source, dict) else []
        lineups = lineups if isinstance(lineups, list) else []
        errors = errors if isinstance(errors, list) else []
        source_status = source.get("status") if isinstance(source, dict) else None

        def identifiers(row: object) -> set[str]:
            """Return explicit provider/fixture identifiers from one row.

            Official feeds do not all use the ESPN fixture identity.  Keep
            these aliases local to the diagnostic projection; they are never
            treated as an entity-registration decision.
            """

            if not isinstance(row, dict):
                return set()
            values: set[str] = set()
            for key in (
                "id",
                "fixture_id",
                "match_id",
                "provider_fixture_id",
                "native_fixture_id",
                "native_match_id",
                "premier_league_match_id",
            ):
                value = row.get(key)
                if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                    values.add(str(value))
            for nested_key in ("source", "official_fixture", "fixture"):
                nested = row.get(nested_key)
                if isinstance(nested, dict):
                    values.update(identifiers(nested))
            return values

        def team_token(value: object) -> str:
            if not isinstance(value, str):
                return ""
            normalized = unicodedata.normalize("NFKC", value).casefold()
            return "".join(character for character in normalized if character.isalnum())

        def fixture_key(row: object) -> tuple[str, str, str] | None:
            if not isinstance(row, dict):
                return None
            base = row
            if (
                not row.get("home_team") or not row.get("away_team") or not row.get("kickoff_at")
            ) and isinstance(row.get("official_fixture"), dict):
                base = row["official_fixture"]
            kickoff = parse_time(base.get("kickoff_at"))
            home_value = base.get("home_team")
            away_value = base.get("away_team")
            home = team_token(
                canonical_team_name(league.id, home_value)
                if isinstance(home_value, str)
                else home_value
            )
            away = team_token(
                canonical_team_name(league.id, away_value)
                if isinstance(away_value, str)
                else away_value
            )
            if kickoff is None or not home or not away:
                return None
            return kickoff.isoformat(), home, away

        candidate_ids = {
            str(item.get("id")) for item in candidates if isinstance(item, dict) and item.get("id")
        }
        candidate_keys: dict[tuple[str, str, str], set[str]] = {}
        for candidate in candidates:
            candidate_id = candidate.get("id") if isinstance(candidate, dict) else None
            key = fixture_key(candidate)
            if isinstance(candidate_id, str) and key is not None:
                candidate_keys.setdefault(key, set()).add(candidate_id)
        source_id_candidates: dict[str, set[str]] = {}
        source_fixtures = source.get("fixtures") if isinstance(source, dict) else []
        for source_fixture in source_fixtures if isinstance(source_fixtures, list) else []:
            key = fixture_key(source_fixture)
            matches = candidate_keys.get(key, set()) if key is not None else set()
            if len(matches) != 1:
                continue
            for identifier in identifiers(source_fixture):
                source_id_candidates.setdefault(identifier, set()).update(matches)

        # The execution scheduler keeps provider-native fixture IDs in its
        # poll plan, while the public diagnostic is keyed by canonical ESPN
        # IDs.  Resolve the plan through the already exact source-fixture
        # identity bridge (and, when available, the same kickoff/team key) so
        # a carried poll is not displayed as a fresh due request.
        schedule = source.get("poll_schedule") if isinstance(source, dict) else None
        scheduled_plans_by_candidate: dict[str, dict] = {}
        if isinstance(schedule, dict) and isinstance(schedule.get("fixture_plans"), list):
            for scheduled in schedule["fixture_plans"]:
                if not isinstance(scheduled, dict):
                    continue
                resolved_ids: set[str] = set()
                for identifier in identifiers(scheduled):
                    resolved_ids.update(source_id_candidates.get(identifier, set()))
                    if identifier in candidate_ids:
                        resolved_ids.add(identifier)
                scheduled_key = fixture_key(scheduled)
                if scheduled_key is not None:
                    resolved_ids.update(candidate_keys.get(scheduled_key, set()))
                for candidate_id in resolved_ids:
                    if candidate_id in candidate_ids:
                        scheduled_plans_by_candidate[candidate_id] = scheduled

        candidate_summaries: list[dict[str, object]] = []
        for fixture in sorted(
            candidates,
            key=lambda item: str(item.get("kickoff_at") or ""),
        ):
            candidate_kickoff = parse_time(fixture.get("kickoff_at"))
            fixture_id = fixture.get("id")
            if candidate_kickoff is None or not isinstance(fixture_id, str) or not fixture_id:
                continue
            candidate_summaries.append(
                {
                    "fixture_id": fixture_id,
                    "kickoff_at": candidate_kickoff.isoformat(),
                    "home_team": fixture.get("home_team"),
                    "away_team": fixture.get("away_team"),
                }
            )
        confirmed_count = sum(
            1
            for row in lineups
            if isinstance(row, dict)
            and isinstance(row.get("lineups"), dict)
            and row["lineups"].get("confirmed") is True
        )
        available_count = sum(
            1
            for row in lineups
            if isinstance(row, dict)
            and isinstance(row.get("lineups"), dict)
            and row["lineups"].get("available") is True
        )
        model_eligible_count = sum(
            1
            for row in lineups
            if isinstance(row, dict)
            and isinstance(row.get("lineups"), dict)
            and row["lineups"].get("model_eligible") is True
        )
        observations = [
            observed
            for item in lineups
            if isinstance(item, dict)
            for observed in (observation_time(item),)
            if observed is not None
        ]
        latest_observed_at = max(observations).isoformat() if observations else None

        lineups_by_fixture: dict[str, list[dict]] = {}
        for item in lineups:
            if not isinstance(item, dict):
                continue
            resolved_ids = {
                candidate_id
                for identifier in identifiers(item)
                for candidate_id in (
                    {identifier}
                    if identifier in candidate_ids
                    else source_id_candidates.get(identifier, set())
                )
                if candidate_id in candidate_ids
            }
            key = fixture_key(item)
            if key is not None and len(candidate_keys.get(key, set())) == 1:
                resolved_ids.update(candidate_keys[key])
            if not resolved_ids:
                raw_fixture_id = item.get("fixture_id")
                if isinstance(raw_fixture_id, str) and raw_fixture_id:
                    resolved_ids.add(raw_fixture_id)
            for fixture_id in resolved_ids:
                lineups_by_fixture.setdefault(fixture_id, []).append(item)

        def stage_projection(
            kickoff: datetime | None,
        ) -> tuple[list[dict[str, object]], str | None, str | None, bool]:
            stage_plan: list[dict[str, object]] = []
            if kickoff is None:
                return stage_plan, None, None, False
            for stage, offset in stage_offsets:
                cutoff = kickoff - offset
                if reference >= kickoff:
                    stage_state = "closed"
                elif reference >= cutoff:
                    stage_state = "due"
                else:
                    stage_state = "upcoming"
                stage_plan.append(
                    {
                        "stage": stage,
                        "cutoff_at": cutoff.isoformat(),
                        "state": stage_state,
                    }
                )
            future_cutoffs = [
                item["cutoff_at"] for item in stage_plan if item["state"] == "upcoming"
            ]
            due_stages = [item["stage"] for item in stage_plan if item["state"] == "due"]
            if due_stages:
                # Once a stage has opened, the latest opened stage is the
                # causal stage for this cycle.  Choosing the first opened
                # stage would keep reporting t_minus_24h forever and would
                # make the scheduler re-poll the same window indefinitely.
                return stage_plan, str(due_stages[-1]), reference.isoformat(), True
            if future_cutoffs:
                return (
                    stage_plan,
                    str(next(item["stage"] for item in stage_plan if item["state"] == "upcoming")),
                    str(future_cutoffs[0]),
                    False,
                )
            return stage_plan, "lineup_confirmation", None, False

        fixture_plans: list[dict[str, object]] = []
        for candidate in candidate_summaries[:64]:
            fixture_id = str(candidate["fixture_id"])
            fixture_lineups = lineups_by_fixture.get(fixture_id, [])
            fixture_stage_plan, fixture_next_stage, fixture_next_poll_at, fixture_poll_due = (
                stage_projection(parse_time(candidate.get("kickoff_at")))
            )
            fixture_observations = [
                observed
                for item in fixture_lineups
                for observed in (observation_time(item),)
                if observed is not None
            ]
            scheduled_plan = scheduled_plans_by_candidate.get(fixture_id)
            if isinstance(scheduled_plan, dict):
                fixture_stage_plan = scheduled_plan.get("stage_plan", fixture_stage_plan)
                fixture_next_stage = scheduled_plan.get("next_stage", fixture_next_stage)
                fixture_next_poll_at = scheduled_plan.get("next_poll_at", fixture_next_poll_at)
                fixture_poll_due = bool(scheduled_plan.get("poll_due", fixture_poll_due))
            fixture_plans.append(
                {
                    **candidate,
                    "stage_plan": fixture_stage_plan,
                    "next_stage": fixture_next_stage,
                    "next_poll_at": fixture_next_poll_at,
                    "poll_due": fixture_poll_due,
                    "source_lineup_count": len(fixture_lineups),
                    "available_lineup_count": sum(
                        1
                        for item in fixture_lineups
                        if isinstance(item.get("lineups"), dict)
                        and item["lineups"].get("available") is True
                    ),
                    "confirmed_lineup_count": sum(
                        1
                        for item in fixture_lineups
                        if isinstance(item.get("lineups"), dict)
                        and item["lineups"].get("confirmed") is True
                    ),
                    "model_eligible_lineup_count": sum(
                        1
                        for item in fixture_lineups
                        if isinstance(item.get("lineups"), dict)
                        and item["lineups"].get("model_eligible") is True
                    ),
                    "latest_lineup_observed_at": max(fixture_observations).isoformat()
                    if fixture_observations
                    else None,
                }
            )

        next_fixture = candidate_summaries[0] if candidate_summaries else None
        next_plan = fixture_plans[0] if fixture_plans else {}
        next_stage_plan = next_plan.get("stage_plan")
        stage_plan = list(next_stage_plan) if isinstance(next_stage_plan, list) else []
        next_stage = next_plan.get("next_stage")
        next_poll_at = next_plan.get("next_poll_at")
        poll_due = bool(next_plan.get("poll_due", False))

        schedule = source.get("poll_schedule") if isinstance(source, dict) else None
        if isinstance(schedule, dict):
            next_stage = schedule.get("next_stage", next_stage)
            next_poll_at = schedule.get("next_poll_at", next_poll_at)
            poll_due = bool(schedule.get("poll_due", poll_due))

        # A source can return an empty, valid response before publication.  A
        # poll is still due at each causal stage, but the empty response must
        # never be treated as an observed XI or as model evidence.
        if source_key is None or not candidate_summaries:
            poll_due = False
        if source_key is None:
            next_stage = None
            next_poll_at = None
            for plan in fixture_plans:
                plan["next_stage"] = None
                plan["next_poll_at"] = None
                plan["poll_due"] = False

        if source_key is None:
            state = "not_configured"
            reason_code = "no_permitted_public_official_adapter"
            requested = False
            source_status = "not_configured"
        elif (
            isinstance(schedule, dict) and schedule.get("requested") is False and candidates
        ) or source_status == "not_due":
            state = "not_due"
            reason_code = str(
                schedule.get("reason_code")
                if isinstance(schedule, dict) and schedule.get("reason_code")
                else "poll_schedule_not_due"
            )
            requested = False
        elif not candidates:
            state = "not_due"
            reason_code = "no_upcoming_fixture_within_poll_window"
            requested = False
        elif errors and (lineups or available_count):
            state = "partial"
            reason_code = "source_errors_with_observations"
            requested = True
        elif errors:
            state = "source_unavailable"
            reason_code = "source_errors"
            requested = True
        elif available_count:
            state = "lineups_observed"
            reason_code = "lineup_rows_observed"
            requested = True
        elif lineups:
            # A provider may return a fixture envelope before its XI is
            # published.  The envelope is an observation of the poll, not an
            # observation of a lineup; keeping this state explicit prevents
            # the UI and downstream operators from treating an empty row as
            # usable team information.
            state = "awaiting_publication"
            reason_code = "source_polled_without_confirmed_xi"
            requested = True
        elif source_status == "not_requested":
            state = "not_requested"
            reason_code = "candidate_window_not_requested_by_adapter"
            requested = False
        else:
            state = "awaiting_publication"
            reason_code = "source_polled_without_confirmed_xi"
            requested = True

        diagnostics[league.id] = {
            "provider": provider,
            "source_key": source_key,
            "source_status": source_status or "unknown",
            "state": state,
            "reason_code": reason_code,
            "requested": requested,
            "reference_at": reference.isoformat(),
            "horizon_hours": horizon_hours,
            "candidate_count": len(candidates),
            "invalid_kickoff_count": invalid_kickoff_count,
            "source_lineup_count": len(lineups),
            "available_lineup_count": available_count,
            "confirmed_lineup_count": confirmed_count,
            "model_eligible_lineup_count": model_eligible_count,
            "error_count": len(errors),
            "next_fixture": next_fixture,
            "fixture_plans": fixture_plans,
            "stage_plan": stage_plan,
            "next_stage": next_stage,
            "next_poll_at": next_poll_at,
            "poll_due": poll_due,
            "latest_lineup_observed_at": latest_observed_at,
        }
    return diagnostics


_LINEUP_POLL_STAGES = (
    ("t_minus_24h", timedelta(hours=24)),
    ("t_minus_6h", timedelta(hours=6)),
    ("t_minus_90m", timedelta(minutes=90)),
)
_LINEUP_CONFIRMATION_RETRY_INTERVAL = timedelta(minutes=15)


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _lineup_stage_projection(
    kickoff: datetime,
    *,
    reference: datetime,
) -> dict[str, Any]:
    """Project the latest open causal stage for one upcoming fixture."""

    stage_plan: list[dict[str, str]] = []
    for stage, offset in _LINEUP_POLL_STAGES:
        cutoff = kickoff - offset
        if reference >= kickoff:
            state = "closed"
        elif reference >= cutoff:
            state = "due"
        else:
            state = "upcoming"
        stage_plan.append(
            {
                "stage": stage,
                "cutoff_at": cutoff.isoformat(),
                "state": state,
            }
        )
    due_indices = [index for index, item in enumerate(stage_plan) if item["state"] == "due"]
    if due_indices:
        index = due_indices[-1]
        return {
            "stage_plan": stage_plan,
            "stage_index": index,
            "next_stage": stage_plan[index]["stage"],
            "next_poll_at": stage_plan[index]["cutoff_at"],
            "poll_due": True,
        }
    future_indices = [
        index for index, item in enumerate(stage_plan) if item["state"] == "upcoming"
    ]
    if future_indices:
        index = future_indices[0]
        return {
            "stage_plan": stage_plan,
            "stage_index": index,
            "next_stage": stage_plan[index]["stage"],
            "next_poll_at": stage_plan[index]["cutoff_at"],
            "poll_due": False,
        }
    return {
        "stage_plan": stage_plan,
        "stage_index": None,
        "next_stage": "lineup_confirmation",
        "next_poll_at": None,
        "poll_due": False,
    }


def _next_stage_after(plan: dict[str, Any]) -> tuple[str | None, str | None]:
    index = plan.get("stage_index")
    stages = plan.get("stage_plan")
    if not isinstance(index, int) or not isinstance(stages, list):
        return None, None
    for item in stages[index + 1 :]:
        if isinstance(item, dict) and item.get("stage"):
            return str(item["stage"]), str(item.get("cutoff_at"))
    return "lineup_confirmation", None


def _official_lineup_poll_plan(
    fixtures: list[dict],
    *,
    competition_id: str,
    source_key: str,
    previous_snapshot: dict[str, Any] | None,
    source_now: datetime,
    horizon_hours: int = 48,
) -> dict[str, Any]:
    """Build an append-only, per-fixture/per-stage lineup poll schedule.

    A successful poll is sufficient for one causal stage.  The next cycle
    waits for the next stage cutoff; a prior source error deliberately keeps
    the fixture retryable.  Legacy snapshots without ``poll_schedule`` use
    their source observation time only when the fixture was present before,
    while newly discovered fixtures remain immediately eligible.
    """

    reference = source_now.astimezone(timezone.utc)
    horizon = reference + timedelta(hours=horizon_hours)
    candidates: list[dict] = []
    for fixture in fixtures:
        if (
            not isinstance(fixture, dict)
            or fixture.get("competition_id") != competition_id
            or fixture.get("status") != "upcoming"
        ):
            continue
        kickoff = _parse_utc_timestamp(fixture.get("kickoff_at"))
        if kickoff is not None and reference <= kickoff <= horizon:
            candidates.append(fixture)
    candidates.sort(key=lambda item: (str(item.get("kickoff_at", "")), str(item.get("id", ""))))

    previous = previous_snapshot if isinstance(previous_snapshot, dict) else {}
    previous_diag = previous.get("lineup_poll_diagnostics", {})
    previous_diag = (
        previous_diag.get(competition_id, {}) if isinstance(previous_diag, dict) else {}
    )
    previous_source = previous.get(source_key, {})
    previous_source = previous_source if isinstance(previous_source, dict) else {}
    previous_schedule = previous_source.get("poll_schedule")
    previous_schedule = previous_schedule if isinstance(previous_schedule, dict) else {}
    previous_by_fixture = previous_schedule.get("last_requested_by_fixture", {})
    previous_by_fixture = previous_by_fixture if isinstance(previous_by_fixture, dict) else {}
    previous_state = str(previous_diag.get("state") or "")
    retry_failures = previous_state in {"source_unavailable", "partial"} or bool(
        previous_source.get("errors")
    )
    confirmed_fixture_ids = {
        str(row.get("fixture_id"))
        for row in previous_source.get("lineups", [])
        if isinstance(row, dict)
        and row.get("fixture_id")
        and isinstance(row.get("lineups"), dict)
        and row["lineups"].get("confirmed") is True
        and row["lineups"].get("model_eligible") is True
    }

    known_previous_ids: set[str] = set(str(key) for key in previous_by_fixture)
    prior_plans = previous_diag.get("fixture_plans") if isinstance(previous_diag, dict) else None
    prior_last_requested_by_fixture: dict[str, datetime] = {}
    if isinstance(prior_plans, list):
        for item in prior_plans:
            if not isinstance(item, dict) or not item.get("fixture_id"):
                continue
            fixture_id = str(item["fixture_id"])
            known_previous_ids.add(fixture_id)
            last_requested = _parse_utc_timestamp(item.get("last_requested_at"))
            if last_requested is not None:
                prior_last_requested_by_fixture[fixture_id] = last_requested
    for row in previous_source.get("lineups", []):
        if isinstance(row, dict) and row.get("fixture_id"):
            known_previous_ids.add(str(row["fixture_id"]))
    for row in previous_source.get("fixtures", []):
        if isinstance(row, dict) and row.get("id"):
            known_previous_ids.add(str(row["id"]))
    legacy_last = _parse_utc_timestamp(previous_schedule.get("last_requested_at"))
    if legacy_last is None and previous_state not in {"", "not_requested", "not_due"}:
        legacy_last = _parse_utc_timestamp(previous_source.get("retrieved_at"))

    request_ids: list[str] = []
    plans: list[dict[str, Any]] = []
    requested_at_by_fixture: dict[str, str] = {
        str(key): str(value)
        for key, value in previous_by_fixture.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    for fixture in candidates:
        fixture_id = str(fixture.get("id") or "")
        kickoff = _parse_utc_timestamp(fixture.get("kickoff_at"))
        if not fixture_id or kickoff is None:
            continue
        projected = _lineup_stage_projection(kickoff, reference=reference)
        last_requested = _parse_utc_timestamp(previous_by_fixture.get(fixture_id))
        if last_requested is None:
            last_requested = prior_last_requested_by_fixture.get(fixture_id)
        if (
            last_requested is None
            and fixture_id in known_previous_ids
            and not previous_by_fixture
            and not prior_last_requested_by_fixture
        ):
            last_requested = legacy_last
        projected_poll_at = _parse_utc_timestamp(projected["next_poll_at"])
        should_request = bool(
            projected["poll_due"]
            and (
                retry_failures
                or last_requested is None
                or (projected_poll_at is not None and last_requested < projected_poll_at)
            )
        )
        next_stage = projected["next_stage"]
        next_poll_at = projected["next_poll_at"]
        poll_reason = "stage_due" if should_request else "poll_schedule_not_due"
        confirmation_window = reference >= kickoff - timedelta(minutes=90)
        if fixture_id in confirmed_fixture_ids:
            should_request = False
            next_stage = "lineup_confirmation"
            next_poll_at = None
            poll_reason = "confirmed_lineup_observed"
        elif (
            confirmation_window
            and last_requested is not None
            and last_requested >= kickoff - timedelta(minutes=90)
        ):
            retry_at = last_requested + _LINEUP_CONFIRMATION_RETRY_INTERVAL
            next_stage = "lineup_confirmation"
            next_poll_at = retry_at.isoformat() if retry_at < kickoff else None
            should_request = bool(retry_at < kickoff and reference >= retry_at)
            poll_reason = (
                "lineup_confirmation_retry_due"
                if should_request
                else "lineup_confirmation_retry_not_due"
                if retry_at < kickoff
                else "lineup_confirmation_window_closed"
            )
        fixture_plan = {
            "fixture_id": fixture_id,
            "kickoff_at": kickoff.isoformat(),
            "home_team": fixture.get("home_team"),
            "away_team": fixture.get("away_team"),
            "stage_plan": projected["stage_plan"],
            "next_stage": next_stage,
            "next_poll_at": next_poll_at,
            "poll_due": should_request,
            "poll_requested": should_request,
            "poll_reason": poll_reason,
            "last_requested_at": last_requested.isoformat() if last_requested else None,
        }
        plans.append(fixture_plan)
        if should_request:
            request_ids.append(fixture_id)
            requested_at_by_fixture[fixture_id] = reference.isoformat()
        elif projected["poll_due"] and not confirmation_window:
            next_stage, next_poll_at = _next_stage_after(projected)
            fixture_plan["next_stage"] = next_stage
            fixture_plan["next_poll_at"] = next_poll_at

    next_cycle = [
        item
        for item in plans
        if isinstance(item.get("next_poll_at"), str) and item.get("next_stage")
    ]
    next_cycle.sort(key=lambda item: str(item.get("next_poll_at")))
    next_plan = next_cycle[0] if next_cycle else {}
    return {
        "schema_version": "1.0.0",
        "competition_id": competition_id,
        "source_key": source_key,
        "candidate_count": len(candidates),
        "requested_fixture_ids": request_ids,
        "carried_forward_fixture_ids": [
            str(item.get("fixture_id"))
            for item in plans
            if item.get("fixture_id") not in request_ids
        ],
        "requested": bool(request_ids),
        "poll_due": bool(request_ids),
        "next_stage": next_plan.get("next_stage"),
        "next_poll_at": next_plan.get("next_poll_at"),
        "reason_code": (
            "lineup_confirmation_retry_due"
            if request_ids
            and any(item.get("poll_reason") == "lineup_confirmation_retry_due" for item in plans)
            else "stage_due"
            if request_ids
            else "confirmed_lineup_observed"
            if plans
            and all(item.get("poll_reason") == "confirmed_lineup_observed" for item in plans)
            else "poll_schedule_not_due"
            if candidates
            else "no_upcoming_fixture_within_poll_window"
        ),
        "last_requested_at": reference.isoformat()
        if request_ids
        else previous_schedule.get("last_requested_at"),
        "last_requested_by_fixture": {
            key: value
            for key, value in requested_at_by_fixture.items()
            if any(str(item.get("fixture_id")) == key for item in plans)
        },
        "fixture_plans": plans,
    }


def _carry_forward_rows(
    previous_source: dict[str, Any] | None,
    *,
    row_key: str,
    fixture_ids: set[str],
) -> list[dict]:
    if not decide_source_rights(
        SourceId.OFFICIAL_LEAGUE_LINEUPS,
        UseCase.SERVE_CURRENT,
    ).is_allowed:
        return []
    if not isinstance(previous_source, dict):
        return []
    rows = previous_source.get(row_key)
    if not isinstance(rows, list):
        return []
    result: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get("fixture_id") if row_key == "lineups" else row.get("id")
        if identifier is not None and str(identifier) in fixture_ids:
            result.append(deepcopy(row))
    return result


def _finalize_official_lineup_poll(
    result: dict[str, Any],
    *,
    previous_source: dict[str, Any] | None,
    plan: dict[str, Any],
    source_now: datetime,
) -> dict[str, Any]:
    """Merge carried observations and attach an explicit poll schedule."""

    payload = dict(result)
    requested_ids = {str(item) for item in plan.get("requested_fixture_ids", [])}
    carried_ids = {
        str(item)
        for item in plan.get("carried_forward_fixture_ids", [])
        if str(item) not in requested_ids
    }
    for key, row_key in (("fixtures", "fixtures"), ("lineups", "lineups")):
        current_rows = payload.get(key)
        current_rows = current_rows if isinstance(current_rows, list) else []
        existing = {
            str(row.get("id") if row_key == "fixtures" else row.get("fixture_id"))
            for row in current_rows
            if isinstance(row, dict)
        }
        for row in _carry_forward_rows(previous_source, row_key=row_key, fixture_ids=carried_ids):
            identifier = row.get("id") if row_key == "fixtures" else row.get("fixture_id")
            if identifier is not None and str(identifier) not in existing:
                current_rows.append(row)
                existing.add(str(identifier))
        payload[key] = current_rows
    payload["retrieved_at"] = source_now.astimezone(timezone.utc).isoformat()
    payload["poll_schedule"] = plan
    if not requested_ids and plan.get("candidate_count", 0):
        payload["status"] = "not_due"
        payload["errors"] = []
    elif not plan.get("candidate_count", 0):
        payload["status"] = "not_requested"
    return payload


def _not_due_official_lineup_result(
    provider: str,
    *,
    previous_source: dict[str, Any] | None,
    plan: dict[str, Any],
    source_now: datetime,
) -> dict[str, Any]:
    return _finalize_official_lineup_poll(
        {
            "provider": provider,
            "retrieved_at": source_now.astimezone(timezone.utc).isoformat(),
            "fixtures": [],
            "lineups": [],
            "errors": [],
            "status": "not_due" if plan.get("candidate_count") else "not_requested",
        },
        previous_source=previous_source,
        plan=plan,
        source_now=source_now,
    )


def _unavailable_official_lineup_result(
    provider: str,
    *,
    retrieved_at: datetime,
    error: Exception,
) -> dict[str, Any]:
    """Keep one official adapter failure isolated from its peers."""

    return {
        "provider": provider,
        "retrieved_at": retrieved_at.isoformat(),
        "fixtures": [],
        "lineups": [],
        "errors": [{"stage": "adapter", "error": f"{type(error).__name__}: {error}"}],
        "status": "unavailable",
    }


def _fetch_secondary_official_lineups(
    upcoming: list[dict],
    *,
    source_now: datetime,
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Gate every official host before optional concurrent fetch execution.

    V260 returns canonical empty blocks before inspecting upcoming fixtures or
    prior snapshots. Concurrent adapter execution is retained only behind a
    future release in which every exact source decision is allowed.
    """

    policy_specifications = (
        (
            "laliga_official",
            SourceId.OFFICIAL_LALIGA_LINEUPS,
            "LaLiga official",
        ),
        (
            "bundesliga_official",
            SourceId.OFFICIAL_BUNDESLIGA_LINEUPS,
            "Bundesliga official",
        ),
        (
            "serie_a_official",
            SourceId.OFFICIAL_SERIE_A_LINEUPS,
            "Serie A official",
        ),
        (
            "ligue1_official",
            SourceId.OFFICIAL_LIGUE1_LINEUPS,
            "Ligue 1 official",
        ),
    )
    decisions = {
        key: decide_source_rights(source_id, UseCase.NETWORK_FETCH)
        for key, source_id, _provider in policy_specifications
    }
    if not all(decision.is_allowed for decision in decisions.values()):
        return {
            key: _blocked_section(
                source_id,
                provider=provider,
                reference_time=source_now,
                empty_fields=("fixtures", "lineups"),
            )
            for key, source_id, provider in policy_specifications
        }

    specifications = (
        (
            "laliga_official",
            "LaLiga official",
            fetch_laliga_lineups,
            "la-liga",
        ),
        (
            "bundesliga_official",
            "Bundesliga official",
            fetch_bundesliga_lineups,
            "bundesliga",
        ),
        (
            "serie_a_official",
            "Serie A official",
            fetch_serie_a_lineups,
            "serie-a",
        ),
        (
            "ligue1_official",
            "Ligue 1 official",
            fetch_ligue1_lineups,
            "ligue-1",
        ),
    )
    # Keep the direct helper backward-compatible for callers that only pass
    # the original four competitions; the production snapshot still projects
    # an explicit not-requested Ligue 1 payload below.
    active_specifications = tuple(
        spec
        for spec in specifications
        if spec[0] != "ligue1_official"
        or any(fixture.get("competition_id") == "ligue-1" for fixture in upcoming)
        or (isinstance(previous_snapshot, dict) and "ligue1_official" in previous_snapshot)
    )

    def submit_args(competition_id: str) -> list[dict]:
        return [fixture for fixture in upcoming if fixture.get("competition_id") == competition_id]

    plans: dict[str, dict[str, Any]] = {}
    previous_sources: dict[str, dict[str, Any] | None] = {}
    for key, _provider, _adapter, competition_id in active_specifications:
        plans[key] = _official_lineup_poll_plan(
            upcoming,
            competition_id=competition_id,
            source_key=key,
            previous_snapshot=previous_snapshot,
            source_now=source_now,
        )
        previous_sources[key] = (
            previous_snapshot.get(key)
            if isinstance(previous_snapshot, dict) and isinstance(previous_snapshot.get(key), dict)
            else None
        )

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(
        max_workers=len(specifications),
        thread_name_prefix="matchline-official",
    ) as executor:
        futures = {
            key: executor.submit(
                adapter,
                [
                    fixture
                    for fixture in submit_args(competition_id)
                    if str(fixture.get("id")) in set(plans[key].get("requested_fixture_ids", []))
                ],
                now=source_now,
                horizon_hours=48,
            )
            for key, _provider, adapter, competition_id in active_specifications
            if plans[key].get("requested_fixture_ids")
        }
        for key, provider, _adapter, _competition_id in active_specifications:
            plan = plans[key]
            if not plan.get("requested_fixture_ids"):
                results[key] = _not_due_official_lineup_result(
                    provider,
                    previous_source=previous_sources[key],
                    plan=plan,
                    source_now=source_now,
                )
                continue
            try:
                value = futures[key].result()
                value = (
                    value
                    if isinstance(value, dict)
                    else _unavailable_official_lineup_result(
                        provider,
                        retrieved_at=source_now,
                        error=TypeError("adapter result must be an object"),
                    )
                )
                results[key] = _finalize_official_lineup_poll(
                    value,
                    previous_source=previous_sources[key],
                    plan=plan,
                    source_now=source_now,
                )
            except Exception as exc:  # noqa: BLE001 - isolate one public host
                results[key] = _finalize_official_lineup_poll(
                    _unavailable_official_lineup_result(
                        provider,
                        retrieved_at=source_now,
                        error=exc,
                    ),
                    previous_source=previous_sources[key],
                    plan=plan,
                    source_now=source_now,
                )
    return results


def _sync_unlocked(
    output: Path,
    *,
    now: datetime | None = None,
    archive_dir: Path | None = None,
    openfootball_raw_archive_dir: Path | None = None,
    intelligence_ledger: Path | None = DEFAULT_INTELLIGENCE_LEDGER,
    crawl4ai_config_path: Path | str | None = None,
    allow_empty_snapshot: bool = False,
    enable_wikidata: bool = False,
) -> dict:
    reference_time = now
    if reference_time is not None and reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    source_now = (reference_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
    archive_root = archive_dir or output.parent / DEFAULT_ARCHIVE_DIR.name
    openfootball_raw_root = _openfootball_raw_archive_root(
        output,
        openfootball_raw_archive_dir,
    )

    if _openfootball_archive_is_volatile(openfootball_raw_root):
        openfootball_current = _admit_openfootball_current(
            _empty_openfootball_current(
                source_now,
                error="OpenFootball raw archive path is volatile and cannot preserve evidence",
            ),
            reference_time=source_now,
        )
        openfootball_current.pop("raw_archive_receipts", None)
        openfootball_current["raw_archive_admission"] = _raw_archive_admission_blocked(
            source_now,
            reason="volatile_raw_archive_path",
        )
    else:
        raw_archive = OpenFootballRawArchive(openfootball_raw_root)
        try:
            openfootball_result: object = fetch_openfootball_current(
                now=source_now,
                raw_sink=raw_archive,
            )
        except Exception as exc:  # noqa: BLE001 - isolate the admitted current source
            openfootball_result = _empty_openfootball_current(
                source_now,
                error=f"{type(exc).__name__}: {exc}",
            )
        openfootball_candidate = _admit_openfootball_current(
            openfootball_result,
            reference_time=source_now,
        )
        openfootball_current = _verify_openfootball_current_from_raw_archive(
            openfootball_candidate,
            archive_root=openfootball_raw_root,
            reference_time=source_now,
        )
    fixture_feed = _openfootball_fixture_feed(openfootball_current)

    blocked = _blocked_current_sections(
        source_now,
        crawl4ai_config=crawl4ai_config_path,
    )
    blocked["crawl4ai"] = _crawl4ai_bridge_section(
        openfootball_current,
        reference_time=source_now,
        config_path=crawl4ai_config_path,
        archive_root=archive_root,
        blocked_section=blocked["crawl4ai"],
    )

    try:
        openligadb_result: object = fetch_openligadb_fixtures(now=source_now)
        openligadb = _isolate_openligadb_current(
            openligadb_result,
            reference_time=source_now,
        )
    except Exception as exc:  # noqa: BLE001 - isolate the serving-only ODbL lane
        openligadb = _isolate_openligadb_current(
            None,
            reference_time=source_now,
            error=f"{type(exc).__name__}: {exc}",
        )

    current_horizon = source_now + timedelta(days=16)
    upcoming: list[dict[str, Any]] = []
    for fixture in fixture_feed["fixtures"]:
        if fixture.get("status") != "upcoming":
            continue
        try:
            kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if (
            kickoff.tzinfo is not None
            and kickoff.utcoffset() is not None
            and source_now < kickoff.astimezone(timezone.utc) <= current_horizon
        ):
            upcoming.append(fixture)

    if enable_wikidata:
        try:
            wikidata_entities = fetch_wikidata_venues(
                upcoming,
                now=source_now,
                max_fixtures=WIKIDATA_MAX_FIXTURES,
                user_agent=WIKIDATA_USER_AGENT,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one permitted entity source
            wikidata_entities = _wikidata_unavailable_section(
                source_now,
                status="unavailable",
                reason=f"{type(exc).__name__}: {exc}",
            )
    else:
        wikidata_entities = _wikidata_unavailable_section(source_now)
    _attach_wikidata_venues(fixture_feed, wikidata_entities)

    lineup_poll_diagnostics = _lineup_poll_diagnostics(
        upcoming,
        reference_time=source_now,
        source_results={
            "premier_league_official": blocked["premier_league_official"],
            "laliga_official": blocked["laliga_official"],
            "bundesliga_official": blocked["bundesliga_official"],
            "serie_a_official": blocked["serie_a_official"],
            "ligue1_official": blocked["ligue1_official"],
        },
    )
    try:
        met_norway_weather = fetch_met_norway_weather(
            upcoming,
            now=source_now,
            user_agent=MET_NORWAY_USER_AGENT,
            # Normal-rank Wikidata venues are useful for the match page's
            # weather context, but their coordinates remain display-only and
            # are explicitly marked non-model-eligible by the adapter.
            allow_display_only_coordinates=True,
        )
    except Exception as exc:  # noqa: BLE001 - isolate one permitted weather source
        met_norway_weather = {
            "provider": "MET Norway Locationforecast",
            "source_id": SourceId.MET_NORWAY_WEATHER.value,
            "license": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "terms_url": "https://api.met.no/doc/TermsOfService",
            "attribution_required": True,
            "retrieved_at": source_now.isoformat(),
            "network_opened": False,
            "status": "unavailable",
            "weather": [],
            "errors": [{"fixture_id": "", "reason": f"{type(exc).__name__}: {exc}"}],
        }
    met_norway_weather = _compact_met_weather_diagnostics(met_norway_weather)
    snapshot: dict[str, Any] = {
        "schema_version": "1.0.0",
        "as_of": source_now.isoformat(),
        "expected_competitions": [league.id for league in LEAGUES],
        "roles": {
            "fixtures_and_results": (
                "OpenFootball CC0 current-cycle rows rebuilt from durable raw evidence only; "
                "stale replay remains disabled"
            ),
            "secondary_current_results": (
                "OpenLigaDB ODbL isolated serving lane; excluded from fixture_feed "
                "and every model/training/redistribution lane"
            ),
            "historical_training": ("OpenFootball durable raw archive verified consumer required"),
            "blocked_current_sources": (
                "All other v260 sources are canonical pre-network rights blocks"
            ),
            "raw_archive_replay": "current_cycle_reparse_only_no_stale_replay",
        },
        "fixture_feed": fixture_feed,
        "openfootball_current": openfootball_current,
        **blocked,
        "met_norway_weather": met_norway_weather,
        "wikidata_entities": wikidata_entities,
        "lineup_poll_diagnostics": lineup_poll_diagnostics,
        "openligadb": openligadb,
    }
    snapshot["source_registry"] = build_runtime_source_registry(snapshot)

    # This diagnostic may describe an empty transition, but v260 never returns
    # the previous payload. The current fail-closed snapshot is what gets
    # published; immutable prior archives remain untouched.
    _guard_empty_snapshot(
        output,
        snapshot,
        reference_time=source_now,
        allow_empty_snapshot=allow_empty_snapshot,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(output)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    _write_published_diagnostic(
        output,
        snapshot,
        reference_time=source_now,
        allow_empty_snapshot=allow_empty_snapshot,
    )
    archive_source_snapshot(
        snapshot,
        archive_root,
        now=source_now,
    )
    if intelligence_ledger is not None:
        archive_snapshot_intelligence(snapshot, intelligence_ledger)
    return snapshot


def sync(
    output: Path = DEFAULT_OUTPUT,
    *,
    now: datetime | None = None,
    archive_dir: Path | None = None,
    openfootball_raw_archive_dir: Path | None = None,
    intelligence_ledger: Path | None = DEFAULT_INTELLIGENCE_LEDGER,
    crawl4ai_config_path: Path | str | None = None,
    allow_empty_snapshot: bool = False,
    enable_wikidata: bool = True,
) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_name(f"{output.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another current-data sync is already running") from exc
        return _sync_unlocked(
            output,
            now=now,
            archive_dir=archive_dir,
            openfootball_raw_archive_dir=openfootball_raw_archive_dir,
            intelligence_ledger=intelligence_ledger,
            crawl4ai_config_path=crawl4ai_config_path,
            allow_empty_snapshot=allow_empty_snapshot,
            enable_wikidata=enable_wikidata,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        type=Path,
        default=None,
        help=(
            "durable OpenFootball raw-evidence root; precedence is explicit flag, "
            f"{OPENFOOTBALL_RAW_ARCHIVE_ENV}, then OUTPUT parent/openfootball-raw; "
            "/dev/shm and paths resolving below it are rejected before network fetch"
        ),
    )
    parser.add_argument("--intelligence-ledger", type=Path, default=DEFAULT_INTELLIGENCE_LEDGER)
    parser.add_argument(
        "--crawl4ai-config",
        type=Path,
        help="explicit Crawl4AI JSON configuration; omitted means no browser pages are crawled",
    )
    parser.add_argument(
        "--allow-empty-snapshot",
        action="store_true",
        help="allow an empty ESPN fixture result to replace current.json (operator-confirmed only)",
    )
    parser.add_argument(
        "--disable-wikidata",
        action="store_true",
        help="disable the bounded Wikidata team-to-venue coordinate lane for this cycle",
    )
    args = parser.parse_args()
    snapshot = sync(
        args.output,
        archive_dir=args.archive_dir,
        openfootball_raw_archive_dir=args.openfootball_raw_archive_dir,
        intelligence_ledger=args.intelligence_ledger,
        crawl4ai_config_path=args.crawl4ai_config,
        allow_empty_snapshot=args.allow_empty_snapshot,
        enable_wikidata=not args.disable_wikidata,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "publication": snapshot.get("publication", {"status": "published"}),
                "as_of": snapshot["as_of"],
                "fixtures": len(fixture_rows(snapshot)),
                "espn_status_updates": len(snapshot["espn_markets"].get("fixture_updates", [])),
                "team_features": len(snapshot["understat"]["team_features"]),
                "markets": len(snapshot["espn_markets"]["markets"]),
                "news_items": snapshot["news"]["item_count"],
                "weather": len(snapshot["weather"]["weather"]),
                "met_norway_weather": len(snapshot["met_norway_weather"]["weather"]),
                "wikidata_venues": len(snapshot["wikidata_entities"]["venues"]),
                "sofascore_events": len(snapshot["sofascore"]["events"]),
                "official_fixtures": len(snapshot["premier_league_official"]["fixtures"]),
                "official_lineups": len(snapshot["premier_league_official"]["lineups"]),
                "laliga_official_lineups": len(snapshot["laliga_official"]["lineups"]),
                "bundesliga_official_lineups": len(snapshot["bundesliga_official"]["lineups"]),
                "serie_a_official_lineups": len(snapshot["serie_a_official"]["lineups"]),
                "ligue1_official_lineups": len(snapshot["ligue1_official"]["lineups"]),
                "lineup_poll_states": {
                    competition_id: row["state"]
                    for competition_id, row in snapshot["lineup_poll_diagnostics"].items()
                },
                "oddstorm_lines": len(snapshot["oddstorm"]["lines"]),
                "openligadb_matches": len(snapshot["openligadb"]["matches"]),
                "crawl4ai_pages": len(snapshot["crawl4ai"].get("pages", [])),
                "espn_roster_teams": len(snapshot["espn_rosters"].get("rosters", [])),
                "espn_injury_team_reports": len(snapshot["espn_injuries"].get("reports", [])),
                "espn_injury_players": snapshot["espn_injuries"].get("reported_player_count", 0),
                "errors": len(fixture_feed_section(snapshot).get("errors", []))
                + len(snapshot["espn_markets"]["errors"])
                + len(snapshot["espn_injuries"]["errors"])
                + len(snapshot["understat"]["errors"])
                + len(snapshot["news"]["errors"])
                + len(snapshot["weather"]["errors"])
                + len(snapshot["met_norway_weather"]["errors"])
                + len(snapshot["geocoding"]["errors"])
                + len(snapshot["sofascore"]["errors"])
                + len(snapshot["premier_league_official"]["errors"])
                + len(snapshot["laliga_official"]["errors"])
                + len(snapshot["bundesliga_official"]["errors"])
                + len(snapshot["serie_a_official"]["errors"])
                + len(snapshot["ligue1_official"]["errors"])
                + len(snapshot["sports_lottery"]["errors"])
                + len(snapshot["oddstorm"]["errors"])
                + len(snapshot["openligadb"]["errors"])
                + len(snapshot["crawl4ai"]["errors"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
