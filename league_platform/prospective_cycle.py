"""Run one causal Matchline prospective data-production cycle.

The cycle is deliberately orchestration-only: it does not alter model
parameters, bypass a source, or turn a failed stage into a success.  A single
exclusive lock serializes sync, prospective capture and evaluation so that a
stale or half-written snapshot cannot be scored by a concurrent run.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from league_platform.capture_prospective import capture
from league_platform.fixture_feed import fixture_rows
from league_platform.prospective_evaluation import (
    evaluate_archive,
    require_durable_openfootball_raw_archive,
    resolve_openfootball_raw_archive_dir,
)
from league_platform.prospective_market_audit import append_market_baselines
from league_platform.prospective_archive import validate_lock_integrity
from league_platform.prospective_lock import (
    assert_lock_identities_match,
    lock_identity_fingerprint,
    read_validated_lock,
    require_lock_path,
)
from league_platform.runtime_paths import DEFAULT_RUNTIME_DIR, resolve_runtime_paths
from league_platform.sync_live import sync
from league_platform.oddstorm_history_archive import capture as capture_oddstorm_history


DEFAULT_LIVE_PATH = Path("data/live/current.json")
DEFAULT_ARCHIVE_DIR = Path("data/live/archive")
DEFAULT_INTELLIGENCE_LEDGER = Path("data/live/intelligence/observations.jsonl")
DEFAULT_PREDICTION_ARCHIVE = Path("data/live/prospective_predictions.jsonl")
DEFAULT_LOCK_PATH = Path("data/live/prospective-cycle.lock")
DEFAULT_EVIDENCE_PATH = Path("docs/evidence/prospective-cycle-latest.json")
DEFAULT_EVALUATION_EVIDENCE_PATH = Path("docs/evidence/prospective-evaluation-current.json")
DEFAULT_HISTORY_PATH = Path("data/live/prospective-cycle.jsonl")
# A manually invoked runtime-only lane may run while the strict systemd lane
# is still active.  Keep its receipts and progress checkpoint separate from
# the strict lane so a later blocked strict run cannot overwrite evidence for
# the data-only run.  Shared live/archive/ledger writes remain serialized by
# the normal runtime cycle lock below.
DEFAULT_RUNTIME_ONLY_EVIDENCE_NAME = "runtime-only-cycle-latest.json"
DEFAULT_RUNTIME_ONLY_EVALUATION_NAME = "runtime-only-evaluation-current.json"
DEFAULT_RUNTIME_ONLY_HISTORY_NAME = "runtime-only-cycle.jsonl"
DEFAULT_RUNTIME_ONLY_PROGRESS_NAME = "runtime-only-cycle-progress.json"
DEFAULT_MODEL_LOCK = Path("docs/evidence/prospective-model-lock-current.json")
DEFAULT_MARKET_AUDIT_OUTPUT = Path("data/live/prospective_market_baselines.jsonl")
DEFAULT_ODDSTORM_HISTORY_OUTPUT = Path("data/live/oddstorm_history.jsonl")
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024

_FREEZE_OFFSETS = (
    ("t_minus_24h", timedelta(hours=24)),
    ("t_minus_6h", timedelta(hours=6)),
    ("t_minus_90m", timedelta(minutes=90)),
)


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _storage_guard(
    paths: list[Path],
    *,
    min_free_bytes: int,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    """Check every filesystem touched by a cycle before network work starts.

    A full filesystem can fail after a source response has already been
    fetched, leaving no room for the atomic evidence file that explains the
    failure.  This guard keeps the cycle fail-closed and explicit while there
    is still enough space to write a small blocked report.  Symlinked paths
    (such as the external live archive) are checked through their target.
    """

    required = max(0, int(min_free_bytes))
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            resolved = str(target.resolve(strict=False))
        except OSError:
            resolved = str(target)
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            usage = disk_usage_fn(target)
            free_bytes = int(usage.free)
            checks.append(
                {
                    "path": str(path),
                    "filesystem": resolved,
                    "free_bytes": free_bytes,
                    "required_free_bytes": required,
                    "status": "ok" if free_bytes >= required else "blocked",
                }
            )
        except OSError as exc:
            checks.append(
                {
                    "path": str(path),
                    "filesystem": resolved,
                    "free_bytes": None,
                    "required_free_bytes": required,
                    "status": "blocked",
                    "reason": "path_unavailable",
                    "error": type(exc).__name__,
                }
            )
    blocked = [item for item in checks if item.get("status") != "ok"]
    return {
        "status": "blocked" if blocked else "ok",
        "required_free_bytes": required,
        "checked": checks,
        "blocked_paths": [item["path"] for item in blocked],
    }


def _assert_runtime_only_writes(
    runtime_root: Path,
    paths: list[Path],
) -> None:
    """Keep data-only writes on the declared external runtime mount.

    The migrated runtime intentionally uses sibling-directory symlinks for
    some append-only archives.  Validate the lexical path stays below the
    declared runtime root, then allow its resolved target only within the
    same external mount directory; a symlink back into the project tree is
    still rejected.
    """

    root = runtime_root.resolve(strict=False)
    lexical_root = runtime_root.absolute()
    mount_root = root.parent
    violations: list[str] = []
    for path in paths:
        lexical = path.absolute()
        resolved = path.resolve(strict=False)
        try:
            lexical.relative_to(lexical_root)
            resolved.relative_to(mount_root)
        except ValueError:
            violations.append(str(path))
    if violations:
        raise ValueError(
            "runtime-only cycle requires every writable path below runtime root: "
            + ", ".join(violations[:8])
        )


def _bind_lock_paths(
    lock_path: Path | str | None,
    approved_lock_path: Path | str | None,
    *,
    runtime_only: bool,
) -> tuple[Path, Path | None]:
    """Bind the cycle to one explicit lock identity before mutable stages.

    The runtime-only lane is an isolated candidate lane and therefore accepts
    one explicit candidate path without an approval target.  A formal lane is
    different: it must receive an explicit approved path and may only operate
    when the input path is that very same resolved file.  This prevents a
    candidate at another path from being treated as an approved lock merely
    because its semantic fingerprint happens to match.
    """

    if lock_path is None:
        if runtime_only:
            raise ValueError("runtime-only cycle requires an explicit candidate lock path")
        lock_path = DEFAULT_MODEL_LOCK
    candidate = require_lock_path(lock_path, field="lock_path", must_exist=True)
    if runtime_only:
        return candidate, None
    if approved_lock_path is None:
        raise ValueError("formal cycle requires an explicit approved lock path")
    approved = require_lock_path(
        approved_lock_path,
        field="approved_lock_path",
        must_exist=True,
    )
    if candidate.resolve(strict=True) != approved.resolve(strict=True):
        raise ValueError(
            "formal cycle candidate lock_path must resolve to approved_lock_path; "
            "an isolated candidate cannot be promoted"
        )
    return candidate, approved


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _next_freeze_metadata(
    snapshot: Mapping[str, Any],
    *,
    competition_id: str,
    reference: datetime,
) -> dict[str, str]:
    """Return the next observed-clock opportunity for a blocked competition.

    This is orchestration diagnostics only.  It reads the already validated
    fixture snapshot and never changes model features or eligibility.
    """

    reference = reference.astimezone(timezone.utc)
    fixtures = fixture_rows(snapshot) if isinstance(snapshot, Mapping) else []
    candidates: list[tuple[datetime, datetime, str, str]] = []
    for fixture in fixtures:
        if not isinstance(fixture, Mapping):
            continue
        if str(fixture.get("competition_id")) != competition_id:
            continue
        if str(fixture.get("status")) != "upcoming":
            continue
        kickoff = _parse_utc(fixture.get("kickoff_at"))
        fixture_id = fixture.get("id")
        if kickoff is None or not isinstance(fixture_id, str) or kickoff <= reference:
            continue
        for stage, offset in _FREEZE_OFFSETS:
            cutoff = kickoff - offset
            if cutoff < reference or cutoff >= kickoff:
                continue
            candidates.append((cutoff, kickoff, fixture_id, stage))
    if not candidates:
        return {}
    cutoff, kickoff, fixture_id, stage = min(
        candidates,
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )
    return {
        "next_fixture_id": fixture_id,
        "next_kickoff_at": kickoff.isoformat(),
        "next_freeze_stage": stage,
        "next_freeze_cutoff_at": cutoff.isoformat(),
    }


def _enrich_capture_blocked_diagnostics(
    captured: dict[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach next-freeze hints to capture diagnostics without changing inputs."""

    diagnostics = captured.get("blocked_diagnostics")
    reference = _parse_utc(captured.get("as_of"))
    if not isinstance(diagnostics, dict) or reference is None:
        return captured
    items = diagnostics.get("items")
    if not isinstance(items, list):
        return captured
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("reason") != "no_freeze_cutoff_observed_before_as_of":
            continue
        competition_id = item.get("competition_id")
        if not isinstance(competition_id, str):
            continue
        item.update(
            _next_freeze_metadata(
                snapshot,
                competition_id=competition_id,
                reference=reference,
            )
        )
    return captured


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except Exception:
        # Preserve the previous artifact and remove only this failed writer's
        # partial file.  The original exception remains the cycle failure.
        temporary.unlink(missing_ok=True)
        raise


def _write_stage_progress(
    path: Path,
    *,
    cycle_started_at: str,
    stage: str,
    status: str = "running",
    stage_started_at: str | None = None,
    failure: Mapping[str, Any] | None = None,
) -> None:
    """Persist a small heartbeat that survives an outer supervisor timeout.

    Python cannot run the normal exception handler after systemd sends TERM at
    the configured start deadline. This pointer records the last entered
    stage before any network or replay work begins. It is deliberately
    best-effort: a heartbeat write must never turn a valid data cycle into a
    failure when the runtime filesystem is temporarily unavailable.
    """

    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "status": status,
        "cycle_started_at": cycle_started_at,
        "stage": stage,
        "stage_started_at": stage_started_at or _utc_now(),
        "updated_at": _utc_now(),
        "pid": os.getpid(),
        "timeout_interpretation": (
            "If status remains running and the process is absent, inspect the "
            "outer supervisor journal; the last stage is the last durable "
            "checkpoint, not a completed result."
        ),
    }
    if failure is not None:
        payload["failure"] = dict(failure)
    try:
        _write_json_atomic(path, payload)
    except (OSError, TypeError, ValueError):
        return


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _assert_model_lock_continuity(lock_path: Path, evidence_path: Path) -> None:
    """Refuse silent model-lock replacement during an active window.

    Lock migrations are valid before a prospective window starts, but once a
    successful cycle has recorded a model hash, replacing that hash would
    strand the existing rows as legacy and make a long-running scheduler look
    as if it had made no progress.  Operators must start a new, explicit
    evaluation window instead of changing the lock underneath the timer.
    """

    try:
        current = json.loads(lock_path.read_text(encoding="utf-8"))
        previous = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(current, dict) or not isinstance(previous, dict):
        return
    if previous.get("status") == "failed":
        return
    artifacts = previous.get("artifacts")
    previous_hash = artifacts.get("model_version_sha256") if isinstance(artifacts, dict) else None
    current_hash = current.get("model_version_sha256")
    if (
        isinstance(previous_hash, str)
        and isinstance(current_hash, str)
        and previous_hash
        and current_hash
        and previous_hash != current_hash
    ):
        # A lock migration is allowed only when the operator created a new
        # pending window with the formal refresh command.  The refresh
        # contract advances ``locked_at`` beyond the completed cycle and sets
        # ``evaluation_window_started_at`` exactly one second later.  This
        # keeps same-window replacements fail-closed while allowing the next
        # explicit evaluation window to proceed after a successful cycle.
        new_locked_at = _parse_utc(current.get("locked_at"))
        new_window_started = _parse_utc(current.get("evaluation_window_started_at"))
        previous_finished_at = _parse_utc(previous.get("finished_at"))
        if (
            current.get("status") == "pending_prospective_window"
            and new_locked_at is not None
            and new_window_started is not None
            and previous_finished_at is not None
            and new_locked_at > previous_finished_at
            and new_window_started == new_locked_at + timedelta(seconds=1)
        ):
            return
        raise RuntimeError(
            "prospective model lock changed after the active window started; "
            "start a new explicit evaluation window before migrating the lock"
        )


def _assert_lock_snapshot_unchanged(
    lock_path: Path,
    expected_identity: Mapping[str, Any],
    *,
    approved_lock_path: Path | None = None,
    expected_approved_identity: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Re-read lock inputs at each mutable-stage boundary.

    A model file or lock can be replaced after the initial preflight while a
    network sync is in flight.  Re-validating the no-follow snapshot before
    capture/evaluation prevents a fresh current pointer from being paired
    with a different implementation identity.  The approved lock is checked
    as well when a formal lane supplied one explicitly.
    """

    current, identity = read_validated_lock(
        lock_path,
        repository_root=Path.cwd(),
        require_training_admission=True,
    )
    for field in ("path", "sha256", "lock_identity_sha256", "model_version_sha256"):
        if identity.get(field) != expected_identity.get(field):
            raise RuntimeError(
                "prospective model lock changed between cycle stages "
                f"({field} mismatch)"
            )
    if approved_lock_path is not None and expected_approved_identity is not None:
        _approved, approved_identity = read_validated_lock(
            approved_lock_path,
            repository_root=Path.cwd(),
            require_training_admission=True,
        )
        if any(
            approved_identity.get(field) != expected_approved_identity.get(field)
            for field in ("path", "sha256", "lock_identity_sha256", "model_version_sha256")
        ):
            raise RuntimeError("approved prospective model lock changed during cycle")
    return current, identity


def _promote_model_lock_if_passed(
    lock_path: Path,
    lock: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    approved_lock_path: Path | None = None,
    expected_identity: Mapping[str, Any] | None = None,
    promoted_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Promote a lock only after the independent prospective gate passes.

    The evaluator is deliberately allowed to return ``pending`` for as long
    as the window is incomplete.  A passed evaluation is the only event that
    may change the lock status, and every required boolean is checked again at
    this boundary so a partial or hand-written evaluator result cannot unlock
    the model.  The write is atomic and idempotent; model files and their
    hashes are never changed here.
    """

    if approved_lock_path is None:
        raise ValueError("formal promotion requires an explicit approved lock path")
    candidate_path = require_lock_path(lock_path, field="lock_path", must_exist=True)
    approved_path = require_lock_path(
        approved_lock_path,
        field="approved_lock_path",
        must_exist=True,
    )
    if candidate_path.resolve(strict=True) != approved_path.resolve(strict=True):
        raise ValueError(
            "formal promotion cannot write an isolated candidate; "
            "lock_path must resolve to approved_lock_path"
        )

    required_flags = (
        "results_not_used_for_selection",
        "sample_requirements_met",
        "all_required_targets_scored",
        "prediction_freezes_verified",
    )
    if evaluation.get("status") != "passed":
        return lock, {
            "status": "not_promoted",
            "reason": "prospective_evaluation_not_passed",
            "evaluation_status": evaluation.get("status"),
        }
    missing = [flag for flag in required_flags if evaluation.get(flag) is not True]
    if evaluation.get("result_conflicts", 0) != 0:
        missing.append("result_conflicts_zero")
    if evaluation.get("invalid_records"):
        missing.append("invalid_records_empty")
    if missing:
        raise ValueError(
            "prospective evaluation reported passed without required evidence: "
            + ", ".join(missing)
        )

    # Re-check the precommitted implementation before changing its status.
    # This catches edits made between capture/evaluation and promotion.
    validate_lock_integrity(lock)
    current, current_identity = read_validated_lock(
        approved_path,
        repository_root=Path.cwd(),
        require_training_admission=True,
    )
    expected_fingerprint = lock_identity_fingerprint(lock)
    if current_identity.get("lock_identity_sha256") != expected_fingerprint:
        raise RuntimeError("approved prospective model lock changed before promotion")
    if expected_identity is not None and any(
        current_identity.get(field) != expected_identity.get(field)
        for field in ("path", "sha256", "lock_identity_sha256", "model_version_sha256")
    ):
        raise RuntimeError("approved prospective model lock changed before promotion")
    lock = current
    if lock.get("status") == "passed":
        return lock, {
            "status": "already_passed",
            "evaluation_generated_at": evaluation.get("generated_at"),
        }
    if lock.get("status") != "pending_prospective_window":
        raise ValueError("prospective lock is not pending and cannot be promoted")

    completed_at = (promoted_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    promoted = dict(lock)
    promoted.update(
        {
            "status": "passed",
            "results_not_used_for_selection": True,
            "sample_requirements_met": True,
            "all_required_targets_scored": True,
            "prediction_freezes_verified": True,
            "prospective_evaluation_completed_at": completed_at.isoformat(),
            "prospective_evaluation_generated_at": evaluation.get("generated_at"),
            "prospective_evaluation_scored_n": evaluation.get("scored_n"),
            "prospective_evaluation_digest": hashlib.sha256(
                json.dumps(
                    evaluation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        }
    )
    notes = list(promoted.get("notes") or [])
    notes.append(
        "Prospective evaluation passed all required targets, freeze stages, "
        "sample thresholds and result-conflict checks; lock promoted atomically."
    )
    promoted["notes"] = notes
    _write_json_atomic(approved_path, promoted)
    return promoted, {
        "status": "promoted",
        "completed_at": completed_at.isoformat(),
        "evaluation_generated_at": evaluation.get("generated_at"),
        "scored_n": evaluation.get("scored_n"),
        "lock_path": str(approved_path),
    }


def run_cycle(
    *,
    openfootball_raw_archive_dir: Path,
    data_dir: Path = Path("data/MatchHistory"),
    live_path: Path = DEFAULT_LIVE_PATH,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    intelligence_ledger: Path = DEFAULT_INTELLIGENCE_LEDGER,
    lock_path: Path | str | None = None,
    approved_lock_path: Path | str | None = None,
    prediction_archive: Path = DEFAULT_PREDICTION_ARCHIVE,
    cycle_lock_path: Path = DEFAULT_LOCK_PATH,
    evidence_path: Path = DEFAULT_EVIDENCE_PATH,
    evaluation_evidence_path: Path | None = None,
    history_path: Path = DEFAULT_HISTORY_PATH,
    progress_path: Path | None = None,
    now: datetime | None = None,
    sync_fn: Callable[..., dict[str, Any]] = sync,
    capture_fn: Callable[..., dict[str, Any]] = capture,
    evaluate_fn: Callable[..., dict[str, Any]] = evaluate_archive,
    market_audit_fn: Callable[..., dict[str, Any]] | None = None,
    market_audit_output: Path | None = None,
    oddstorm_history_fn: Callable[..., dict[str, Any]] | None = None,
    oddstorm_history_output: Path | None = None,
    crawl4ai_config_path: Path | str | None = None,
    allow_empty_snapshot: bool = False,
    enable_wikidata: bool = False,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
    runtime_only: bool = False,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Run the causal cycle and optional audit sidecar under one lock.

    The market and OddStorm-history sidecars are deliberately optional for
    callers and isolated from the primary sync/capture/evaluation result.
    Production CLI runs enable them, while tests and embedders can omit the
    output paths.
    """

    writable_paths = [
        live_path,
        archive_dir,
        intelligence_ledger,
        prediction_archive,
        cycle_lock_path,
        evidence_path,
        history_path,
    ]
    if progress_path is not None:
        writable_paths.append(progress_path)
    if evaluation_evidence_path is not None:
        writable_paths.append(evaluation_evidence_path)
    if market_audit_output is not None:
        writable_paths.append(market_audit_output)
    if oddstorm_history_output is not None:
        writable_paths.append(oddstorm_history_output)
    if runtime_only:
        if runtime_root is None:
            raise ValueError("runtime-only cycle requires runtime_root")
        _assert_runtime_only_writes(runtime_root, writable_paths)

    cycle_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with cycle_lock_path.open("a", encoding="utf-8") as cycle_lock:
        try:
            fcntl.flock(cycle_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another prospective cycle is already running") from exc

        started_at = _utc_now()
        stage = "storage_guard"
        progress_path = progress_path or history_path.with_name("prospective-cycle-progress.json")
        snapshot: dict[str, Any] = {}
        captured: dict[str, Any] = {}
        model_lock: dict[str, Any] = {}
        lock_identity: dict[str, Any] = {
            "path": str(
                (Path(lock_path) if lock_path is not None else DEFAULT_MODEL_LOCK).absolute()
            ),
        }
        approved_lock_identity: dict[str, Any] | None = None
        evaluation: dict[str, Any] = {}
        lock_promotion: dict[str, Any] | None = None
        evaluation_evidence_written = False
        market_audit: dict[str, Any] | None = None
        oddstorm_history: dict[str, Any] | None = None
        publication_block: dict[str, Any] | None = None
        storage_guard: dict[str, Any] = {}
        try:
            storage_paths = [
                live_path,
                archive_dir,
                intelligence_ledger,
                prediction_archive,
                evidence_path,
                history_path,
                cycle_lock_path,
                progress_path,
            ]
            # The model lock is read for continuity and integrity.  It stays
            # in the repository by contract and is not part of the growing
            # runtime budget.  If a later promotion must write it and the
            # repository filesystem cannot accept that bounded write, the
            # promotion is allowed to fail closed and the failure evidence is
            # still written to the runtime ledger above.
            if evaluation_evidence_path is not None:
                storage_paths.append(evaluation_evidence_path)
            if market_audit_output is not None:
                storage_paths.append(market_audit_output)
            if oddstorm_history_output is not None:
                storage_paths.append(oddstorm_history_output)
            # The scheduled post-chain also writes bounded reports, offline
            # bundles and the Sites build into the repository filesystem.  A
            # runtime root on an external volume can therefore be healthy
            # while the actual cycle still fails later on a full repository
            # disk.  Budget the working tree up front so the normal cycle
            # fails closed before any network/browser work starts.  The
            # explicitly opt-in runtime-only lane writes only below its
            # external runtime root and never runs the post-chain.
            if not runtime_only:
                storage_paths.append(Path.cwd())
            storage_guard = _storage_guard(
                storage_paths,
                min_free_bytes=min_free_bytes,
                disk_usage_fn=disk_usage_fn,
            )
            storage_guard["runtime_only"] = runtime_only
            storage_guard["repository_check_skipped"] = runtime_only
            storage_guard["read_only_paths"] = [str(lock_path)]
            if approved_lock_path is not None:
                storage_guard["read_only_paths"].append(str(approved_lock_path))
            if storage_guard["status"] != "ok":
                finished_at = _utc_now()
                blocked_evaluation = {
                    "status": "blocked_storage",
                    "scored_n": 0,
                    "pending_n": 0,
                    "reason": "insufficient_filesystem_space",
                }
                blocked_capture = {
                    "status": "blocked",
                    "predictions": 0,
                    "reason": "insufficient_filesystem_space",
                    "model_entry_allowed": False,
                }
                blocked_report: dict[str, Any] = {
                    "schema_version": "1.0.0",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "status": "blocked_storage",
                    "runtime_only": runtime_only,
                    "degraded": True,
                    "storage": storage_guard,
                    "sync": {
                        "status": "blocked",
                        "reason": "insufficient_filesystem_space",
                    },
                    "capture": blocked_capture,
                    "evaluation": blocked_evaluation,
                    "lock_promotion": None,
                    "market_audit": None,
                    "oddstorm_history": None,
                    "publication": None,
                    "artifacts": {
                        "live_snapshot": str(live_path),
                        "progress": str(progress_path),
                        "prediction_archive": str(prediction_archive),
                        "model_lock": str(lock_path),
                        "model_lock_sha256": lock_identity.get("sha256"),
                        "lock_identity_sha256": lock_identity.get("lock_identity_sha256"),
                        "evaluation_evidence": (
                            str(evaluation_evidence_path)
                            if evaluation_evidence_path is not None
                            else None
                        ),
                    },
                }
                if evaluation_evidence_path is not None:
                    _write_json_atomic(
                        evaluation_evidence_path,
                        {
                            **blocked_evaluation,
                            "storage": storage_guard,
                            "degraded": True,
                        },
                    )
                    evaluation_evidence_written = True
                _write_json_atomic(evidence_path, blocked_report)
                _append_jsonl(history_path, blocked_report)
                _write_stage_progress(
                    progress_path,
                    cycle_started_at=started_at,
                    stage="storage_guard",
                    status="blocked_storage",
                )
                return blocked_report
            # Bind the control-plane lock before any source sync.  The formal
            # lane cannot infer approval from a candidate path (or from a
            # matching fingerprint at a different path); runtime-only is the
            # sole lane allowed to proceed with one explicit candidate.
            stage = "model_lock_binding"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            lock_path, approved_lock_path = _bind_lock_paths(
                lock_path,
                approved_lock_path,
                runtime_only=runtime_only,
            )
            lock_identity["path"] = str(lock_path)
            storage_guard["read_only_paths"] = [str(lock_path)]
            if approved_lock_path is not None:
                storage_guard["read_only_paths"].append(str(approved_lock_path))
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            stage = "openfootball_raw_archive_guard"
            blocked_raw_reason = "openfootball_raw_archive_invalid"
            captured = {
                "status": "blocked",
                "predictions": 0,
                "reason": blocked_raw_reason,
                "model_entry_allowed": False,
            }
            evaluation = {
                "status": "blocked",
                "scored_n": 0,
                "pending_n": 0,
                "reason": blocked_raw_reason,
                "promotion_eligible": False,
            }
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            openfootball_raw_archive_dir = require_durable_openfootball_raw_archive(
                openfootball_raw_archive_dir
            )
            captured = {}
            evaluation = {}
            stage = "lock_continuity"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            _assert_model_lock_continuity(lock_path, evidence_path)
            # Validate the precommitted implementation before any sync or
            # archive write.  ``capture`` validates the lock again at its own
            # trust boundary, but waiting until that stage would allow a
            # changed model file to publish a fresh current snapshot first.
            # Every cycle caller uses the full precommitted lock contract.
            # Validate before sync so a changed model file cannot publish a
            # fresh current snapshot first; capture validates again at its
            # own append trust boundary.
            stage = "model_lock_integrity"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            model_lock, lock_identity = read_validated_lock(
                lock_path,
                repository_root=Path.cwd(),
                require_training_admission=True,
            )
            # Formal binding above guarantees the approved path is the exact
            # same resolved file.  Still read it through the no-follow
            # validator and retain its identity so every receipt records the
            # path, exact bytes digest and semantic fingerprint used for the
            # cycle. Runtime-only candidates intentionally have no approved
            # identity and can never reach promotion.
            if approved_lock_path is not None and not runtime_only:
                _approved_lock, approved_lock_identity = read_validated_lock(
                    approved_lock_path,
                    repository_root=Path.cwd(),
                    require_training_admission=True,
                )
                assert_lock_identities_match(lock_identity, approved_lock_identity)
            sync_kwargs: dict[str, Any] = {
                "now": now,
                "archive_dir": archive_dir,
                "openfootball_raw_archive_dir": openfootball_raw_archive_dir,
                "intelligence_ledger": intelligence_ledger,
                # Wikidata is a bounded display-only enrichment lane.  Keep it
                # opt-in for the causal cycle so a slow/unavailable entity
                # endpoint cannot hold the primary fixture sync open.
                "enable_wikidata": enable_wikidata,
            }
            if allow_empty_snapshot:
                sync_kwargs["allow_empty_snapshot"] = True
            if crawl4ai_config_path is not None:
                sync_kwargs["crawl4ai_config_path"] = crawl4ai_config_path
            stage = "sync"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            # The heartbeat above can yield to another process (or an
            # operator replacing a candidate) immediately before the network
            # sync starts.  Re-read the no-follow lock snapshot at this final
            # boundary so a model-file/hash/path drift is rejected before
            # ``sync_fn`` can publish a fresh current pointer.
            model_lock, lock_identity = _assert_lock_snapshot_unchanged(
                lock_path,
                lock_identity,
                approved_lock_path=(approved_lock_path if not runtime_only else None),
                expected_approved_identity=(
                    approved_lock_identity if not runtime_only else None
                ),
            )
            snapshot = sync_fn(live_path, **sync_kwargs)
            publication = snapshot.get("publication") if isinstance(snapshot, dict) else None
            if isinstance(publication, dict) and publication.get("status") == "blocked":
                # sync_live has already preserved the last known-good pointer
                # and written a bounded attempted/previous diagnostic.  Keep
                # the source failure visible, but do not discard safe work
                # that does not depend on a fresh fixture snapshot (archive
                # evaluation, lock checks and audit evidence).  Crucially,
                # no new prediction is generated from this stale pointer.
                publication_block = dict(publication)
            stage = "snapshot_validation"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            # A separately launched sync_live process can update the same path
            # after our sync lock is released.  Never score a snapshot different
            # from the one this cycle just produced; fail closed instead.
            current_on_disk = json.loads(live_path.read_text(encoding="utf-8"))
            if current_on_disk.get("as_of") != snapshot.get("as_of"):
                raise RuntimeError("live snapshot changed between sync and prospective capture")
            model_lock, lock_identity = _assert_lock_snapshot_unchanged(
                lock_path,
                lock_identity,
                approved_lock_path=(approved_lock_path if not runtime_only else None),
                expected_approved_identity=(
                    approved_lock_identity if not runtime_only else None
                ),
            )
            if oddstorm_history_fn is not None and oddstorm_history_output is not None:
                stage = "oddstorm_history"
                _write_stage_progress(
                    progress_path,
                    cycle_started_at=started_at,
                    stage=stage,
                )
                if publication_block is not None:
                    oddstorm_history = {
                        "status": "skipped",
                        "reason": "live_snapshot_publication_blocked",
                        "output": str(oddstorm_history_output),
                    }
                else:
                    try:
                        oddstorm_history = oddstorm_history_fn(
                            live_path,
                            oddstorm_history_output,
                            now=now,
                        )
                    except Exception as history_exc:
                        # Public history is an audit-sidecar.  A transient source
                        # failure must never invalidate the already validated
                        # current snapshot or block prospective predictions.
                        oddstorm_history = {
                            "status": "failed",
                            "type": type(history_exc).__name__,
                            "message": str(history_exc),
                            "output": str(oddstorm_history_output),
                        }
            stage = "capture"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            if publication_block is not None:
                captured = {
                    "status": "blocked",
                    "predictions": 0,
                    "reason": "live_snapshot_publication_blocked",
                    "publication": publication_block,
                    "model_entry_allowed": False,
                }
            else:
                captured = capture_fn(
                    data_dir=data_dir,
                    live_path=live_path,
                    archive_dir=archive_dir,
                    openfootball_raw_archive_dir=openfootball_raw_archive_dir,
                    lock_path=lock_path,
                    output=prediction_archive,
                )
            if isinstance(captured, dict):
                _enrich_capture_blocked_diagnostics(captured, snapshot)
            stage = "model_lock"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            # Re-read after capture so a concurrent edit cannot be carried
            # into evaluation or lock promotion.  The capture implementation
            # also validates this same object before appending predictions.
            model_lock, lock_identity = _assert_lock_snapshot_unchanged(
                lock_path,
                lock_identity,
                approved_lock_path=(approved_lock_path if not runtime_only else None),
                expected_approved_identity=(
                    approved_lock_identity if not runtime_only else None
                ),
            )
            stage = "evaluation"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            evaluation = evaluate_fn(
                prediction_archive,
                archive_dir,
                openfootball_raw_archive_dir=openfootball_raw_archive_dir,
                lock=model_lock,
            )
            stage = "model_lock_promotion"
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
            )
            # Evaluation may be slow and can overlap an operator replacing a
            # lock file. Revalidate one final time immediately before any
            # status write so a passed result can never promote stale lock
            # bytes or a drifted candidate identity.
            model_lock, lock_identity = _assert_lock_snapshot_unchanged(
                lock_path,
                lock_identity,
                approved_lock_path=(approved_lock_path if not runtime_only else None),
                expected_approved_identity=(
                    approved_lock_identity if not runtime_only else None
                ),
            )
            if runtime_only:
                lock_promotion = {
                    "status": "not_promoted",
                    "reason": "runtime_only_no_repository_writes",
                }
            else:
                model_lock, lock_promotion = _promote_model_lock_if_passed(
                    lock_path,
                    model_lock,
                    evaluation,
                    approved_lock_path=approved_lock_path,
                    expected_identity=lock_identity,
                    promoted_at=now,
                )
            if evaluation_evidence_path is not None:
                stage = "evaluation_evidence"
                _write_stage_progress(
                    progress_path,
                    cycle_started_at=started_at,
                    stage=stage,
                )
                evaluation_evidence = dict(evaluation)
                if lock_promotion is not None:
                    evaluation_evidence["lock_promotion"] = lock_promotion
                evaluation_evidence["lock_identity"] = dict(lock_identity)
                if approved_lock_identity is not None:
                    evaluation_evidence["approved_lock_identity"] = dict(approved_lock_identity)
                evaluation_evidence["publication"] = publication_block
                evaluation_evidence["degraded"] = publication_block is not None
                evaluation_evidence["storage"] = storage_guard
                _write_json_atomic(evaluation_evidence_path, evaluation_evidence)
                evaluation_evidence_written = True
            if market_audit_fn is not None and market_audit_output is not None:
                stage = "market_audit"
                _write_stage_progress(
                    progress_path,
                    cycle_started_at=started_at,
                    stage=stage,
                )
                try:
                    market_audit = market_audit_fn(
                        prediction_archive,
                        archive_dir,
                        market_audit_output,
                    )
                except Exception as audit_exc:
                    # This is a display/audit sidecar.  Preserve the primary
                    # prediction cycle result but make the sidecar failure
                    # explicit in both evidence ledgers.
                    market_audit = {
                        "status": "failed",
                        "type": type(audit_exc).__name__,
                        "message": str(audit_exc),
                    }
        except (Exception, KeyboardInterrupt) as exc:
            # Persist failures before re-raising so a scheduler or operator can
            # distinguish a blocked cycle from a missing run entirely.
            failed_at = _utc_now()
            interrupted = isinstance(exc, KeyboardInterrupt)
            failure_status = "interrupted" if interrupted else "failed"
            if stage == "openfootball_raw_archive_guard" and evaluation_evidence_path is not None:
                _write_json_atomic(evaluation_evidence_path, evaluation)
                evaluation_evidence_written = True
            sync_evidence: dict[str, Any] = {
                "as_of": snapshot.get("as_of"),
                "fixtures": len(fixture_rows(snapshot)),
                "markets": len(snapshot.get("espn_markets", {}).get("markets", [])),
                "snapshot_sha256": _sha256(live_path),
            }
            if stage == "openfootball_raw_archive_guard":
                sync_evidence.update(
                    {
                        "status": "blocked",
                        "reason": "openfootball_raw_archive_invalid",
                    }
                )
            failure_report: dict[str, Any] = {
                "schema_version": "1.0.0",
                "started_at": started_at,
                "finished_at": failed_at,
                "status": failure_status,
                "runtime_only": runtime_only,
                "failure": {
                    "stage": stage,
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "operator_interrupted": interrupted,
                },
                "sync": sync_evidence,
                "capture": captured,
                "evaluation": evaluation,
                "lock_promotion": lock_promotion,
                "market_audit": market_audit,
                "oddstorm_history": oddstorm_history,
                "publication": publication_block,
                "lock_identity": dict(lock_identity),
                "approved_lock_identity": (
                    dict(approved_lock_identity) if approved_lock_identity is not None else None
                ),
                "storage": storage_guard,
                "artifacts": {
                    "live_snapshot": str(live_path),
                    "openfootball_raw_archive": str(openfootball_raw_archive_dir),
                    "progress": str(progress_path),
                    "prediction_archive": str(prediction_archive),
                    "market_audit_output": (
                        str(market_audit_output) if market_audit_output is not None else None
                    ),
                    "model_lock": str(lock_path),
                    "model_lock_sha256": lock_identity.get("sha256"),
                    "lock_identity_sha256": lock_identity.get("lock_identity_sha256"),
                    "model_version_sha256": model_lock.get("model_version_sha256"),
                    "prediction_archive_sha256": _sha256(prediction_archive),
                    "evaluation_evidence": (
                        str(evaluation_evidence_path)
                        if evaluation_evidence_path is not None
                        else None
                    ),
                    "evaluation_evidence_sha256": (
                        _sha256(evaluation_evidence_path)
                        if evaluation_evidence_written and evaluation_evidence_path is not None
                        else None
                    ),
                    "market_audit_output_sha256": (
                        _sha256(market_audit_output) if market_audit_output is not None else None
                    ),
                    "oddstorm_history_output": (
                        str(oddstorm_history_output)
                        if oddstorm_history_output is not None
                        else None
                    ),
                    "oddstorm_history_output_sha256": (
                        _sha256(oddstorm_history_output)
                        if oddstorm_history_output is not None
                        else None
                    ),
                },
            }
            _write_stage_progress(
                progress_path,
                cycle_started_at=started_at,
                stage=stage,
                status=failure_status,
                failure=failure_report["failure"],
            )
            _write_json_atomic(evidence_path, failure_report)
            _append_jsonl(history_path, failure_report)
            raise
        finished_at = _utc_now()
        report: dict[str, Any] = {
            "schema_version": "1.0.0",
            "started_at": started_at,
            "finished_at": finished_at,
            "status": str(evaluation.get("status") or "unknown"),
            "runtime_only": runtime_only,
            "sync": {
                "as_of": snapshot.get("as_of"),
                "fixtures": len(fixture_rows(snapshot)),
                "markets": len(snapshot.get("espn_markets", {}).get("markets", [])),
                "snapshot_sha256": _sha256(live_path),
            },
            "capture": captured,
            "evaluation": evaluation,
            "lock_promotion": lock_promotion,
            "market_audit": market_audit,
            "oddstorm_history": oddstorm_history,
            "publication": publication_block,
            "lock_identity": dict(lock_identity),
            "approved_lock_identity": (
                dict(approved_lock_identity) if approved_lock_identity is not None else None
            ),
            "degraded": publication_block is not None,
            "storage": storage_guard,
            "artifacts": {
                "live_snapshot": str(live_path),
                "openfootball_raw_archive": str(openfootball_raw_archive_dir),
                "progress": str(progress_path),
                "prediction_archive": str(prediction_archive),
                "market_audit_output": (
                    str(market_audit_output) if market_audit_output is not None else None
                ),
                "model_lock": str(lock_path),
                "model_lock_sha256": lock_identity.get("sha256"),
                "lock_identity_sha256": lock_identity.get("lock_identity_sha256"),
                "model_version_sha256": model_lock.get("model_version_sha256"),
                "prediction_archive_sha256": _sha256(prediction_archive),
                "evaluation_evidence": (
                    str(evaluation_evidence_path) if evaluation_evidence_path is not None else None
                ),
                "evaluation_evidence_sha256": (
                    _sha256(evaluation_evidence_path)
                    if evaluation_evidence_written and evaluation_evidence_path is not None
                    else None
                ),
                "market_audit_output_sha256": (
                    _sha256(market_audit_output) if market_audit_output is not None else None
                ),
                "oddstorm_history_output": (
                    str(oddstorm_history_output) if oddstorm_history_output is not None else None
                ),
                "oddstorm_history_output_sha256": (
                    _sha256(oddstorm_history_output)
                    if oddstorm_history_output is not None
                    else None
                ),
            },
        }
        _write_json_atomic(evidence_path, report)
        _append_jsonl(history_path, report)
        _write_stage_progress(
            progress_path,
            cycle_started_at=started_at,
            stage="complete",
            status="completed",
            stage_started_at=finished_at,
        )
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help=(
            "optional root for growing runtime artifacts (current snapshot, "
            "raw archive, ledgers and receipts); no files are moved automatically"
        ),
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/MatchHistory"))
    parser.add_argument("--live-path", type=Path)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--openfootball-raw-archive-dir", type=Path)
    parser.add_argument("--intelligence-ledger", type=Path)
    # Keep the formal lane compatible with the repository active lock when no
    # candidate is selected, but require runtime-only callers to name their
    # isolated candidate explicitly.  Approval is never inferred from the
    # lock input itself.
    parser.add_argument("--lock", type=Path, default=None)
    parser.add_argument(
        "--approved-lock",
        "--current-lock",
        dest="approved_lock_path",
        type=Path,
        help=(
            "formal-lane approved active lock to compare with an explicit candidate; "
            "runtime-only lanes intentionally do not require equality"
        ),
    )
    parser.add_argument("--prediction-archive", type=Path)
    parser.add_argument("--cycle-lock", type=Path)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=None,
        help="latest cycle evidence; defaults beside --runtime-dir when supplied",
    )
    parser.add_argument(
        "--evaluation-evidence",
        type=Path,
        default=None,
        help="latest evaluation evidence; defaults beside --runtime-dir when supplied",
    )
    parser.add_argument("--history", type=Path)
    parser.add_argument("--market-audit-output", type=Path)
    parser.add_argument("--oddstorm-history-output", type=Path)
    parser.add_argument(
        "--crawl4ai-config",
        type=Path,
        help="explicit Crawl4AI JSON configuration; omitted means browser pages are not crawled",
    )
    parser.add_argument(
        "--allow-empty-snapshot",
        action="store_true",
        help="allow an operator-confirmed empty ESPN fixture window to replace current.json",
    )
    parser.add_argument(
        "--enable-wikidata",
        action="store_true",
        help=(
            "opt in to the bounded Wikidata venue-enrichment lane; disabled by default "
            "so entity-source latency cannot block the primary cycle"
        ),
    )
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=DEFAULT_MIN_FREE_BYTES,
        help="fail closed before network work when any cycle filesystem has less free space",
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help=(
            "opt-in data-production lane: require --runtime-dir, write only below "
            "that external root, skip the repository-space check and never promote the model lock"
        ),
    )
    args = parser.parse_args()
    if args.runtime_only and args.runtime_dir is None:
        parser.error("--runtime-only requires --runtime-dir")
    if args.runtime_only and args.lock is None:
        parser.error("--runtime-only requires an explicit --lock candidate path")
    if not args.runtime_only and args.approved_lock_path is None:
        parser.error("formal cycle requires an explicit --approved-lock path")
    runtime = resolve_runtime_paths(args.runtime_dir or DEFAULT_RUNTIME_DIR)
    openfootball_raw_archive_dir = resolve_openfootball_raw_archive_dir(
        args.openfootball_raw_archive_dir,
        runtime_default=runtime.openfootball_raw_archive_dir,
    )
    runtime_only_evidence = runtime.root / DEFAULT_RUNTIME_ONLY_EVIDENCE_NAME
    runtime_only_evaluation = runtime.root / DEFAULT_RUNTIME_ONLY_EVALUATION_NAME
    runtime_only_history = runtime.root / DEFAULT_RUNTIME_ONLY_HISTORY_NAME
    runtime_only_progress = runtime.root / DEFAULT_RUNTIME_ONLY_PROGRESS_NAME
    evidence_path = (
        args.evidence
        if args.evidence is not None
        else runtime_only_evidence
        if args.runtime_only
        else runtime.cycle_evidence
        if args.runtime_dir is not None
        else DEFAULT_EVIDENCE_PATH
    )
    evaluation_evidence_path = (
        args.evaluation_evidence
        if args.evaluation_evidence is not None
        else runtime_only_evaluation
        if args.runtime_only
        else runtime.evaluation_evidence
        if args.runtime_dir is not None
        else DEFAULT_EVALUATION_EVIDENCE_PATH
    )
    report = run_cycle(
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        data_dir=args.data_dir,
        live_path=args.live_path or runtime.live_path,
        archive_dir=args.archive_dir or runtime.archive_dir,
        intelligence_ledger=args.intelligence_ledger or runtime.intelligence_ledger,
        lock_path=args.lock or DEFAULT_MODEL_LOCK,
        approved_lock_path=args.approved_lock_path,
        prediction_archive=args.prediction_archive or runtime.prediction_archive,
        cycle_lock_path=args.cycle_lock or runtime.cycle_lock,
        evidence_path=evidence_path,
        evaluation_evidence_path=evaluation_evidence_path,
        history_path=(
            args.history or runtime_only_history
            if args.runtime_only
            else args.history or runtime.history
        ),
        progress_path=runtime_only_progress if args.runtime_only else None,
        market_audit_fn=append_market_baselines,
        market_audit_output=args.market_audit_output or runtime.market_audit_output,
        oddstorm_history_fn=capture_oddstorm_history,
        oddstorm_history_output=args.oddstorm_history_output or runtime.oddstorm_history_output,
        crawl4ai_config_path=args.crawl4ai_config,
        allow_empty_snapshot=args.allow_empty_snapshot,
        enable_wikidata=args.enable_wikidata,
        min_free_bytes=args.min_free_bytes,
        runtime_only=args.runtime_only,
        runtime_root=runtime.root if args.runtime_only else None,
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "started_at",
                    "finished_at",
                    "sync",
                    "capture",
                    "market_audit",
                    "oddstorm_history",
                )
            },
            ensure_ascii=False,
        )
    )
    # A storage block already wrote its bounded diagnostic evidence.  Return a
    # non-zero status so systemd does not continue into strict-report, bundle,
    # and Sites build post stages that would write to a second, potentially
    # full filesystem and turn a clean fail-closed report into a partial one.
    return 2 if report.get("status") == "blocked_storage" else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["_promote_model_lock_if_passed", "run_cycle"]
