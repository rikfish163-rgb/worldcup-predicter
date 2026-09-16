"""Run the local-to-Sites publication streams in dependency order.

The cycle is an operational wrapper, not a second data store. Fixture
identities are published first, then the append-only intelligence ledger and
audit-only source-health and lineup diagnostics, and finally frozen forecasts. A hard failure in
the fixture or intelligence prerequisites prevents forecast delivery for that
run; a lineup diagnostic failure is visible but non-blocking.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from league_platform.fixture_serving import fixture_serving_status
from league_platform.prospective_archive import validate_lock_integrity
from league_platform.publish_forecast import publication_model_version, publish_archive
from league_platform.publish_intelligence import (
    PublicationMaturityContext,
    build_publication_maturity_context,
    build_prospective_gate_metadata,
    require_publication_maturity,
    resolve_publication_epoch,
    publish_ledger,
    publish_lineup_diagnostics,
    publish_source_health_diagnostics,
)
from league_platform.publish_snapshot import publish_snapshot
from league_platform.publish_stage_evaluations import (
    publish_stage_evaluations,
    stage_evaluation_publication_status,
)
from league_platform.publish_site_manifest import (
    build_site_publication_barrier,
    build_site_publication_payload,
    publish_site_manifest,
)
from league_platform.runtime_paths import resolve_future_openfootball_raw_archive_dir


DEFAULT_SNAPSHOT = Path("data/live/current.json")
DEFAULT_LEDGER = Path("data/live/intelligence/observations.jsonl")
DEFAULT_ARCHIVE = Path("data/live/prospective_predictions.jsonl")
DEFAULT_STAGE_LOCK = Path("docs/evidence/prospective-model-lock-current.json")
DEFAULT_CYCLE_EVIDENCE = Path("docs/evidence/prospective-cycle-latest.json")
# Cloudflare D1 applies a lower bound to SQLite bind variables than the wire
# payload limits.  Keep the operational cycle below that bound while leaving
# the standalone publishers' larger limits available for non-D1 deployments.
# The Sites API splits variable-bearing statements at the D1 boundary. Keep
# the publisher transport batches large enough to avoid excessive round trips
# while retaining the API's conservative per-statement bind budget.
D1_SAFE_FIXTURE_BATCH_SIZE = 25
D1_SAFE_INTELLIGENCE_BATCH_SIZE = 25


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _aware_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _prospective_publication_identity(
    lock_path: Path,
    cycle_evidence_path: Path,
    *,
    snapshot_path: Path | None = None,
    max_age_seconds: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Validate the immutable model identity before any publication phase."""

    if not lock_path.is_file():
        return None, {
            "status": "blocked",
            "reason": "prospective_lock_not_found",
            "path": str(lock_path),
        }
    try:
        loaded_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, {
            "status": "blocked",
            "reason": "prospective_lock_invalid_json",
            "path": str(lock_path),
            "error": str(exc),
        }
    if not isinstance(loaded_lock, dict):
        return None, {
            "status": "blocked",
            "reason": "prospective_lock_invalid_shape",
            "path": str(lock_path),
        }
    try:
        validate_lock_integrity(loaded_lock)
    except (OSError, ValueError) as exc:
        return None, {
            "status": "blocked",
            "reason": "prospective_lock_integrity_failed",
            "path": str(lock_path),
            "error": str(exc),
        }
    model_version = loaded_lock.get("freeze_model_name")
    model_hash = loaded_lock.get("model_version_sha256")
    if not isinstance(model_version, str) or not model_version.strip():
        return None, {
            "status": "blocked",
            "reason": "prospective_lock_model_version_missing",
            "path": str(lock_path),
        }
    if not _is_sha256(model_hash):
        # ``validate_lock_integrity`` currently enforces the same invariant;
        # keep it explicit at this publication boundary so neither the name
        # nor digest can ever be forwarded as an optional value.
        return None, {
            "status": "blocked",
            "reason": "prospective_lock_model_hash_invalid",
            "path": str(lock_path),
        }
    normalized_model_files = [
        {"path": str(entry["path"]), "sha256": str(entry["sha256"])}
        for entry in loaded_lock["model_files"]
    ]
    aggregate_hash = hashlib.sha256(
        json.dumps(
            {
                "model_name": loaded_lock.get("model_name"),
                "freeze_model_name": loaded_lock.get("freeze_model_name"),
                "model_files": normalized_model_files,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if aggregate_hash != str(model_hash).lower():
        return None, {
            "status": "blocked",
            "reason": "prospective_lock_integrity_failed",
            "path": str(lock_path),
            "error": "prospective lock aggregate model hash mismatch",
            "declaredModelVersionSha256": str(model_hash).lower(),
            "computedModelVersionSha256": aggregate_hash,
        }

    if not cycle_evidence_path.is_file():
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_evidence_not_found",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
        }
    try:
        cycle = json.loads(cycle_evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_evidence_invalid_json",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
            "error": str(exc),
        }
    if not isinstance(cycle, dict):
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_evidence_invalid_shape",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
        }
    if cycle.get("schema_version") != "1.0.0":
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_schema_invalid",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
        }
    if cycle.get("status") not in {"pending_prospective_window", "passed"}:
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_status_not_publishable",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
            "cycleStatus": cycle.get("status"),
        }
    try:
        cycle_started_at = _aware_utc_timestamp(cycle.get("started_at"))
        cycle_finished_at = _aware_utc_timestamp(cycle.get("finished_at"))
    except (TypeError, ValueError):
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_time_invalid",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
        }
    now = datetime.now(timezone.utc)
    cycle_age_seconds = (now - cycle_finished_at).total_seconds()
    if cycle_finished_at < cycle_started_at or cycle_age_seconds < 0:
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_time_invalid",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
        }
    if max_age_seconds is not None and cycle_age_seconds > max_age_seconds:
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_stale",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
            "finishedAt": cycle_finished_at.isoformat().replace("+00:00", "Z"),
            "ageSeconds": int(cycle_age_seconds),
            "maxAgeSeconds": max_age_seconds,
        }
    artifacts = cycle.get("artifacts") if isinstance(cycle, dict) else None
    if not isinstance(artifacts, dict):
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_identity_missing",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
        }
    if snapshot_path is not None:
        if not snapshot_path.is_file():
            return None, {
                "status": "blocked",
                "reason": "prospective_cycle_snapshot_not_found",
                "path": str(lock_path),
                "cycleEvidencePath": str(cycle_evidence_path),
                "snapshotPath": str(snapshot_path),
            }
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            snapshot = None
        capture = cycle.get("capture")
        snapshot_as_of = snapshot.get("as_of") if isinstance(snapshot, dict) else None
        capture_as_of = capture.get("as_of") if isinstance(capture, dict) else None
        if (
            not isinstance(snapshot_as_of, str)
            or not snapshot_as_of.strip()
            or capture_as_of != snapshot_as_of
        ):
            return None, {
                "status": "blocked",
                "reason": "prospective_cycle_snapshot_identity_mismatch",
                "path": str(lock_path),
                "cycleEvidencePath": str(cycle_evidence_path),
                "snapshotPath": str(snapshot_path),
                "snapshotAsOf": snapshot_as_of,
                "cycleCaptureAsOf": capture_as_of,
            }
        sync = cycle.get("sync")
        if isinstance(sync, dict) and sync.get("as_of") not in (None, snapshot_as_of):
            return None, {
                "status": "blocked",
                "reason": "prospective_cycle_snapshot_identity_mismatch",
                "path": str(lock_path),
                "cycleEvidencePath": str(cycle_evidence_path),
                "snapshotPath": str(snapshot_path),
                "snapshotAsOf": snapshot_as_of,
                "cycleSyncAsOf": sync.get("as_of"),
            }
        try:
            snapshot_file_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        except OSError:
            return None, {
                "status": "blocked",
                "reason": "prospective_cycle_snapshot_identity_mismatch",
                "path": str(lock_path),
                "cycleEvidencePath": str(cycle_evidence_path),
                "snapshotPath": str(snapshot_path),
            }
        declared_snapshot_file_hashes = [
            sync.get("snapshot_sha256") if isinstance(sync, dict) else None,
            artifacts.get("live_snapshot_sha256"),
        ]
        for declared_snapshot_hash in declared_snapshot_file_hashes:
            if declared_snapshot_hash is not None and (
                not _is_sha256(declared_snapshot_hash)
                or str(declared_snapshot_hash).lower() != snapshot_file_hash
            ):
                return None, {
                    "status": "blocked",
                    "reason": "prospective_cycle_snapshot_hash_mismatch",
                    "path": str(lock_path),
                    "cycleEvidencePath": str(cycle_evidence_path),
                    "snapshotPath": str(snapshot_path),
                    "snapshotSha256": snapshot_file_hash,
                    "cycleSnapshotSha256": declared_snapshot_hash,
                }
        declared_live_snapshot = artifacts.get("live_snapshot")
        if isinstance(declared_live_snapshot, str) and declared_live_snapshot.strip():
            try:
                declared_live_path = Path(declared_live_snapshot).expanduser().resolve()
                actual_live_path = snapshot_path.expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                declared_live_path = None
                actual_live_path = None
            if declared_live_path is None or declared_live_path != actual_live_path:
                return None, {
                    "status": "blocked",
                    "reason": "prospective_cycle_snapshot_path_mismatch",
                    "path": str(lock_path),
                    "cycleEvidencePath": str(cycle_evidence_path),
                    "snapshotPath": str(snapshot_path),
                    "declaredLiveSnapshot": declared_live_snapshot,
                }
    cycle_hash = artifacts.get("model_version_sha256")
    if not _is_sha256(cycle_hash):
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_identity_missing",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
        }
    if str(cycle_hash).lower() != str(model_hash).lower():
        return None, {
            "status": "blocked",
            "reason": "prospective_cycle_model_identity_mismatch",
            "path": str(lock_path),
            "cycleEvidencePath": str(cycle_evidence_path),
            "lockModelVersionSha256": str(model_hash).lower(),
            "cycleModelVersionSha256": str(cycle_hash).lower(),
        }
    declared_lock = artifacts.get("model_lock")
    if isinstance(declared_lock, str) and declared_lock.strip():
        try:
            declared_path = Path(declared_lock).expanduser().resolve()
            actual_path = lock_path.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            declared_path = None
            actual_path = None
        if declared_path is None or declared_path != actual_path:
            return None, {
                "status": "blocked",
                "reason": "prospective_cycle_lock_path_mismatch",
                "path": str(lock_path),
                "cycleEvidencePath": str(cycle_evidence_path),
                "declaredLock": declared_lock,
            }
    normalized = dict(loaded_lock)
    normalized["freeze_model_name"] = model_version.strip()
    normalized["model_version_sha256"] = str(model_hash).lower()
    return normalized, {
        "status": "ok",
        "path": str(lock_path),
        "cycleEvidencePath": str(cycle_evidence_path),
        "modelVersion": model_version.strip(),
        "modelVersionSha256": str(model_hash).lower(),
        "cycleFinishedAt": cycle_finished_at.isoformat().replace("+00:00", "Z"),
        "cycleAgeSeconds": int(cycle_age_seconds),
    }


def _archive_publication_identity(
    cycle_evidence_path: Path,
    archive_path: Path,
) -> dict[str, Any]:
    """Bind the exact forecast archive and its evaluation before any write."""

    try:
        cycle = json.loads(cycle_evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "status": "blocked",
            "reason": "prediction_archive_cycle_evidence_invalid",
            "error": str(exc),
        }
    if not isinstance(cycle, dict) or not isinstance(cycle.get("artifacts"), dict):
        return {
            "status": "blocked",
            "reason": "prediction_archive_cycle_identity_missing",
        }
    artifacts = cycle["artifacts"]
    declared_archive = artifacts.get("prediction_archive")
    declared_archive_sha = artifacts.get("prediction_archive_sha256")
    if not isinstance(declared_archive, str) or not declared_archive.strip():
        return {
            "status": "blocked",
            "reason": "prediction_archive_cycle_identity_missing",
        }
    if not _is_sha256(declared_archive_sha):
        return {
            "status": "blocked",
            "reason": "prediction_archive_cycle_hash_missing",
        }
    try:
        actual_archive_path = archive_path.expanduser().resolve(strict=True)
        declared_archive_path = Path(declared_archive).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return {
            "status": "blocked",
            "reason": "prediction_archive_path_invalid",
        }
    if actual_archive_path != declared_archive_path:
        return {
            "status": "blocked",
            "reason": "prediction_archive_path_mismatch",
            "archivePath": str(actual_archive_path),
            "cycleArchivePath": str(declared_archive_path),
        }
    try:
        actual_archive_sha = hashlib.sha256(actual_archive_path.read_bytes()).hexdigest()
    except OSError as exc:
        return {
            "status": "blocked",
            "reason": "prediction_archive_unreadable",
            "error": str(exc),
        }
    if actual_archive_sha != str(declared_archive_sha).lower():
        return {
            "status": "blocked",
            "reason": "prediction_archive_hash_mismatch",
            "predictionArchiveSha256": actual_archive_sha,
            "cyclePredictionArchiveSha256": str(declared_archive_sha).lower(),
        }

    evaluation_path_value = artifacts.get("evaluation_evidence")
    evaluation_sha_value = artifacts.get("evaluation_evidence_sha256")
    if (
        not isinstance(evaluation_path_value, str)
        or not evaluation_path_value.strip()
        or not _is_sha256(evaluation_sha_value)
    ):
        return {
            "status": "blocked",
            "reason": "prediction_archive_evaluation_identity_missing",
        }
    try:
        evaluation_path = Path(evaluation_path_value).expanduser().resolve(strict=True)
        evaluation_bytes = evaluation_path.read_bytes()
        evaluation = json.loads(evaluation_bytes.decode("utf-8"))
    except (OSError, UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "blocked",
            "reason": "prediction_archive_evaluation_invalid",
            "error": str(exc),
        }
    evaluation_sha = hashlib.sha256(evaluation_bytes).hexdigest()
    if evaluation_sha != str(evaluation_sha_value).lower():
        return {
            "status": "blocked",
            "reason": "prediction_archive_evaluation_hash_mismatch",
        }
    if not isinstance(evaluation, dict):
        return {
            "status": "blocked",
            "reason": "prediction_archive_evaluation_invalid",
        }
    evaluation_scored_n = evaluation.get("scored_n")
    evaluation_pending_n = evaluation.get("pending_n")
    if (
        isinstance(evaluation_scored_n, bool)
        or not isinstance(evaluation_scored_n, int)
        or evaluation_scored_n < 0
        or isinstance(evaluation_pending_n, bool)
        or not isinstance(evaluation_pending_n, int)
        or evaluation_pending_n < 0
    ):
        return {
            "status": "blocked",
            "reason": "prediction_archive_evaluation_counts_invalid",
        }
    evaluation_archive = evaluation.get("prediction_archive")
    evaluation_archive_sha = evaluation.get("prediction_archive_sha256")
    try:
        evaluation_archive_path = (
            Path(evaluation_archive).expanduser().resolve(strict=True)
            if isinstance(evaluation_archive, str) and evaluation_archive.strip()
            else None
        )
    except (OSError, RuntimeError, ValueError):
        evaluation_archive_path = None
    if (
        evaluation_archive_path != actual_archive_path
        or not _is_sha256(evaluation_archive_sha)
        or str(evaluation_archive_sha).lower() != actual_archive_sha
    ):
        return {
            "status": "blocked",
            "reason": "prediction_archive_evaluation_identity_mismatch",
        }
    return {
        "status": "ok",
        "archivePath": str(actual_archive_path),
        "predictionArchiveSha256": actual_archive_sha,
        "evaluationEvidencePath": str(evaluation_path),
        "evaluationEvidenceSha256": evaluation_sha,
        "evaluationScoredN": evaluation_scored_n,
        "evaluationPendingN": evaluation_pending_n,
    }


def _phase_failed(result: object) -> bool:
    return isinstance(result, dict) and int(result.get("failed", 0) or 0) > 0


def _forecast_failed(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("skipped"))


def _stage_evaluation_failed(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("status") in {"blocked", "failed", "partial"}:
        return True
    # A dry run with no stage rows did not exercise the final publication
    # contract (usually because the isolated test/run has no scored archive).
    # Keep that visible as a partial dry run instead of reporting a green
    # end-to-end publication check.
    return result.get("status") == "dry_run" and result.get("stageRows") == 0


def _audit_diagnostic_failed(result: object) -> bool:
    return isinstance(result, dict) and (
        result.get("status") in {"failed", "partial"} or int(result.get("failed", 0) or 0) > 0
    )


def _snapshot_freshness(snapshot_path: Path, max_age_seconds: int) -> dict[str, object]:
    """Return a fail-closed freshness result for a live publication snapshot."""

    if max_age_seconds <= 0:
        raise ValueError("max_snapshot_age_seconds must be positive")
    if not snapshot_path.exists():
        return {"status": "blocked", "reason": "snapshot_not_found"}
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "blocked", "reason": "snapshot_invalid_json"}
    if not isinstance(raw, dict):
        return {"status": "blocked", "reason": "snapshot_not_object"}
    as_of = raw.get("as_of")
    if not isinstance(as_of, str) or not as_of.strip():
        return {"status": "blocked", "reason": "snapshot_as_of_missing"}
    try:
        observed_at = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return {"status": "blocked", "reason": "snapshot_as_of_invalid"}
    if observed_at.tzinfo is None:
        return {"status": "blocked", "reason": "snapshot_as_of_timezone_missing"}
    observed_at = observed_at.astimezone(timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - observed_at).total_seconds()
    if age_seconds < 0:
        return {
            "status": "blocked",
            "reason": "snapshot_as_of_in_future",
            "asOf": observed_at.isoformat().replace("+00:00", "Z"),
        }
    if age_seconds > max_age_seconds:
        return {
            "status": "blocked",
            "reason": "snapshot_stale",
            "asOf": observed_at.isoformat().replace("+00:00", "Z"),
            "ageSeconds": int(age_seconds),
            "maxAgeSeconds": max_age_seconds,
        }
    return {
        "status": "ok",
        "asOf": observed_at.isoformat().replace("+00:00", "Z"),
        "ageSeconds": int(age_seconds),
        "maxAgeSeconds": max_age_seconds,
    }


def _snapshot_canonical_sha256(snapshot: dict) -> str:
    """Match the immutable snapshot digest written by ``sync_live``."""

    raw = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _snapshot_publication(
    snapshot_path: Path,
    *,
    required: bool,
) -> dict[str, object]:
    """Verify that the publish input is the latest atomically published snapshot.

    The prospective producer writes ``current.publication.json`` beside
    ``current.json``.  Sites publication can run from a different runtime
    directory, so freshness alone is insufficient: a recent but unrelated
    snapshot must not be sent to D1.  The check is opt-in for library callers
    and required by the production systemd unit.
    """

    diagnostic_path = snapshot_path.with_name(f"{snapshot_path.stem}.publication.json")
    if not diagnostic_path.exists():
        return {
            "status": "blocked" if required else "not_checked",
            "reason": "publication_diagnostic_not_found",
            "path": str(diagnostic_path),
        }
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "status": "blocked",
            "reason": "publication_diagnostic_invalid_json",
            "path": str(diagnostic_path),
        }
    if not isinstance(snapshot, dict) or not isinstance(diagnostic, dict):
        return {
            "status": "blocked",
            "reason": "publication_diagnostic_invalid_shape",
            "path": str(diagnostic_path),
        }
    published = diagnostic.get("published")
    if diagnostic.get("status") != "published" or not isinstance(published, dict):
        return {
            "status": "blocked",
            "reason": "publication_diagnostic_not_published",
            "path": str(diagnostic_path),
            "diagnosticStatus": diagnostic.get("status"),
        }
    declared_output = published.get("output") or diagnostic.get("output")
    if not isinstance(declared_output, str) or not declared_output.strip():
        return {
            "status": "blocked",
            "reason": "publication_output_missing",
            "path": str(diagnostic_path),
        }
    try:
        output_matches = (
            Path(declared_output).expanduser().resolve() == snapshot_path.expanduser().resolve()
        )
    except (OSError, RuntimeError, ValueError):
        output_matches = False
    if not output_matches:
        return {
            "status": "blocked",
            "reason": "publication_output_mismatch",
            "path": str(diagnostic_path),
            "declaredOutput": declared_output,
            "snapshot": str(snapshot_path),
        }
    expected_as_of = snapshot.get("as_of")
    if published.get("as_of") != expected_as_of:
        return {
            "status": "blocked",
            "reason": "publication_as_of_mismatch",
            "path": str(diagnostic_path),
            "publishedAsOf": published.get("as_of"),
            "snapshotAsOf": expected_as_of,
        }
    try:
        expected_digest = _snapshot_canonical_sha256(snapshot)
    except (TypeError, ValueError):
        return {
            "status": "blocked",
            "reason": "publication_snapshot_not_canonical",
            "path": str(diagnostic_path),
        }
    if published.get("content_sha256") != expected_digest:
        return {
            "status": "blocked",
            "reason": "publication_content_hash_mismatch",
            "path": str(diagnostic_path),
            "publishedContentSha256": published.get("content_sha256"),
            "snapshotContentSha256": expected_digest,
        }
    serving = fixture_serving_status(snapshot)
    if serving.get("status") != "ok":
        return {
            "status": "blocked",
            "reason": serving.get("reason", "fixture_serving_not_authorized"),
            "path": str(diagnostic_path),
            "output": str(snapshot_path),
            "fixtureServing": serving,
        }
    return {
        "status": "ok",
        "path": str(diagnostic_path),
        "output": str(snapshot_path),
        "asOf": expected_as_of,
        "contentSha256": expected_digest,
        "fixtureCount": published.get("fixture_count"),
        "fixtureServing": serving,
    }


def publish_cycle(
    *,
    snapshot_path: Path = DEFAULT_SNAPSHOT,
    ledger_path: Path = DEFAULT_LEDGER,
    archive_path: Path = DEFAULT_ARCHIVE,
    fixtures_endpoint: str = "",
    intelligence_endpoint: str = "",
    forecast_register_endpoint: str = "",
    forecast_endpoint: str = "",
    stage_evaluations_endpoint: str = "",
    publication_endpoint: str = "",
    token: str = "",
    publication_epoch: str | None = None,
    maturity_context: PublicationMaturityContext | None = None,
    dry_run: bool = False,
    fixtures_receipts: Path | None = None,
    intelligence_receipts: Path | None = None,
    source_health_receipts: Path | None = None,
    lineup_diagnostics_receipts: Path | None = None,
    forecast_receipts: Path | None = None,
    stage_evaluations_receipts: Path | None = None,
    stage_lock_path: Path = DEFAULT_STAGE_LOCK,
    cycle_evidence_path: Path = DEFAULT_CYCLE_EVIDENCE,
    openfootball_raw_archive_dir: Path | str | None = None,
    limit: int | None = None,
    max_snapshot_age_seconds: int | None = None,
    require_publication_diagnostic: bool = False,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
    fixture_batch_size: int = D1_SAFE_FIXTURE_BATCH_SIZE,
    intelligence_batch_size: int = D1_SAFE_INTELLIGENCE_BATCH_SIZE,
    publish_snapshot_fn: Callable[..., dict] = publish_snapshot,
    publish_ledger_fn: Callable[..., dict] = publish_ledger,
    publish_source_health_diagnostics_fn: Callable[..., dict] = publish_source_health_diagnostics,
    publish_lineup_diagnostics_fn: Callable[..., dict] = publish_lineup_diagnostics,
    publish_archive_fn: Callable[..., dict] = publish_archive,
    publish_stage_evaluations_fn: Callable[..., dict] = publish_stage_evaluations,
    publish_site_manifest_fn: Callable[..., dict] = publish_site_manifest,
) -> dict:
    """Publish each stream and return bounded phase summaries."""

    if max_snapshot_age_seconds is not None and max_snapshot_age_seconds <= 0:
        raise ValueError("max_snapshot_age_seconds must be positive")
    epoch = resolve_publication_epoch(publication_epoch, dry_run=dry_run)
    stage_lock, prospective_identity = _prospective_publication_identity(
        stage_lock_path,
        cycle_evidence_path,
        snapshot_path=snapshot_path,
        max_age_seconds=max_snapshot_age_seconds,
    )
    if stage_lock is None:
        return {
            "status": "blocked",
            "reason": prospective_identity["reason"],
            "prospectiveLock": prospective_identity,
            "snapshotFreshness": {"status": "not_checked"},
            "snapshotPublication": {"status": "not_checked"},
            "phases": {},
        }

    archive_identity = (
        {"status": "not_checked"}
        if dry_run
        else _archive_publication_identity(cycle_evidence_path, archive_path)
    )
    if archive_identity.get("status") == "blocked":
        return {
            "status": "blocked",
            "reason": archive_identity["reason"],
            "prospectiveLock": prospective_identity,
            "predictionArchive": archive_identity,
            "snapshotFreshness": {"status": "not_checked"},
            "snapshotPublication": {"status": "not_checked"},
            "phases": {},
        }
    stage_evaluations_not_applicable = (
        not dry_run and archive_identity.get("evaluationScoredN") == 0
    )
    stage_publication = (
        {
            "status": "not_applicable",
            "reason": "no_scored_stage_rows",
        }
        if stage_evaluations_not_applicable
        else stage_evaluation_publication_status()
    )
    if (
        not dry_run
        and not stage_evaluations_not_applicable
        and stage_publication.get("status") != "ok"
    ):
        return {
            "status": "blocked",
            "reason": stage_publication.get(
                "reason",
                "stage_evaluation_publication_unavailable",
            ),
            "stageEvaluationPublication": stage_publication,
            "prospectiveLock": prospective_identity,
            "predictionArchive": archive_identity,
            "snapshotFreshness": {"status": "not_checked"},
            "snapshotPublication": {"status": "not_checked"},
            "phases": {},
        }
    maturity = require_publication_maturity(maturity_context, dry_run=dry_run)

    required_endpoints = (
        fixtures_endpoint,
        intelligence_endpoint,
        forecast_register_endpoint,
        forecast_endpoint,
        publication_endpoint,
        token,
    )
    if not dry_run and (
        not all(required_endpoints)
        or (not stage_evaluations_not_applicable and not stage_evaluations_endpoint)
    ):
        raise ValueError(
            "all required publication endpoints and MATCHLINE_INGEST_TOKEN are required"
        )
    if not dry_run and (max_snapshot_age_seconds is None or not require_publication_diagnostic):
        raise ValueError(
            "non-dry-run publication requires max_snapshot_age_seconds and publication diagnostic"
        )

    snapshot_freshness = (
        _snapshot_freshness(snapshot_path, max_snapshot_age_seconds)
        if max_snapshot_age_seconds is not None
        else {"status": "not_checked"}
    )
    snapshot_publication = _snapshot_publication(
        snapshot_path,
        required=require_publication_diagnostic,
    )
    if snapshot_freshness.get("status") == "blocked":
        return {
            "status": "blocked",
            "reason": snapshot_freshness.get("reason", "snapshot_not_ready"),
            "prospectiveLock": prospective_identity,
            "snapshotFreshness": snapshot_freshness,
            "snapshotPublication": snapshot_publication,
            "phases": {},
        }
    if snapshot_publication.get("status") == "blocked":
        return {
            "status": "blocked",
            "reason": snapshot_publication.get("reason", "snapshot_publication_not_ready"),
            "prospectiveLock": prospective_identity,
            "snapshotFreshness": snapshot_freshness,
            "snapshotPublication": snapshot_publication,
            "phases": {},
        }

    result: dict = {
        "status": "dry_run" if dry_run else "ok",
        "prospectiveLock": prospective_identity,
        "predictionArchive": archive_identity,
        "publicationMaturity": maturity,
        "snapshotFreshness": snapshot_freshness,
        "snapshotPublication": snapshot_publication,
        "phases": {},
        "stageEvaluationPublication": stage_publication,
    }
    raw_archive_kwargs = (
        {"openfootball_raw_archive_dir": openfootball_raw_archive_dir}
        if openfootball_raw_archive_dir is not None
        else {}
    )
    locked_model_version = str(stage_lock["freeze_model_name"])
    locked_model_version_sha256 = str(stage_lock["model_version_sha256"])
    try:
        snapshot_value = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot_value, dict):
            raise ValueError("snapshot root is not an object")
        source_as_of = snapshot_value.get("as_of")
        if not isinstance(source_as_of, str):
            raise ValueError("snapshot as_of is missing")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            **result,
            "status": "blocked",
            "reason": "publication_snapshot_identity_invalid",
            "publicationBarrier": {
                "status": "blocked",
                "reason": str(exc),
            },
        }

    if dry_run:
        publication_barrier = {
            "status": "not_applicable",
            "reason": "dry_run",
            "published": 0,
            "requests": 0,
        }
    else:
        try:
            barrier_payload = build_site_publication_barrier(
                publication_epoch=epoch,
                source_as_of=source_as_of,
                model_version=publication_model_version(
                    locked_model_version,
                    locked_model_version_sha256,
                ),
                model_lock_sha256=locked_model_version_sha256,
            )
            publication_barrier = publish_site_manifest_fn(
                barrier_payload,
                endpoint=publication_endpoint,
                token=token,
                publication_epoch=epoch,
                maturity_context=maturity_context,
                dry_run=False,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
            )
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            publication_barrier = {
                "status": "failed",
                "published": 0,
                "requests": 0,
                "error": str(exc),
            }
        if publication_barrier.get("status") != "ok":
            return {
                **result,
                "status": "blocked",
                "reason": "publication_barrier_failed",
                "publicationBarrier": publication_barrier,
            }
    result["publicationBarrier"] = publication_barrier
    prospective_min_observed_at = (
        stage_lock.get("evaluation_window_started_at")
        if isinstance(stage_lock.get("evaluation_window_started_at"), str)
        else None
    )
    prospective_gate_metadata = build_prospective_gate_metadata(
        lock_path=stage_lock_path,
        evaluation_path=Path("docs/evidence/prospective-evaluation-current.json"),
        selection_path=Path("docs/evidence/model-selection-audit-current.json"),
        strict_report_path=Path("docs/evidence/strict-backtest-current.json"),
        cycle_path=cycle_evidence_path,
    )
    try:
        fixtures = publish_snapshot_fn(
            snapshot_path,
            endpoint=fixtures_endpoint or "https://invalid.example/api/fixtures/register",
            token=token,
            receipts_path=fixtures_receipts,
            publication_epoch=epoch,
            maturity_context=maturity_context,
            dry_run=dry_run,
            batch_size=fixture_batch_size,
            limit=limit,
            timeout=timeout,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            **raw_archive_kwargs,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        fixtures = {"status": "failed", "failed": 1, "error": str(exc)}
    result["phases"]["fixtures"] = fixtures

    try:
        intelligence = publish_ledger_fn(
            ledger_path,
            snapshot_path=snapshot_path,
            endpoint=intelligence_endpoint or "https://invalid.example/api/ingest",
            token=token,
            receipts_path=intelligence_receipts,
            publication_epoch=epoch,
            maturity_context=maturity_context,
            dry_run=dry_run,
            batch_size=intelligence_batch_size,
            limit=limit,
            min_observed_at=prospective_min_observed_at,
            timeout=timeout,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            **raw_archive_kwargs,
        )
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        intelligence = {"status": "failed", "failed": 1, "error": str(exc)}
    result["phases"]["intelligence"] = intelligence

    # The registry is an audit stream for every configured/quarantined source,
    # including providers with zero observations. It is intentionally not a
    # forecast prerequisite; one D1/source failure is isolated per provider.
    if not snapshot_path.exists():
        source_health = {
            "status": "skipped",
            "requests": 0,
            "reason": "snapshot_not_found",
        }
    else:
        try:
            source_health = publish_source_health_diagnostics_fn(
                snapshot_path,
                endpoint=intelligence_endpoint or "https://invalid.example/api/ingest",
                token=token,
                receipts_path=source_health_receipts,
                publication_epoch=epoch,
                maturity_context=maturity_context,
                dry_run=dry_run,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
                prospective_gate_metadata=prospective_gate_metadata,
            )
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            source_health = {"status": "failed", "failed": 1, "requests": 0, "error": str(exc)}
    result["phases"]["source_health_diagnostics"] = source_health

    # Lineup polling is an audit/display stream.  It is published before the
    # forecast so researchers can see the coverage state from the same cycle,
    # but a missing or blocked diagnostic must never block a forecast that has
    # already passed the fixture/intelligence provenance gate.
    if not snapshot_path.exists():
        lineup_diagnostics = {
            "status": "skipped",
            "requests": 0,
            "reason": "snapshot_not_found",
        }
    else:
        try:
            lineup_diagnostics = publish_lineup_diagnostics_fn(
                snapshot_path,
                endpoint=intelligence_endpoint or "https://invalid.example/api/ingest",
                token=token,
                receipts_path=lineup_diagnostics_receipts,
                publication_epoch=epoch,
                maturity_context=maturity_context,
                dry_run=dry_run,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
            )
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            lineup_diagnostics = {
                "status": "failed",
                "failed": 1,
                "requests": 0,
                "error": str(exc),
            }
    result["phases"]["lineup_diagnostics"] = lineup_diagnostics

    # A forecast is only useful on Sites when both its canonical fixture and
    # the supporting append-only intelligence ledger reached D1.  Publishing
    # it after a hard ledger failure would leave a prediction that cannot be
    # traced back to the evidence visible to researchers.
    if _phase_failed(fixtures) or _phase_failed(intelligence):
        forecast = {
            "status": "blocked",
            "published": 0,
            "skipped": [
                {
                    "reason": "fixture_publication_failed"
                    if _phase_failed(fixtures)
                    else "intelligence_publication_failed"
                }
            ],
            "requests": 0,
        }
    else:
        try:
            forecast_kwargs = {
                "register_endpoint": forecast_register_endpoint
                or "https://invalid.example/api/forecast/register",
                "forecast_endpoint": forecast_endpoint or "https://invalid.example/api/forecast",
                "token": token,
                "model_version": locked_model_version,
                "model_version_sha256": locked_model_version_sha256,
                "limit": limit,
                "dry_run": dry_run,
                "receipts_path": forecast_receipts,
                "publication_epoch": epoch,
                "maturity_context": maturity_context,
                "cycle_evidence_path": cycle_evidence_path,
                "timeout": timeout,
                "max_retries": max_retries,
                "retry_base_seconds": retry_base_seconds,
                **raw_archive_kwargs,
            }
            forecast = publish_archive_fn(archive_path, snapshot_path, **forecast_kwargs)
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            forecast = {
                "status": "failed",
                "published": 0,
                "skipped": [{"reason": str(exc)}],
                "requests": 0,
            }
    result["phases"]["forecast"] = forecast

    if stage_evaluations_not_applicable:
        stage_evaluations = {
            "status": "ok",
            "stageRows": 0,
            "published": 0,
            "requests": 0,
            "notApplicable": True,
            "reason": "no_scored_stage_rows",
        }
    elif _phase_failed(fixtures) or _phase_failed(intelligence) or _forecast_failed(forecast):
        stage_evaluations = {
            "status": "blocked",
            "stageRows": 0,
            "reason": "forecast_publication_failed",
        }
    elif stage_publication.get("status") != "ok":
        stage_evaluations = {
            "status": "blocked",
            "stageRows": 0,
            "reason": stage_publication.get("reason", "stage_evaluation_publication_unavailable"),
        }
    else:
        try:
            stage_evaluations = publish_stage_evaluations_fn(
                archive_path,
                snapshot_path.parent / "archive",
                endpoint=stage_evaluations_endpoint
                or "https://invalid.example/api/stage-evaluations",
                token=token,
                lock=stage_lock,
                receipts_path=stage_evaluations_receipts,
                publication_epoch=epoch,
                maturity_context=maturity_context,
                dry_run=dry_run,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
            )
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            stage_evaluations = {"status": "failed", "stageRows": 0, "error": str(exc)}
    result["phases"]["stage_evaluations"] = stage_evaluations

    if (
        _phase_failed(fixtures)
        or _phase_failed(intelligence)
        or _audit_diagnostic_failed(source_health)
        or _audit_diagnostic_failed(lineup_diagnostics)
        or _forecast_failed(forecast)
        or _stage_evaluation_failed(stage_evaluations)
    ):
        result["status"] = "partial" if not dry_run else "dry_run_partial"
    elif isinstance(intelligence, dict) and intelligence.get("status") in {
        "partial_integrity",
        "dry_run_partial_integrity",
    }:
        result["status"] = "dry_run_partial_integrity" if dry_run else "partial_integrity"

    if result["status"] not in {"ok", "dry_run"}:
        result["phases"]["publication_manifest"] = {
            "status": "blocked",
            "published": 0,
            "requests": 0,
            "reason": "publication_phases_incomplete",
        }
        return result

    try:
        manifest_payload = build_site_publication_payload(
            publication_epoch=epoch,
            source_as_of=source_as_of,
            model_version=publication_model_version(
                locked_model_version,
                locked_model_version_sha256,
            ),
            model_lock_sha256=locked_model_version_sha256,
            fixtures=fixtures,
            intelligence=intelligence,
            source_health=source_health,
            lineups=lineup_diagnostics,
            forecast=forecast,
            stage_evaluations=stage_evaluations,
        )
        publication_manifest = publish_site_manifest_fn(
            manifest_payload,
            endpoint=(publication_endpoint or "https://invalid.example/api/publication/register"),
            token=token,
            publication_epoch=epoch,
            maturity_context=maturity_context,
            dry_run=dry_run,
            timeout=timeout,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        publication_manifest = {
            "status": "failed",
            "published": 0,
            "requests": 0,
            "error": str(exc),
        }
    result["phases"]["publication_manifest"] = publication_manifest
    if publication_manifest.get("status") not in {"ok", "dry_run"}:
        result["status"] = "partial" if not dry_run else "dry_run_partial"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime_root_default = (
        Path(os.environ["MATCHLINE_RUNTIME_DIR"])
        if os.environ.get("MATCHLINE_RUNTIME_DIR")
        else None
    )
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--publication-epoch",
        default=os.environ.get("MATCHLINE_PUBLICATION_EPOCH"),
    )
    parser.add_argument("--fixtures-receipts", type=Path)
    parser.add_argument("--intelligence-receipts", type=Path)
    parser.add_argument("--source-health-receipts", type=Path)
    parser.add_argument("--lineup-diagnostics-receipts", type=Path)
    parser.add_argument("--forecast-receipts", type=Path)
    parser.add_argument("--stage-evaluations-receipts", type=Path)
    parser.add_argument("--stage-lock", type=Path, default=DEFAULT_STAGE_LOCK)
    parser.add_argument(
        "--cycle-evidence",
        type=Path,
        default=DEFAULT_CYCLE_EVIDENCE,
        help="latest prospective cycle evidence bound to the active model lock",
    )
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        type=Path,
        help="durable OpenFootball raw archive used to verify publication rows",
    )
    parser.add_argument("--max-snapshot-age-seconds", type=int)
    parser.add_argument("--maturity-receipt", type=Path)
    parser.add_argument("--maturity-runtime-root", type=Path, default=runtime_root_default)
    parser.add_argument("--maturity-platform-verification", type=Path)
    parser.add_argument("--maturity-strict-report", type=Path)
    parser.add_argument("--maturity-evaluation", type=Path)
    parser.add_argument("--maturity-offline-snapshot", type=Path)
    parser.add_argument("--maturity-sites-build-evidence", type=Path)
    parser.add_argument("--maturity-test-evidence", type=Path)
    parser.add_argument("--maturity-publication-audit", type=Path)
    parser.add_argument("--maturity-prediction-archive", type=Path)
    parser.add_argument(
        "--maturity-repository-root",
        type=Path,
        default=Path.cwd(),
    )
    parser.add_argument(
        "--require-publication-diagnostic",
        action="store_true",
        help="fail closed unless current.json matches its atomic publication diagnostic",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-base-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    fixtures_endpoint = os.environ.get("MATCHLINE_FIXTURES_REGISTER_URL", "")
    intelligence_endpoint = os.environ.get("MATCHLINE_INGEST_URL", "")
    forecast_register_endpoint = os.environ.get("MATCHLINE_FORECAST_REGISTER_URL", "")
    forecast_endpoint = os.environ.get("MATCHLINE_FORECAST_URL", "")
    stage_evaluations_endpoint = os.environ.get("MATCHLINE_STAGE_EVALUATIONS_URL", "")
    publication_endpoint = os.environ.get("MATCHLINE_PUBLICATION_REGISTER_URL", "")
    token = os.environ.get("MATCHLINE_INGEST_TOKEN", "")
    try:
        maturity_context = None
        maturity_inputs = (
            args.maturity_receipt,
            args.maturity_runtime_root,
            args.maturity_platform_verification,
            args.maturity_strict_report,
            args.maturity_evaluation,
            args.maturity_offline_snapshot,
            args.maturity_sites_build_evidence,
            args.maturity_test_evidence,
            args.maturity_publication_audit,
            args.maturity_prediction_archive,
        )
        if all(value is not None for value in maturity_inputs):
            if args.maturity_prediction_archive.resolve() != args.archive.resolve():
                raise ValueError("maturity prediction archive must match publication archive")
            maturity_context = build_publication_maturity_context(
                receipt_path=args.maturity_receipt,
                platform_verification_path=args.maturity_platform_verification,
                strict_report_path=args.maturity_strict_report,
                prospective_lock_path=args.stage_lock,
                cycle_path=args.cycle_evidence,
                evaluation_path=args.maturity_evaluation,
                live_snapshot_path=args.snapshot,
                offline_snapshot_path=args.maturity_offline_snapshot,
                sites_build_evidence_path=args.maturity_sites_build_evidence,
                test_evidence_path=args.maturity_test_evidence,
                publication_audit_path=args.maturity_publication_audit,
                prediction_archive_path=args.maturity_prediction_archive,
                publication_diagnostic_path=args.snapshot.with_name("current.publication.json"),
                runtime_root=args.maturity_runtime_root,
                repository_root=args.maturity_repository_root,
            )
        elif not args.dry_run:
            raise ValueError("complete maturity artifact paths are required")
        result = publish_cycle(
            snapshot_path=args.snapshot,
            ledger_path=args.ledger,
            archive_path=args.archive,
            fixtures_endpoint=fixtures_endpoint,
            intelligence_endpoint=intelligence_endpoint,
            forecast_register_endpoint=forecast_register_endpoint,
            forecast_endpoint=forecast_endpoint,
            stage_evaluations_endpoint=stage_evaluations_endpoint,
            publication_endpoint=publication_endpoint,
            token=token,
            publication_epoch=args.publication_epoch,
            maturity_context=maturity_context,
            dry_run=args.dry_run,
            fixtures_receipts=args.fixtures_receipts,
            intelligence_receipts=args.intelligence_receipts,
            source_health_receipts=args.source_health_receipts,
            lineup_diagnostics_receipts=args.lineup_diagnostics_receipts,
            forecast_receipts=args.forecast_receipts,
            stage_evaluations_receipts=args.stage_evaluations_receipts,
            stage_lock_path=args.stage_lock,
            cycle_evidence_path=args.cycle_evidence,
            openfootball_raw_archive_dir=resolve_future_openfootball_raw_archive_dir(
                args.openfootball_raw_archive_dir
            ),
            limit=args.limit,
            max_snapshot_age_seconds=args.max_snapshot_age_seconds,
            require_publication_diagnostic=args.require_publication_diagnostic,
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_base_seconds=args.retry_base_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"publication cycle failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] in {"ok", "dry_run", "partial_integrity"} else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["publish_cycle"]
