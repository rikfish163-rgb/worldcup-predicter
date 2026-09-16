"""Refresh latest runtime evidence pointers after a prospective cycle.

The cycle and Sites bundle are authoritative append-only/derived artifacts.
This module only refreshes the small ``*-latest`` audit pointers after both
artifacts exist; it never edits predictions, source snapshots, or model files.
Missing optional pointer files are skipped so an audit-only helper cannot make
the production cycle fail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from league_platform.intelligence import recover_observation_fragments
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchiveError,
    load_verified_openfootball_archive,
)
from league_platform.platform_verification import (
    _validate_prediction_archive_row,
    canonical_model_version_sha256,
    strict_report_v260_failures,
)
from league_platform.runtime_paths import DEFAULT_RUNTIME_DIR, resolve_runtime_paths
from league_platform.strict_report import (
    build_input_fingerprint,
    build_report,
    load_cached_report,
    write_report_cache,
)

fcntl: Any
try:  # pragma: no cover - Windows fallback keeps audit reads functional.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


ROOT = Path("docs/evidence")
DEFAULT_CYCLE = ROOT / "prospective-cycle-latest.json"
DEFAULT_EVALUATION = ROOT / "prospective-evaluation-current.json"
DEFAULT_LOCK = ROOT / "prospective-model-lock-current.json"
DEFAULT_SNAPSHOT = Path("data/live/current.json")
DEFAULT_BUNDLE = Path("league_platform/site/offline_data.js")
DEFAULT_ARCHIVE = Path("data/live/prospective_predictions.jsonl")
DEFAULT_INTELLIGENCE_LEDGER = Path("data/live/intelligence/observations.jsonl")
DEFAULT_INTELLIGENCE_QUARANTINE = Path("data/live/intelligence/observations.quarantine.jsonl")
DEFAULT_SELECTION = ROOT / "model-selection-audit-current.json"
DEFAULT_LOCK_INTEGRITY = ROOT / "prospective-lock-integrity-2026-08-14-v16-latest.json"
DEFAULT_PLATFORM = ROOT / "platform-maturity-verification-2026-08-14-v16-latest.json"
DEFAULT_ODDSTORM = ROOT / "oddstorm-time-eligibility-2026-08-14-v16.json"
DEFAULT_FREEZE = ROOT / "prospective-freeze-validation-2026-08-14-v19.json"
SUCCESSFUL_CYCLE_STATUSES = {"pending_prospective_window", "passed"}
HISTORY_RECEIPT_SCHEMA = "matchline.openfootball_history_refresh.v1"
VERIFIED_HISTORY_INPUT_SCHEMA = "matchline.verified_strict_history_input.v1"
HISTORY_RECEIPT_MAX_AGE = timedelta(hours=36)
HISTORY_RECEIPT_FIELDS = {
    "schema_version",
    "status",
    "observed_at",
    "source_count",
    "source_ids",
    "parsed_fixture_count",
    "finished_fixture_count",
    "unfinished_fixture_count",
    "admission_sha256",
    "rows_sha256",
    "source_manifest_sha256",
    "parser_contract_sha256",
    "errors",
}


class VerifiedHistoryInputError(RuntimeError):
    """Raised before a mutable strict-report pointer can be replaced."""


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _history_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise VerifiedHistoryInputError(f"history receipt {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerifiedHistoryInputError(f"history receipt {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerifiedHistoryInputError(f"history receipt {field} is invalid")
    return parsed.astimezone(timezone.utc)


def _history_checked_at(value: datetime | None) -> datetime:
    checked_at = value or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise VerifiedHistoryInputError("history receipt checked_at is invalid")
    return checked_at.astimezone(timezone.utc)


def _fixed_runtime_path(
    *,
    runtime_root: Path,
    candidate: Path,
    relative: Path,
    kind: str,
    must_exist: bool,
) -> Path:
    root = Path(os.path.abspath(runtime_root))
    value = Path(os.path.abspath(candidate))
    expected = root / relative
    if not runtime_root.is_absolute() or not candidate.is_absolute() or ".." in candidate.parts:
        raise VerifiedHistoryInputError(f"history {kind} path is invalid")
    if value != expected:
        raise VerifiedHistoryInputError(f"history {kind} path escapes runtime root")
    if not root.is_dir() or root.is_symlink():
        raise VerifiedHistoryInputError("history runtime root is invalid")
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise VerifiedHistoryInputError(f"history {kind} path contains a symlink")
        elif must_exist:
            raise VerifiedHistoryInputError(f"history {kind} is missing")
    if must_exist and kind == "raw archive" and not value.is_dir():
        raise VerifiedHistoryInputError("history raw archive is not a directory")
    if must_exist and kind != "raw archive" and not value.is_file():
        raise VerifiedHistoryInputError(f"history {kind} is not a regular file")
    return value


def _immutable_history_receipt(
    *,
    raw_archive_dir: Path,
    pointer_payload: bytes,
) -> tuple[Path, str]:
    digest = hashlib.sha256(pointer_payload).hexdigest()
    relative = Path("history-refresh-receipts") / "sha256" / digest[:2] / f"{digest}.json"
    immutable = raw_archive_dir / relative
    current = raw_archive_dir
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise VerifiedHistoryInputError("history immutable receipt path contains a symlink")
    try:
        immutable_payload = immutable.read_bytes()
    except OSError as exc:
        raise VerifiedHistoryInputError("history immutable receipt is missing") from exc
    if immutable_payload != pointer_payload:
        raise VerifiedHistoryInputError("history receipt pointer is not content-addressed")
    return immutable, digest


def verify_openfootball_history_input(
    *,
    runtime_root: Path,
    raw_archive_dir: Path,
    receipt_path: Path,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Re-read the fixed history pointer and replay raw bytes at its cutoff."""

    reference = _history_checked_at(checked_at)
    root = Path(os.path.abspath(runtime_root))
    raw_root = _fixed_runtime_path(
        runtime_root=root,
        candidate=raw_archive_dir,
        relative=Path("openfootball-raw"),
        kind="raw archive",
        must_exist=True,
    )
    try:
        raw_root.resolve(strict=True).relative_to(Path("/dev/shm").resolve(strict=True))
    except ValueError:
        pass
    except OSError as exc:
        raise VerifiedHistoryInputError("history raw archive path cannot be resolved") from exc
    else:
        raise VerifiedHistoryInputError("history raw archive is not durable")
    pointer = _fixed_runtime_path(
        runtime_root=root,
        candidate=receipt_path,
        relative=Path("openfootball-history-refresh-current.json"),
        kind="receipt",
        must_exist=True,
    )
    try:
        pointer_payload = pointer.read_bytes()
    except OSError as exc:
        raise VerifiedHistoryInputError("history receipt is unreadable") from exc
    if not pointer_payload or len(pointer_payload) > 1024 * 1024:
        raise VerifiedHistoryInputError("history receipt size is invalid")
    _immutable_history_receipt(
        raw_archive_dir=raw_root,
        pointer_payload=pointer_payload,
    )
    try:
        receipt = json.loads(pointer_payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VerifiedHistoryInputError("history receipt JSON is invalid") from exc
    if not isinstance(receipt, Mapping) or set(receipt) != HISTORY_RECEIPT_FIELDS:
        raise VerifiedHistoryInputError("history receipt fields are invalid")
    try:
        canonical_receipt = (
            json.dumps(
                receipt,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise VerifiedHistoryInputError("history receipt is not canonical JSON") from exc
    if pointer_payload != canonical_receipt:
        raise VerifiedHistoryInputError("history receipt is not canonical JSON")
    if (
        receipt.get("schema_version") != HISTORY_RECEIPT_SCHEMA
        or receipt.get("status") != "verified_raw_history"
        or receipt.get("errors") != []
    ):
        raise VerifiedHistoryInputError("history receipt status is not verified")
    expected_source_ids = sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    if (
        receipt.get("source_count") != len(expected_source_ids)
        or receipt.get("source_ids") != expected_source_ids
    ):
        raise VerifiedHistoryInputError("history receipt source_ids are invalid")
    observed_before = _history_utc(receipt.get("observed_at"), field="observed_at")
    if observed_before > reference:
        raise VerifiedHistoryInputError("history receipt cutoff is in the future")
    if reference - observed_before > HISTORY_RECEIPT_MAX_AGE:
        raise VerifiedHistoryInputError("history receipt is expired")
    for field in (
        "parsed_fixture_count",
        "finished_fixture_count",
        "unfinished_fixture_count",
    ):
        value = receipt.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise VerifiedHistoryInputError(f"history receipt {field} is invalid")
    for field in (
        "admission_sha256",
        "rows_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
    ):
        value = receipt.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise VerifiedHistoryInputError(f"history receipt {field} is invalid")
    try:
        replay = load_verified_openfootball_archive(
            raw_root,
            source_ids=expected_source_ids,
            observed_before=observed_before,
        )
    except (OpenFootballRawArchiveError, OSError, TypeError, ValueError) as exc:
        raise VerifiedHistoryInputError("history raw replay failed") from exc
    rows = replay.get("rows")
    if not isinstance(rows, list):
        raise VerifiedHistoryInputError("history raw replay rows are invalid")
    finished = sum(
        isinstance(row, Mapping) and row.get("status") == "finished" for row in rows
    )
    unfinished = len(rows) - finished
    replay_identity = {
        "admission_sha256": replay.get("admission_sha256"),
        "rows_sha256": replay.get("rows_sha256"),
        "source_manifest_sha256": replay.get("source_manifest_sha256"),
        "parser_contract_sha256": replay.get("parser_contract_sha256"),
    }
    if any(receipt.get(field) != value for field, value in replay_identity.items()) or (
        receipt.get("parsed_fixture_count") != len(rows)
        or receipt.get("finished_fixture_count") != finished
        or receipt.get("unfinished_fixture_count") != unfinished
    ):
        raise VerifiedHistoryInputError("history receipt does not match raw replay")
    return {
        "schema_version": VERIFIED_HISTORY_INPUT_SCHEMA,
        "status": "verified",
        "raw_archive_dir": str(raw_root),
        "receipt_path": str(pointer),
        "receipt_raw_sha256": hashlib.sha256(pointer_payload).hexdigest(),
        "observed_before": observed_before.isoformat(),
        "source_ids": expected_source_ids,
        **replay_identity,
    }


def _strict_output_path(runtime_root: Path, path: Path, filename: str) -> Path:
    return _fixed_runtime_path(
        runtime_root=runtime_root,
        candidate=path,
        relative=Path(filename),
        kind=filename,
        must_exist=False,
    )


def _validate_generated_strict_report(
    report: Mapping[str, Any],
    verified_input: Mapping[str, Any],
) -> None:
    failures = strict_report_v260_failures(report)
    admission = report.get("training_admission")
    contract = report.get("training_source_contract")
    if not isinstance(admission, Mapping) or not isinstance(contract, Mapping):
        failures.append("strict_report.training_contract:missing")
    else:
        expected = {
            "raw_admission_sha256": verified_input.get("admission_sha256"),
            "raw_rows_sha256": verified_input.get("rows_sha256"),
            "source_manifest_sha256": verified_input.get("source_manifest_sha256"),
            "parser_contract_sha256": verified_input.get("parser_contract_sha256"),
        }
        for field, value in expected.items():
            if admission.get(field) != value:
                failures.append(f"strict_report.training_admission.{field}:history_mismatch")
        if contract.get("observed_before") != verified_input.get("observed_before"):
            failures.append("strict_report.training_source_contract.observed_before:history_mismatch")
        if contract.get("source_ids") != verified_input.get("source_ids"):
            failures.append("strict_report.training_source_contract.source_ids:history_mismatch")
    if failures:
        raise VerifiedHistoryInputError(
            "generated strict report failed verified history contract: " + ", ".join(failures)
        )


def run_strict_report_from_verified_history(
    *,
    runtime_root: Path,
    raw_archive_dir: Path,
    receipt_path: Path,
    output_path: Path,
    cache_path: Path,
    candidate_evidence_dir: Path,
    prospective_lock_path: Path,
    checked_at: datetime | None = None,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    """Build formal strict evidence only after live replay of the fixed receipt."""

    repository_root = Path(__file__).resolve().parent.parent
    expected_python = repository_root / ".venv/bin/python"
    executable = Path(sys.executable) if python_executable is None else python_executable
    if Path(os.path.abspath(executable)) != Path(os.path.abspath(expected_python)) or Path(
        os.path.abspath(sys.executable)
    ) != Path(os.path.abspath(expected_python)):
        raise VerifiedHistoryInputError("strict report runner requires the project .venv")
    verified = verify_openfootball_history_input(
        runtime_root=runtime_root,
        raw_archive_dir=raw_archive_dir,
        receipt_path=receipt_path,
        checked_at=checked_at,
    )
    output = _strict_output_path(runtime_root, output_path, "strict-backtest-current.json")
    cache = _strict_output_path(runtime_root, cache_path, "strict-report-cache.json")
    observed_before = _history_utc(verified["observed_before"], field="observed_before")
    source_ids = verified["source_ids"]
    if not isinstance(source_ids, list):
        raise VerifiedHistoryInputError("verified history source_ids are invalid")
    fingerprint = build_input_fingerprint(
        openfootball_raw_archive=raw_archive_dir,
        observed_before=observed_before,
        source_ids=source_ids,
        candidate_evidence_dir=candidate_evidence_dir,
        prospective_lock_path=prospective_lock_path,
    )
    cached = load_cached_report(cache, output, fingerprint)
    if cached is not None:
        _validate_generated_strict_report(cached, verified)
        return {
            "status": "pass",
            "strict_report_status": "skipped_unchanged",
            "history_input": verified,
            "output": str(output),
        }
    report = build_report(
        openfootball_raw_archive=raw_archive_dir,
        observed_before=observed_before,
        source_ids=source_ids,
        candidate_evidence_dir=candidate_evidence_dir,
        prospective_lock_path=prospective_lock_path,
    )
    _validate_generated_strict_report(report, verified)
    _write_atomic(output, report)
    write_report_cache(cache, output, fingerprint)
    return {
        "status": "pass",
        "strict_report_status": "generated",
        "history_input": verified,
        "output": str(output),
    }


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.runtime.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _mirror_pointer(source: Path, target: Path) -> str:
    """Atomically mirror one small runtime pointer into a bounded target.

    The append-only runtime remains authoritative on the migrated filesystem,
    The caller chooses whether that target is a runtime projection or the
    historical repository audit path. This helper only mirrors bounded JSON
    pointers; it never copies the growing ledgers or model lock.
    """

    payload = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.mirror.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(payload).hexdigest()


def mirror_runtime_pointers(
    runtime_dir: Path,
    *,
    cycle_path: Path | None = None,
    evaluation_path: Path | None = None,
    cycle_target: Path = DEFAULT_CYCLE,
    evaluation_target: Path = DEFAULT_EVALUATION,
) -> dict[str, Any]:
    """Mirror the two bounded cycle pointers after an external-runtime run."""

    pairs = (
        (cycle_path or runtime_dir / "prospective-cycle-latest.json", cycle_target),
        (
            evaluation_path or runtime_dir / "prospective-evaluation-current.json",
            evaluation_target,
        ),
    )
    mirrored: list[dict[str, str]] = []
    for source, target in pairs:
        if source.resolve(strict=False) == target.resolve(strict=False):
            continue
        mirrored.append(
            {
                "source": str(source),
                "target": str(target),
                "sha256": _mirror_pointer(source, target),
            }
        )
    return {"status": "mirrored", "pointers": mirrored}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _archive_summary(
    path: Path,
    lock: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    physical = valid = current = conflicts = scored = 0
    legacy_or_superseded = 0
    invalid_json = non_object = invalid_shape = 0
    archive_status = "pass"
    stages: Counter[str] = Counter()
    seen_freezes: set[str] = set()
    if not path.is_file():
        archive_status = "missing"
        lines: list[str] = []
    else:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            archive_status = "unreadable"
            lines = []
    if archive_status == "pass":
        computed_model_sha = canonical_model_version_sha256(lock)
        if computed_model_sha is None or lock.get("model_version_sha256") != computed_model_sha:
            archive_status = "invalid_lock"
    if archive_status == "pass":
        for line in lines:
            if not line.strip():
                continue
            physical += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid_json += 1
                continue
            if not isinstance(row, dict):
                non_object += 1
                continue
            # Rows written under an earlier prospective lock remain immutable
            # audit evidence, but they are outside the current model window.
            # Older archive contracts did not carry
            # ``model_lock_window_started_at`` and therefore cannot pass the
            # current structural validator.  The model digest is the trust
            # boundary here: a well-formed, non-current digest is explicitly
            # classified as superseded rather than misreported as a corrupt
            # current row.  Rows with a missing/malformed digest still fail
            # closed below.
            row_model_sha = row.get("model_version_sha256")
            lock_model_sha = lock.get("model_version_sha256")
            if (
                isinstance(row_model_sha, str)
                and len(row_model_sha) == 64
                and all(char in "0123456789abcdefABCDEF" for char in row_model_sha)
                and row_model_sha != lock_model_sha
            ):
                legacy_or_superseded += 1
                continue
            try:
                freeze_key, is_current = _validate_prediction_archive_row(row, lock=lock)
            except (OSError, ValueError):
                invalid_shape += 1
                continue
            valid += 1
            if row.get("conflict") is True:
                conflicts += 1
                continue
            if not is_current:
                continue
            if freeze_key in seen_freezes:
                continue
            seen_freezes.add(freeze_key)
            current += 1
            prediction = row["prediction"]
            stage = prediction.get("freeze_stage")
            if isinstance(stage, str):
                stages[stage] += 1
    scored_value = evaluation.get("scored_n")
    if isinstance(scored_value, int) and scored_value >= 0:
        scored = scored_value
    invalid_count = invalid_json + non_object + invalid_shape
    if invalid_count:
        archive_status = "invalid_records"
    elif conflicts:
        archive_status = "conflict_records"
    elif archive_status == "pass" and current == 0:
        archive_status = "empty_current_lock"
    return (
        {
            "status": archive_status,
            "physical_records": physical,
            "valid_json_records": valid,
            "invalid_record_count": invalid_count,
            "invalid_json_records": invalid_json,
            "non_object_records": non_object,
            "invalid_shape_records": invalid_shape,
            "current_lock_records": current,
            "legacy_or_superseded_records": legacy_or_superseded + max(0, valid - current),
            "scored_records": scored,
            "conflict_records": conflicts,
            "results_used_for_selection": False,
        },
        dict(stages),
    )


def _oddstorm_summary(
    snapshot: Mapping[str, Any], *, snapshot_path: Path, bundle_sha: str | None
) -> dict[str, Any]:
    source = snapshot.get("oddstorm")
    source = source if isinstance(source, Mapping) else {}
    raw_lines = source.get("lines")
    lines: list[Any] = raw_lines if isinstance(raw_lines, list) else []
    fallback = sum(
        1
        for row in lines
        if isinstance(row, Mapping)
        and row.get("effective_at_source") == "observed_at_fallback_no_line_timestamp"
    )
    eligible = sum(
        1 for row in lines if isinstance(row, Mapping) and row.get("enters_model") is True
    )
    reasons = Counter(
        str(row.get("model_exclusion_reason"))
        for row in lines
        if isinstance(row, Mapping) and row.get("model_exclusion_reason")
    )
    return {
        "path": str(snapshot_path),
        "as_of": snapshot.get("as_of"),
        "sha256": _sha256(snapshot_path),
        "sites_bundle_sha256": bundle_sha,
        "line_count": len(lines),
        "fallback_effective_at_source_count": fallback,
        "enters_model_count": eligible,
        "exclusion_reasons": dict(reasons),
    }


def _quarantine_keys(path: Path) -> set[tuple[int, str]]:
    """Read line identities from the append-only quarantine manifest."""

    keys: set[tuple[int, str]] = set()
    if not path.is_file():
        return keys
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict):
                    continue
                number = value.get("line")
                digest = value.get("line_sha256")
                if isinstance(number, int) and isinstance(digest, str):
                    keys.add((number, digest))
    except OSError:
        return set()
    return keys


def _intelligence_ledger_summary(
    path: Path,
    *,
    quarantine_path: Path | None = None,
) -> dict[str, Any]:
    """Summarize the append-only intelligence ledger without rewriting it.

    The ledger is scanned as raw JSON rather than loaded through
    ``ObservationLedger.read`` so runtime evidence remains bounded in memory
    even when the ledger contains hundreds of thousands of records. Malformed
    lines are retained as diagnostics instead of being silently treated as
    clean history.
    """

    if quarantine_path is None:
        quarantine_path = (
            DEFAULT_INTELLIGENCE_QUARANTINE
            if path == DEFAULT_INTELLIGENCE_LEDGER
            else path.with_name(f"{path.stem}.quarantine{path.suffix}")
        )
    quarantine_keys = _quarantine_keys(quarantine_path)
    summary: dict[str, Any] = {
        "path": str(path),
        "sha256": _sha256(path),
        "quarantine_path": str(quarantine_path),
        "quarantine_sha256": _sha256(quarantine_path),
        "quarantine_entry_count": len(quarantine_keys),
        "physical_records": 0,
        "valid_json_records": 0,
        "recovered_json_records": 0,
        "read_error_count": 0,
        "quarantined_read_error_count": 0,
        "unquarantined_read_error_count": 0,
        "read_errors": [],
    }
    if not path.is_file():
        summary["status"] = "missing"
        return summary
    errors: list[dict[str, Any]] = []
    lock_handle = None
    try:
        lock_handle = path.with_name(f"{path.name}.lock").open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
        stream = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        if lock_handle is not None:
            lock_handle.close()
        summary["status"] = "unreadable"
        summary["read_errors"] = [{"line": 0, "bytes": 0, "error": str(exc)}]
        summary["read_error_count"] = 1
        return summary
    try:
        with stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                summary["physical_records"] += 1
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("observation record must be a JSON object")
                except (json.JSONDecodeError, ValueError) as exc:
                    summary["read_error_count"] += 1
                    raw_line = line.encode("utf-8", errors="replace")
                    line_key = (line_number, hashlib.sha256(raw_line).hexdigest())
                    quarantined = line_key in quarantine_keys
                    if quarantined:
                        summary["quarantined_read_error_count"] += 1
                    else:
                        summary["unquarantined_read_error_count"] += 1
                    fragments = recover_observation_fragments(line)
                    summary["recovered_json_records"] += len(fragments)
                    if len(errors) < 20:
                        errors.append(
                            {
                                "line": line_number,
                                "bytes": len(raw_line),
                                "error": str(exc),
                                "quarantined": quarantined,
                                "recovered_records": [
                                    {
                                        "offset": fragment.get("offset"),
                                        "end": fragment.get("end"),
                                        "record_digest": fragment.get("record_digest"),
                                    }
                                    for fragment in fragments
                                ],
                            }
                        )
                    continue
                summary["valid_json_records"] += 1
    finally:
        if lock_handle is not None:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
    summary["read_errors"] = errors
    if summary["read_error_count"] == 0:
        summary["status"] = "pass"
    elif summary["unquarantined_read_error_count"] == 0:
        # The original bytes remain invalid and are still excluded.  This is
        # controlled quarantine, not a green integrity result.
        summary["status"] = "quarantined_read_errors"
    else:
        summary["status"] = "diagnostic_read_errors"
    return summary


def refresh_runtime_evidence(
    *,
    cycle_path: Path = DEFAULT_CYCLE,
    evaluation_path: Path = DEFAULT_EVALUATION,
    lock_path: Path = DEFAULT_LOCK,
    snapshot_path: Path = DEFAULT_SNAPSHOT,
    bundle_path: Path = DEFAULT_BUNDLE,
    archive_path: Path = DEFAULT_ARCHIVE,
    intelligence_ledger_path: Path | None = None,
    intelligence_quarantine_path: Path | None = None,
    selection_path: Path | None = None,
    lock_integrity_path: Path | None = None,
    platform_path: Path | None = None,
    oddstorm_path: Path | None = None,
    freeze_path: Path | None = None,
    audit_output_dir: Path | None = None,
) -> dict[str, Any]:
    """Refresh existing latest pointers and return a compact audit summary."""

    # Callers that provide an isolated lock/evidence directory (notably tests
    # and offline replays) must not accidentally rewrite the repository's
    # production pointer files.  The scheduler still resolves to the normal
    # docs/evidence paths because its lock path is DEFAULT_LOCK.
    pointer_root = audit_output_dir or lock_path.parent
    lock_integrity_path = lock_integrity_path or pointer_root / DEFAULT_LOCK_INTEGRITY.name
    platform_path = platform_path or pointer_root / DEFAULT_PLATFORM.name
    oddstorm_path = oddstorm_path or pointer_root / DEFAULT_ODDSTORM.name
    freeze_path = freeze_path or pointer_root / DEFAULT_FREEZE.name

    cycle = _read(cycle_path)
    evaluation = _read(evaluation_path)
    lock = _read(lock_path)
    snapshot = _read(snapshot_path)
    checked_at = _now()
    raw_sync = cycle.get("sync")
    sync = raw_sync if isinstance(raw_sync, Mapping) else {}
    declared_snapshot_sha = sync.get("snapshot_sha256")
    actual_snapshot_sha = _sha256(snapshot_path)
    snapshot_matches = (
        isinstance(declared_snapshot_sha, str)
        and len(declared_snapshot_sha) == 64
        and declared_snapshot_sha == actual_snapshot_sha
    )
    snapshot_sha = actual_snapshot_sha
    snapshot_integrity = {
        "status": "pass" if snapshot_matches else "blocked",
        "declared_sha256": declared_snapshot_sha,
        "actual_sha256": actual_snapshot_sha,
    }
    bundle_sha = _sha256(bundle_path)
    model_sha = str(lock.get("model_version_sha256") or "")
    archive, stage_counts = _archive_summary(archive_path, lock, evaluation)
    pending = int(evaluation.get("pending_n") or 0)
    scored = int(evaluation.get("scored_n") or 0)
    live_results = cycle.get("capture", {}).get("live_results", {})
    market_audit = cycle.get("market_audit") or {}
    cycle_status = str(cycle.get("status") or "unknown")
    runtime_failures: list[str] = []
    if cycle_status not in SUCCESSFUL_CYCLE_STATUSES:
        runtime_failures.append(f"cycle.status:{cycle_status}")
    if not snapshot_matches:
        runtime_failures.append("cycle.sync.snapshot_sha256:mismatch")
    archive_status = str(archive.get("status") or "unknown")
    if archive_status != "pass":
        runtime_failures.append(f"prediction_archive:{archive_status}")
    if bundle_sha is None:
        runtime_failures.append("sites_bundle:missing_or_unreadable")
    expected_current = scored + pending
    current_records = archive.get("current_lock_records")
    if archive_status == "pass" and current_records != expected_current:
        runtime_failures.append(
            "prediction_archive.current_lock_records:evaluation_count_mismatch"
        )
    capture = cycle.get("capture")
    capture = capture if isinstance(capture, Mapping) else {}
    cycle_predictions = capture.get("predictions")
    if (
        archive_status == "pass"
        and isinstance(cycle_predictions, int)
        and not isinstance(cycle_predictions, bool)
        and isinstance(current_records, int)
        and cycle_predictions > current_records
    ):
        runtime_failures.append(
            "prediction_archive.current_lock_records:cycle_prediction_mismatch"
        )
    cycle_exit_code = 0 if not runtime_failures else 1
    intelligence_ledger = (
        _intelligence_ledger_summary(
            intelligence_ledger_path,
            quarantine_path=intelligence_quarantine_path,
        )
        if intelligence_ledger_path is not None
        else None
    )

    updated: list[str] = []
    if lock_integrity_path.is_file():
        report = _read(lock_integrity_path)
        report.update(
            {
                "checked_at": checked_at,
                "lock_sha256": _sha256(lock_path),
                "model_version_sha256": model_sha,
            }
        )
        report["prediction_archive"] = archive
        if intelligence_ledger is not None:
            report["intelligence_ledger"] = intelligence_ledger
        report["latest_cycle"] = {
            "status": cycle_status,
            "started_at": cycle.get("started_at"),
            "finished_at": cycle.get("finished_at"),
            "exit_code": cycle_exit_code,
            "snapshot_sha256": snapshot_sha,
            "predictions_appended": cycle.get("capture", {}).get("appended", 0),
            "duplicates_skipped": cycle.get("capture", {}).get("skipped_duplicate", 0),
            "blocked_predictions": cycle.get("capture", {}).get("blocked", 0),
            "live_result_conflicts": live_results.get("conflict_count", 0),
            "market_audit_conflicts": market_audit.get("conflicts", 0),
            "market_audit_diagnostics": market_audit.get("diagnostics", 0),
            "market_audit_diagnostic_reasons": market_audit.get("diagnostic_reasons", {}),
        }
        raw_sites_bundle = report.get("sites_bundle")
        sites_bundle: dict[str, Any] = (
            raw_sites_bundle if isinstance(raw_sites_bundle, dict) else {}
        )
        report["sites_bundle"] = {
            **sites_bundle,
            "snapshot_source_as_of": cycle.get("sync", {}).get("as_of"),
            "sha256": bundle_sha,
            "identity_status": "file_present_source_identity_not_verified",
        }
        decision = (
            "Runtime evidence is blocked: " + ", ".join(runtime_failures) + "."
            if runtime_failures
            else "Lock, snapshot and archive runtime integrity pass; the Sites bundle file is present but this receipt does not establish source synchronization. The prospective window remains pending "
            f"because the {pending} current rows have not reached a verified final result."
            if cycle_status != "passed"
            else "Lock, snapshot, archive and prospective-window runtime integrity pass; the Sites bundle file is present but source synchronization is not established here."
        )
        if intelligence_ledger is not None and intelligence_ledger["read_error_count"]:
            decision += (
                f" The intelligence ledger retains {intelligence_ledger['read_error_count']} diagnostic read error(s); "
                "the original history is preserved and the error is not treated as model input."
            )
        report["decision"] = decision
        _write_atomic(lock_integrity_path, report)
        updated.append(str(lock_integrity_path))

    if platform_path.is_file():
        report = _read(platform_path)
        # This helper refreshes only runtime pointers.  It does not rerun the
        # platform gates or bind them to the current strict report, so retain
        # the original verification clock and record the narrower event under
        # a separate field.
        report["runtime_pointer_refreshed_at"] = checked_at
        raw_verification = report.get("verification")
        verification: dict[str, Any] = (
            raw_verification if isinstance(raw_verification, dict) else {}
        )
        verification.update(
            {
                "latest_cycle": f"exit {cycle_exit_code}; snapshot {snapshot_sha}; live-result conflicts {live_results.get('conflict_count', 0)}; lock_model_sha256={model_sha}",
                "latest_evaluation": f"{evaluation.get('status')}; scored_n={scored}; pending_n={pending}; result_conflicts={evaluation.get('result_conflicts', 0)}; invalid_records={len(evaluation.get('invalid_records') or [])}",
                "sites_bundle": f"file present only; source snapshot identity not verified; observed cycle as_of {cycle.get('sync', {}).get('as_of')}; SHA-256 {bundle_sha}",
            }
        )
        if intelligence_ledger is not None:
            verification["intelligence_ledger"] = (
                f"{intelligence_ledger['status']}; physical_records={intelligence_ledger['physical_records']}; "
                f"valid_json_records={intelligence_ledger['valid_json_records']}; "
                f"read_error_count={intelligence_ledger['read_error_count']}; "
                f"sha256={intelligence_ledger['sha256']}"
            )
        verification["market_audit"] = (
            f"diagnostics={market_audit.get('diagnostics', 0)}; "
            f"reasons={market_audit.get('diagnostic_reasons', {})}; "
            f"conflicts={market_audit.get('conflicts', 0)}"
        )
        ledger_note = ""
        if intelligence_ledger is not None and intelligence_ledger["read_error_count"]:
            ledger_note = (
                f" The intelligence ledger has {intelligence_ledger['read_error_count']} diagnostic read error(s); "
                "they remain excluded from model input."
            )
        report["decision"] = (
            "Runtime evidence is blocked: " + ", ".join(runtime_failures) + "."
            if runtime_failures
            else (
                "Operational cycle, snapshot and archive integrity pass; Sites bundle source synchronization is not established by this receipt. Release remains blocked by the independent "
                "statistical, coverage and prospective gates recorded in the current maturity report."
                + ledger_note
            )
        )
        report["verification"] = verification
        _write_atomic(platform_path, report)
        updated.append(str(platform_path))

    if oddstorm_path.is_file():
        report = _read(oddstorm_path)
        report["generated_at"] = checked_at
        report["runtime_snapshot"] = _oddstorm_summary(
            snapshot, snapshot_path=snapshot_path, bundle_sha=bundle_sha
        )
        raw_verification = report.get("verification")
        verification = raw_verification if isinstance(raw_verification, dict) else {}
        verification["prospective_cycle"] = (
            f"exit {cycle_exit_code}; snapshot {snapshot_sha}; live-result conflicts {live_results.get('conflict_count', 0)}; Sites bundle file observed without source-identity verification"
        )
        report["verification"] = verification
        _write_atomic(oddstorm_path, report)
        updated.append(str(oddstorm_path))

    if freeze_path.is_file():
        report = _read(freeze_path)
        report.update(
            {
                "checked_at": checked_at,
                "lock_model_version_sha256": model_sha,
                "current_lock_rows": archive["current_lock_records"],
                "freeze_stage_counts": {
                    stage: stage_counts.get(stage, 0)
                    for stage in (
                        "t_minus_24h",
                        "t_minus_6h",
                        "t_minus_90m",
                        "lineup_confirmation",
                    )
                },
            }
        )
        raw_runtime = report.get("runtime")
        runtime: dict[str, Any] = raw_runtime if isinstance(raw_runtime, dict) else {}
        runtime.update(
            {
                "latest_cycle_status": cycle_status,
                "latest_cycle_exit_code": cycle_exit_code,
                "latest_snapshot_as_of": cycle.get("sync", {}).get("as_of"),
                "latest_snapshot_sha256": snapshot_sha,
                "sites_bundle_sha256": bundle_sha,
                "scored_n": scored,
                "pending_n": pending,
                "result_conflicts": evaluation.get("result_conflicts", 0),
                "live_result_conflicts": live_results.get("conflict_count", 0),
            }
        )
        report["runtime"] = runtime
        raw_checks = report.get("checks")
        checks: dict[str, Any] = raw_checks if isinstance(raw_checks, dict) else {}
        checks["result_fields_present"] = scored > 0
        report["checks"] = checks
        report["decision"] = (
            "Runtime evidence is blocked: " + ", ".join(runtime_failures) + "."
            if runtime_failures
            else f"Current-lock freezes remain causally valid and append-only; {pending} rows await verified results and later real cutoffs."
            if cycle_status != "passed"
            else "Current-lock freezes and prospective scoring pass."
        )
        _write_atomic(freeze_path, report)
        updated.append(str(freeze_path))

    if selection_path is None:
        candidate = pointer_root / DEFAULT_SELECTION.name
        selection_path = candidate if candidate.is_file() else None
    if selection_path is not None and selection_path.is_file():
        # Keep the model-selection evidence aligned with the lock used by the
        # same cycle.  This is an audit pointer only; it cannot promote a
        # pending lock or change model parameters.
        from league_platform.model_selection_audit import build_model_selection_audit

        selection = build_model_selection_audit(selection_path.parent, prospective_lock=lock)
        _write_atomic(selection_path, selection)
        updated.append(str(selection_path))

    result: dict[str, Any] = {
        "status": "pass" if not runtime_failures else "blocked",
        "failures": runtime_failures,
        "updated": updated,
        "checked_at": checked_at,
        "snapshot_sha256": snapshot_sha,
        "snapshot_integrity": snapshot_integrity,
        "bundle_sha256": bundle_sha,
        "archive": archive,
        "freeze_stage_counts": stage_counts,
    }
    if intelligence_ledger is not None:
        result["intelligence_ledger"] = intelligence_ledger
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="optional root for growing snapshot, archive and ledger inputs; no files are moved automatically",
    )
    parser.add_argument(
        "--cycle",
        type=Path,
        default=None,
        help="cycle evidence; defaults beside --runtime-dir when supplied",
    )
    parser.add_argument(
        "--evaluation",
        type=Path,
        default=None,
        help="evaluation evidence; defaults beside --runtime-dir when supplied",
    )
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--intelligence-ledger", type=Path)
    parser.add_argument(
        "--intelligence-quarantine",
        type=Path,
        default=None,
        help="append-only quarantine manifest (defaults beside --intelligence-ledger)",
    )
    parser.add_argument(
        "--audit-output-dir",
        type=Path,
        default=None,
        help=(
            "optional root for mutable audit pointers; the model lock remains "
            "a read-only input at --lock"
        ),
    )
    parser.add_argument(
        "--mirror-target-dir",
        type=Path,
        default=None,
        help=(
            "optional root for canonical prospective-cycle/evaluation pointer "
            "copies; defaults to docs/evidence for backward compatibility"
        ),
    )
    parser.add_argument(
        "--require-passed",
        action="store_true",
        help="return nonzero after printing when runtime evidence is blocked",
    )
    parser.add_argument(
        "--strict-report-from-openfootball-history",
        action="store_true",
        help="verify the fixed durable history receipt, then build the formal v260 report",
    )
    parser.add_argument("--openfootball-history-receipt", type=Path)
    parser.add_argument("--openfootball-raw-archive", type=Path)
    parser.add_argument("--strict-report-output", type=Path)
    parser.add_argument("--strict-report-cache", type=Path)
    parser.add_argument("--candidate-evidence-dir", type=Path)
    parser.add_argument("--strict-report-prospective-lock", type=Path)
    args = parser.parse_args(argv)
    if args.strict_report_from_openfootball_history:
        required = {
            "--runtime-dir": args.runtime_dir,
            "--openfootball-history-receipt": args.openfootball_history_receipt,
            "--openfootball-raw-archive": args.openfootball_raw_archive,
            "--strict-report-output": args.strict_report_output,
            "--strict-report-cache": args.strict_report_cache,
            "--candidate-evidence-dir": args.candidate_evidence_dir,
            "--strict-report-prospective-lock": args.strict_report_prospective_lock,
        }
        missing = [flag for flag, value in required.items() if value is None]
        if missing:
            parser.error(
                "strict history report mode requires " + ", ".join(sorted(missing))
            )
        try:
            result = run_strict_report_from_verified_history(
                runtime_root=args.runtime_dir,
                raw_archive_dir=args.openfootball_raw_archive,
                receipt_path=args.openfootball_history_receipt,
                output_path=args.strict_report_output,
                cache_path=args.strict_report_cache,
                candidate_evidence_dir=args.candidate_evidence_dir,
                prospective_lock_path=args.strict_report_prospective_lock,
            )
        except (OSError, TypeError, ValueError, VerifiedHistoryInputError) as exc:
            sys.stderr.write(
                json.dumps(
                    {"status": "blocked", "error": str(exc)},
                    ensure_ascii=False,
                )
                + "\n"
            )
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0
    runtime = resolve_runtime_paths(args.runtime_dir or DEFAULT_RUNTIME_DIR)
    cycle_path = (
        args.cycle
        if args.cycle is not None
        else runtime.cycle_evidence
        if args.runtime_dir is not None
        else DEFAULT_CYCLE
    )
    evaluation_path = (
        args.evaluation
        if args.evaluation is not None
        else runtime.evaluation_evidence
        if args.runtime_dir is not None
        else DEFAULT_EVALUATION
    )
    result = refresh_runtime_evidence(
        cycle_path=cycle_path,
        evaluation_path=evaluation_path,
        lock_path=args.lock,
        snapshot_path=args.snapshot or runtime.live_path,
        bundle_path=args.bundle,
        archive_path=args.archive or runtime.prediction_archive,
        intelligence_ledger_path=args.intelligence_ledger or runtime.intelligence_ledger,
        intelligence_quarantine_path=args.intelligence_quarantine
        or runtime.intelligence_quarantine,
        audit_output_dir=args.audit_output_dir,
    )
    if args.runtime_dir is not None:
        mirror_target_dir = args.mirror_target_dir or ROOT
        result["mirrored_pointers"] = mirror_runtime_pointers(
            runtime.root,
            cycle_path=cycle_path,
            evaluation_path=evaluation_path,
            cycle_target=mirror_target_dir / runtime.cycle_evidence.name,
            evaluation_target=mirror_target_dir / runtime.evaluation_evidence.name,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if args.require_passed and result.get("status") != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["mirror_runtime_pointers", "refresh_runtime_evidence"]
