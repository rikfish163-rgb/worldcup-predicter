"""Build the static Matchline bundle from the current snapshot and strict report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from league_platform.fixture_feed import fixture_rows
from league_platform.fixture_serving import (
    fixture_commercial_reuse_status,
    require_fixture_serving,
)
from league_platform.news_evidence import resolve_news_rows
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchiveError,
    verify_openfootball_snapshot_admission,
)
from league_platform.runtime_paths import resolve_future_openfootball_raw_archive_dir
from league_platform.source_rights import (
    POLICY_VERSION,
    SourceId,
    UseCase,
    decide_source_rights,
)
from league_platform.store import PlatformStore


def _bundle_build_id(snapshot: dict) -> str:
    """Return a deterministic identity for the exact source evidence bundle."""

    raw = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"bundle-{hashlib.sha256(raw).hexdigest()[:20]}"


def _metric(value: dict) -> dict:
    if not isinstance(value, dict):
        return {
            "sample_n": 0,
            "brier_score": None,
            "log_loss": None,
            "rps": None,
            "ece": None,
        }
    return {
        "sample_n": value.get("sample_n", 0),
        "brier_score": value.get("brier"),
        "log_loss": value.get("log_loss"),
        "rps": value.get("rps"),
        "ece": value.get("ece"),
    }


def _validated_ci(value: object) -> dict[str, float] | None:
    """Keep only a finite, ordered bootstrap interval at the public boundary."""

    if not isinstance(value, dict):
        return None
    try:
        mean = float(value["mean"])
        lower = float(value["lower"])
        upper = float(value["upper"])
        level = float(value["level"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (mean, lower, upper, level)):
        return None
    if not 0.0 < level <= 1.0 or lower > mean or mean > upper:
        return None
    return {
        "mean": round(mean, 8),
        "lower": round(lower, 8),
        "upper": round(upper, 8),
        "level": round(level, 6),
    }


def _independent_scoreline_compatibility(value: dict) -> dict | None:
    """Project the bounded, non-market scoreline diagnostic.

    The strict report keeps the independent scoreline probability separate
    from any market-blended display probability.  Sites only needs the
    comparison metrics, not the row-level matrices, so keep this projection
    deliberately small and tolerate older reports without the field.
    """

    three_way = value.get("three_way")
    if not isinstance(three_way, dict):
        return None
    independent = three_way.get("independent_scoreline")
    if not isinstance(independent, dict):
        return None

    def sample_n(key: str) -> int:
        raw = independent.get(key)
        if isinstance(raw, bool):
            return 0
        if isinstance(raw, int) and raw >= 0:
            return raw
        if isinstance(raw, float) and math.isfinite(raw) and raw.is_integer() and raw >= 0:
            return int(raw)
        return 0

    def metric(key: str) -> dict:
        item = independent.get(key)
        return _metric(item) if isinstance(item, dict) else _metric({})

    return {
        "status": independent.get("status")
        if isinstance(independent.get("status"), str)
        else "unavailable",
        "sample_n": sample_n("sample_n"),
        "market_sample_n": sample_n("market_sample_n"),
        "model": metric("model"),
        "market": metric("market"),
        "model_on_market_sample": metric("model_on_market_sample"),
        "market_comparison": {
            "brier_delta_ci": _validated_ci(independent.get("model_minus_market_brier_ci")),
        },
    }


def _strict_compatibility(value: dict) -> dict:
    three_way = value["three_way"]
    gate = value["gates"]["three_way"]
    model = three_way.get("model_on_market_sample") or three_way["model"]
    full_model = three_way["model"]
    historical = (
        three_way.get("historical_frequency_on_market_sample") or three_way["historical_frequency"]
    )
    full_historical = three_way["historical_frequency"]
    market = three_way.get("market")
    projected = {
        "status": "evaluated",
        "model": value["model"],
        "sample_n": model["sample_n"],
        "full_sample_n": value["sample_n"],
        "evaluation_season": "扩展逐季",
        "held_out_seasons": value["held_out_seasons"],
        "warmup_seasons": value["warmup_seasons"],
        "data_cutoff": value["data_cutoff"],
        "quality_gate": gate["status"],
        "selected_candidate": value["model"],
        "selected_metrics": _metric(model),
        "full_metrics": _metric(full_model),
        "baselines": {
            "historical_frequency": _metric(historical),
            "full_historical_frequency": _metric(full_historical),
            "market": _metric(market) if market else None,
        },
        "market_comparison": {
            "brier_delta_ci": _validated_ci(three_way.get("model_minus_market_brier_ci")),
            "model_brier_ci": _validated_ci(three_way.get("model_brier_ci")),
            "market_brier_ci": _validated_ci(three_way.get("market_brier_ci")),
        },
        "walk_forward_folds": [
            {
                "fold": fold["fold"],
                "start_at": fold["start_at"],
                "end_at": fold["end_at"],
                "sample_n": fold["sample_n"],
                "brier_score": fold["three_way_brier"],
                "log_loss": None,
                "rps": None,
                "ece": None,
            }
            for fold in value.get("walk_forward_folds", [])
        ],
        "strict_targets": value["targets"],
        "gates": value["gates"],
        "time_audit": value.get("time_audit"),
        "freeze_stages": value.get("freeze_stages", {}),
    }
    independent = _independent_scoreline_compatibility(value)
    if independent is not None:
        projected["independent_scoreline"] = independent
    return projected


def _compact_prediction(value: dict) -> dict:
    """Keep the fields needed by the Sites research view and nothing else."""

    fields = (
        "fixture_id",
        "competition_id",
        "kickoff_at",
        "home_team",
        "away_team",
        "training_cutoff",
        "as_of",
        "cutoff_at",
        "prediction_observed_at",
        "model_version",
        "model_version_sha256",
        "prediction_source",
        "archive_record_key",
        "archive_content_sha256",
        "team_identities",
        "history_prior",
        "history_context",
        "player_availability_shadow",
        "freeze_stage",
        "freeze_versions",
        "status",
        "research_snapshot_status",
        "stale_snapshot",
        "stale_snapshot_as_of",
        "stale_snapshot_reference_at",
        "stale_snapshot_reason",
        "quality_gate",
        "coverage",
        "feature_coverage",
        "primary_probability_1x2",
        "scoreline_top5",
        "scoreline_matrix",
        "total_goals_probability",
        "half_full_probability",
        "handicap_line",
        "handicap_probability",
        "total_over_under_line",
        "total_over_under_probability",
        "market_probability",
        "market_provider",
        "market_baseline_kind",
        "market_retrieved_at",
        "expected_goals",
        "live_feature_fields",
        "live_feature_sources",
        "factor_trace",
    )
    return {key: value.get(key) for key in fields}


def _compact_source_registry(value: dict) -> list[dict]:
    registry = value.get("source_registry")
    if not isinstance(registry, list):
        return []
    compact: list[dict] = []
    for item in registry:
        if not isinstance(item, dict):
            continue
        runtime = item.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        compact.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "source_tier": item.get("source_tier"),
                "role": item.get("role"),
                "fact_source": item.get("fact_source"),
                "access_policy": item.get("access_policy"),
                "model_policy": item.get("model_policy"),
                "status": item.get("status"),
                "runtime": {
                    key: runtime.get(key)
                    for key in (
                        "status",
                        "retrieved_at",
                        "checked_at",
                        "rights_status",
                        "terms_url",
                        "network_opened",
                        "access_allowed",
                        "authorization_required",
                        "record_count",
                        "extracted_record_count",
                        "fixture_count",
                        "available_count",
                        "model_eligible_count",
                        "not_published_count",
                        "error_count",
                        "errors",
                        "security_policy",
                        "tls_runtime",
                        "fanout",
                        "execution",
                        "execution_layer_count",
                        "fact_source_count",
                        "fanout_source_count",
                        "match_discovery",
                        "fixture_join",
                        "page_summaries",
                    )
                    if key in runtime
                },
            }
        )
    return compact


def _compact_lineup_poll_diagnostics(
    value: object,
    *,
    sources: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, object]]:
    """Keep aggregate lineup readiness without redistributing provider plans.

    The full sync snapshot contains fixture-level poll plans, but those plans
    are derived from a source lane that may be rights-blocked.  A public
    bundle only needs the bounded operational state: provider identity,
    rights/network status and counts.  Dropping fixture plans prevents a
    blocked source from looking like an observed lineup schedule while still
    letting the workbench explain why the lineup column is unavailable.
    """

    if not isinstance(value, Mapping):
        return {}
    source_map = sources if isinstance(sources, Mapping) else {}
    result: dict[str, dict[str, object]] = {}
    for competition_id, raw_row in list(value.items())[:32]:
        if not isinstance(competition_id, str) or not competition_id.strip():
            continue
        if not isinstance(raw_row, Mapping):
            continue
        source_key = raw_row.get("source_key")
        source_key = source_key if isinstance(source_key, str) and source_key.strip() else None
        source = source_map.get(source_key) if source_key else None
        source = source if isinstance(source, Mapping) else {}
        source_status = source.get("status")
        if not isinstance(source_status, str) or not source_status.strip():
            source_status = raw_row.get("source_status")
        source_status = source_status if isinstance(source_status, str) else None
        rights_status = source.get("rights_status")
        if not isinstance(rights_status, str) or not rights_status.strip():
            rights_status = raw_row.get("rights_status")
        rights_status = rights_status if isinstance(rights_status, str) else None
        network_opened = source.get("network_opened")
        if not isinstance(network_opened, bool):
            network_opened = raw_row.get("network_opened")
        if not isinstance(network_opened, bool):
            network_opened = None
        rights_blocked = source_status in {"rights_blocked", "blocked"} or (
            isinstance(rights_status, str) and rights_status.startswith("blocked")
        )
        state = "rights_blocked" if rights_blocked else raw_row.get("state")
        state = state if isinstance(state, str) and state.strip() else "unknown"

        def count(key: str) -> int:
            item = raw_row.get(key)
            if isinstance(item, bool):
                return 0
            if isinstance(item, int) and item >= 0:
                return item
            if isinstance(item, float) and math.isfinite(item) and item.is_integer() and item >= 0:
                return int(item)
            return 0

        poll_due = raw_row.get("poll_due") if isinstance(raw_row.get("poll_due"), bool) else False
        if rights_blocked:
            poll_due = False
        result[competition_id] = {
            "state": state[:48],
            "reason_code": str(raw_row.get("reason_code") or "lineup_readiness_pending")[:120],
            "provider": str(raw_row.get("provider") or "官方首发来源")[:160],
            "source_key": source_key,
            "source_status": source_status[:48] if source_status else None,
            "rights_status": rights_status[:120] if rights_status else None,
            "network_opened": network_opened,
            "candidate_count": count("candidate_count"),
            "source_lineup_count": count("source_lineup_count"),
            "available_lineup_count": count("available_lineup_count"),
            "confirmed_lineup_count": count("confirmed_lineup_count"),
            "model_eligible_lineup_count": count("model_eligible_lineup_count"),
            "poll_due": poll_due,
            "planning_scope": "aggregate_readiness_only",
        }
    return result


def _compact_intelligence_ledger(value: object, *, checked_at: object = None) -> dict | None:
    """Project only bounded integrity counters for the append-only ledger.

    The raw intelligence ledger is intentionally never embedded in the Sites
    bundle.  The runtime evidence pointer already contains a bounded summary;
    expose its counters so the offline read model can show real coverage while
    keeping malformed lines and quarantine diagnostics explicit.
    """

    if not isinstance(value, dict):
        return None

    def non_negative_int(key: str) -> int | None:
        item = value.get(key)
        if isinstance(item, bool):
            return None
        if isinstance(item, int) and item >= 0:
            return item
        if isinstance(item, float) and math.isfinite(item) and item.is_integer() and item >= 0:
            return int(item)
        return None

    compact = {
        "status": value.get("status") if isinstance(value.get("status"), str) else None,
        "physical_records": non_negative_int("physical_records"),
        "valid_json_records": non_negative_int("valid_json_records"),
        "recovered_json_records": non_negative_int("recovered_json_records"),
        "read_error_count": non_negative_int("read_error_count"),
        "quarantined_read_error_count": non_negative_int("quarantined_read_error_count"),
        "unquarantined_read_error_count": non_negative_int("unquarantined_read_error_count"),
        "quarantine_entry_count": non_negative_int("quarantine_entry_count"),
        "sha256": value.get("sha256") if isinstance(value.get("sha256"), str) else None,
        "quarantine_sha256": value.get("quarantine_sha256")
        if isinstance(value.get("quarantine_sha256"), str)
        else None,
        "checked_at": checked_at if isinstance(checked_at, str) else None,
    }
    # Do not publish an empty/invalid pointer as if it were a real count.
    if compact["valid_json_records"] is None and compact["physical_records"] is None:
        return None
    return compact


def _load_intelligence_ledger_projection(evidence_dir: Path | None) -> dict | None:
    """Load the newest bounded ledger summary written by runtime_evidence."""

    if evidence_dir is None or not evidence_dir.is_dir():
        return None
    candidates = sorted(
        evidence_dir.glob("prospective-lock-integrity-*-latest.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        projection = _compact_intelligence_ledger(
            payload.get("intelligence_ledger"),
            checked_at=payload.get("checked_at"),
        )
        if projection is not None:
            projection["evidence_pointer"] = f"{path.name}:intelligence_ledger"
            return projection
    return None


def _load_runtime_archive_projection(manifest_path: Path | None) -> dict | None:
    """Expose bounded recovery-archive metadata without reading the archive.

    The Sites bundle must say whether a durable recovery copy exists, but the
    read-model build should not decompress a hundreds-of-megabytes archive on
    every refresh.  The archive service owns hash verification; this projection
    only reads its small manifest and checks that the referenced file exists.
    It therefore never labels an archive as verified merely because a manifest
    is present.
    """

    if manifest_path is None:
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "status": "manifest_unavailable",
            "manifest_id": manifest_path.name,
            "archive_exists": False,
            "verification": "not_checked",
        }
    if not isinstance(raw, dict):
        return {
            "status": "manifest_invalid",
            "manifest_id": manifest_path.name,
            "archive_exists": False,
            "verification": "not_checked",
        }

    archive_file = raw.get("archive_file")
    safe_archive_file = (
        archive_file
        if isinstance(archive_file, str)
        and archive_file
        and Path(archive_file).name == archive_file
        and not Path(archive_file).is_absolute()
        else None
    )
    archive_exists = bool(
        safe_archive_file and (manifest_path.parent / safe_archive_file).is_file()
    )
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}

    def non_negative(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value >= 0:
            return int(value)
        return None

    def sha256(value: object) -> str | None:
        if not isinstance(value, str) or len(value) != 64:
            return None
        normalized = value.lower()
        return normalized if all(char in "0123456789abcdef" for char in normalized) else None

    verification = "not_checked"
    verified_at: str | None = None
    receipt_path = manifest_path.parent / "runtime-archive-verification.json"
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            verification = "invalid_receipt"
        else:
            if not isinstance(receipt, dict):
                verification = "invalid_receipt"
            elif receipt.get("status") != "verified":
                verification = "failed"
            else:
                exact_binding = (
                    archive_exists
                    and receipt.get("schema_version")
                    == "matchline.runtime_archive_verification.v1"
                    and receipt.get("manifest_file") == manifest_path.name
                    and receipt.get("archive_file") == safe_archive_file
                    and sha256(receipt.get("archive_sha256")) == sha256(raw.get("archive_sha256"))
                    and sha256(receipt.get("source_state_sha256"))
                    == sha256(source.get("state_sha256"))
                    and non_negative(receipt.get("archive_member_files"))
                    == non_negative(source.get("file_count"))
                    and isinstance(receipt.get("verified_at"), str)
                    and bool(receipt.get("verified_at"))
                )
                if exact_binding:
                    verification = "verified"
                    verified_at = (
                        receipt.get("verified_at")
                        if isinstance(receipt.get("verified_at"), str)
                        else None
                    )
                else:
                    verification = "stale_receipt"

    return {
        "status": raw.get("status") if isinstance(raw.get("status"), str) else "unknown",
        "manifest_id": manifest_path.name,
        "captured_at": raw.get("finished_at") if isinstance(raw.get("finished_at"), str) else None,
        "archive_file": safe_archive_file,
        "archive_exists": archive_exists,
        "archive_sha256": sha256(raw.get("archive_sha256")),
        "archive_bytes": non_negative(raw.get("archive_bytes")),
        "source_file_count": non_negative(source.get("file_count")),
        "source_bytes": non_negative(source.get("bytes")),
        "source_state_sha256": sha256(source.get("state_sha256")),
        "free_bytes_after": non_negative(raw.get("free_bytes_after")),
        "retained_archive_count": non_negative(raw.get("retained_archive_count")),
        "runtime_persistence": raw.get("runtime_persistence")
        if isinstance(raw.get("runtime_persistence"), str)
        else None,
        "restore_boundary": raw.get("restore_boundary")
        if isinstance(raw.get("restore_boundary"), str)
        else None,
        "verification": verification,
        "verified_at": verified_at,
    }


def _compact_publication_audit(value: object) -> dict:
    """Keep the public/local parity result small and machine-readable.

    The audit is an operational assertion, not a deployment action.  It is
    copied into the offline bundle so a researcher can see whether the public
    hostname was actually checked against the snapshot that produced this
    bundle.  Missing evidence remains ``not_verified`` rather than becoming a
    green default.
    """

    raw = value if isinstance(value, dict) else {}
    status = raw.get("status") if raw.get("status") in {"pass", "blocked"} else "not_verified"
    checks = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    local = raw.get("local") if isinstance(raw.get("local"), dict) else {}
    public = raw.get("public") if isinstance(raw.get("public"), dict) else {}
    preflight = (
        raw.get("public_preflight") if isinstance(raw.get("public_preflight"), dict) else {}
    )
    errors = raw.get("errors") if isinstance(raw.get("errors"), list) else []
    normalized_errors = [str(item)[:160] for item in errors if str(item).strip()][:12]
    failed_routes = [
        str(item)[:240]
        for item in (
            preflight.get("failed_routes")
            if isinstance(preflight.get("failed_routes"), list)
            else []
        )
        if isinstance(item, str) and item.strip()
    ][:20]

    def optional_bool(item: object) -> bool | None:
        return item if isinstance(item, bool) else None

    def optional_int(item: object) -> int | None:
        return int(item) if isinstance(item, (int, float)) and math.isfinite(float(item)) else None

    return {
        "schema_version": "matchline.publication_audit.v1",
        "status": status,
        "checked_at": raw.get("checked_at") if isinstance(raw.get("checked_at"), str) else None,
        "public_url": raw.get("public_url") if isinstance(raw.get("public_url"), str) else None,
        "errors": normalized_errors,
        "checks": {
            "public_read_model_reachable": optional_bool(
                checks.get("public_read_model_reachable")
            ),
            "model_lock_match": optional_bool(checks.get("model_lock_match")),
            "snapshot_time_comparable": optional_bool(checks.get("snapshot_time_comparable")),
            "snapshot_lag_seconds": optional_int(checks.get("snapshot_lag_seconds")),
            "snapshot_within_lag_budget": optional_bool(checks.get("snapshot_within_lag_budget")),
            "production_allowed_by_public_gate": optional_bool(
                checks.get("production_allowed_by_public_gate")
            ),
            "public_contract_routes_ok": optional_bool(
                checks.get("public_contract_routes_ok")
                if "public_contract_routes_ok" in checks
                else preflight.get("all_expected_routes_ok")
            ),
        },
        "local": {
            "as_of": local.get("as_of") if isinstance(local.get("as_of"), str) else None,
            "model_version_sha256": local.get("model_version_sha256")
            if isinstance(local.get("model_version_sha256"), str)
            else None,
            "production_ready": optional_bool(local.get("production_ready")),
        },
        "public": {
            "mode": public.get("mode") if isinstance(public.get("mode"), str) else None,
            "as_of": public.get("as_of") if isinstance(public.get("as_of"), str) else None,
            "model_version_sha256": public.get("model_version_sha256")
            if isinstance(public.get("model_version_sha256"), str)
            else None,
            "production_ready": optional_bool(public.get("production_ready")),
            "production_allowed": optional_bool(public.get("production_allowed")),
        },
        "public_preflight": {
            "status": preflight.get("status")
            if isinstance(preflight.get("status"), str)
            else None,
            "reachable": optional_bool(preflight.get("reachable")),
            "deployment_ready": optional_bool(preflight.get("deployment_ready")),
            "all_expected_routes_ok": optional_bool(preflight.get("all_expected_routes_ok")),
            "failed_routes": failed_routes,
        },
        "deployment_required": raw.get("deployment_required") is True,
        "interpretation": raw.get("interpretation")
        if isinstance(raw.get("interpretation"), str)
        else "尚未取得独立公网一致性审计证据。",
    }


def _compact_news(
    value: dict,
    *,
    resolved_rows: list[dict] | None = None,
    limit: int = 30,
) -> list[dict]:
    feeds = value.get("feeds") if isinstance(value, dict) else None
    if not isinstance(feeds, list):
        return []
    resolution_by_link: dict[str, dict] = {}
    for row in resolved_rows or []:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict) or not isinstance(payload.get("link"), str):
            continue
        if isinstance(payload.get("match_resolution"), dict):
            resolution_by_link[payload["link"]] = {
                "association_type": "fixture",
                "association_status": payload["match_resolution"].get("method"),
                "fixture_id": row.get("entity_id"),
                "association_confidence": payload["match_resolution"].get("confidence"),
            }
        elif isinstance(payload.get("team_resolution"), dict):
            resolution_by_link[payload["link"]] = {
                "association_type": "team",
                "association_status": payload["team_resolution"].get("method"),
                "team_provider_id": payload["team_resolution"].get("team_provider_id"),
                "team_name": payload["team_resolution"].get("canonical_team"),
                "association_confidence": payload["team_resolution"].get("confidence"),
            }
    items: list[dict] = []
    for feed in feeds:
        if not isinstance(feed, dict):
            continue
        source_name = feed.get("name") or value.get("provider") or "Public RSS"
        for item in feed.get("items", []):
            if not isinstance(item, dict):
                continue
            link = item.get("link") or feed.get("url")
            compact = {
                "source_name": source_name,
                "source_url": link,
                "title": item.get("title"),
                "summary": item.get("summary"),
                "published_at": item.get("published_at"),
            }
            if isinstance(link, str) and link in resolution_by_link:
                compact["resolution"] = resolution_by_link[link]
            items.append(compact)
            if len(items) >= limit:
                return items
    return items


def _compact_matches(
    snapshot: dict,
    predictions: dict,
    *,
    research_predictions: dict | None = None,
    raw_sources: dict | None = None,
    upcoming_limit: int = 300,
    recent_limit: int = 120,
) -> list[dict]:
    matches = snapshot.get("matches")
    if not isinstance(matches, list):
        return []
    source_fixtures = fixture_rows(raw_sources) if isinstance(raw_sources, dict) else []
    source_by_id = {
        item.get("id"): item
        for item in source_fixtures
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    prediction_rows = predictions.get("predictions") if isinstance(predictions, dict) else None
    by_fixture = {
        item.get("fixture_id"): _compact_prediction(item)
        for item in prediction_rows or []
        if isinstance(item, dict) and item.get("fixture_id")
    }
    research_rows = (
        research_predictions.get("predictions") if isinstance(research_predictions, dict) else None
    )
    research_by_fixture = {
        item.get("fixture_id"): _compact_prediction(item)
        for item in research_rows or []
        if isinstance(item, dict) and item.get("fixture_id")
    }
    research_blocked_by_fixture = {
        item.get("fixture_id"): item
        for item in (
            research_predictions.get("blocked", [])
            if isinstance(research_predictions, dict)
            else []
        )
        if isinstance(item, dict) and item.get("fixture_id")
    }
    upcoming = [
        item
        for item in matches
        if isinstance(item, dict) and item.get("status") in {"upcoming", "postponed"}
    ]
    recent = [
        item for item in matches if isinstance(item, dict) and item.get("status") == "finished"
    ]
    upcoming.sort(key=lambda item: item.get("kickoff_at") or "")
    recent.sort(key=lambda item: item.get("kickoff_at") or "", reverse=True)
    selected: list[dict] = []
    for item in [*upcoming[:upcoming_limit], *recent[:recent_limit]]:
        fixture_id = item.get("id")
        source_fixture = source_by_id.get(fixture_id, {})
        provider_fixture_ids = item.get("provider_fixture_ids")
        if not isinstance(provider_fixture_ids, dict):
            provider_fixture_ids = source_fixture.get("provider_fixture_ids")
        if not isinstance(provider_fixture_ids, dict):
            provider_fixture_ids = {}
        provider_fixture_ids = {
            str(provider): str(provider_fixture_id)
            for provider, provider_fixture_id in provider_fixture_ids.items()
            if isinstance(provider, str)
            and provider.strip()
            and isinstance(provider_fixture_id, (str, int))
            and not isinstance(provider_fixture_id, bool)
            and str(provider_fixture_id).strip()
        }
        source = (
            source_fixture.get("source") if isinstance(source_fixture.get("source"), dict) else {}
        )
        source_name = source.get("name") if isinstance(source.get("name"), str) else None
        if (
            not provider_fixture_ids
            and source_name
            in {
                "OpenFootball",
                "CFL official",
                "ESPN",
            }
            and isinstance(fixture_id, str)
        ):
            # Current fixture IDs are provider-scoped and immutable. Preserve
            # that exact identity for D1/offline runtime joins instead of
            # relabelling every row as the retired ESPN source.
            provider_fixture_ids[source_name] = fixture_id
        venue = item.get("venue")
        if venue is None:
            venue = source_fixture.get("venue")
        if not isinstance(venue, (dict, str)):
            venue = None
        research_prediction = research_by_fixture.get(fixture_id)
        research_block = research_blocked_by_fixture.get(fixture_id)
        if research_prediction is not None:
            research_state = "available"
            research_reason = None
            research_message = None
            research_observed_at = research_prediction.get("as_of")
        elif item.get("sales_only") is True:
            research_state = "not_eligible"
            research_reason = item.get("model_exclusion_reason") or "sales_only_fixture"
            research_message = (
                item.get("model_exclusion_reason")
                or "销售场次尚未完成规范赛事实体绑定，保留在展示层。"
            )
            research_observed_at = None
        elif item.get("status") not in {"upcoming", "postponed"}:
            research_state = "not_applicable"
            research_reason = "fixture_not_upcoming"
            research_message = "已结束或非未来场次不生成当前研究预测。"
            research_observed_at = None
        elif research_block is not None:
            research_state = "blocked"
            research_reason = research_block.get("reason") or "research_prediction_blocked"
            research_message = research_block.get("message")
            research_observed_at = None
        else:
            research_state = "unavailable"
            research_reason = "no_research_prediction_record"
            research_message = "当前快照没有可追溯的研究预测或阻断记录。"
            research_observed_at = None
        selected.append(
            {
                "id": fixture_id,
                "competition_id": item.get("competition_id"),
                "season": item.get("season"),
                "kickoff_at": item.get("kickoff_at"),
                "home_team": item.get("home_team"),
                "away_team": item.get("away_team"),
                "home_provider_team_id": item.get("home_provider_team_id")
                or source_fixture.get("home_provider_team_id"),
                "away_provider_team_id": item.get("away_provider_team_id")
                or source_fixture.get("away_provider_team_id"),
                "provider_fixture_ids": provider_fixture_ids,
                "venue": venue,
                "status": item.get("status"),
                "score": item.get("score"),
                "market_probability": item.get("market_probability"),
                "prediction": by_fixture.get(fixture_id),
                "research_prediction": research_prediction,
                "research_prediction_state": research_state,
                "research_prediction_reason": research_reason,
                "research_prediction_message": research_message,
                "research_prediction_observed_at": research_observed_at,
                "sales_only": item.get("sales_only") is True,
                "model_eligible": item.get("model_eligible") is True,
                "model_exclusion_reason": item.get("model_exclusion_reason"),
                "sales_link_status": item.get("sales_link_status"),
                "sales_link_reason": item.get("sales_link_reason"),
                "sales_market": item.get("sales_market"),
            }
        )
    return selected


def _research_prediction_coverage(snapshot: dict, research_predictions: dict | None) -> dict:
    """Build an auditable coverage ledger for the researcher-facing forecast lane.

    The ledger deliberately counts upcoming/postponed fixtures only. Finished
    matches and sales-only rows remain visible in the match list, but are not
    silently treated as missing model coverage.
    """

    rows = research_predictions if isinstance(research_predictions, dict) else {}
    predictions = {
        item.get("fixture_id"): item
        for item in rows.get("predictions", [])
        if isinstance(item, dict) and item.get("fixture_id")
    }
    blocked = {
        item.get("fixture_id"): item
        for item in rows.get("blocked", [])
        if isinstance(item, dict) and item.get("fixture_id")
    }

    def parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    reference_candidates: list[tuple[str, object]] = [
        ("research_predictions.as_of", rows.get("as_of"))
    ]
    current = snapshot.get("current_data")
    if isinstance(current, dict):
        reference_candidates.append(("snapshot.current_data.as_of", current.get("as_of")))
    reference_candidates.append(("snapshot.generated_at", snapshot.get("generated_at")))
    reference_source: str | None = None
    reference_at: datetime | None = None
    for candidate_source, candidate_value in reference_candidates:
        parsed = parse_timestamp(candidate_value)
        if parsed is not None:
            reference_source = candidate_source
            reference_at = parsed
            break

    candidate_matches = [
        match
        for match in snapshot.get("matches", [])
        if isinstance(match, dict) and match.get("status") in {"upcoming", "postponed"}
    ]

    def state_for(match: dict) -> tuple[str, str | None]:
        fixture_id = match.get("id")
        if fixture_id in predictions:
            return "available", None
        if match.get("sales_only") is True:
            return "not_eligible", None
        if fixture_id in blocked:
            return "blocked", blocked.get(fixture_id, {}).get("reason") or "unknown"
        return "unavailable", "no_research_prediction_record"

    def summarize(
        matches: list[dict], *, window: str, start: datetime | None, end: datetime | None
    ) -> dict:
        by_competition: dict[str, dict[str, int]] = {}
        by_reason: dict[str, int] = {}
        counts = {"total": 0, "available": 0, "blocked": 0, "not_eligible": 0, "unavailable": 0}
        coverage_bands = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
        for match in matches:
            competition_id = str(match.get("competition_id") or "unknown")
            bucket = by_competition.setdefault(
                competition_id,
                {"total": 0, "available": 0, "blocked": 0, "not_eligible": 0, "unavailable": 0},
            )
            bucket["total"] += 1
            counts["total"] += 1
            fixture_id = match.get("id")
            state, reason = state_for(match)
            bucket[state] += 1
            counts[state] += 1
            if state == "available":
                coverage = predictions[fixture_id].get("coverage")
                level = coverage.get("level") if isinstance(coverage, dict) else None
                if level in coverage_bands:
                    coverage_bands[level] += 1
                else:
                    coverage_bands["unknown"] += 1
            elif reason is not None:
                by_reason[str(reason)] = by_reason.get(str(reason), 0) + 1
        model_scope = counts["total"] - counts["not_eligible"]
        non_low_available = coverage_bands["high"] + coverage_bands["medium"]
        return {
            "status": "available"
            if window == "catalog" or reference_at is not None
            else "unavailable",
            "window": window,
            "start_at": start.isoformat().replace("+00:00", "Z") if start else None,
            "end_at": end.isoformat().replace("+00:00", "Z") if end else None,
            "total_fixtures": counts["total"],
            "model_scope_fixtures": model_scope,
            "available": counts["available"],
            "blocked": counts["blocked"],
            "not_eligible": counts["not_eligible"],
            "unavailable": counts["unavailable"],
            "coverage_ratio": round(counts["available"] / model_scope, 6)
            if model_scope > 0
            else None,
            "coverage_bands": coverage_bands,
            "non_low_available": non_low_available,
            "non_low_coverage_ratio": round(non_low_available / model_scope, 6)
            if model_scope > 0
            else None,
            "high_coverage_ratio": round(coverage_bands["high"] / model_scope, 6)
            if model_scope > 0
            else None,
            "by_competition": by_competition,
            "by_reason": by_reason,
        }

    catalog = summarize(candidate_matches, window="catalog", start=None, end=None)
    windows: dict[str, dict] = {}
    for label, days in (("3d", 3), ("7d", 7)):
        if reference_at is None:
            end = None
            selected: list[dict] = []
        else:
            end = reference_at + timedelta(days=days)
            selected = [
                match
                for match in candidate_matches
                if (kickoff := parse_timestamp(match.get("kickoff_at"))) is not None
                and reference_at < kickoff <= end
            ]
        windows[label] = summarize(
            selected,
            window=label,
            start=reference_at,
            end=end,
        )

    catalog.update(
        {
            "status": rows.get("status") or "unavailable",
            "as_of": rows.get("as_of"),
            "scope": "upcoming_and_postponed_catalog",
            "coverage_denominator": "full_upcoming_and_postponed_catalog",
            "window_reference_at": reference_at.isoformat().replace("+00:00", "Z")
            if reference_at
            else None,
            "window_reference_source": reference_source,
            "windows": windows,
            "public_window": "7d",
            "public_window_total_fixtures": windows["7d"]["total_fixtures"],
            "public_window_model_scope_fixtures": windows["7d"]["model_scope_fixtures"],
            "public_window_available": windows["7d"]["available"],
            "public_window_coverage_ratio": windows["7d"]["coverage_ratio"],
            "model_boundary": "research_only_unfrozen_display",
            "production_allowed": False,
        }
    )
    return catalog


def _sales_only_competition_id(label: object) -> str:
    """Return a stable display-only competition ID for an unmapped sales label.

    The Sports Lottery feed can contain competitions outside the model
    leagues.  These labels are intentionally not promoted into the canonical
    model catalog, but they still deserve a stable ID in the offline research
    desk so a researcher can filter and open the sold fixture.  Hashing the
    normalized label avoids using untrusted text as an identifier while still
    keeping the ID stable across refreshes.
    """

    raw = unicodedata.normalize("NFKC", str(label or "")).strip().casefold()
    normalized = " ".join(raw.split()) or "unknown"
    digest = hashlib.sha1(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"sales-only:{digest}"


def _sales_only_competition(label: object, competition_id: str) -> dict[str, Any]:
    name = str(label or "赛事待确认").strip() or "赛事待确认"
    return {
        "id": competition_id,
        "name_zh": name,
        "name_en": f"Sports Lottery · {name}",
        "country_zh": "中国体育彩票销售台",
        "timezone": "Asia/Shanghai",
        "source_status": "sales_schedule",
        "source_message": "仅来自体彩公开销售赛程，未进入当前模型赛事目录。",
        "latest_event_at": None,
        "data_quality": {
            "row_count": 0,
            "duplicate_fixture_rate": None,
            "score_completeness": None,
            "odds_completeness": None,
        },
        "model_health": {
            "status": "not_evaluated",
            "model": "display_only_sales_schedule",
            "quality_gate": "sales_schedule_display_only",
            "sample_n": 0,
            "message": "未建立历史训练、校准和严格留出样本；不生成胜平负或比分预测。",
        },
        "catalog_scope": "sales_only",
    }


def _sales_only_fixture_id(row: dict[str, Any]) -> str | None:
    """Derive the same stable display ID used by the sales-only projection."""

    fixture_id = row.get("fixture_id")
    if isinstance(fixture_id, str) and fixture_id:
        return fixture_id
    match_id = row.get("match_id")
    if isinstance(match_id, (str, int)) and not isinstance(match_id, bool):
        native_match_id = str(match_id).strip()
        if native_match_id:
            return f"sporttery:{native_match_id}"
    material = "|".join(
        str(row.get(key) or "")
        for key in ("match_id", "match_num", "kickoff_at", "home_team", "away_team")
    )
    if not material.strip("|"):
        return None
    return (
        "sporttery:"
        + hashlib.sha1(material.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    )


def _sales_only_matches(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """Project unmapped official sales rows into a separate offline lane.

    This is deliberately a bundle-time projection.  It does not alter the
    canonical model-competition catalog, D1 fixture identity, or prospective model
    lock.  Rows remain ``sales_only`` and ``model_eligible=false`` even when
    the source provides official odds.
    """

    current = snapshot.get("current_data")
    if not isinstance(current, dict):
        return [], []
    rows = current.get("lottery_sales_schedule")
    if not isinstance(rows, list):
        return [], []
    existing_ids = {
        str(item.get("id"))
        for item in snapshot.get("matches", [])
        if isinstance(item, dict) and item.get("id")
    }
    matches: list[dict] = []
    competitions: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Linked/known rows already have a canonical fixture in the snapshot;
        # never create a duplicate visual row.
        if row.get("link_status") == "linked_to_model_fixture":
            continue
        kickoff = row.get("kickoff_at")
        home = str(row.get("home_team") or "").strip()
        away = str(row.get("away_team") or "").strip()
        if not isinstance(kickoff, str) or not kickoff or not home or not away:
            continue
        fixture_id = _sales_only_fixture_id(row)
        if fixture_id is None:
            continue
        if fixture_id in existing_ids:
            continue
        competition_id = row.get("competition_id")
        if not isinstance(competition_id, str) or not competition_id:
            competition_id = _sales_only_competition_id(row.get("league"))
        competitions.setdefault(
            competition_id, _sales_only_competition(row.get("league"), competition_id)
        )
        native_match_id = row.get("match_id")
        provider_fixture_ids = (
            {"Sports Lottery": str(native_match_id).strip()}
            if isinstance(native_match_id, (str, int))
            and not isinstance(native_match_id, bool)
            and str(native_match_id).strip()
            else {}
        )
        matches.append(
            {
                "id": fixture_id,
                "competition_id": competition_id,
                "season": kickoff[:4],
                "kickoff_at": kickoff,
                "home_team": home,
                "away_team": away,
                "home_provider_team_id": None,
                "away_provider_team_id": None,
                "provider_fixture_ids": provider_fixture_ids,
                "venue": None,
                "status": "upcoming",
                "score": None,
                "market_probability": None,
                "prediction": None,
                "sales_only": True,
                "model_eligible": False,
                "model_exclusion_reason": "sports_lottery_sales_fixture_not_canonically_joined",
                "sales_link_status": row.get("link_status") or "quarantined",
                "sales_link_reason": row.get("link_reason"),
                "source": row.get("source"),
                "sales_market": {
                    "provider": "Sports Lottery",
                    "had_odds": row.get("had_odds"),
                    "had_probability": row.get("had_probability"),
                    "hhad_line": row.get("hhad_line"),
                    "hhad_odds": row.get("hhad_odds"),
                    "hhad_probability": row.get("hhad_probability"),
                    "ttg_odds": row.get("ttg_odds"),
                    "ttg_probability": row.get("ttg_probability"),
                    "hafu_odds": row.get("hafu_odds"),
                    "hafu_probability": row.get("hafu_probability"),
                    "crs_odds": row.get("crs_odds"),
                    "crs_probability": row.get("crs_probability"),
                    "source": row.get("source"),
                },
            }
        )
    matches.sort(key=lambda item: (str(item.get("kickoff_at")), str(item.get("id"))))
    return matches, list(competitions.values())


def _news_candidate_rows(snapshot: dict) -> list[dict[str, Any]]:
    """Turn RSS feeds into resolver inputs without changing the raw ledger."""

    news = snapshot.get("news")
    if not isinstance(news, dict):
        return []
    rows: list[dict[str, Any]] = []
    for feed in news.get("feeds", []):
        if not isinstance(feed, dict):
            continue
        feed_source = {
            "name": feed.get("name") or news.get("provider") or "Public RSS",
            "url": feed.get("url"),
            "source_tier": "reliable_media",
            "raw_sha256": feed.get("raw_sha256"),
            "retrieved_at": feed.get("retrieved_at"),
        }
        for item in feed.get("items", []):
            if not isinstance(item, dict) or not isinstance(item.get("link"), str):
                continue
            rows.append(
                {
                    "kind": "news_fact_candidate",
                    "entity_type": "news",
                    "entity_id": item["link"],
                    "effective_at": item.get("published_at"),
                    "observed_at": feed.get("retrieved_at"),
                    "payload": {
                        key: item.get(key) for key in ("title", "summary", "link", "published_at")
                    },
                    "source_name": feed_source["name"],
                    "source_url": feed_source["url"],
                    "source_tier": feed_source["source_tier"],
                    "raw_hash": feed_source["raw_sha256"],
                    "enters_model": False,
                    "model_exclusion_reason": "news_fact_not_structured_model_feature",
                }
            )
    return rows


def _resolve_news_candidates(
    snapshot: dict, selected_matches: list[dict]
) -> tuple[list[dict], dict[str, Any]]:
    return resolve_news_rows(_news_candidate_rows(snapshot), selected_matches)


_OFFLINE_EVIDENCE_CATEGORIES = (
    "lineups",
    "playerAvailability",
    "odds",
    "weather",
    "news",
    "intelligence",
    "timeline",
    "alerts",
    "events",
    "matchStats",
)
_OFFLINE_EVIDENCE_LIMIT = 80


def _source_text(value: Any, *, limit: int = 512) -> str | None:
    """Keep provenance labels bounded before they enter the public bundle."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _source_details(
    value: Any, *, fallback_name: str, fallback_tier: str = "public_provider"
) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {
        "sourceName": source.get("name") or fallback_name,
        "sourceUrl": source.get("url") or "",
        "sourceTier": source.get("source_tier") or fallback_tier,
        "rawHash": source.get("raw_sha256"),
        "retrievedAt": _iso(source.get("retrieved_at")),
    }
    # Crawl4AI is a shared execution layer.  Preserve the declared source
    # identity and page lineage beside each joined fixture fact so a
    # researcher can distinguish the provider from the browser runtime and
    # walk an entry page -> detail page chain without reopening raw archives.
    provenance_fields = {
        "sourceId": ("source_id", "fact_source_id"),
        "factSourceId": ("fact_source_id", "source_id"),
        "sourceRole": ("source_role",),
        "captureRole": ("capture_role",),
        "captureEngine": ("capture_engine",),
        "parserContract": ("parser_contract",),
        "crawlStage": ("crawl_stage",),
        "parentUrl": ("parent_url",),
        "followReason": ("follow_reason",),
        "finalUrl": ("final_url",),
    }
    for output_key, input_keys in provenance_fields.items():
        value = next((source.get(key) for key in input_keys if source.get(key) is not None), None)
        text = _source_text(value)
        if text is not None:
            result[output_key] = text
    for output_key, input_key in (
        ("parentContentSha256", "parent_content_sha256"),
        ("contentSha256", "content_sha256"),
    ):
        digest = _source_text(source.get(input_key), limit=128)
        if digest is not None:
            result[output_key] = digest
    source_license = _source_text(
        source.get("license") or source.get("license_status"),
        limit=80,
    )
    if source_license is not None:
        result["sourceLicense"] = source_license
    return result


def _iso(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _causal_state(observed_at: str | None, kickoff_at: str | None) -> str:
    if not observed_at or not kickoff_at:
        return "time_unknown"
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        kickoff = datetime.fromisoformat(kickoff_at.replace("Z", "+00:00"))
    except ValueError:
        return "time_unknown"
    if observed.tzinfo is None or kickoff.tzinfo is None:
        return "time_unknown"
    return "pre_kickoff" if observed <= kickoff else "post_kickoff_excluded"


def _new_fixture_evidence() -> dict[str, list[dict[str, Any]]]:
    return {category: [] for category in _OFFLINE_EVIDENCE_CATEGORIES}


def _append_fixture_evidence(
    target: dict[str, dict[str, list[dict[str, Any]]]],
    fixture_id: Any,
    category: str,
    row: dict[str, Any],
) -> None:
    if (
        not isinstance(fixture_id, str)
        or not fixture_id
        or category not in _OFFLINE_EVIDENCE_CATEGORIES
    ):
        return
    evidence = target.setdefault(fixture_id, _new_fixture_evidence())
    rows = evidence[category]
    if len(rows) < _OFFLINE_EVIDENCE_LIMIT:
        # The compact package is a read-only evidence projection.  Enforce the
        # model boundary centrally so a newly added source adapter cannot
        # accidentally publish a model-eligible row into Sites.
        projected = dict(row)
        projected["entersModel"] = False
        rows.append(projected)


def _timeline_row(
    *,
    fixture_id: str,
    category: str,
    kind: str,
    source: dict[str, Any],
    observed_at: str | None,
    kickoff_at: str | None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"offline:{fixture_id}:{category}:{observed_at or 'unknown'}:{kind}",
        "category": category,
        "kind": kind,
        "effectiveAt": effective_at,
        "observedAt": observed_at,
        "causalState": _causal_state(observed_at, kickoff_at),
        "confidence": None,
        "entersModel": False,
        "conflictGroup": None,
        **source,
    }


def _public_page_admission(value: Any) -> dict[str, Any] | None:
    """Project a joined public-page admission decision without raw payloads.

    Crawl4AI is a shared fetch runtime, so a joined record must carry the
    declared source's own admission state into the offline research package.
    Keep only bounded, boolean checks that help a researcher understand why a
    row is display-only; never turn a candidate into a model feature here.
    """

    if not isinstance(value, dict):
        return None
    admission: dict[str, Any] = {}
    for source_key, target_key, limit in (
        ("status", "status", 40),
        ("reason_code", "reasonCode", 100),
        ("next_action", "nextAction", 180),
        ("source_license_status", "sourceLicenseStatus", 40),
    ):
        item = value.get(source_key)
        if isinstance(item, str) and item.strip():
            admission[target_key] = item.strip()[:limit]
    enters_model = value.get("enters_model")
    admission["entersModel"] = enters_model is True
    raw_checks = value.get("checks")
    if isinstance(raw_checks, dict):
        checks: dict[str, bool] = {}
        for key in (
            "parser_supported",
            "exact_fixture_join",
            "pre_match",
            "observed_before_kickoff",
            "license_reviewed",
        ):
            item = raw_checks.get(key)
            if isinstance(item, bool):
                checks[key] = item
        if checks:
            admission["checks"] = checks
    return admission or None


def _compact_lineup_players(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for player in value[:40]:
        if not isinstance(player, dict):
            continue
        rows.append(
            {
                key: player.get(key)
                for key in (
                    "id",
                    "player_id",
                    "playerId",
                    "provider_player_id",
                    "name",
                    "displayName",
                    "canonicalName",
                    "position",
                    "starter",
                    "status",
                    "status_type",
                    "jersey",
                    "age",
                    "citizenship",
                    "profile_url",
                    "expectedMinutes",
                    "replacementValue",
                )
                if key in player
            }
        )
    return rows


def _compact_event_participant_ids(value: Any) -> list[str]:
    """Keep bounded provider participant IDs in the display-only event row."""

    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for candidate in value[:6]:
        if isinstance(candidate, bool) or not isinstance(candidate, (str, int)):
            continue
        identifier = str(candidate).strip()
        if identifier and len(identifier) <= 80 and identifier not in ids:
            ids.append(identifier)
    return ids


def _compact_event_point(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    point: dict[str, float] = {}
    for axis in ("x", "y"):
        coordinate = value.get(axis)
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            return None
        if not math.isfinite(float(coordinate)) or not 0 <= float(coordinate) <= 100:
            return None
        point[axis] = round(float(coordinate), 3)
    return point


def _compact_event_location(value: Any) -> dict[str, Any] | None:
    """Preserve only provider-declared, bounded event coordinates."""

    if not isinstance(value, dict):
        return None
    if (
        value.get("provider_declared") is not True
        and value.get("providerDeclared") is not True
        and value.get("source") != "provider_declared"
    ):
        return None
    origin = _compact_event_point(value.get("origin"))
    target = _compact_event_point(value.get("target"))
    goal_y = value.get("goal_y", value.get("goalY"))
    if (
        isinstance(goal_y, bool)
        or not isinstance(goal_y, (int, float))
        or not math.isfinite(float(goal_y))
        or not 0 <= float(goal_y) <= 100
    ):
        goal_y = None
    if origin is None and target is None and goal_y is None:
        return None
    coordinate_system = value.get("coordinate_system", value.get("coordinateSystem"))
    if not isinstance(coordinate_system, str) or not coordinate_system.strip():
        coordinate_system = "provider_field_percent"
    result: dict[str, Any] = {
        "coordinate_system": coordinate_system.strip()[:80],
        "source": "provider_declared",
        "provider_declared": True,
        "origin": origin,
        "target": target,
        "goal_y": round(float(goal_y), 3) if goal_y is not None else None,
        "model_eligible": False,
    }
    return {key: item for key, item in result.items() if item is not None}


def _compact_event_score(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for side in ("home", "away"):
        score = value.get(side)
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or score < 0
            or int(score) != score
        ):
            return None
        result[side] = int(score)
    return result


def _compact_fixture_evidence(
    snapshot: dict,
    selected_matches: list[dict],
    *,
    resolved_news: list[dict] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Project current source records into a bounded, display-only fixture map.

    This map is deliberately separate from the prediction payload.  Every row
    carries the source URL/hash when available, an observed timestamp, and
    ``entersModel=false`` because a compact offline package is not a causal
    feature snapshot.  The projection improves the research view while never
    promoting a browser/API record into the model.
    """

    fixture_by_id = {
        item.get("id"): item
        for item in selected_matches
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    target: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def kickoff(fixture_id: str) -> str | None:
        value = fixture_by_id.get(fixture_id, {}).get("kickoff_at")
        return _iso(value)

    # Every canonical schedule row needs a minimum auditable lineage packet,
    # even when no market, lineup, weather, or news evidence is available.
    # This keeps the workbench traceable without publishing the raw payload.
    for fixture in fixture_rows(snapshot):
        fixture_id = fixture.get("id")
        if not isinstance(fixture_id, str) or fixture_id not in fixture_by_id:
            continue
        source_value = fixture.get("source")
        source = _source_details(
            source_value,
            fallback_name=str(
                (snapshot.get("fixture_feed") or snapshot.get("espn") or {}).get("provider")
                or "规范赛程源"
            ),
        )
        observed_at = _iso(
            source_value.get("retrieved_at") if isinstance(source_value, dict) else None
        )
        row = {
            "id": f"offline-fixture-{fixture_id}-{observed_at or 'unknown'}",
            "fixtureId": fixture_id,
            "kind": "canonical_fixture_schedule",
            "competitionId": fixture.get("competition_id"),
            "kickoffAt": fixture.get("kickoff_at"),
            "homeTeam": fixture.get("home_team"),
            "awayTeam": fixture.get("away_team"),
            "observedAt": observed_at,
            "modelExclusionReason": "offline_lineage_projection_not_model_input",
            **source,
        }
        _append_fixture_evidence(target, fixture_id, "intelligence", row)
        _append_fixture_evidence(
            target,
            fixture_id,
            "timeline",
            _timeline_row(
                fixture_id=fixture_id,
                category="intelligence",
                kind="canonical_fixture_schedule",
                source=source,
                observed_at=observed_at,
                kickoff_at=kickoff(fixture_id),
            ),
        )

    markets = snapshot.get("espn_markets")
    if isinstance(markets, dict):
        for item in markets.get("markets", []):
            if not isinstance(item, dict):
                continue
            fixture_id = item.get("fixture_id")
            observed_at = _iso(item.get("retrieved_at"))
            source = _source_details(
                item.get("source"), fallback_name=str(item.get("provider") or "市场来源")
            )
            row = {
                "id": f"offline-odds-{fixture_id}-{observed_at or 'unknown'}",
                "fixtureId": fixture_id,
                "marketType": "three_way",
                "marketRole": "public_reference",
                "officialPurchaseEligible": False,
                "marketEligibilityReason": "公开市场对照，不是体彩官方赔率，未完成官方销售场次绑定",
                "provider": item.get("provider"),
                "line": None,
                "odds": item.get("american_odds"),
                "impliedProbability": item.get("probability"),
                "observedAt": observed_at,
                "sourceObservationId": None,
                **source,
            }
            _append_fixture_evidence(target, fixture_id, "odds", row)
            if isinstance(fixture_id, str):
                _append_fixture_evidence(
                    target,
                    fixture_id,
                    "timeline",
                    _timeline_row(
                        fixture_id=fixture_id,
                        category="odds",
                        kind="three_way",
                        source=source,
                        observed_at=observed_at,
                        kickoff_at=kickoff(fixture_id),
                    ),
                )

        for item in markets.get("team_status", []):
            if not isinstance(item, dict):
                continue
            fixture_id = item.get("fixture_id")
            observed_at = _iso(item.get("retrieved_at"))
            source = _source_details(
                item.get("source"), fallback_name=str(item.get("provider") or "阵容来源")
            )
            teams = item.get("teams") if isinstance(item.get("teams"), dict) else {}
            for side in ("home", "away"):
                team = teams.get(side) if isinstance(teams.get(side), dict) else {}
                row = {
                    "id": f"offline-roster-{fixture_id}-{side}-{observed_at or 'unknown'}",
                    "fixtureId": fixture_id,
                    "teamId": team.get("provider_team_id"),
                    "teamName": team.get("name"),
                    "stage": "lineup_confirmation"
                    if item.get("confirmed") is True
                    else "roster_evidence",
                    "confirmed": item.get("confirmed") is True,
                    "formation": None,
                    "players": _compact_lineup_players(team.get("players")),
                    "observedAt": observed_at,
                    "sourceObservationId": None,
                    "modelEligible": False,
                    **source,
                }
                _append_fixture_evidence(target, fixture_id, "lineups", row)
                if isinstance(fixture_id, str):
                    _append_fixture_evidence(
                        target,
                        fixture_id,
                        "timeline",
                        _timeline_row(
                            fixture_id=fixture_id,
                            category="lineup",
                            kind=row["stage"],
                            source=source,
                            observed_at=observed_at,
                            kickoff_at=kickoff(fixture_id),
                        ),
                    )
    else:
        markets = {}

    # The dedicated ESPN roster endpoint is team-scoped rather than
    # fixture-scoped.  Attach it only to the exact upcoming fixture IDs that
    # caused the bounded poll, preserving the provider/team join and keeping
    # the result as roster evidence (never a confirmed XI).
    roster_container = snapshot.get("espn_rosters")
    if isinstance(roster_container, dict):
        espn_fixture_by_id = {
            item.get("id"): item
            for item in (
                snapshot.get("espn", {}).get("fixtures", [])
                if isinstance(snapshot.get("espn"), dict)
                else []
            )
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for roster in roster_container.get("rosters", []):
            if not isinstance(roster, dict):
                continue
            source = _source_details(
                roster.get("source"),
                fallback_name="ESPN team roster",
                fallback_tier="reliable_public_provider",
            )
            observed_at = _iso(roster.get("retrieved_at"))
            provider_team_id = roster.get("provider_team_id")
            for fixture_id in roster.get("scheduled_fixture_ids", []):
                fixture = espn_fixture_by_id.get(fixture_id)
                if not isinstance(fixture, dict) or not isinstance(provider_team_id, str):
                    continue
                side = next(
                    (
                        candidate
                        for candidate in ("home", "away")
                        if fixture.get(f"{candidate}_provider_team_id") == provider_team_id
                    ),
                    None,
                )
                if side is None:
                    continue
                row = {
                    "id": f"offline-espn-roster-{fixture_id}-{provider_team_id}-{observed_at or 'unknown'}",
                    "fixtureId": fixture_id,
                    "teamId": provider_team_id,
                    "teamName": roster.get("team_name"),
                    "teamSide": side,
                    "stage": "roster_evidence",
                    "confirmed": False,
                    "formation": None,
                    "players": _compact_lineup_players(roster.get("athletes")),
                    "observedAt": observed_at,
                    "sourceObservationId": None,
                    "modelEligible": False,
                    "modelExclusionReason": "team_roster_not_fixture_specific_or_confirmed_lineup",
                    **source,
                }
                _append_fixture_evidence(target, fixture_id, "lineups", row)
                _append_fixture_evidence(
                    target,
                    fixture_id,
                    "timeline",
                    _timeline_row(
                        fixture_id=fixture_id,
                        category="lineup",
                        kind="roster_evidence",
                        source=source,
                        observed_at=observed_at,
                        kickoff_at=kickoff(fixture_id),
                    ),
                )

    for item in markets.get("incidents", []):
        if not isinstance(item, dict):
            continue
        fixture_id = item.get("fixture_id")
        observed_at = _iso(item.get("retrieved_at"))
        source = _source_details(item.get("source"), fallback_name="ESPN 比赛事件")
        events = item.get("events") if isinstance(item.get("events"), list) else []
        for index, event in enumerate(events[:_OFFLINE_EVIDENCE_LIMIT]):
            if not isinstance(event, dict):
                continue
            participants = (
                event.get("participants") if isinstance(event.get("participants"), list) else []
            )
            participant_ids = _compact_event_participant_ids(
                event.get("participant_ids", event.get("participantIds"))
            )
            location = _compact_event_location(event.get("location"))
            score = _compact_event_score(event.get("score"))
            period = event.get("period")
            if (
                isinstance(period, bool)
                or not isinstance(period, (int, float))
                or not math.isfinite(float(period))
                or not 0 <= period <= 10
                or int(period) != period
            ):
                period = None
            else:
                period = int(period)
            scoring_play = event.get("scoring_play", event.get("scoringPlay"))
            if not isinstance(scoring_play, bool):
                scoring_play = None
            wallclock = event.get("wallclock")
            if (
                not isinstance(wallclock, str)
                or not wallclock.strip()
                or len(wallclock.strip()) > 80
            ):
                wallclock = None
            row = {
                "id": f"offline-event-{fixture_id}-{index}",
                "fixtureId": fixture_id,
                "minute": event.get("minute"),
                "occurredAt": None,
                "label": event.get("type"),
                "player": participants[0] if participants else None,
                "assist": participants[1] if len(participants) > 1 else None,
                "participantIds": participant_ids,
                "score": score,
                "detail": event.get("text"),
                "location": location,
                "period": period,
                "scoringPlay": scoring_play,
                "wallclock": wallclock,
                "sourceObservationId": None,
                "observedAt": observed_at,
                "entersModel": False,
                **source,
            }
            _append_fixture_evidence(target, fixture_id, "events", row)
            if isinstance(fixture_id, str):
                _append_fixture_evidence(
                    target,
                    fixture_id,
                    "timeline",
                    _timeline_row(
                        fixture_id=fixture_id,
                        category="intelligence",
                        kind=str(event.get("type") or "match_event"),
                        source=source,
                        observed_at=observed_at,
                        kickoff_at=kickoff(fixture_id),
                    ),
                )

    for item in markets.get("match_stats", []):
        if not isinstance(item, dict):
            continue
        fixture_id = item.get("fixture_id")
        observed_at = _iso(item.get("retrieved_at"))
        source = _source_details(item.get("source"), fallback_name="ESPN 比赛统计")
        row = {
            "id": f"offline-stats-{fixture_id}-{observed_at or 'unknown'}",
            "fixtureId": fixture_id,
            "observedAt": observed_at,
            "sourceObservationId": None,
            "teams": item.get("teams"),
            "players": item.get("players") if isinstance(item.get("players"), list) else [],
            "entersModel": False,
            **source,
        }
        _append_fixture_evidence(target, fixture_id, "matchStats", row)
        if isinstance(fixture_id, str):
            _append_fixture_evidence(
                target,
                fixture_id,
                "timeline",
                _timeline_row(
                    fixture_id=fixture_id,
                    category="intelligence",
                    kind="match_stats",
                    source=source,
                    observed_at=observed_at,
                    kickoff_at=kickoff(fixture_id),
                ),
            )

    # The official Sports Lottery sales ledger is a separate market lane. It
    # is attached to sales-only fixtures so a researcher can inspect the
    # published quote and its provenance without treating an unresolved
    # competition/entity as a canonical model market.
    current_data = snapshot.get("current_data")
    if isinstance(current_data, dict):
        for item in current_data.get("lottery_sales_schedule", []):
            if not isinstance(item, dict):
                continue
            fixture_id = _sales_only_fixture_id(item)
            if fixture_id is None or fixture_id not in fixture_by_id:
                continue
            observed_at = (
                _iso((item.get("source") or {}).get("retrieved_at"))
                if isinstance(item.get("source"), dict)
                else None
            )
            source = _source_details(
                item.get("source"), fallback_name="Sports Lottery", fallback_tier="official"
            )
            row = {
                "id": f"offline-sales-odds-{fixture_id}-{observed_at or 'unknown'}",
                "fixtureId": fixture_id,
                "marketType": "sports_lottery_sales",
                "marketRole": "official_purchase_reference",
                "officialPurchaseEligible": False,
                "marketEligibilityReason": "销售场次未完成规范赛事实体绑定，保留官方原始口径但不进入模型",
                "provider": "Sports Lottery",
                "line": item.get("hhad_line"),
                "odds": {
                    "had": item.get("had_odds"),
                    "hhad": item.get("hhad_odds"),
                    "ttg": item.get("ttg_odds"),
                    "hafu": item.get("hafu_odds"),
                    "crs": item.get("crs_odds"),
                },
                "impliedProbability": {
                    "had": item.get("had_probability"),
                    "hhad": item.get("hhad_probability"),
                    "ttg": item.get("ttg_probability"),
                    "hafu": item.get("hafu_probability"),
                    "crs": item.get("crs_probability"),
                },
                "linkStatus": item.get("link_status"),
                "linkReason": item.get("link_reason"),
                "observedAt": observed_at,
                "sourceObservationId": None,
                **source,
            }
            _append_fixture_evidence(target, fixture_id, "odds", row)
            _append_fixture_evidence(
                target,
                fixture_id,
                "timeline",
                _timeline_row(
                    fixture_id=fixture_id,
                    category="odds",
                    kind="sports_lottery_sales",
                    source=source,
                    observed_at=observed_at,
                    kickoff_at=kickoff(fixture_id),
                ),
            )

    # Structured public-page records are a separate evidence lane. They are
    # shown beside canonical data only after the exact fixture join has been
    # recorded by the Crawl4AI edge; they never become purchase odds or model
    # inputs in the compact package.
    crawl4ai = snapshot.get("crawl4ai")
    if isinstance(crawl4ai, dict):
        for page in crawl4ai.get("pages", []):
            if not isinstance(page, dict):
                continue
            joined_records = page.get("joined_fixture_records")
            if not isinstance(joined_records, list):
                continue
            observed_at = _iso(page.get("observed_at"))
            source = _source_details(
                {
                    "name": page.get("source") or "公开网页",
                    "source_id": page.get("source_id"),
                    "fact_source_id": page.get("fact_source_id"),
                    "source_role": page.get("source_role"),
                    "capture_role": page.get("capture_role"),
                    "capture_engine": page.get("capture_engine") or "Crawl4AI",
                    "parser_contract": page.get("parser_contract")
                    or (
                        page.get("extraction", {}).get("parser_contract")
                        if isinstance(page.get("extraction"), dict)
                        else None
                    ),
                    "crawl_stage": page.get("crawl_stage") or "index",
                    "parent_url": page.get("parent_url"),
                    "parent_content_sha256": page.get("parent_content_sha256"),
                    "follow_reason": page.get("follow_reason"),
                    "url": page.get("url"),
                    "final_url": page.get("final_url"),
                    "source_tier": page.get("source_tier") or "reliable_public_provider",
                    "raw_sha256": page.get("raw_sha256") or page.get("content_sha256"),
                    "content_sha256": page.get("content_sha256"),
                    "retrieved_at": observed_at,
                },
                fallback_name="公开网页",
                fallback_tier="reliable_public_provider",
            )
            for item in joined_records:
                if not isinstance(item, dict) or item.get("join_status") != "exact":
                    continue
                fixture_id = item.get("fixture_id")
                if not isinstance(fixture_id, str):
                    continue
                parser_contract = item.get("source_parser") or (
                    page.get("extraction", {}).get("parser")
                    if isinstance(page.get("extraction"), dict)
                    else None
                )
                if not isinstance(parser_contract, str) or not parser_contract:
                    parser_contract = "public_page_contract_unknown"
                if not isinstance(item.get("one_x_two_odds"), dict):
                    context = {
                        "id": f"offline-public-page-context-{fixture_id}-{parser_contract}-{observed_at or 'unknown'}",
                        "fixtureId": fixture_id,
                        "contextType": "public_fixture_context",
                        "provider": page.get("source") or "公开网页",
                        "parserContract": parser_contract,
                        "sourceMatchId": item.get("source_match_id"),
                        "sourceEventId": item.get("source_event_id"),
                        "matchUrl": item.get("match_url"),
                        "canonicalHomeTeam": item.get("canonical_home_team"),
                        "canonicalAwayTeam": item.get("canonical_away_team"),
                        "sourceKickoffAt": item.get("source_kickoff_at"),
                        "canonicalKickoffAt": item.get("canonical_kickoff_at"),
                        "kickoffDeltaSeconds": item.get("kickoff_delta_seconds"),
                        "associationStatus": "exact",
                        "associationConfidence": item.get("join_confidence"),
                        "observedAt": observed_at,
                        "modelEligible": False,
                        "modelExclusionReason": "public_page_fixture_context_requires_source_and_time_review",
                        "admission": _public_page_admission(item.get("admission")),
                        "sourceObservationId": None,
                        **source,
                    }
                    _append_fixture_evidence(target, fixture_id, "intelligence", context)
                    _append_fixture_evidence(
                        target,
                        fixture_id,
                        "timeline",
                        _timeline_row(
                            fixture_id=fixture_id,
                            category="intelligence",
                            kind="public_fixture_context",
                            source=source,
                            observed_at=observed_at,
                            kickoff_at=kickoff(fixture_id),
                        ),
                    )
                    continue
                row = {
                    "id": f"offline-public-page-odds-{fixture_id}-{item.get('source_match_id') or 'unknown'}-{observed_at or 'unknown'}",
                    "fixtureId": fixture_id,
                    "marketType": "three_way",
                    "marketRole": "public_browser_comparison",
                    "officialPurchaseEligible": False,
                    "marketEligibilityReason": "公开页面对照；来源许可未核验，不是体彩官方赔率",
                    "provider": page.get("source") or "公开网页",
                    "line": None,
                    "odds": item.get("one_x_two_odds"),
                    "impliedProbability": None,
                    "sourceMatchId": item.get("source_match_id"),
                    "canonicalHomeTeam": item.get("canonical_home_team"),
                    "canonicalAwayTeam": item.get("canonical_away_team"),
                    "sourceKickoffAt": item.get("source_kickoff_at"),
                    "canonicalKickoffAt": item.get("canonical_kickoff_at"),
                    "kickoffDeltaSeconds": item.get("kickoff_delta_seconds"),
                    "associationStatus": "exact",
                    "associationConfidence": item.get("join_confidence"),
                    "observedAt": observed_at,
                    "admission": _public_page_admission(item.get("admission")),
                    "sourceObservationId": None,
                    **source,
                }
                _append_fixture_evidence(target, fixture_id, "odds", row)
                _append_fixture_evidence(
                    target,
                    fixture_id,
                    "timeline",
                    _timeline_row(
                        fixture_id=fixture_id,
                        category="odds",
                        kind="public_page_market_comparison",
                        source=source,
                        observed_at=observed_at,
                        kickoff_at=kickoff(fixture_id),
                    ),
                )

    weather_sections = (
        snapshot.get("weather"),
        snapshot.get("met_norway_weather"),
    )
    for weather in weather_sections:
        if not isinstance(weather, dict):
            continue
        for item in weather.get("weather", []):
            if not isinstance(item, dict):
                continue
            fixture_id = item.get("fixture_id")
            source = _source_details(
                item.get("source"), fallback_name="天气来源", fallback_tier="authorized"
            )
            observed_at = _iso(source.get("retrievedAt"))
            row = {
                "id": f"offline-weather-{fixture_id}-{observed_at or 'unknown'}",
                "fixtureId": fixture_id,
                "kickoffAt": item.get("kickoff_at") or item.get("forecast_at"),
                "temperatureC": item.get("temperature_c"),
                "humidityPercent": item.get("humidity_percent"),
                "precipitationProbabilityPercent": item.get("precipitation_probability_percent"),
                "precipitationMm": item.get("precipitation_mm"),
                "windSpeedKmh": item.get("wind_speed_kmh"),
                "windSpeedMps": item.get("wind_speed_mps"),
                "windDirectionDeg": item.get("wind_direction_deg"),
                "symbolCode": item.get("symbol_code"),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
                "coordinateConfidence": item.get("coordinate_confidence"),
                "coordinateModelEligible": item.get("coordinate_model_eligible"),
                "observedAt": observed_at,
                "sourceObservationId": None,
                "entersModel": False,
                **source,
            }
            _append_fixture_evidence(target, fixture_id, "weather", row)
            if isinstance(fixture_id, str):
                _append_fixture_evidence(
                    target,
                    fixture_id,
                    "timeline",
                    _timeline_row(
                        fixture_id=fixture_id,
                        category="weather",
                        kind="weather",
                        source=source,
                        observed_at=observed_at,
                        kickoff_at=kickoff(fixture_id),
                    ),
                )

    # Official lineup adapters use slightly different payload shapes.  Keep a
    # narrow common projection and leave unparseable fields untouched in the
    # raw source archive rather than guessing team/player identities.
    for provider_key in (
        "premier_league_official",
        "laliga_official",
        "bundesliga_official",
        "serie_a_official",
        "ligue1_official",
    ):
        provider = snapshot.get(provider_key)
        if not isinstance(provider, dict):
            continue
        provider_name = str(provider.get("provider") or provider_key)
        for item in provider.get("lineups", []):
            if not isinstance(item, dict):
                continue
            fixture_id = item.get("fixture_id")
            payload = item.get("lineups") if isinstance(item.get("lineups"), dict) else {}
            fixture = (
                item.get("official_fixture")
                if isinstance(item.get("official_fixture"), dict)
                else {}
            )
            source = _source_details(
                item.get("source") or fixture.get("source"),
                fallback_name=provider_name,
                fallback_tier="official",
            )
            observed_at = _iso(source.get("retrievedAt")) or _iso(provider.get("retrieved_at"))
            lineup_stage = (
                "lineup_confirmation"
                if payload.get("confirmed") is True
                else "official_lineup_not_published"
            )
            for side in ("home", "away"):
                side_payload = payload.get(side) if isinstance(payload.get(side), dict) else {}
                row = {
                    "id": f"offline-official-lineup-{fixture_id}-{side}-{observed_at or 'unknown'}",
                    "fixtureId": fixture_id,
                    "teamId": side_payload.get("team_id") or side_payload.get("provider_team_id"),
                    "teamName": fixture.get(f"{side}_team"),
                    "stage": lineup_stage,
                    "confirmed": payload.get("confirmed") is True,
                    "formation": side_payload.get("formation"),
                    "players": _compact_lineup_players(side_payload.get("players")),
                    "observedAt": observed_at,
                    "sourceObservationId": None,
                    "modelEligible": False,
                    **source,
                }
                _append_fixture_evidence(target, fixture_id, "lineups", row)
            # One provider response is one observed timeline fact even though
            # the lineup table projects separate home/away rows.  Emitting a
            # timeline item inside the side loop creates duplicate identities
            # and falsely suggests two independent observations.
            if isinstance(fixture_id, str):
                _append_fixture_evidence(
                    target,
                    fixture_id,
                    "timeline",
                    _timeline_row(
                        fixture_id=fixture_id,
                        category="lineup",
                        kind=lineup_stage,
                        source=source,
                        observed_at=observed_at,
                        kickoff_at=kickoff(fixture_id),
                    ),
                )

    # RSS is a lead stream, not a fixture feed.  Resolve only exact team-pair
    # rows whose publication and observation timestamps are both before the
    # fixture kickoff.  Unbound articles stay in the global news projection;
    # they must never be copied into every match detail as if they were facts
    # about that match.
    resolved_news_rows = resolved_news
    if resolved_news_rows is None:
        resolved_news_rows, _news_diagnostics = _resolve_news_candidates(
            snapshot, selected_matches
        )
    if resolved_news_rows:
        for row in resolved_news_rows:
            if row.get("entity_type") != "fixture" or not isinstance(row.get("entity_id"), str):
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            resolution = (
                payload.get("match_resolution")
                if isinstance(payload.get("match_resolution"), dict)
                else {}
            )
            fixture_id = row["entity_id"]
            observed_at = _iso(row.get("observed_at"))
            effective_at = _iso(row.get("effective_at") or payload.get("published_at"))
            source = _source_details(
                {
                    "name": row.get("source_name"),
                    "url": row.get("source_url"),
                    "source_tier": row.get("source_tier"),
                    "raw_sha256": row.get("raw_hash"),
                    "retrieved_at": observed_at,
                },
                fallback_name="Public RSS",
                fallback_tier="reliable_media",
            )
            news_row = {
                "id": f"offline-news-{row.get('raw_hash') or 'unknown'}-{effective_at or 'unknown'}",
                "fixtureId": fixture_id,
                "title": payload.get("title"),
                "summary": payload.get("summary"),
                "link": payload.get("link"),
                "publishedAt": effective_at,
                "observedAt": observed_at,
                "associationStatus": resolution.get("method") or "exact_team_pair_pre_kickoff",
                "associationConfidence": resolution.get("confidence"),
                "sourceObservationId": None,
                "entersModel": False,
                "modelExclusionReason": "news_fact_not_structured_model_feature",
                **source,
            }
            _append_fixture_evidence(target, fixture_id, "news", news_row)
            _append_fixture_evidence(
                target,
                fixture_id,
                "timeline",
                _timeline_row(
                    fixture_id=fixture_id,
                    category="news",
                    kind="news_fact_candidate",
                    source=source,
                    observed_at=observed_at,
                    kickoff_at=kickoff(fixture_id),
                    effective_at=effective_at,
                ),
            )
    return {
        fixture_id: {category: rows for category, rows in evidence.items() if rows}
        for fixture_id, evidence in target.items()
        if fixture_id in fixture_by_id
    }


def _compact_site_snapshot(
    snapshot: dict,
    predictions: dict,
    *,
    research_predictions: dict | None = None,
    raw_sources: dict | None = None,
) -> dict:
    current_value = snapshot.get("current_data")
    current: dict[str, Any] = current_value if isinstance(current_value, dict) else {}
    strict_value = snapshot.get("strict_backtest")
    strict: dict[str, Any] = strict_value if isinstance(strict_value, dict) else {}
    prospective_value = snapshot.get("prospective_evaluation")
    prospective: dict[str, Any] = prospective_value if isinstance(prospective_value, dict) else {}
    evidence_source = raw_sources if isinstance(raw_sources, dict) else snapshot
    # ``raw_sources`` is the append-only live source ledger, while the sales
    # schedule is a derived projection added to ``snapshot.current_data``.
    # Keep both views available to the evidence compactor; otherwise a
    # sales-only fixture can be displayed but its official quote silently
    # disappears from the match-detail audit packet.
    if evidence_source is not snapshot:
        evidence_source = {**evidence_source, "current_data": current}
    compact_matches = _compact_matches(
        snapshot,
        predictions,
        research_predictions=research_predictions,
        raw_sources=evidence_source,
    )
    research_coverage = _research_prediction_coverage(snapshot, research_predictions)
    resolved_news, news_resolution = _resolve_news_candidates(evidence_source, compact_matches)
    research_status = (
        research_predictions.get("status")
        if isinstance(research_predictions, dict)
        else "unavailable"
    )
    research_message = (
        research_predictions.get("message")
        if isinstance(research_predictions, dict)
        and isinstance(research_predictions.get("message"), str)
        else None
    )
    if not research_message:
        research_message = (
            "研究基线按当前快照生成，未写入严格前瞻归档；不可作为生产预测或投注优势。"
        )
    research_blocked_rows = (
        research_predictions.get("blocked", [])
        if isinstance(research_predictions, dict)
        and isinstance(research_predictions.get("blocked"), list)
        else []
    )
    blocked_diagnostic_count = (
        research_predictions.get("blocked_total")
        if isinstance(research_predictions, dict)
        and isinstance(research_predictions.get("blocked_total"), int)
        else len(research_blocked_rows)
    )
    blocked_fixture_count = (
        research_coverage.get("blocked")
        if isinstance(research_coverage.get("blocked"), int)
        else 0
    )
    unavailable_fixture_count = (
        research_coverage.get("unavailable")
        if isinstance(research_coverage.get("unavailable"), int)
        else 0
    )
    model_scope_fixture_count = (
        research_coverage.get("model_scope_fixtures")
        if isinstance(research_coverage.get("model_scope_fixtures"), int)
        else 0
    )
    research_coverage_ratio = research_coverage.get("coverage_ratio")
    compact_current_data = {
        key: current.get(key)
        for key in (
            "status",
            "message",
            "as_of",
            "age_seconds",
            "roles",
            "provider_errors",
            "fixture_count",
            "canonical_fixture_provider",
            "canonical_fixture_count",
            "display_only_competitions",
            "display_only_fixture_count",
            "display_only_fixtures",
            "espn_fixture_count",
            "lottery_only_fixture_count",
            "sales_only_display_fixture_count",
            "sales_only_display_competition_count",
            "live_match_count",
            "recent_finished_match_count",
            "event_observation_count",
            "match_stats_observation_count",
            "xg_team_count",
            "market_count",
            "match_center",
            "official_fixture_join_diagnostics",
            "espn_team_status_count",
            "espn_confirmed_lineup_count",
            "espn_roster_team_count",
            "espn_roster_athlete_count",
            "espn_injury_report_count",
            "espn_injury_player_count",
            "espn_injury_complete_fixture_count",
            "espn_injury_partial_fixture_count",
            "openligadb_status",
            "openligadb_count",
            "sports_lottery_status",
            "sports_lottery_count",
            "openligadb_local_display_count",
            "openligadb_public_status",
            "model_admission",
            "rights_quarantine",
            "role_contract",
            "public_match_quarantine_count",
            "public_match_quarantine_sources",
            "oddstorm_status",
            "oddstorm_line_count",
            "news_status",
            "news_feed_count",
            "news_item_count",
            "weather_status",
            "weather_count",
            "geocoding_status",
            "geocoding_count",
            "sofascore_status",
            "sofascore_event_count",
            "crawl4ai_status",
            "crawl4ai_whoscored_joined_count",
            "premier_league_official_status",
            "premier_league_official_fixture_count",
            "premier_league_official_lineup_count",
            "laliga_official_status",
            "laliga_official_fixture_count",
            "laliga_official_lineup_count",
            "laliga_official_confirmed_lineup_count",
            "laliga_official_not_published_count",
            "bundesliga_official_status",
            "bundesliga_official_fixture_count",
            "bundesliga_official_lineup_count",
            "serie_a_official_status",
            "serie_a_official_fixture_count",
            "serie_a_official_lineup_count",
            "ligue1_official_status",
            "ligue1_official_fixture_count",
            "ligue1_official_lineup_count",
            "ligue1_official_confirmed_lineup_count",
            "ligue1_official_not_published_count",
            "fotmob_status",
            "fotmob_lineup_count",
            "source_registry",
            "lineup_poll_diagnostics",
            "lottery_sales_schedule",
            "intelligence_ledger",
        )
        if key in current and key != "source_registry"
    }
    # ``current.py`` intentionally does not carry the mutable provider health
    # registry into its model-facing read model.  The public bundle still
    # needs that operational ledger so researchers can see which sources were
    # blocked, stale, or never opened without receiving their factual rows.
    if not compact_current_data.get("source_registry") and isinstance(evidence_source, Mapping):
        source_registry = evidence_source.get("source_registry")
        if isinstance(source_registry, list):
            compact_current_data["source_registry"] = _compact_source_registry(
                {"source_registry": source_registry}
            )
    if not compact_current_data.get("official_fixture_join_diagnostics") and isinstance(
        evidence_source, Mapping
    ):
        raw_join = evidence_source.get("official_fixture_join_diagnostics")
        if isinstance(raw_join, Mapping):
            compact_current_data["official_fixture_join_diagnostics"] = {
                str(competition_id): {
                    key: diagnostic.get(key)
                    for key in (
                        "status",
                        "join_basis",
                        "canonical_candidate_count",
                        "matched_count",
                        "unmatched_count",
                        "ambiguous_count",
                    )
                    if key in diagnostic
                }
                for competition_id, diagnostic in raw_join.items()
                if isinstance(diagnostic, Mapping)
            }
    if not compact_current_data.get("official_fixture_join_diagnostics"):
        # A rights-blocked official lineup lane still needs an explicit join
        # diagnostic.  This is operational metadata only: zero candidates and
        # zero matches make it impossible to mistake a blocked pre-network
        # lane for an empty or complete lineup feed.
        registry_rows = compact_current_data.get("source_registry")
        registry_by_id = (
            {
                str(item.get("id")): item
                for item in registry_rows
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            }
            if isinstance(registry_rows, list)
            else {}
        )
        fallback_join: dict[str, dict[str, object]] = {}
        for competition_id, source_id in _OFFICIAL_MATCH_FEATURE_POLICIES.items():
            source = registry_by_id.get(source_id) or registry_by_id.get(
                SourceId.OFFICIAL_LEAGUE_LINEUPS.value
            )
            runtime = source.get("runtime") if isinstance(source, Mapping) else None
            status = runtime.get("status") if isinstance(runtime, Mapping) else None
            if status in {"rights_blocked", "unavailable", "not_configured", "error"}:
                fallback_join[competition_id] = {
                    "status": "unavailable",
                    "join_basis": "strict_kickoff_and_normalized_team_pair",
                    "canonical_candidate_count": 0,
                    "matched_count": 0,
                }
        if fallback_join:
            compact_current_data["official_fixture_join_diagnostics"] = fallback_join
    raw_lineup_diagnostics = (
        evidence_source.get("lineup_poll_diagnostics")
        if isinstance(evidence_source, Mapping)
        else None
    )
    if isinstance(raw_lineup_diagnostics, Mapping):
        compact_current_data["lineup_poll_diagnostics"] = _compact_lineup_poll_diagnostics(
            raw_lineup_diagnostics,
            sources=evidence_source,
        )
    elif isinstance(compact_current_data.get("lineup_poll_diagnostics"), Mapping):
        compact_current_data["lineup_poll_diagnostics"] = _compact_lineup_poll_diagnostics(
            compact_current_data["lineup_poll_diagnostics"],
            sources=evidence_source,
        )
    if isinstance(evidence_source, Mapping):
        lottery = evidence_source.get("sports_lottery")
        if isinstance(lottery, Mapping):
            compact_current_data.setdefault(
                "sports_lottery_status",
                lottery.get("status") if isinstance(lottery.get("status"), str) else "unknown",
            )
            # No Sports Lottery row is redistributed until its explicit
            # commercial/publication contract is verified.  Keep an empty
            # ledger so the UI can distinguish blocked from missing.
            compact_current_data.setdefault("lottery_sales_schedule", [])
    return {
        "schema_version": "matchline.offline_snapshot.v1",
        "build_id": snapshot.get("build_id"),
        "generated_at": snapshot.get("generated_at"),
        "as_of": current.get("as_of") or snapshot.get("generated_at"),
        # This is operational provenance, not a production-readiness flag.
        # A tmpfs mirror is useful for recovery from a read-only external disk,
        # but its append-only updates disappear on reboot and must be shown as
        # such to researchers.
        "runtime_persistence": os.environ.get("MATCHLINE_RUNTIME_PERSISTENCE", "unknown"),
        "timezone": snapshot.get("timezone"),
        "source": "offline_snapshot",
        "live": False,
        "production_ready": False,
        "summary": snapshot.get("summary", {}),
        "competitions": snapshot.get("competitions", []),
        "matches": compact_matches,
        "fixture_evidence": _compact_fixture_evidence(
            evidence_source, compact_matches, resolved_news=resolved_news
        ),
        "predictions": [
            _compact_prediction(item)
            for item in predictions.get("predictions", [])
            if isinstance(item, dict)
        ]
        if isinstance(predictions, dict)
        else [],
        "blocked": predictions.get("blocked", []) if isinstance(predictions, dict) else [],
        "research_predictions": [
            _compact_prediction(item)
            for item in (
                research_predictions.get("predictions", [])
                if isinstance(research_predictions, dict)
                else []
            )
            if isinstance(item, dict)
        ],
        "research_prediction_summary": {
            "status": research_status,
            "as_of": research_predictions.get("as_of")
            if isinstance(research_predictions, dict)
            else None,
            "reference_at": research_predictions.get("reference_at")
            if isinstance(research_predictions, dict)
            else None,
            "stale_snapshot": research_status == "stale_snapshot",
            "prediction_count": len(research_predictions.get("predictions", []))
            if isinstance(research_predictions, dict)
            and isinstance(research_predictions.get("predictions"), list)
            else 0,
            # ``blocked_count`` is retained for compatibility, but its
            # semantics are now explicit: it counts diagnostic records, not
            # distinct fixtures in the upcoming catalogue.
            "blocked_count": blocked_diagnostic_count,
            "blocked_count_semantics": "diagnostic_records_not_fixture_count",
            "blocked_diagnostic_count": blocked_diagnostic_count,
            "blocked_diagnostic_sample_count": len(research_blocked_rows),
            "available_fixture_count": (
                research_coverage.get("available")
                if isinstance(research_coverage.get("available"), int)
                else 0
            ),
            "blocked_fixture_count": blocked_fixture_count,
            "unavailable_fixture_count": unavailable_fixture_count,
            "model_scope_fixture_count": model_scope_fixture_count,
            "coverage_ratio": research_coverage_ratio,
            "model_boundary": "research_only_unfrozen_display",
            "production_allowed": False,
            "message": research_message,
        },
        "research_prediction_coverage": research_coverage,
        "current_data": compact_current_data,
        "news": {
            "provider": snapshot.get("news", {}).get("provider")
            if isinstance(snapshot.get("news"), dict)
            else None,
            "retrieved_at": snapshot.get("news", {}).get("retrieved_at")
            if isinstance(snapshot.get("news"), dict)
            else None,
            "items": _compact_news(snapshot.get("news", {}), resolved_rows=resolved_news),
        },
        "news_resolution": news_resolution,
        "strict_backtest": strict,
        "prospective_evaluation": prospective,
        "publication_audit": _compact_publication_audit(snapshot.get("publication_audit")),
        "live_overlay": snapshot.get("live_overlay")
        if isinstance(snapshot.get("live_overlay"), dict)
        else None,
        "runtime_archive": snapshot.get("runtime_archive")
        if isinstance(snapshot.get("runtime_archive"), dict)
        else None,
    }


_REDISTRIBUTION = UseCase.REDISTRIBUTION
_POLICY_SOURCE_IDS = frozenset(item.value for item in SourceId)
_FOTMOB_POLICY_ID = "fotmob_public_api"

_SECTION_POLICIES: dict[str, str] = {
    "openfootball_current": SourceId.OPENFOOTBALL_CURRENT.value,
    "cfl_official": SourceId.CFL_OFFICIAL_CURRENT.value,
    "espn": SourceId.ESPN_SCHEDULE_SUMMARY.value,
    "espn_markets": SourceId.ESPN_MARKET_SUMMARY.value,
    "espn_rosters": SourceId.ESPN_TEAM_ROSTERS.value,
    "espn_injuries": SourceId.ESPN_INJURY_REPORTS.value,
    "understat": SourceId.UNDERSTAT_XG.value,
    "news": SourceId.PUBLIC_RSS_NEWS.value,
    "geocoding": SourceId.OPEN_METEO_GEOCODING.value,
    "weather": SourceId.OPEN_METEO_WEATHER.value,
    "met_norway_weather": SourceId.MET_NORWAY_WEATHER.value,
    "wikidata_entities": SourceId.WIKIDATA_ENTITIES.value,
    "sofascore": SourceId.SOFASCORE_PREMATCH.value,
    "fotmob": _FOTMOB_POLICY_ID,
    "sports_lottery": SourceId.SPORTS_LOTTERY_OFFICIAL.value,
    "oddstorm": SourceId.ODDSTORM_MARKET_COMPARISON.value,
    "openligadb": SourceId.OPENLIGADB_SECONDARY_RESULTS.value,
    "premier_league_official": SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS.value,
    "laliga_official": SourceId.OFFICIAL_LALIGA_LINEUPS.value,
    "bundesliga_official": SourceId.OFFICIAL_BUNDESLIGA_LINEUPS.value,
    "serie_a_official": SourceId.OFFICIAL_SERIE_A_LINEUPS.value,
    "ligue1_official": SourceId.OFFICIAL_LIGUE1_LINEUPS.value,
}

_SECTION_FACT_KEYS: dict[str, tuple[str, ...]] = {
    "openfootball_current": (
        "fixtures",
        "date_only_fixtures",
        "recent_results",
        "upcoming_3_days",
        "upcoming_7_days",
    ),
    "cfl_official": (
        "fixtures",
        "date_only_fixtures",
        "recent_results",
        "upcoming_3_days",
        "upcoming_7_days",
    ),
    "espn": ("fixtures",),
    "espn_markets": (
        "markets",
        "team_status",
        "fixture_updates",
        "incidents",
        "match_stats",
    ),
    "espn_rosters": ("rosters",),
    "espn_injuries": ("competition_reports", "reports", "fallbacks"),
    "understat": ("team_features",),
    "news": ("feeds",),
    "geocoding": ("geocodes",),
    "weather": ("weather",),
    "met_norway_weather": ("weather",),
    "wikidata_entities": ("venues",),
    "sofascore": ("events",),
    "fotmob": ("lineups",),
    "sports_lottery": ("matches",),
    "oddstorm": ("lines",),
    "openligadb": ("matches",),
    "premier_league_official": ("fixtures", "lineups"),
    "laliga_official": ("fixtures", "lineups"),
    "bundesliga_official": ("fixtures", "lineups"),
    "serie_a_official": ("fixtures", "lineups"),
    "ligue1_official": ("fixtures", "lineups"),
}

_FIXTURE_SECTION_FACT_KEYS = (
    "fixtures",
    "date_only_fixtures",
    "recent_results",
    "upcoming_3_days",
    "upcoming_7_days",
)

_PROVIDER_POLICY_IDS: dict[str, str] = {
    "OpenFootball": SourceId.OPENFOOTBALL_CURRENT.value,
    "football-data.co.uk": SourceId.FOOTBALL_DATA_HISTORICAL.value,
    "CFL official": SourceId.CFL_OFFICIAL_CURRENT.value,
    "ESPN": SourceId.ESPN_SCHEDULE_SUMMARY.value,
    "ESPN event summary": SourceId.ESPN_MARKET_SUMMARY.value,
    "ESPN team roster": SourceId.ESPN_TEAM_ROSTERS.value,
    "ESPN injury report": SourceId.ESPN_INJURY_REPORTS.value,
    "Understat": SourceId.UNDERSTAT_XG.value,
    "Public RSS": SourceId.PUBLIC_RSS_NEWS.value,
    "RSS": SourceId.PUBLIC_RSS_NEWS.value,
    "Open-Meteo": SourceId.OPEN_METEO_WEATHER.value,
    "MET Norway Locationforecast": SourceId.MET_NORWAY_WEATHER.value,
    "Wikidata structured data": SourceId.WIKIDATA_ENTITIES.value,
    "Open-Meteo Geocoding": SourceId.OPEN_METEO_GEOCODING.value,
    "SofaScore": SourceId.SOFASCORE_PREMATCH.value,
    "FotMob": _FOTMOB_POLICY_ID,
    "Sports Lottery": SourceId.SPORTS_LOTTERY_OFFICIAL.value,
    "OddStorm public bookmaker comparison": SourceId.ODDSTORM_MARKET_COMPARISON.value,
    "OpenLigaDB": SourceId.OPENLIGADB_SECONDARY_RESULTS.value,
    "Premier League official": SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS.value,
    "LaLiga official": SourceId.OFFICIAL_LALIGA_LINEUPS.value,
    "Bundesliga official": SourceId.OFFICIAL_BUNDESLIGA_LINEUPS.value,
    "Serie A official": SourceId.OFFICIAL_SERIE_A_LINEUPS.value,
    "Ligue 1 official": SourceId.OFFICIAL_LIGUE1_LINEUPS.value,
    "WhoScored": SourceId.WHOSCORED_PUBLIC_PAGES.value,
}

_PROVIDER_FIXTURE_ID_POLICY_IDS: dict[str, str] = {
    "OpenFootball": SourceId.OPENFOOTBALL_CURRENT.value,
    "ESPN": SourceId.ESPN_SCHEDULE_SUMMARY.value,
    "CFL": SourceId.CFL_OFFICIAL_CURRENT.value,
    "cfl_official": SourceId.CFL_OFFICIAL_CURRENT.value,
    "OpenLigaDB": SourceId.OPENLIGADB_SECONDARY_RESULTS.value,
    "openligadb": SourceId.OPENLIGADB_SECONDARY_RESULTS.value,
    "premier_league_official": SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS.value,
    "laliga_official": SourceId.OFFICIAL_LALIGA_LINEUPS.value,
    "bundesliga_official": SourceId.OFFICIAL_BUNDESLIGA_LINEUPS.value,
    "serie_a_official": SourceId.OFFICIAL_SERIE_A_LINEUPS.value,
    "ligue1_official": SourceId.OFFICIAL_LIGUE1_LINEUPS.value,
}

_OPENFOOTBALL_KICKOFF_SOURCE_LABELS = frozenset({"explicit", "group_inherited", "missing"})

_REGISTRY_POLICY_IDS: dict[str, str] = {
    "open_meteo": SourceId.OPEN_METEO_WEATHER.value,
    "official_league_lineups": SourceId.OFFICIAL_LEAGUE_LINEUPS.value,
    "laliga_official_match_directory": SourceId.OFFICIAL_LALIGA_LINEUPS.value,
    "fotmob_public_api": _FOTMOB_POLICY_ID,
}

_SOURCE_METADATA_LIST_KEYS = frozenset(
    {
        "errors",
        "warnings",
        "provider_status",
        "blocked_paths",
        "checked",
        "failed_routes",
    }
)
_SOURCE_METADATA_MAPPING_KEYS = frozenset(
    {
        "provider",
        "retrieved_at",
        "checked_at",
        "status",
        "network_opened",
        "policy",
        "diagnostics",
        "source_contract",
        "training_admitted",
        "serve_current_rights",
        "raw_archive_admission",
    }
)

_DIAGNOSTIC_FACT_KEYS = frozenset(
    {
        "fixture_id",
        "fixture_ids",
        "canonical_fixture_id",
        "canonical_fixture_ids",
        "official_fixture_id",
        "official_fixture_ids",
        "match_id",
        "event_id",
        "provider_fixture_id",
        "native_fixture_id",
        "source_match_id",
        "kickoff_at",
        "home_team",
        "away_team",
        "score",
        "odds",
        "players",
        "lineup",
    }
)
_FACT_ROW_MARKER_KEYS = _DIAGNOSTIC_FACT_KEYS | frozenset(
    {
        "events",
        "stats",
        "probability",
        "temperature_c",
        "injuries",
        "teams",
        "title",
        "published_at",
    }
)
_MAX_DIAGNOSTIC_ITEMS = 64
_MAX_DIAGNOSTIC_NODES = 512
_MAX_DIAGNOSTIC_TEXT = 1000
# Display-only competition rows are still bounded metadata, but a current
# multi-league refresh can legitimately contain more than the diagnostic
# list cap above (for example, a full secondary league season).
_MAX_DISPLAY_ONLY_FIXTURES = 2000

_SNAPSHOT_NON_SOURCE_KEYS = frozenset(
    {
        "schema_version",
        "generated_at",
        "timezone",
        "summary",
        "competitions",
        "matches",
        "current_data",
        "strict_backtest",
        "prospective_evaluation",
        "publication_audit",
        "runtime_archive",
        "live_overlay",
        "build_id",
    }
)

_RAW_SOURCE_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "as_of",
        "expected_competitions",
        "roles",
        "source_registry",
        "lineup_poll_diagnostics",
        "official_fixture_join_diagnostics",
        "raw_archive_admission",
    }
)

_RAW_ARCHIVE_ADMISSION_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "policy_version",
        "observed_before",
        "source_ids",
        "row_count",
        "selected_records",
        "admission_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
        "rows_sha256",
    }
)
_RAW_ARCHIVE_RECORD_KEYS = frozenset({"source_id", "retrieved_at", "record_sha256", "raw_sha256"})

_MATCH_FEATURE_POLICIES: dict[str, str] = {
    "home": SourceId.UNDERSTAT_XG.value,
    "away": SourceId.UNDERSTAT_XG.value,
    "market": SourceId.ESPN_MARKET_SUMMARY.value,
    "team_status": SourceId.ESPN_MARKET_SUMMARY.value,
    "weather": SourceId.OPEN_METEO_WEATHER.value,
    "sofascore": SourceId.SOFASCORE_PREMATCH.value,
    "espn_injuries": SourceId.ESPN_INJURY_REPORTS.value,
    "fotmob_lineup": _FOTMOB_POLICY_ID,
    "lottery_market": SourceId.SPORTS_LOTTERY_OFFICIAL.value,
    "whoscored_market": SourceId.WHOSCORED_PUBLIC_PAGES.value,
}

_OFFICIAL_MATCH_FEATURE_POLICIES: dict[str, str] = {
    "premier-league": SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS.value,
    "la-liga": SourceId.OFFICIAL_LALIGA_LINEUPS.value,
    "bundesliga": SourceId.OFFICIAL_BUNDESLIGA_LINEUPS.value,
    "serie-a": SourceId.OFFICIAL_SERIE_A_LINEUPS.value,
    "ligue-1": SourceId.OFFICIAL_LIGUE1_LINEUPS.value,
}

_MATCH_SECONDARY_FACT_KEYS = frozenset(
    {
        "american_odds",
        "events",
        "incidents",
        "injuries",
        "lineup",
        "lineups",
        "market_probability",
        "match_stats",
        "odds",
        "one_x_two_odds",
        "prediction",
        "research_prediction",
        "roster",
        "rosters",
        "sales_market",
        "stats",
        "team_status",
        "weather",
    }
)

_OPENFOOTBALL_UNOWNED_IDENTITY_KEYS = frozenset(
    {
        "away_provider_team_id",
        "effective_at",
        "home_provider_team_id",
        "kickoff_time_observed_at",
        "native_fixture_id",
        "observed_at",
        "provider_competition_id",
        "provider_team_ids",
        "time_semantics",
    }
)

_LIVE_OVERLAY_KEYS = frozenset(
    {
        "as_of",
        "checked_at",
        "errors",
        "fixture_count",
        "fixtures",
        "freshness_budget_seconds",
        "policy",
        "retrieved_at",
        "schema_version",
        "source",
        "status",
        "warnings",
    }
)


def _source_policy_id(source: Mapping[str, Any]) -> str | None:
    """Resolve a stable policy identity without trusting payload allow flags."""

    name = source.get("name") or source.get("provider")
    if isinstance(name, str) and name.strip() in _PROVIDER_POLICY_IDS:
        return _PROVIDER_POLICY_IDS[name.strip()]
    for key in ("policy_source_id", "fact_source_id", "source_id"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _row_source(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    source = row.get("source")
    if isinstance(source, Mapping):
        return source
    direct_source_id = row.get("fact_source_id") or row.get("source_id")
    if isinstance(source, str) and source.strip():
        return {
            "name": source.strip(),
            "source_id": direct_source_id,
        }
    if isinstance(direct_source_id, str) and direct_source_id.strip():
        return {"source_id": direct_source_id.strip()}
    return None


def _raise_rights_block(source_id: str) -> None:
    if source_id in {
        SourceId.OPENFOOTBALL_CURRENT.value,
        SourceId.OPENFOOTBALL_HISTORICAL.value,
    }:
        # The policy-level CC0 decision is necessary but not sufficient here:
        # bundle rows must also pass the exact registered current-fixture
        # identity contract in ``_require_row_policy``.  No equivalent trusted
        # offline identity path exists for a historical or nested fact row.
        raise ValueError("openfootball_source_contract_unverified")
    if source_id not in _POLICY_SOURCE_IDS:
        raise ValueError(f"source_rights_unknown_provider:{source_id}")
    decision = decide_source_rights(source_id, _REDISTRIBUTION)
    if decision.is_allowed:
        return
    raise ValueError(f"source_rights_blocked:{decision.source_id}:{_REDISTRIBUTION.value}")


def _require_row_policy(
    row: Mapping[str, Any],
    *,
    path: str,
    allow_trusted_openfootball_fixture: bool = False,
) -> str:
    source = _row_source(row)
    if source is None:
        raise ValueError(f"offline_bundle_source_provenance_missing:{path}")
    source_id = _source_policy_id(source)
    if source_id is None:
        raise ValueError(f"offline_bundle_source_provenance_missing:{path}")
    if source_id == SourceId.OPENFOOTBALL_CURRENT.value:
        if not allow_trusted_openfootball_fixture:
            raise ValueError("openfootball_source_contract_unverified")
        decision = fixture_commercial_reuse_status(row, use_case=_REDISTRIBUTION)
        if decision.get("status") != "ok":
            raise ValueError(
                str(decision.get("reason") or "openfootball_source_contract_unverified")
            )
        return source_id
    _raise_rights_block(source_id)
    return source_id


def _nonempty(value: Any) -> bool:
    return value is not None and value is not False and value != "" and value != [] and value != {}


def _require_nested_match_sources(row: Mapping[str, Any], *, path: str) -> None:
    """Reject secondary facts hidden inside an otherwise trusted fixture row."""

    root_source = _row_source(row)
    if root_source is None:
        raise ValueError(f"offline_bundle_source_provenance_missing:{path}")

    for key in _OPENFOOTBALL_UNOWNED_IDENTITY_KEYS:
        if _nonempty(row.get(key)):
            raise ValueError(f"offline_bundle_source_provenance_missing:{path}.{key}")

    for key in _MATCH_SECONDARY_FACT_KEYS:
        value = row.get(key)
        if not _nonempty(value):
            continue
        fact_path = f"{path}.{key}"
        if isinstance(value, Mapping):
            _require_row_policy(value, path=fact_path)
            continue
        if isinstance(value, list):
            for index, fact_row in enumerate(value):
                row_path = f"{fact_path}[{index}]"
                if not isinstance(fact_row, Mapping):
                    raise ValueError(f"offline_bundle_source_row_malformed:{row_path}")
                _require_row_policy(fact_row, path=row_path)
            continue
        raise ValueError(f"offline_bundle_source_provenance_missing:{fact_path}")

    provider_fixture_ids = row.get("provider_fixture_ids")
    if provider_fixture_ids is not None:
        if not isinstance(provider_fixture_ids, Mapping):
            raise ValueError(
                f"offline_bundle_source_provenance_malformed:{path}.provider_fixture_ids"
            )
        for provider, provider_fixture_id in provider_fixture_ids.items():
            if not _nonempty(provider_fixture_id):
                continue
            if provider == "OpenFootball":
                if provider_fixture_id != row.get("id"):
                    raise ValueError("openfootball_source_contract_unverified")
                continue
            source_id = _PROVIDER_FIXTURE_ID_POLICY_IDS.get(str(provider))
            if source_id is None:
                raise ValueError(f"source_rights_unknown_provider:{provider}")
            _raise_rights_block(source_id)

    kickoff_time_source = row.get("kickoff_time_source")
    if (
        isinstance(kickoff_time_source, str)
        and kickoff_time_source
        and kickoff_time_source not in _OPENFOOTBALL_KICKOFF_SOURCE_LABELS
    ):
        source_id = _PROVIDER_POLICY_IDS.get(kickoff_time_source)
        if source_id is None:
            raise ValueError(
                f"offline_bundle_source_provenance_missing:{path}.kickoff_time_source"
            )
        _raise_rights_block(source_id)

    field_sources = row.get("field_sources")
    if field_sources is not None:
        if not isinstance(field_sources, Mapping):
            raise ValueError(f"offline_bundle_source_provenance_malformed:{path}.field_sources")
        for field_name, source in field_sources.items():
            if not isinstance(source, Mapping):
                raise ValueError(
                    f"offline_bundle_source_provenance_malformed:{path}.field_sources.{field_name}"
                )
            source_id = _source_policy_id(source)
            if source_id is None:
                raise ValueError(
                    f"offline_bundle_source_provenance_missing:{path}.field_sources.{field_name}"
                )
            if source_id == SourceId.OPENFOOTBALL_CURRENT.value:
                if any(
                    source.get(key) != root_source.get(key)
                    for key in ("name", "source_id", "url", "license")
                ):
                    raise ValueError("openfootball_source_contract_unverified")
            else:
                _raise_rights_block(source_id)

    overlay = row.get("schedule_overlay")
    if _nonempty(overlay):
        if not isinstance(overlay, Mapping):
            raise ValueError(f"offline_bundle_source_provenance_malformed:{path}.schedule_overlay")
        provider = overlay.get("provider")
        source_id = _PROVIDER_POLICY_IDS.get(provider) if isinstance(provider, str) else None
        if source_id is None:
            raise ValueError(f"offline_bundle_source_provenance_missing:{path}.schedule_overlay")
        _raise_rights_block(source_id)

    venue = row.get("venue")
    if _nonempty(venue):
        if not isinstance(venue, Mapping):
            raise ValueError(f"offline_bundle_source_provenance_malformed:{path}.venue")
        venue_source = venue.get("source")
        if not isinstance(venue_source, Mapping):
            raise ValueError(f"offline_bundle_source_provenance_missing:{path}.venue.source")
        venue_source_id = _source_policy_id(venue_source)
        if venue_source_id is None:
            raise ValueError(f"offline_bundle_source_provenance_missing:{path}.venue.source")
        if venue_source_id == SourceId.OPENFOOTBALL_CURRENT.value:
            if any(
                venue_source.get(field) != root_source.get(field)
                for field in ("name", "source_id", "url", "license")
            ):
                raise ValueError("openfootball_source_contract_unverified")
        else:
            _raise_rights_block(venue_source_id)

    current_features = row.get("current_features")
    if current_features is not None:
        if not isinstance(current_features, Mapping):
            raise ValueError(f"offline_bundle_source_provenance_malformed:{path}.current_features")
        for feature_name, value in current_features.items():
            if not _nonempty(value) or feature_name in {
                "lottery_match_conflict",
                "lottery_match_confidence",
                "fixture_origin",
            }:
                continue
            source_id = _MATCH_FEATURE_POLICIES.get(feature_name)
            if feature_name == "official_lineup":
                source_id = _OFFICIAL_MATCH_FEATURE_POLICIES.get(
                    str(row.get("competition_id") or "")
                )
            if source_id is not None:
                _raise_rights_block(source_id)
            if not isinstance(value, Mapping):
                raise ValueError(f"offline_bundle_unknown_current_feature:{path}.{feature_name}")
            _require_row_policy(value, path=f"{path}.current_features.{feature_name}")

    def inspect_nested(value: Any, nested_path: str, *, root: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{nested_path}.{key}"
                if key == "source" and not root:
                    if not isinstance(child, Mapping):
                        raise ValueError(
                            f"offline_bundle_source_provenance_malformed:{child_path}"
                        )
                    source_id = _source_policy_id(child)
                    if source_id is None:
                        raise ValueError(f"offline_bundle_source_provenance_missing:{child_path}")
                    if source_id == SourceId.OPENFOOTBALL_CURRENT.value:
                        if any(
                            child.get(field) != root_source.get(field)
                            for field in ("name", "source_id", "url", "license")
                        ):
                            raise ValueError("openfootball_source_contract_unverified")
                    else:
                        _raise_rights_block(source_id)
                    continue
                if key in {"field_sources", "current_features", "schedule_overlay"}:
                    continue
                inspect_nested(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect_nested(child, f"{nested_path}[{index}]")

    inspect_nested(row, path, root=True)


def _require_match_rows(snapshot: Mapping[str, Any]) -> None:
    matches = snapshot.get("matches")
    if matches is None:
        return
    if not isinstance(matches, list):
        raise ValueError("offline_bundle_source_rows_malformed:snapshot.matches")
    for index, row in enumerate(matches):
        path = f"snapshot.matches[{index}]"
        if not isinstance(row, Mapping):
            raise ValueError(f"offline_bundle_source_row_malformed:{path}")
        source_id = _require_row_policy(
            row,
            path=path,
            allow_trusted_openfootball_fixture=True,
        )
        if source_id == SourceId.OPENFOOTBALL_CURRENT.value:
            _require_nested_match_sources(row, path=path)


def _section_fact_rows(
    section: Mapping[str, Any],
    *,
    section_key: str,
    path: str,
) -> list[Mapping[str, Any]]:
    fact_keys = _SECTION_FACT_KEYS[section_key]
    rows: list[Mapping[str, Any]] = []
    for key in fact_keys:
        value = section.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise ValueError(f"offline_bundle_source_rows_malformed:{path}.{key}")
        for index, row in enumerate(value):
            if not isinstance(row, Mapping):
                raise ValueError(f"offline_bundle_source_row_malformed:{path}.{key}[{index}]")
            rows.append(row)
    for key, value in section.items():
        if (
            key in fact_keys
            or key in _SOURCE_METADATA_LIST_KEYS
            or key in _SOURCE_METADATA_MAPPING_KEYS
        ):
            continue
        if isinstance(value, list) and value:
            if section_key == "openfootball_current":
                raise ValueError(f"offline_bundle_unknown_source_rows:{path}.{key}")
            # A newly added list on a blocked provider envelope is factual until
            # explicitly classified as bounded diagnostics.
            rows.append({"source": {"source_id": _SECTION_POLICIES[section_key]}})
        elif isinstance(value, Mapping) and _row_collections(
            value,
            path=f"{path}.{key}",
            depth=1,
        ):
            if section_key == "openfootball_current":
                raise ValueError(f"offline_bundle_unknown_source_rows:{path}.{key}")
            rows.append({"source": {"source_id": _SECTION_POLICIES[section_key]}})
    return rows


def _network_opened(value: Mapping[str, Any]) -> Any:
    if "network_opened" in value:
        return value.get("network_opened")
    for key in ("policy", "runtime"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and "network_opened" in nested:
            return nested.get("network_opened")
    return None


def _has_source_activity(value: Mapping[str, Any]) -> bool:
    if any(key in value for key in ("status", "retrieved_at", "checked_at", "rights_status")):
        return True
    errors = value.get("errors")
    return isinstance(errors, list) and bool(errors)


def _require_bounded_diagnostic(value: Any, *, path: str) -> None:
    nodes = 0

    def inspect(item: Any, item_path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_DIAGNOSTIC_NODES or depth > 8:
            raise ValueError(f"offline_bundle_diagnostic_unbounded:{path}")
        if isinstance(item, str):
            if len(item) > _MAX_DIAGNOSTIC_TEXT:
                raise ValueError(f"offline_bundle_diagnostic_unbounded:{item_path}")
            return
        if isinstance(item, list):
            if len(item) > _MAX_DIAGNOSTIC_ITEMS:
                raise ValueError(f"offline_bundle_diagnostic_unbounded:{item_path}")
            for index, child in enumerate(item):
                inspect(child, f"{item_path}[{index}]", depth + 1)
            return
        if isinstance(item, Mapping):
            if len(item) > _MAX_DIAGNOSTIC_ITEMS:
                raise ValueError(f"offline_bundle_diagnostic_unbounded:{item_path}")
            for key, child in item.items():
                child_path = f"{item_path}.{key}"
                if key in _DIAGNOSTIC_FACT_KEYS and _nonempty(child):
                    raise ValueError(
                        f"offline_bundle_diagnostic_contains_factual_row:{child_path}"
                    )
                inspect(child, child_path, depth + 1)

    inspect(value, path, 0)


def _require_source_diagnostics(section: Mapping[str, Any], *, path: str) -> None:
    for key in _SOURCE_METADATA_LIST_KEYS:
        if key in section:
            value = section.get(key)
            if not isinstance(value, list):
                raise ValueError(f"offline_bundle_source_rows_malformed:{path}.{key}")
            _require_bounded_diagnostic(value, path=f"{path}.{key}")


def _require_sha256_digest(value: Any, *, path: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
        or value == "0" * 64
    ):
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}")


def _require_admission_timestamp(value: Any, *, path: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}")
    return parsed.astimezone(timezone.utc)


def _require_raw_archive_admission(value: Any, *, path: str) -> None:
    """Validate the bounded admission envelope before any replay is attempted.

    The envelope is a claim carried by the mutable live snapshot.  It is not
    accepted as provenance by itself; ``_require_openfootball_raw_replay``
    below must still rebuild it from the durable archive.  Keeping this
    structural check separate prevents a malformed or oversized claim from
    reaching the replay boundary.
    """

    if not isinstance(value, Mapping) or set(value) != _RAW_ARCHIVE_ADMISSION_KEYS:
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}")
    if value.get("schema_version") != "matchline.openfootball_raw_admission.v1":
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}.schema_version")
    if value.get("status") != "verified_current_raw":
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}.status")
    if value.get("policy_version") != POLICY_VERSION:
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}.policy_version")
    observed_before = _require_admission_timestamp(
        value.get("observed_before"),
        path=f"{path}.observed_before",
    )
    source_ids = value.get("source_ids")
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or len(source_ids) > 128
        or source_ids != sorted(source_ids)
        or len(set(source_ids)) != len(source_ids)
        or any(
            not isinstance(source_id, str)
            or not source_id.startswith("openfootball:")
            or len(source_id) > 200
            for source_id in source_ids
        )
    ):
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}.source_ids")
    row_count = value.get("row_count")
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count < 1
        or row_count > 200_000
    ):
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}.row_count")
    selected_records = value.get("selected_records")
    if not isinstance(selected_records, list) or len(selected_records) != len(source_ids):
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}.selected_records")
    seen_source_ids: set[str] = set()
    for index, record in enumerate(selected_records):
        record_path = f"{path}.selected_records[{index}]"
        if not isinstance(record, Mapping) or set(record) != _RAW_ARCHIVE_RECORD_KEYS:
            raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{record_path}")
        source_id = record.get("source_id")
        if (
            not isinstance(source_id, str)
            or source_id not in source_ids
            or source_id in seen_source_ids
        ):
            raise ValueError(
                f"offline_bundle_raw_archive_admission_invalid:{record_path}.source_id"
            )
        seen_source_ids.add(source_id)
        retrieved_at = _require_admission_timestamp(
            record.get("retrieved_at"),
            path=f"{record_path}.retrieved_at",
        )
        if retrieved_at > observed_before:
            raise ValueError(
                f"offline_bundle_raw_archive_admission_invalid:{record_path}.retrieved_at"
            )
        _require_sha256_digest(record.get("record_sha256"), path=f"{record_path}.record_sha256")
        _require_sha256_digest(record.get("raw_sha256"), path=f"{record_path}.raw_sha256")
    if set(seen_source_ids) != set(source_ids):
        raise ValueError(f"offline_bundle_raw_archive_admission_invalid:{path}.selected_records")
    for key in (
        "admission_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
        "rows_sha256",
    ):
        _require_sha256_digest(value.get(key), path=f"{path}.{key}")


def _require_openfootball_raw_replay(
    raw_sources: Mapping[str, Any],
    *,
    archive_root: Path | None,
) -> None:
    """Bind a public OpenFootball section to the durable raw archive.

    This is intentionally invoked only after all other source rows have been
    inspected.  A blocked ancillary source should retain its precise rights
    error instead of being masked by a missing OpenFootball admission claim.
    """

    fixture_section = raw_sources.get("fixture_feed")
    if not isinstance(fixture_section, Mapping):
        return
    if fixture_section.get("provider") != "OpenFootball":
        return
    fixtures = fixture_section.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        return
    admission = fixture_section.get("raw_archive_admission")
    _require_raw_archive_admission(
        admission,
        path="raw_sources.fixture_feed.raw_archive_admission",
    )
    if archive_root is None:
        raise ValueError("offline_bundle_raw_archive_admission_unavailable")
    try:
        verified = verify_openfootball_snapshot_admission(
            raw_sources,
            archive_root=archive_root,
        )
    except (OpenFootballRawArchiveError, OSError, ValueError) as exc:
        raise ValueError(f"offline_bundle_raw_archive_replay_failed:{exc}") from exc
    expected = admission
    assert isinstance(expected, Mapping)
    for key in (
        "schema_version",
        "status",
        "observed_before",
        "source_ids",
        "admission_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
        "rows_sha256",
    ):
        if expected.get(key) != verified.get(key):
            raise ValueError(f"offline_bundle_raw_archive_replay_mismatch:{key}")
    if expected.get("row_count") != verified.get("raw_row_count"):
        raise ValueError("offline_bundle_raw_archive_replay_mismatch:row_count")
    if verified.get("published_fixture_count") != len(fixtures):
        raise ValueError("offline_bundle_raw_archive_replay_mismatch:fixture_count")


def _public_raw_source_projection(raw_sources: Mapping[str, Any]) -> dict[str, Any]:
    """Remove blocked source facts while retaining bounded public diagnostics."""

    projected = deepcopy(dict(raw_sources))
    for section_key, section_source_id in _SECTION_POLICIES.items():
        section = projected.get(section_key)
        if not isinstance(section, Mapping):
            continue
        if decide_source_rights(section_source_id, _REDISTRIBUTION).is_allowed:
            continue
        retained: dict[str, Any] = {
            key: deepcopy(value)
            for key, value in section.items()
            if key in _SOURCE_METADATA_LIST_KEYS | _SOURCE_METADATA_MAPPING_KEYS
        }
        retained["provider"] = section.get("provider")
        retained["status"] = "rights_blocked"
        retained["network_opened"] = False
        errors = retained.get("errors")
        retained["errors"] = [
            *(errors if isinstance(errors, list) else []),
            {
                "source_id": section_source_id,
                "stage": "redistribution",
                "error": "source_rights_blocked_for_static_bundle",
            },
        ]
        for fact_key in _SECTION_FACT_KEYS.get(section_key, ()):
            retained[fact_key] = []
        projected[section_key] = retained

    crawl4ai = projected.get("crawl4ai")
    if isinstance(crawl4ai, Mapping):
        retained = {
            key: deepcopy(value)
            for key, value in crawl4ai.items()
            if key in _SOURCE_METADATA_LIST_KEYS | _SOURCE_METADATA_MAPPING_KEYS
        }
        retained["status"] = "rights_blocked"
        retained["network_opened"] = False
        retained["pages"] = []
        errors = retained.get("errors")
        retained["errors"] = [
            *(errors if isinstance(errors, list) else []),
            {
                "source_id": "crawl4ai_execution_layer",
                "stage": "redistribution",
                "error": "crawl4ai_is_transport_not_public_fact_source",
            },
        ]
        projected["crawl4ai"] = retained

    registry = projected.get("source_registry")
    if isinstance(registry, list):
        for item in registry:
            if not isinstance(item, dict):
                continue
            raw_source_id = item.get("id")
            source_id: str | None = None
            if isinstance(raw_source_id, str):
                source_id = _REGISTRY_POLICY_IDS.get(raw_source_id)
            if source_id is None and isinstance(raw_source_id, str):
                source_id = raw_source_id
            if not isinstance(source_id, str):
                continue
            decision = decide_source_rights(source_id, _REDISTRIBUTION)
            runtime = item.get("runtime")
            if not isinstance(runtime, dict):
                runtime = item
            if not decision.is_allowed:
                fact_count_keys = {
                    "record_count",
                    "extracted_record_count",
                    "fixture_count",
                    "available_count",
                    "model_eligible_count",
                    "fact_source_count",
                    "match_count",
                    "event_count",
                    "lineup_count",
                }
                quarantined = sum(
                    int(value)
                    for key, value in runtime.items()
                    if key in fact_count_keys
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and value > 0
                )
                for key in fact_count_keys:
                    if key in runtime and isinstance(runtime[key], (int, float)):
                        runtime[key] = 0
                if quarantined:
                    runtime["quarantined_record_count"] = quarantined
                runtime["status"] = "rights_blocked"
                runtime["network_opened"] = False
    return projected


def _public_snapshot_match_projection(
    snapshot: dict[str, Any],
    raw_sources: Mapping[str, Any],
) -> None:
    """Keep only canonical OpenFootball rows witnessed by the raw replay."""

    fixture_section = raw_sources.get("fixture_feed")
    if not isinstance(fixture_section, Mapping):
        return
    raw_rows = fixture_section.get("fixtures")
    if not isinstance(raw_rows, list):
        return
    raw_by_id = {
        row.get("id"): row
        for row in raw_rows
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    matches = snapshot.get("matches")
    if not isinstance(matches, list):
        return
    existing_ids = {
        row.get("id")
        for row in matches
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            continue
        fixture_id = raw_row.get("id")
        if isinstance(fixture_id, str) and fixture_id not in existing_ids:
            matches.append(deepcopy(dict(raw_row)))
            existing_ids.add(fixture_id)
    retained: list[dict[str, Any]] = []
    quarantined_sources: set[str] = set()
    compared_fields = (
        "id",
        "competition_id",
        "season",
        "round",
        "kickoff_date",
        "kickoff_at",
        "kickoff_time_quality",
        "home_team",
        "away_team",
        "status",
        "provider_fixture_ids",
    )
    source_fields = (
        "name",
        "source_id",
        "url",
        "retrieved_at",
        "raw_sha256",
        "license",
    )
    for index, row in enumerate(matches):
        if not isinstance(row, Mapping):
            quarantined_sources.add("malformed_match_row")
            continue
        source = _row_source(row)
        source_id = _source_policy_id(source) if source is not None else None
        if source_id != SourceId.OPENFOOTBALL_CURRENT.value:
            quarantined_sources.add(source_id or "unknown_match_source")
            continue
        fixture_id = row.get("id")
        expected = raw_by_id.get(fixture_id)
        if not isinstance(expected, Mapping):
            quarantined_sources.add("openfootball_unreplayed_match")
            continue
        if any(row.get(field) != expected.get(field) for field in compared_fields):
            raise ValueError(
                f"offline_bundle_snapshot_openfootball_replay_mismatch:matches[{index}]"
            )
        row_source = row.get("source")
        expected_source = expected.get("source")
        if not isinstance(row_source, Mapping) or not isinstance(expected_source, Mapping):
            raise ValueError(
                f"offline_bundle_snapshot_openfootball_provenance_missing:matches[{index}]"
            )
        if any(row_source.get(field) != expected_source.get(field) for field in source_fields):
            raise ValueError(
                f"offline_bundle_snapshot_openfootball_replay_mismatch:matches[{index}].source"
            )
        retained.append(dict(row))
    snapshot["matches"] = retained
    current_data = snapshot.get("current_data")
    if isinstance(current_data, dict):
        snapshot["current_data"] = {
            **current_data,
            "public_match_quarantine_count": len(matches) - len(retained),
            "public_match_quarantine_sources": sorted(quarantined_sources),
        }


def _public_prediction_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Strip factual identities from blocked prediction diagnostics."""

    projected = deepcopy(dict(value))
    blocked = value.get("blocked")
    if not isinstance(blocked, list):
        return projected
    safe_keys = (
        "reason",
        "reason_code",
        "status",
        "stage",
        "freeze_stage",
        "message",
        "source_id",
    )
    projected["blocked"] = [
        {
            key: item[key]
            for key in safe_keys
            if isinstance(item, Mapping)
            and key in item
            and isinstance(item[key], (str, int, float, bool))
        }
        for item in blocked
        if isinstance(item, Mapping)
    ][:_MAX_DIAGNOSTIC_ITEMS]
    projected["blocked_total"] = len(blocked)
    return projected


def _prediction_source_index(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Index the already admitted fixture source for prediction provenance.

    ``PlatformStore`` prediction rows intentionally carry a compact set of
    source fields for the causal archive.  The public bundle still needs the
    complete source contract (source id, allowlisted URL, licence and raw
    digest) before it can publish a prediction.  Resolve that contract only
    from the same snapshot fixture that passed the raw replay gate; never
    synthesize it from a prediction's self-reported provider name.
    """

    matches = snapshot.get("matches")
    if not isinstance(matches, list):
        return {}
    index: dict[str, Mapping[str, Any]] = {}
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        fixture_id = match.get("id") or match.get("fixture_id")
        source = match.get("source")
        if (
            isinstance(fixture_id, str)
            and fixture_id.strip()
            and isinstance(source, Mapping)
            and fixture_id not in index
        ):
            index[fixture_id] = match
    return index


def _bind_snapshot_prediction_provenance(
    value: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind prediction rows to the admitted snapshot fixture source.

    A prediction may be displayed as research-only, but it cannot enter a
    public artifact with an untraceable source.  Rows whose compact fields do
    not agree with the snapshot lose their source identity so the rights gate
    rejects them instead of guessing an identity.
    """

    projected = deepcopy(dict(value))
    rows = value.get("predictions")
    if not isinstance(rows, list):
        return projected
    source_index = _prediction_source_index(snapshot)

    def unbound(row: Mapping[str, Any]) -> dict[str, Any]:
        """Remove source identity when it did not match the trusted fixture."""

        result = dict(row)
        for key in ("source", "source_id", "fact_source_id", "policy_source_id"):
            result.pop(key, None)
        return result

    bound_rows: list[Any] = []
    for row in rows:
        if not isinstance(row, Mapping):
            bound_rows.append(row)
            continue
        fixture_id = row.get("fixture_id")
        match = source_index.get(fixture_id) if isinstance(fixture_id, str) else None
        if match is None:
            bound_rows.append(unbound(row) if _row_source(row) is not None else row)
            continue
        source = match.get("source")
        if not isinstance(source, Mapping):
            bound_rows.append(unbound(row) if _row_source(row) is not None else row)
            continue
        provider = source.get("name")
        source_id = source.get("source_id")
        url = source.get("url")
        raw_sha = source.get("raw_sha256") or source.get("sha256")
        license_name = source.get("license") or source.get("license_status")
        if not all(
            isinstance(item, str) and item.strip()
            for item in (provider, source_id, url, raw_sha, license_name)
        ):
            bound_rows.append(unbound(row) if _row_source(row) is not None else row)
            continue
        existing_source = _row_source(row)
        if existing_source is not None:
            existing_provider = existing_source.get("name") or existing_source.get("provider")
            existing_source_id = existing_source.get("source_id")
            existing_url = existing_source.get("url")
            existing_license = existing_source.get("license") or existing_source.get(
                "license_status"
            )
            existing_raw_sha = existing_source.get("raw_sha256") or existing_source.get("sha256")
            existing_retrieved_at = existing_source.get("retrieved_at")
            expected_retrieved_at = source.get("retrieved_at")
            if any(
                value is not None and value != expected
                for value, expected in (
                    (existing_provider, provider),
                    (existing_source_id, source_id),
                    (existing_url, url),
                    (existing_license, license_name),
                    (existing_raw_sha, raw_sha),
                    (existing_retrieved_at, expected_retrieved_at),
                )
            ):
                bound_rows.append(unbound(row))
                continue
        row_provider = row.get("source_name")
        row_sha = row.get("source_sha256")
        if row_provider not in (None, provider) or row_sha not in (None, raw_sha):
            bound_rows.append(unbound(row))
            continue
        bound = dict(row)
        # Research projections from the causal archive can omit identity
        # columns even when the exact fixture is present in the admitted
        # snapshot.  Fill only missing values from that fixture; a mismatch
        # remains unbound and is rejected below rather than being corrected.
        for field in ("competition_id", "season", "kickoff_at", "home_team", "away_team"):
            if bound.get(field) is None and match.get(field) is not None:
                bound[field] = match[field]
        bound["source"] = {
            "name": provider,
            "source_id": source_id,
            "url": url,
            "retrieved_at": source.get("retrieved_at"),
            "raw_sha256": raw_sha,
            "hash": source.get("hash") or f"sha256:{raw_sha}",
            "license": license_name,
        }
        bound_rows.append(bound)
    projected["predictions"] = bound_rows
    return projected


def _quarantine_unbound_predictions(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep valid prediction rows while isolating source-clock mismatches."""

    projected = deepcopy(dict(value))
    rows = value.get("predictions")
    if not isinstance(rows, list):
        return projected
    valid_rows: list[Any] = []
    quarantined = 0
    for row in rows:
        if isinstance(row, Mapping) and _row_source(row) is not None:
            valid_rows.append(row)
        else:
            quarantined += 1
    if quarantined == 0:
        return projected
    projected["predictions"] = valid_rows
    existing_blocked = value.get("blocked")
    blocked = list(existing_blocked) if isinstance(existing_blocked, list) else []
    blocked.extend(
        {
            "reason": "prediction_source_contract_unverified",
            "reason_code": "source_snapshot_mismatch",
            "status": "blocked",
            "message": "预测来源未与本轮已验证赛程原始归档精确绑定，已隔离。",
        }
        for _ in range(quarantined)
    )
    projected["blocked"] = blocked[:_MAX_DIAGNOSTIC_ITEMS]
    projected["blocked_total"] = len(blocked)
    return projected


def _require_fixture_section_provenance(
    section: Mapping[str, Any],
    *,
    path: str,
) -> None:
    source_contract = section.get("source_contract")
    if source_contract is not None:
        if not isinstance(source_contract, Mapping):
            raise ValueError(f"offline_bundle_source_provenance_malformed:{path}.source_contract")
        field_sources = source_contract.get("field_sources")
        if field_sources is not None:
            if not isinstance(field_sources, list):
                raise ValueError(
                    f"offline_bundle_source_provenance_malformed:{path}.source_contract.field_sources"
                )
            for index, provider in enumerate(field_sources):
                if provider == "OpenFootball":
                    continue
                if not isinstance(provider, str) or not provider.strip():
                    raise ValueError(
                        "offline_bundle_source_provenance_malformed:"
                        f"{path}.source_contract.field_sources[{index}]"
                    )
                _raise_rights_block(_PROVIDER_POLICY_IDS.get(provider) or provider)

    for key in (
        "schedule_overlay_diagnostics",
        "schedule_overlay_diagnostics_by_competition",
    ):
        if key in section:
            diagnostic = section.get(key)
            if not isinstance(diagnostic, Mapping):
                raise ValueError(f"offline_bundle_source_section_malformed:{path}.{key}")
            _require_bounded_diagnostic(diagnostic, path=f"{path}.{key}")

    known_keys = (
        frozenset(_FIXTURE_SECTION_FACT_KEYS)
        | _SOURCE_METADATA_LIST_KEYS
        | {
            "provider",
            "retrieved_at",
            "checked_at",
            "status",
            "network_opened",
            "policy",
            "source_contract",
            "schedule_overlay_diagnostics",
            "schedule_overlay_diagnostics_by_competition",
            "raw_archive_admission",
        }
    )
    for key, value in section.items():
        if key in known_keys or not isinstance(value, (Mapping, list)):
            continue
        if _row_collections(value, path=f"{path}.{key}", depth=1):
            raise ValueError(f"offline_bundle_unknown_source_rows:{path}.{key}")


def _require_blocked_diagnostic(
    section: Mapping[str, Any],
    *,
    source_id: str,
    path: str,
    explicit_snapshot_source: bool,
) -> None:
    _require_bounded_diagnostic(section, path=path)
    if decide_source_rights(source_id, _REDISTRIBUTION).is_allowed:
        if (
            explicit_snapshot_source
            and _has_source_activity(section)
            and _network_opened(section) is not False
        ):
            raise ValueError(
                f"offline_bundle_blocked_diagnostic_requires_network_opened_false:{path}"
            )
        return
    if not explicit_snapshot_source and not _has_source_activity(section):
        # Empty schema placeholders used by lightweight/offline stores carry no
        # source observation and are not a provider-health assertion.
        return
    if _network_opened(section) is not False:
        raise ValueError(f"offline_bundle_blocked_diagnostic_requires_network_opened_false:{path}")


def _require_known_source_sections(
    payload: Mapping[str, Any],
    *,
    path: str,
    explicit_snapshot_source: bool,
) -> None:
    fixture_section = payload.get("fixture_feed")
    legacy_section = payload.get("espn")
    selected_fixture_section = fixture_section if fixture_section is not None else legacy_section
    if selected_fixture_section is not None:
        selected_key = "fixture_feed" if fixture_section is not None else "espn"
        selected_path = f"{path}.{selected_key}"
        if not isinstance(selected_fixture_section, Mapping):
            raise ValueError(f"offline_bundle_source_section_malformed:{selected_path}")
        _require_source_diagnostics(selected_fixture_section, path=selected_path)
        _require_fixture_section_provenance(selected_fixture_section, path=selected_path)
        raw_fixtures = selected_fixture_section.get("fixtures")
        if raw_fixtures is not None and not isinstance(raw_fixtures, list):
            raise ValueError(f"offline_bundle_source_rows_malformed:{selected_path}.fixtures")
        fact_keys = _FIXTURE_SECTION_FACT_KEYS if selected_key == "fixture_feed" else ("fixtures",)
        fixture_fact_rows: list[tuple[str, int, Mapping[str, Any]]] = []
        for fact_key in fact_keys:
            value = selected_fixture_section.get(fact_key)
            if value is None:
                continue
            if not isinstance(value, list):
                raise ValueError(
                    f"offline_bundle_source_rows_malformed:{selected_path}.{fact_key}"
                )
            for index, row in enumerate(value):
                row_path = f"{selected_path}.{fact_key}[{index}]"
                if not isinstance(row, Mapping):
                    raise ValueError(f"offline_bundle_source_row_malformed:{row_path}")
                fixture_fact_rows.append((fact_key, index, row))
        if isinstance(raw_fixtures, list) and raw_fixtures:
            require_fixture_serving(payload, use_case=_REDISTRIBUTION)
        for fact_key, index, row in fixture_fact_rows:
            row_path = f"{selected_path}.{fact_key}[{index}]"
            source_id = _require_row_policy(
                row,
                path=row_path,
                allow_trusted_openfootball_fixture=True,
            )
            if source_id == SourceId.OPENFOOTBALL_CURRENT.value:
                _require_nested_match_sources(row, path=row_path)
        if not fixture_fact_rows:
            provider = selected_fixture_section.get("provider")
            diagnostic_source_id = _source_policy_id(
                {"name": provider}
                if isinstance(provider, str)
                else {"source_id": f"unknown_{selected_key}_provider"}
            )
            _require_blocked_diagnostic(
                selected_fixture_section,
                source_id=diagnostic_source_id or f"unknown_{selected_key}_provider",
                path=selected_path,
                explicit_snapshot_source=explicit_snapshot_source,
            )

    for section_key, source_id in _SECTION_POLICIES.items():
        if section_key == "espn" and selected_fixture_section is legacy_section:
            continue
        section = payload.get(section_key)
        if section is None:
            continue
        section_path = f"{path}.{section_key}"
        if not isinstance(section, Mapping):
            raise ValueError(f"offline_bundle_source_section_malformed:{section_path}")
        _require_source_diagnostics(section, path=section_path)
        rows = _section_fact_rows(
            section,
            section_key=section_key,
            path=section_path,
        )
        if rows:
            if source_id == SourceId.OPENFOOTBALL_CURRENT.value:
                _require_raw_archive_admission(
                    section.get("raw_archive_admission"),
                    path=f"{section_path}.raw_archive_admission",
                )
                for index, row in enumerate(rows):
                    resolved = _require_row_policy(
                        row,
                        path=f"{section_path}[{index}]",
                        allow_trusted_openfootball_fixture=True,
                    )
                    if resolved == SourceId.OPENFOOTBALL_CURRENT.value:
                        _require_nested_match_sources(row, path=f"{section_path}[{index}]")
            else:
                _raise_rights_block(source_id)
        else:
            _require_blocked_diagnostic(
                section,
                source_id=source_id,
                path=section_path,
                explicit_snapshot_source=explicit_snapshot_source,
            )

    crawl4ai = payload.get("crawl4ai")
    if crawl4ai is not None:
        if not isinstance(crawl4ai, Mapping):
            raise ValueError(f"offline_bundle_source_section_malformed:{path}.crawl4ai")
        _require_source_diagnostics(crawl4ai, path=f"{path}.crawl4ai")
        pages = crawl4ai.get("pages")
        if pages is not None and not isinstance(pages, list):
            raise ValueError(f"offline_bundle_source_rows_malformed:{path}.crawl4ai.pages")
        for index, page in enumerate(pages or []):
            if not isinstance(page, Mapping):
                raise ValueError(
                    f"offline_bundle_source_row_malformed:{path}.crawl4ai.pages[{index}]"
                )
            _require_row_policy(page, path=f"{path}.crawl4ai.pages[{index}]")


def _positive_fact_count(value: Mapping[str, Any]) -> bool:
    fact_count_keys = {
        "record_count",
        "extracted_record_count",
        "fixture_count",
        "available_count",
        "model_eligible_count",
        "fact_source_count",
        "match_count",
        "event_count",
        "lineup_count",
    }
    for key, item in value.items():
        if (
            key in fact_count_keys
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
        ):
            if item > 0:
                return True
        if isinstance(item, Mapping) and _positive_fact_count(item):
            return True
    return False


def _require_source_registry(value: Any, *, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError(f"offline_bundle_source_rows_malformed:{path}")
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"offline_bundle_source_row_malformed:{item_path}")
        raw_source_id = item.get("id")
        if not isinstance(raw_source_id, str) or not raw_source_id.strip():
            raise ValueError(f"offline_bundle_source_provenance_missing:{item_path}")
        source_id = _REGISTRY_POLICY_IDS.get(raw_source_id, raw_source_id)
        decision = decide_source_rights(source_id, _REDISTRIBUTION)
        runtime = item.get("runtime")
        runtime = runtime if isinstance(runtime, Mapping) else item
        _require_source_diagnostics(runtime, path=f"{item_path}.runtime")
        _require_bounded_diagnostic(runtime, path=f"{item_path}.runtime")
        if decision.is_allowed:
            continue
        if _positive_fact_count(runtime):
            _raise_rights_block(source_id)
        if _network_opened(runtime) is not False:
            raise ValueError(
                f"offline_bundle_blocked_diagnostic_requires_network_opened_false:{item_path}"
            )


def _require_provider_error_diagnostics(value: Any, *, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"offline_bundle_source_section_malformed:{path}")
    for provider, diagnostic in value.items():
        item_path = f"{path}.{provider}"
        if isinstance(diagnostic, list):
            _require_bounded_diagnostic(diagnostic, path=item_path)
            if diagnostic:
                # The historical list-only shape cannot prove that a blocked
                # provider stayed offline, so only its empty placeholder is
                # safe for a redistribution bundle.
                raise ValueError(
                    f"offline_bundle_blocked_diagnostic_requires_network_opened_false:{item_path}"
                )
            continue
        if not isinstance(diagnostic, Mapping):
            raise ValueError(f"offline_bundle_source_section_malformed:{item_path}")
        _require_bounded_diagnostic(diagnostic, path=item_path)
        has_activity = any(
            key != "network_opened" and _nonempty(item) for key, item in diagnostic.items()
        )
        if has_activity and _network_opened(diagnostic) is not False:
            raise ValueError(
                f"offline_bundle_blocked_diagnostic_requires_network_opened_false:{item_path}"
            )


def _require_current_data(value: Any, *, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"offline_bundle_source_section_malformed:{path}")

    # OpenFootball competitions outside the trained model catalog are kept in
    # a bounded display-only lane.  They are scoreboard facts, not model
    # inputs, and therefore intentionally have no provider-policy marker.  A
    # small explicit validator keeps them out of the generic source-row path
    # (which would otherwise reject their human-readable IDs as provenance).
    display_competitions = value.get("display_only_competitions")
    if display_competitions is not None:
        if (
            not isinstance(display_competitions, list)
            or len(display_competitions) > _MAX_DIAGNOSTIC_ITEMS
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > _MAX_DIAGNOSTIC_TEXT
                for item in display_competitions
            )
        ):
            raise ValueError(
                f"offline_bundle_source_rows_malformed:{path}.display_only_competitions"
            )

    display_rows = value.get("display_only_fixtures")
    if display_rows is not None:
        if not isinstance(display_rows, list) or len(display_rows) > _MAX_DISPLAY_ONLY_FIXTURES:
            raise ValueError(
                f"offline_bundle_source_rows_malformed:{path}.display_only_fixtures"
            )
        allowed_display_keys = frozenset(
            {
                "competition_id",
                "fixture_id",
                "kickoff_at",
                "kickoff_date",
                "home_team",
                "away_team",
                "status",
                "reason",
            }
        )
        for index, row in enumerate(display_rows):
            row_path = f"{path}.display_only_fixtures[{index}]"
            if not isinstance(row, Mapping) or not set(row).issubset(allowed_display_keys):
                raise ValueError(f"offline_bundle_source_row_malformed:{row_path}")
            required = ("competition_id", "fixture_id", "status", "reason")
            if any(
                not isinstance(row.get(key), str)
                or not row.get(key, "").strip()
                or len(row[key]) > _MAX_DIAGNOSTIC_TEXT
                for key in required
            ):
                raise ValueError(f"offline_bundle_source_row_malformed:{row_path}")
            if row["status"] != "display_only" or row["reason"] != "competition_not_in_model_catalog":
                raise ValueError(f"offline_bundle_source_row_malformed:{row_path}")
            for key in ("kickoff_at", "kickoff_date", "home_team", "away_team"):
                item = row.get(key)
                if item is not None and (
                    not isinstance(item, str) or len(item) > _MAX_DIAGNOSTIC_TEXT
                ):
                    raise ValueError(f"offline_bundle_source_row_malformed:{row_path}.{key}")
        if isinstance(display_competitions, list):
            row_competitions = sorted(
                {
                    row.get("competition_id")
                    for row in display_rows
                    if isinstance(row, Mapping) and isinstance(row.get("competition_id"), str)
                }
            )
            if row_competitions != sorted(display_competitions):
                raise ValueError(
                    f"offline_bundle_source_rows_malformed:{path}.display_only_competitions"
                )
    lottery = value.get("lottery_sales_schedule")
    if lottery is not None:
        if not isinstance(lottery, list):
            raise ValueError(f"offline_bundle_source_rows_malformed:{path}.lottery_sales_schedule")
        if lottery:
            _raise_rights_block(SourceId.SPORTS_LOTTERY_OFFICIAL.value)

    match_center = value.get("match_center")
    if match_center is not None:
        if not isinstance(match_center, Mapping):
            raise ValueError(f"offline_bundle_source_section_malformed:{path}.match_center")
        for key in ("live", "recent_finished"):
            rows = match_center.get(key)
            if rows is not None and not isinstance(rows, list):
                raise ValueError(f"offline_bundle_source_rows_malformed:{path}.match_center.{key}")
            if rows:
                _raise_rights_block(SourceId.ESPN_MARKET_SUMMARY.value)
        _require_unknown_top_level_sections(
            match_center,
            path=f"{path}.match_center",
            metadata_keys=frozenset(
                {
                    "provider",
                    "retrieved_at",
                    "status",
                    "live",
                    "recent_finished",
                    "live_match_count",
                    "recent_finished_count",
                    "incident_observation_count",
                    "stats_observation_count",
                    "enters_model",
                    "model_exclusion_reason",
                    "errors",
                }
            ),
        )

    _require_source_registry(value.get("source_registry"), path=f"{path}.source_registry")

    official_diagnostics = value.get("official_fixture_join_diagnostics")
    if official_diagnostics is not None and not isinstance(official_diagnostics, Mapping):
        raise ValueError(
            f"offline_bundle_source_section_malformed:{path}.official_fixture_join_diagnostics"
        )
    if isinstance(official_diagnostics, Mapping):
        for competition_id, diagnostic in official_diagnostics.items():
            if not isinstance(diagnostic, Mapping):
                raise ValueError(
                    "offline_bundle_source_section_malformed:"
                    f"{path}.official_fixture_join_diagnostics.{competition_id}"
                )
            samples = diagnostic.get("samples")
            if samples is not None and not isinstance(samples, list):
                raise ValueError(
                    "offline_bundle_source_rows_malformed:"
                    f"{path}.official_fixture_join_diagnostics.{competition_id}.samples"
                )
            if isinstance(samples, list) and samples:
                source_id = _OFFICIAL_MATCH_FEATURE_POLICIES.get(str(competition_id))
                _raise_rights_block(source_id or SourceId.OFFICIAL_LEAGUE_LINEUPS.value)

    lineup_diagnostics = value.get("lineup_poll_diagnostics")
    if lineup_diagnostics is not None and not isinstance(lineup_diagnostics, Mapping):
        raise ValueError(f"offline_bundle_source_section_malformed:{path}.lineup_poll_diagnostics")
    if isinstance(lineup_diagnostics, Mapping):
        for competition_id, diagnostic in lineup_diagnostics.items():
            if not isinstance(diagnostic, Mapping):
                raise ValueError(
                    "offline_bundle_source_section_malformed:"
                    f"{path}.lineup_poll_diagnostics.{competition_id}"
                )
            has_rows = any(
                isinstance(diagnostic.get(key), list) and bool(diagnostic.get(key))
                for key in (
                    "fixture_plans",
                    "candidate_fixture_ids",
                    "requested_fixture_ids",
                    "carried_forward_fixture_ids",
                )
            )
            if has_rows:
                source_id = _OFFICIAL_MATCH_FEATURE_POLICIES.get(str(competition_id))
                _raise_rights_block(source_id or SourceId.OFFICIAL_LEAGUE_LINEUPS.value)

    provider_errors = value.get("provider_errors")
    _require_provider_error_diagnostics(
        provider_errors,
        path=f"{path}.provider_errors",
    )

    _require_unknown_top_level_sections(
        value,
        path=path,
        metadata_keys=frozenset(
            {
                "source_registry",
                "lottery_sales_schedule",
                "match_center",
                "official_fixture_join_diagnostics",
                "lineup_poll_diagnostics",
                "provider_errors",
                "roles",
                "expected_competitions",
                "catalog_expansion_pending_competitions",
                "display_only_competitions",
                "display_only_fixture_count",
                "display_only_fixtures",
                "intelligence_ledger",
                "model_admission",
                "rights_quarantine",
                "role_contract",
                "public_match_quarantine_count",
                "public_match_quarantine_sources",
            }
        ),
    )


def _row_collections(value: Any, *, path: str, depth: int = 0) -> list[tuple[str, list[Any]]]:
    if depth > 16:
        raise ValueError(f"offline_bundle_source_section_too_deep:{path}")
    collections: list[tuple[str, list[Any]]] = []
    if isinstance(value, Mapping):
        if depth and any(
            key in value and _nonempty(value.get(key)) for key in _FACT_ROW_MARKER_KEYS
        ):
            collections.append((path, [value]))
            return collections
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(child, list) and child and key not in _SOURCE_METADATA_LIST_KEYS:
                collections.append((child_path, child))
            elif isinstance(child, Mapping):
                collections.extend(_row_collections(child, path=child_path, depth=depth + 1))
    elif isinstance(value, list) and value:
        collections.append((path, value))
    return collections


def _require_unknown_source_section(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        _require_source_diagnostics(value, path=path)
    collections = _row_collections(value, path=path, depth=1)
    if collections:
        for collection_path, rows in collections:
            for index, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    raise ValueError(
                        f"offline_bundle_source_row_malformed:{collection_path}[{index}]"
                    )
                source_id = _require_row_policy(
                    row,
                    path=f"{collection_path}[{index}]",
                )
                if source_id == SourceId.OPENFOOTBALL_CURRENT.value:
                    raise ValueError("openfootball_source_contract_unverified")
        return
    if isinstance(value, Mapping) and any(
        key in value for key in ("provider", "source", "source_id", "fact_source_id")
    ):
        _require_bounded_diagnostic(value, path=path)
        if _network_opened(value) is not False:
            raise ValueError(
                f"offline_bundle_blocked_diagnostic_requires_network_opened_false:{path}"
            )


def _require_unknown_top_level_sections(
    payload: Mapping[str, Any],
    *,
    path: str,
    metadata_keys: frozenset[str],
) -> None:
    known = metadata_keys | frozenset(_SECTION_POLICIES) | {"fixture_feed", "crawl4ai"}
    for key, value in payload.items():
        if key in known:
            continue
        if isinstance(value, (Mapping, list)):
            collections = _row_collections(value, path=f"{path}.{key}", depth=1)
            declares_source = isinstance(value, Mapping) and any(
                name in value for name in ("provider", "source", "source_id", "fact_source_id")
            )
            if collections or declares_source:
                _require_unknown_source_section(value, path=f"{path}.{key}")
            elif _nonempty(value):
                _require_bounded_diagnostic(value, path=f"{path}.{key}")
                raise ValueError(f"offline_bundle_unknown_source_section:{path}.{key}")


def _require_prediction_rows(
    value: Any,
    *,
    path: str,
    allow_verified_openfootball: bool = False,
    allow_research_provider_shortcut: bool = False,
) -> None:
    if not isinstance(value, Mapping):
        if value is None:
            return
        raise ValueError(f"offline_bundle_source_section_malformed:{path}")
    rows = value.get("predictions")
    if rows is None:
        rows = []
    elif not isinstance(rows, list):
        raise ValueError(f"offline_bundle_source_rows_malformed:{path}.predictions")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"offline_bundle_source_row_malformed:{path}.predictions[{index}]")
        source = _row_source(row)
        if source is None and allow_research_provider_shortcut:
            providers = row.get("providers")
            if (
                row.get("status") == "research_only"
                and isinstance(providers, list)
                and providers == ["OpenFootball"]
            ):
                continue
        _require_row_policy(
            row,
            path=f"{path}.predictions[{index}]",
            allow_trusted_openfootball_fixture=allow_verified_openfootball,
        )
    blocked = value.get("blocked")
    if blocked is not None:
        if not isinstance(blocked, list):
            raise ValueError(f"offline_bundle_source_rows_malformed:{path}.blocked")
        _require_bounded_diagnostic(blocked, path=f"{path}.blocked")


def _require_live_overlay(value: Any, *, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"offline_bundle_source_section_malformed:{path}")
    _require_source_diagnostics(value, path=path)
    fixtures = value.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError(f"offline_bundle_source_rows_malformed:{path}.fixtures")
    fixture_count = value.get("fixture_count")
    if fixture_count is not None and (
        not isinstance(fixture_count, int)
        or isinstance(fixture_count, bool)
        or fixture_count != len(fixtures)
    ):
        raise ValueError(f"offline_bundle_source_rows_malformed:{path}.fixture_count")
    unknown_keys = sorted(set(value) - _LIVE_OVERLAY_KEYS)
    if unknown_keys:
        # Inspect first so a hidden match fact gets the more precise factual
        # diagnostic error rather than looking like a harmless schema drift.
        _require_bounded_diagnostic(value, path=path)
        raise ValueError(f"offline_bundle_unknown_source_rows:{path}.{unknown_keys[0]}")
    source = value.get("source")
    if source is not None and not isinstance(source, Mapping):
        raise ValueError(f"offline_bundle_source_provenance_malformed:{path}.source")
    envelope_source_id = _source_policy_id(source) if isinstance(source, Mapping) else None
    if fixtures:
        if envelope_source_id is None:
            raise ValueError(f"offline_bundle_source_provenance_missing:{path}.source")
        if envelope_source_id == SourceId.OPENFOOTBALL_CURRENT.value:
            # The registered OpenFootball contract authenticates canonical
            # schedule rows; it is not a producer identity for live scores,
            # clocks or events.  An overlay cannot borrow that identity even
            # when it copies every allowlisted source field exactly.
            raise ValueError(
                "offline_bundle_live_overlay_producer_unverified:"
                f"{SourceId.OPENFOOTBALL_CURRENT.value}"
            )
        _raise_rights_block(envelope_source_id)
        for index, row in enumerate(fixtures):
            if not isinstance(row, Mapping):
                raise ValueError(f"offline_bundle_source_row_malformed:{path}.fixtures[{index}]")
            source_id = _require_row_policy(
                row,
                path=f"{path}.fixtures[{index}]",
                allow_trusted_openfootball_fixture=True,
            )
            if source_id == SourceId.OPENFOOTBALL_CURRENT.value:
                _require_nested_match_sources(row, path=f"{path}.fixtures[{index}]")
        return
    if envelope_source_id is not None or _has_source_activity(value):
        _require_blocked_diagnostic(
            value,
            source_id=envelope_source_id or "unknown_live_overlay_provider",
            path=path,
            explicit_snapshot_source=True,
        )


def _require_offline_redistribution_rights(
    snapshot: Mapping[str, Any],
    predictions: Mapping[str, Any],
    research_predictions: Mapping[str, Any],
    *,
    raw_sources: Mapping[str, Any] | None,
    raw_archive_root: Path | None,
    raw_replay_verified: bool = False,
) -> None:
    """Fail before serialization if any public factual row lacks redistribution rights."""

    if raw_sources is not None:
        _require_known_source_sections(
            raw_sources,
            path="raw_sources",
            explicit_snapshot_source=True,
        )
        _require_source_registry(
            raw_sources.get("source_registry"),
            path="raw_sources.source_registry",
        )
        _require_unknown_top_level_sections(
            raw_sources,
            path="raw_sources",
            metadata_keys=_RAW_SOURCE_METADATA_KEYS,
        )
        if not raw_replay_verified:
            _require_openfootball_raw_replay(
                raw_sources,
                archive_root=raw_archive_root,
            )

    _require_match_rows(snapshot)
    _require_known_source_sections(
        snapshot,
        path="snapshot",
        explicit_snapshot_source=False,
    )
    _require_current_data(snapshot.get("current_data"), path="snapshot.current_data")
    _require_source_registry(snapshot.get("source_registry"), path="snapshot.source_registry")
    _require_prediction_rows(
        predictions,
        path="predictions",
        allow_verified_openfootball=raw_replay_verified,
    )
    _require_prediction_rows(
        research_predictions,
        path="research_predictions",
        allow_verified_openfootball=raw_replay_verified,
        allow_research_provider_shortcut=raw_replay_verified,
    )
    _require_live_overlay(snapshot.get("live_overlay"), path="snapshot.live_overlay")
    _require_unknown_top_level_sections(
        snapshot,
        path="snapshot",
        metadata_keys=_SNAPSHOT_NON_SOURCE_KEYS,
    )


def _load_optional_live_sources(live_path: Path | None) -> dict | None:
    """Load an optional live snapshot without treating corruption as absence."""

    if live_path is None:
        return None
    try:
        contents = live_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"live_snapshot_unreadable: {live_path}") from exc
    try:
        value = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise ValueError(f"live_snapshot_invalid_json: {live_path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"live_snapshot_root_not_object: {live_path}")
    return value


def _load_optional_live_overlay(live_overlay_path: Path | None) -> dict | None:
    """Load a requested overlay without treating corruption as absence."""

    if live_overlay_path is None:
        return None
    try:
        contents = live_overlay_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"live_overlay_unreadable: {live_overlay_path}") from exc
    try:
        value = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise ValueError(f"live_overlay_invalid_json: {live_overlay_path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"live_overlay_root_not_object: {live_overlay_path}")
    if value.get("schema_version") != "matchline.live_overlay.v1":
        raise ValueError(f"live_overlay_schema_invalid: {live_overlay_path}")
    return value


def _fsync_output_directories(paths: list[Path]) -> None:
    """Persist staged/replaced directory entries on the local filesystem."""

    for directory in sorted({path.parent for path in paths}, key=str):
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _replace_prepared_outputs(outputs: list[tuple[Path, str]]) -> None:
    """Stage every payload and roll back a partially replaced output set."""

    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for target, payload in outputs:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            staged.append((target, temporary))
            try:
                stream = os.fdopen(descriptor, "w", encoding="utf-8")
            except Exception:
                os.close(descriptor)
                raise
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod((target.stat().st_mode & 0o777) if target.exists() else 0o644)

        # A single filesystem rename is atomic, but the bundle can expose up
        # to three independently named files.  Snapshot each old target before
        # the first rename so a later rename failure cannot leave a mixed
        # generation visible after this function returns.
        for target, _temporary in staged:
            if not target.exists():
                backups[target] = None
                continue
            descriptor, backup_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".bak",
                dir=target.parent,
            )
            os.close(descriptor)
            backup = Path(backup_name)
            backups[target] = backup
            shutil.copy2(target, backup)

        # File fsync above persists every staged payload; directory fsync makes
        # the corresponding temporary names durable before public replacement.
        _fsync_output_directories([target for target, _temporary in staged])

        # Compact consumers switch first; the full bundle is the final public
        # marker, so it can never point at a compact payload that was not
        # completely staged.
        try:
            for target, temporary in staged:
                temporary.replace(target)
                committed.append(target)
            _fsync_output_directories(committed)
        except BaseException as replace_error:
            rollback_errors: list[tuple[Path, BaseException]] = []
            for target in reversed(committed):
                rollback_backup = backups[target]
                try:
                    if rollback_backup is None:
                        target.unlink(missing_ok=True)
                    else:
                        rollback_backup.replace(target)
                except BaseException as rollback_error:
                    rollback_errors.append((target, rollback_error))
            if committed:
                try:
                    _fsync_output_directories(committed)
                except BaseException as rollback_error:
                    rollback_errors.append((committed[-1].parent, rollback_error))
            if rollback_errors:
                failed = ",".join(str(target) for target, _error in rollback_errors)
                raise RuntimeError(
                    f"offline_bundle_output_rollback_failed:{failed}"
                ) from replace_error
            raise
    finally:
        for _target, temporary in staged:
            temporary.unlink(missing_ok=True)
        for cleanup_backup in backups.values():
            if cleanup_backup is not None:
                cleanup_backup.unlink(missing_ok=True)


def _verified_research_projection(value: object) -> dict[str, object]:
    """Return only the v260 verified-raw research lane for serialization."""

    model_input = value.get("model_input") if isinstance(value, dict) else None
    is_verified = (
        isinstance(value, dict)
        and value.get("status") == "research_only"
        and isinstance(model_input, dict)
        and model_input.get("status") == "verified"
        and model_input.get("trust_anchor") == "verified_openfootball_raw_archive_replay"
        and model_input.get("snapshot_model_facts_accepted") is False
    )
    if is_verified:
        assert isinstance(value, dict)
        return {
            **{str(key): item for key, item in value.items()},
            "production_allowed": False,
        }

    raw_value = value if isinstance(value, dict) else {}
    reason = raw_value.get("reason")
    message = raw_value.get("message")
    return {
        "status": "unavailable",
        "as_of": raw_value.get("as_of"),
        "current_data_status": raw_value.get("current_data_status"),
        "predictions": [],
        "blocked": [],
        "reason": (
            reason if isinstance(reason, str) else "verified_openfootball_future_unavailable"
        ),
        "model_input": {
            "status": "unavailable",
            "trust_anchor": "verified_openfootball_raw_archive_replay",
            "snapshot_model_facts_accepted": False,
        },
        "model_health": {},
        "message": (
            message
            if isinstance(message, str)
            else "verified OpenFootball raw archive replay is unavailable"
        ),
        "production_allowed": False,
    }


def build_bundle(
    *,
    data_dir: Path,
    live_path: Path | None,
    strict_report_path: Path,
    output: Path,
    compact_output: Path | None = None,
    compact_js_output: Path | None = None,
    evidence_dir: Path | None = None,
    prospective_lock_path: Path | None = None,
    runtime_evidence_dir: Path | None = None,
    live_overlay_path: Path | None = None,
    runtime_archive_manifest_path: Path | None = None,
    openfootball_raw_archive_dir: Path | str | None = None,
) -> dict[str, int | str]:
    report = json.loads(strict_report_path.read_text(encoding="utf-8"))
    # Use one aware instant for the base snapshot, current attachment and all
    # verified archive replays performed by this build.
    observed_before = datetime.now(timezone.utc)
    resolved_raw_archive_dir = resolve_future_openfootball_raw_archive_dir(
        openfootball_raw_archive_dir
    )
    store_options: dict[str, Any] = {
        "now": observed_before,
        "live_path": live_path,
        "strict_report_path": strict_report_path,
        "evidence_dir": evidence_dir,
        "openfootball_raw_archive_dir": resolved_raw_archive_dir,
    }
    if prospective_lock_path is not None:
        store_options["prospective_lock_path"] = prospective_lock_path
    # Preserve compatibility with lightweight PlatformStore fakes used by
    # embedders while making the split explicit whenever the caller requests
    # it.  Omitting this option retains the historical single-root contract.
    if runtime_evidence_dir is not None:
        store_options["runtime_evidence_dir"] = runtime_evidence_dir
    store = PlatformStore(data_dir, **store_options)
    snapshot = store.snapshot()
    runtime_archive = _load_runtime_archive_projection(runtime_archive_manifest_path)
    if runtime_archive is not None:
        snapshot["runtime_archive"] = runtime_archive
    intelligence_ledger = _load_intelligence_ledger_projection(evidence_dir)
    if intelligence_ledger is not None:
        current_data = snapshot.get("current_data")
        if isinstance(current_data, dict):
            snapshot["current_data"] = {
                **current_data,
                "intelligence_ledger": intelligence_ledger,
            }
    # The static package is a public surface. Keep the causal archive and the
    # on-site verified future consumer as separate lanes. In particular, do
    # not ask PlatformStore for its legacy stale-snapshot research fallback.
    public_predictions = getattr(store, "public_predictions", None)
    direct_predictions = getattr(store, "predictions", None)
    if callable(public_predictions):
        try:
            public_result = public_predictions()
        except TypeError:
            # Keep compatibility with small test/dry-run stores that expose
            # only the keyword-bearing migration method.
            public_result = public_predictions(include_research_drafts=False)
        predictions = (
            dict(public_result)
            if isinstance(public_result, dict)
            else {"predictions": [], "blocked": []}
        )
        predictions.pop("research_predictions", None)
        if callable(direct_predictions):
            direct_result = direct_predictions()
            research_predictions = (
                direct_result
                if isinstance(direct_result, dict)
                else {"status": "unavailable", "predictions": [], "blocked": []}
            )
        else:
            # Narrow compatibility for lightweight pre-v260 fakes only. The
            # real PlatformStore always has ``predictions`` and never reaches
            # this stale/carry-forward branch.
            try:
                legacy_result = public_predictions(include_research_drafts=True)
            except TypeError:
                legacy_result = public_result
            legacy_research = (
                legacy_result.get("research_predictions")
                if isinstance(legacy_result, dict)
                else None
            )
            research_predictions = (
                legacy_research
                if isinstance(legacy_research, dict)
                else {"status": "unavailable", "predictions": [], "blocked": []}
            )
    else:
        # Older lightweight stores only expose the causal research method.
        # Preserve their historical behavior for tests/tools, but do not
        # duplicate that payload into a second lane without an explicit
        # public-prediction projection.
        predictions = store.predictions()
        research_predictions = {"status": "unavailable", "predictions": [], "blocked": []}
    # Keep unmapped Sports Lottery sales rows in a separate display-only lane.
    # This expands the researcher-facing schedule without changing the
    # canonical model-competition catalog or the prospective prediction input.
    sales_matches, sales_competitions = _sales_only_matches(snapshot)
    if sales_matches:
        snapshot["matches"] = [*snapshot.get("matches", []), *sales_matches]
        snapshot["matches"].sort(key=lambda item: str(item.get("kickoff_at") or ""), reverse=True)
    if sales_competitions:
        snapshot["competitions"] = [*snapshot.get("competitions", []), *sales_competitions]
        summary = snapshot.get("summary")
        snapshot["summary"] = {
            **(dict(summary) if isinstance(summary, dict) else {}),
            "sales_only_competitions": len(sales_competitions),
            "sales_only_fixture_count": len(sales_matches),
        }
        current_data = snapshot.get("current_data")
        if isinstance(current_data, dict):
            snapshot["current_data"] = {
                **current_data,
                "sales_only_display_fixture_count": len(sales_matches),
                "sales_only_display_competition_count": len(sales_competitions),
            }
    raw_sources = _load_optional_live_sources(live_path)
    raw_replay_verified = False
    if raw_sources is not None:
        fixture_section = raw_sources.get("fixture_feed")
        admission = (
            fixture_section.get("raw_archive_admission")
            if isinstance(fixture_section, Mapping)
            else None
        )
        if isinstance(admission, Mapping) and admission.get("status") == "verified_current_raw":
            _require_openfootball_raw_replay(
                raw_sources,
                archive_root=resolved_raw_archive_dir,
            )
            raw_sources = _public_raw_source_projection(raw_sources)
            _public_snapshot_match_projection(snapshot, raw_sources)
            predictions = _public_prediction_projection(predictions)
            research_predictions = _public_prediction_projection(research_predictions)
            predictions = _bind_snapshot_prediction_provenance(
                predictions,
                snapshot=snapshot,
            )
            research_predictions = _bind_snapshot_prediction_provenance(
                research_predictions,
                snapshot=snapshot,
            )
            predictions = _quarantine_unbound_predictions(predictions)
            research_predictions = _quarantine_unbound_predictions(research_predictions)
            raw_replay_verified = True
            # OpenLigaDB is a serving-only lane.  Keep its count/status in
            # current_data, but never carry its match rows into redistribution.
            if "openligadb" in snapshot:
                snapshot.pop("openligadb", None)
                current_data = snapshot.get("current_data")
                if isinstance(current_data, dict):
                    snapshot["current_data"] = {
                        **current_data,
                        "openligadb_local_display_count": 0,
                        "openligadb_public_status": "rights_blocked",
                    }
    live_overlay = _load_optional_live_overlay(live_overlay_path)
    if live_overlay is not None:
        snapshot["live_overlay"] = live_overlay
    # Both the full payload and the compact/Sites projections are public
    # redistribution surfaces.  Preflight the exact assembled inputs before
    # report decoration, serialization, staging, or replacement.  In
    # particular, a trusted OpenFootball fixture cannot launder an ancillary
    # provider row or an official field-level schedule overlay.
    _require_offline_redistribution_rights(
        snapshot,
        predictions,
        research_predictions,
        raw_sources=raw_sources,
        raw_archive_root=resolved_raw_archive_dir,
        raw_replay_verified=raw_replay_verified,
    )
    # Rights preflight above must inspect the original rows before this
    # publication projection can drop a stale/self-reported research object.
    research_predictions = _verified_research_projection(research_predictions)
    report_leagues = report["leagues"]
    strict_status = snapshot.get("strict_backtest")
    strict_is_current = (
        isinstance(strict_status, dict) and strict_status.get("status") == "current"
    )
    if strict_is_current:
        for competition in snapshot["competitions"]:
            value = report_leagues.get(competition["id"])
            if value is not None:
                competition["strict_model_health"] = _strict_compatibility(value)
        independent_scoreline = {}
        for league_id, value in report_leagues.items():
            projected = _independent_scoreline_compatibility(value)
            if projected is not None:
                independent_scoreline[league_id] = projected
        snapshot["strict_backtest"] = {
            "status": "current",
            "generated_at": report.get("generated_at"),
            "combined": report.get("combined"),
            "overall": report.get("overall"),
            "independent_scoreline": independent_scoreline,
            "time_audit": {
                league_id: value.get("time_audit") for league_id, value in report_leagues.items()
            },
            "freeze_stages": {
                league_id: value.get("freeze_stages", {})
                for league_id, value in report_leagues.items()
            },
        }
    elif not isinstance(strict_status, dict):
        snapshot["strict_backtest"] = {
            "status": "unavailable",
            "reason": "strict_report_lock_not_verified",
        }
    audit_dir = evidence_dir or Path("docs/evidence")
    audit_path = audit_dir / "publication-audit-current.json"
    try:
        snapshot["publication_audit"] = _compact_publication_audit(
            json.loads(audit_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError):
        snapshot["publication_audit"] = _compact_publication_audit(None)
    # The identity is derived only after all source, strict-report and audit
    # projections have been assembled. It is a bundle identity, not a model
    # quality or deployment claim.
    snapshot["build_id"] = _bundle_build_id(snapshot)
    full_payload = (
        "window.__MATCHLINE_OFFLINE_SNAPSHOT__ = "
        + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        + ";\nwindow.__MATCHLINE_OFFLINE_PREDICTIONS__ = "
        + json.dumps(predictions, ensure_ascii=False, separators=(",", ":"))
        + ";\nwindow.__MATCHLINE_RESEARCH_PREDICTIONS__ = "
        + json.dumps(research_predictions, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    )
    compact_snapshot = None
    compact_payload = None
    compact_js_payload = None
    if compact_output is not None or compact_js_output is not None:
        compact_snapshot = _compact_site_snapshot(
            snapshot,
            predictions,
            research_predictions=research_predictions,
            raw_sources=raw_sources,
        )
        compact_payload = json.dumps(compact_snapshot, ensure_ascii=False, separators=(",", ":"))
    if (
        compact_js_output is not None
        and compact_snapshot is not None
        and compact_payload is not None
    ):
        compact_predictions = {
            "predictions": compact_snapshot.get("predictions", []),
            "blocked": compact_snapshot.get("blocked", []),
        }
        compact_js_payload = (
            "window.__MATCHLINE_OFFLINE_SNAPSHOT__ = "
            + compact_payload
            + ";\nwindow.__MATCHLINE_OFFLINE_PREDICTIONS__ = "
            + json.dumps(compact_predictions, ensure_ascii=False, separators=(",", ":"))
            + ";\nwindow.__MATCHLINE_RESEARCH_PREDICTIONS__ = "
            + json.dumps(
                {"predictions": compact_snapshot.get("research_predictions", []), "blocked": []},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + ";\n"
        )
    requested_outputs: list[tuple[Path, str]] = []
    if compact_output is not None and compact_payload is not None:
        requested_outputs.append((compact_output, compact_payload))
    if compact_js_output is not None and compact_js_payload is not None:
        requested_outputs.append((compact_js_output, compact_js_payload))
    requested_outputs.append((output, full_payload))
    _replace_prepared_outputs(requested_outputs)
    return {
        "output": str(output),
        "snapshot_generated_at": snapshot["generated_at"],
        "strict_sample_n": report["combined"]["sample_n"] if strict_is_current else 0,
        "strict_report_status": (
            snapshot.get("strict_backtest", {}).get("status")
            if isinstance(snapshot.get("strict_backtest"), dict)
            else "unavailable"
        ),
        "prediction_count": len(predictions.get("predictions", [])),
        "research_prediction_count": len(research_predictions.get("predictions", []))
        if isinstance(research_predictions, dict)
        and isinstance(research_predictions.get("predictions"), list)
        else 0,
        "compact_output": str(compact_output) if compact_output is not None else "",
        "compact_js_output": str(compact_js_output) if compact_js_output is not None else "",
    }


def main() -> None:
    repository_evidence_dir = Path("docs/evidence")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/MatchHistory"))
    parser.add_argument("--live-path", type=Path, default=Path("data/live/current.json"))
    parser.add_argument(
        "--strict-report",
        type=Path,
        dest="strict_report_path",
        default=repository_evidence_dir / "strict-backtest-current.json",
    )
    parser.add_argument(
        "--prospective-lock",
        type=Path,
        dest="prospective_lock_path",
        default=repository_evidence_dir / "prospective-model-lock-current.json",
        help="prospective lock paired with the selected strict report",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("league_platform/site/offline_data.js")
    )
    parser.add_argument("--compact-output", type=Path, default=None)
    parser.add_argument(
        "--compact-js-output",
        type=Path,
        default=None,
        help="optional JavaScript assignment bundle generated from the bounded Sites snapshot",
    )
    parser.add_argument(
        "--live-overlay",
        type=Path,
        dest="live_overlay_path",
        default=None,
        help="optional display-only live overlay produced by league_platform.live_overlay",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="prospective evidence directory; defaults to docs/evidence",
    )
    parser.add_argument(
        "--runtime-evidence-dir",
        type=Path,
        default=None,
        help=(
            "optional live cycle/evaluation pointer directory; model lock and "
            "selection evidence continue to come from --evidence-dir"
        ),
    )
    parser.add_argument(
        "--runtime-archive-manifest",
        type=Path,
        dest="runtime_archive_manifest_path",
        default=None,
        help="optional small manifest for the durable runtime recovery archive",
    )
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        type=Path,
        default=None,
        help=(
            "durable OpenFootball raw archive; when omitted, use only the "
            "operator-managed MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"
        ),
    )
    args = parser.parse_args()
    print(json.dumps(build_bundle(**vars(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = ["build_bundle"]
