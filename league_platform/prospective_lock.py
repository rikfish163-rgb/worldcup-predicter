"""Create an explicit prospective-window lock migration.

The prospective archive is a causal experiment, not an always-on training
loop.  When a locked implementation changes, the old rows must remain
auditable while a new model-file inventory and evaluation window are created.
This module makes that operation deterministic and fail-closed instead of
requiring an operator to hand-edit a large JSON lock file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
)
from league_platform.openfootball_history_sync import (
    RECEIPT_ARCHIVE_NAME,
    RECEIPT_SCHEMA_VERSION,
)
from league_platform.openfootball_raw_archive import (
    load_verified_openfootball_archive,
)
from league_platform.source_rights import POLICY_VERSION
from league_platform.sources.openfootball_verified import (
    VerifiedOpenFootballHistorySource,
)


V260_TRAINING_ADMISSION_SCHEMA = "matchline.prospective_training_admission.v1"
V260_MANDATORY_MODEL_ADMISSION_FILES = (
    "league_platform/prospective_lock.py",
    "league_platform/source_rights.py",
    "league_platform/openfootball_raw_archive.py",
    "league_platform/openfootball_history_sync.py",
    "league_platform/sources/openfootball_verified.py",
    "league_platform/model_admission.py",
    "league_platform/strict_report.py",
    "league_platform/store.py",
    "league_platform/runtime_paths.py",
    "league_platform/prospective_cycle.py",
    "league_platform/platform_verification.py",
    "league_platform/maturity_validator.py",
)
_HISTORY_RECEIPT_FIELDS = {
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

# The cycle consumes a v260 lock as a pre-committed input.  Keep this
# contract local to the lock module so orchestration code does not have to
# infer identity from a model hash alone.  A runtime-only candidate may live
# at an isolated path, but its semantic fingerprint (and exact bytes) must be
# explicit and reproducible before any data-producing stage starts.  Formal
# promotion binds the candidate and approved inputs to one resolved file.
PROSPECTIVE_LOCK_SCHEMA_VERSION = "1.0.0"
PROSPECTIVE_LOCK_STATUSES = frozenset({"pending_prospective_window", "passed"})
_LOCK_IDENTITY_FIELDS = (
    "schema_version",
    "status",
    "locked_at",
    "evaluation_window_started_at",
    "evaluation_window",
    "model_name",
    "freeze_model_name",
    "model_version_sha256",
    "model_files",
    "policy_version",
    "observed_before",
    "source_ids",
    "training_admission",
)
_TRAINING_ADMISSION_REQUIRED_FIELDS = (
    "schema_version",
    "status",
    "policy_version",
    "observed_before",
    "source_ids",
    "training_admission_sha256",
    "raw_admission_sha256",
    "source_manifest_sha256",
    "parser_contract_sha256",
    "raw_rows_sha256",
    "domain_rows_sha256",
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(
    lock: Mapping[str, Any],
    *,
    repository_root: Path,
    additional_model_files: Iterable[str] = (),
) -> list[dict[str, str]]:
    previous = lock.get("model_files")
    if not isinstance(previous, list) or not previous:
        raise ValueError("prospective lock model_files is missing")
    entries: list[Mapping[str, Any]] = list(previous)
    known_paths = {
        entry.get("path")
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("path"), str)
    }
    for display_path in additional_model_files:
        if not isinstance(display_path, str) or not display_path.strip():
            raise ValueError("additional model file paths must be non-empty strings")
        if display_path not in known_paths:
            entries.append({"path": display_path})
            known_paths.add(display_path)
    current: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            raise ValueError(f"model file entry {index} is invalid")
        display_path = str(entry["path"])
        path = Path(display_path)
        if not path.is_absolute():
            path = repository_root / path
        if not path.is_file():
            raise ValueError(f"model file is missing: {display_path}")
        current.append({"path": display_path, "sha256": _sha256(path)})
    return current


def _open_absolute_directory_nofollow(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("repository_root must be an absolute confined path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _sha256_v260_model_file(repository_root: Path, display_path: str) -> str:
    relative = Path(display_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"v260 model file path escapes repository: {display_path}")
    directory_descriptor = _open_absolute_directory_nofollow(repository_root)
    try:
        for part in relative.parts[:-1]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise ValueError(
                    f"v260 model file path contains a missing or symlinked directory: {display_path}"
                ) from exc
            os.close(directory_descriptor)
            directory_descriptor = child
        try:
            descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise ValueError(
                f"v260 model file is missing or is a symlink: {display_path}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 32 * 1024 * 1024:
                raise ValueError(f"v260 model file is invalid: {display_path}")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                total != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError(f"v260 model file changed while hashing: {display_path}")
            return digest.hexdigest()
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_descriptor)


def _v260_inventory(
    lock: Mapping[str, Any],
    *,
    repository_root: Path,
) -> list[dict[str, str]]:
    previous = lock.get("model_files")
    if not isinstance(previous, list) or not previous:
        raise ValueError("prospective lock model_files is missing")
    paths: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(previous):
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"path", "sha256"}
            or not isinstance(entry.get("path"), str)
            or not _valid_sha256(entry.get("sha256"))
        ):
            raise ValueError(f"v259 model file entry {index} is invalid")
        display_path = str(entry["path"])
        if display_path in seen:
            raise ValueError(f"v259 model file path is duplicated: {display_path}")
        paths.append(display_path)
        seen.add(display_path)
    for display_path in V260_MANDATORY_MODEL_ADMISSION_FILES:
        if display_path not in seen:
            paths.append(display_path)
            seen.add(display_path)
    return [
        {
            "path": display_path,
            "sha256": _sha256_v260_model_file(repository_root, display_path),
        }
        for display_path in paths
    ]


def _model_version(lock: Mapping[str, Any], files: list[dict[str, str]]) -> str:
    """Return a deterministic digest for the locked implementation inventory."""

    return hashlib.sha256(
        _canonical(
            {
                "model_name": lock.get("model_name"),
                "freeze_model_name": lock.get("freeze_model_name"),
                "model_files": files,
            }
        )
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
        and value != "0" * 64
    )


def canonical_model_version_sha256(lock: Mapping[str, Any]) -> str | None:
    """Recompute the aggregate implementation identity for a lock.

    The aggregate is intentionally derived from the exact ordered model-file
    inventory and the two model labels.  A lock with individually valid files
    but a stale aggregate is not an authorized lock identity.
    """

    model_name = lock.get("model_name")
    freeze_model_name = lock.get("freeze_model_name")
    files = lock.get("model_files")
    if (
        not isinstance(model_name, str)
        or not model_name.strip()
        or not isinstance(freeze_model_name, str)
        or not freeze_model_name.strip()
        or not isinstance(files, list)
        or not files
    ):
        return None
    for entry in files:
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"path", "sha256"}
            or not isinstance(entry.get("path"), str)
            or not entry["path"].strip()
            or not _valid_sha256(entry.get("sha256"))
        ):
            return None
    return hashlib.sha256(
        _canonical(
            {
                "model_name": model_name,
                "freeze_model_name": freeze_model_name,
                "model_files": files,
            }
        )
    ).hexdigest()


def lock_identity_fingerprint(lock: Mapping[str, Any]) -> str:
    """Return the semantic fingerprint used to compare candidate/current locks."""

    missing = [field for field in _LOCK_IDENTITY_FIELDS if field not in lock]
    if missing:
        raise ValueError(
            "prospective lock identity is missing required fields: " + ", ".join(missing)
        )
    return hashlib.sha256(
        _canonical({field: lock[field] for field in _LOCK_IDENTITY_FIELDS})
    ).hexdigest()


def _resolve_lock_model_file(repository_root: Path, display_path: str) -> Path:
    """Resolve a lock-listed file without following a symlink component."""

    relative = Path(display_path)
    if not relative.parts or "\x00" in display_path:
        raise ValueError(f"prospective lock model file path is invalid: {display_path}")
    if relative.is_absolute():
        resolved = relative
    else:
        if ".." in relative.parts:
            raise ValueError(f"prospective lock model file path escapes repository: {display_path}")
        resolved = repository_root / relative
    if ".." in resolved.parts:
        raise ValueError(f"prospective lock model file path escapes repository: {display_path}")
    current = Path(resolved.anchor)
    for index, part in enumerate(resolved.parts[1:], start=1):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"prospective lock model file is missing: {display_path}") from exc
        except OSError as exc:
            raise ValueError(f"prospective lock model file cannot be inspected: {display_path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"prospective lock model file path is symlinked: {display_path}")
        if index < len(resolved.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"prospective lock model file parent is not a directory: {display_path}")
    try:
        resolved.relative_to(Path("/dev/shm").resolve(strict=True))
    except ValueError:
        pass
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"prospective lock model file path cannot be resolved: {display_path}") from exc
    else:
        raise ValueError(f"prospective lock model file is volatile: {display_path}")
    return resolved


def _stable_model_file_sha256(path: Path, display_path: str) -> str:
    """Hash one regular model file while detecting replacement/truncation."""

    try:
        parent_descriptor = _open_absolute_directory_nofollow(path.parent)
    except OSError as exc:
        raise ValueError(f"prospective lock model file parent is unsafe: {display_path}") from exc
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ValueError(f"prospective lock model file is missing or symlinked: {display_path}") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"prospective lock model file is not regular: {display_path}")
            # Model implementation files are source/config artifacts.  Bound
            # reads so a forged lock cannot make a cycle consume unbounded
            # memory or CPU before the first gate.
            max_bytes = 64 * 1024 * 1024
            if before.st_size < 0 or before.st_size > max_bytes:
                raise ValueError(f"prospective lock model file is too large: {display_path}")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
                if total > max_bytes:
                    raise ValueError(f"prospective lock model file is too large: {display_path}")
            after = os.fstat(descriptor)
            if (
                total != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError(f"prospective lock model file changed while hashing: {display_path}")
            return digest.hexdigest()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _validate_training_admission(lock: Mapping[str, Any]) -> None:
    """Validate the v260 raw-history admission carried by a lock."""

    if lock.get("policy_version") != POLICY_VERSION or POLICY_VERSION != "v260":
        raise ValueError("prospective lock policy_version is not v260")
    source_ids = sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    if lock.get("source_ids") != source_ids:
        raise ValueError("prospective lock source_ids do not match the v260 allowlist")
    observed_before = lock.get("observed_before")
    if not isinstance(observed_before, str):
        raise ValueError("prospective lock observed_before is invalid")
    observed = _utc(observed_before)
    if observed.isoformat() != observed_before:
        raise ValueError("prospective lock observed_before is not canonical UTC")
    admission = lock.get("training_admission")
    if not isinstance(admission, Mapping):
        raise ValueError("prospective lock training_admission is missing")
    missing = [field for field in _TRAINING_ADMISSION_REQUIRED_FIELDS if field not in admission]
    if missing:
        raise ValueError(
            "prospective lock training_admission is incomplete: " + ", ".join(missing)
        )
    if (
        admission.get("schema_version") != V260_TRAINING_ADMISSION_SCHEMA
        or admission.get("status") != "training_admitted"
        or admission.get("policy_version") != "v260"
        or admission.get("observed_before") != observed_before
        or admission.get("source_ids") != source_ids
    ):
        raise ValueError("prospective lock training_admission identity is invalid")
    for field in _TRAINING_ADMISSION_REQUIRED_FIELDS:
        if field.endswith("sha256") and not _valid_sha256(admission.get(field)):
            raise ValueError(f"prospective lock training_admission {field} is invalid")


def validate_lock_identity(
    lock: Mapping[str, Any],
    *,
    repository_root: Path | None = None,
    require_training_admission: bool = True,
) -> dict[str, str]:
    """Validate the complete lock identity before a cycle may write data."""

    if not isinstance(lock, Mapping):
        raise ValueError("prospective lock root must be an object")
    if lock.get("schema_version") != PROSPECTIVE_LOCK_SCHEMA_VERSION:
        raise ValueError("prospective lock schema_version is invalid")
    if lock.get("status") not in PROSPECTIVE_LOCK_STATUSES:
        raise ValueError("prospective lock status is not scoreable")
    for field in ("model_name", "freeze_model_name", "evaluation_window"):
        value = lock.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"prospective lock {field} is missing")
    locked_at = _utc(str(lock.get("locked_at")))
    window_started = _utc(str(lock.get("evaluation_window_started_at")))
    if window_started <= locked_at:
        raise ValueError("prospective lock evaluation window does not start after locked_at")
    for field in (
        "results_not_used_for_selection",
        "sample_requirements_met",
        "all_required_targets_scored",
        "prediction_freezes_verified",
    ):
        if not isinstance(lock.get(field), bool):
            raise ValueError(f"prospective lock {field} is invalid")
    if lock.get("status") == "pending_prospective_window":
        if lock.get("results_not_used_for_selection") is not True:
            raise ValueError("pending prospective lock must exclude results from selection")
        if any(lock.get(field) is not False for field in (
            "sample_requirements_met",
            "all_required_targets_scored",
            "prediction_freezes_verified",
        )):
            raise ValueError("pending prospective lock has an invalid completion status")
    else:
        if any(lock.get(field) is not True for field in (
            "results_not_used_for_selection",
            "sample_requirements_met",
            "all_required_targets_scored",
            "prediction_freezes_verified",
        )):
            raise ValueError("passed prospective lock is missing completion evidence")
        for field in (
            "prospective_evaluation_completed_at",
            "prospective_evaluation_generated_at",
        ):
            _utc(str(lock.get(field)))
        scored_n = lock.get("prospective_evaluation_scored_n")
        if isinstance(scored_n, bool) or not isinstance(scored_n, int) or scored_n < 0:
            raise ValueError("passed prospective lock scored_n is invalid")
        if not _valid_sha256(lock.get("prospective_evaluation_digest")):
            raise ValueError("passed prospective lock evaluation digest is invalid")
    files = lock.get("model_files")
    if not isinstance(files, list) or not files:
        raise ValueError("prospective lock model_files is missing")
    root = (repository_root or Path.cwd()).absolute()
    seen: set[str] = set()
    for index, entry in enumerate(files):
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"path", "sha256"}
            or not isinstance(entry.get("path"), str)
            or not entry["path"].strip()
            or not _valid_sha256(entry.get("sha256"))
        ):
            raise ValueError(f"prospective lock model file entry {index} is invalid")
        display_path = str(entry["path"])
        if display_path in seen:
            raise ValueError(f"prospective lock model file path is duplicated: {display_path}")
        seen.add(display_path)
        actual = _stable_model_file_sha256(
            _resolve_lock_model_file(root, display_path),
            display_path,
        )
        if actual != entry["sha256"]:
            raise ValueError(f"prospective lock model file hash mismatch: {display_path}")
    expected_model_version = canonical_model_version_sha256(lock)
    if expected_model_version is None:
        raise ValueError("prospective lock model identity is invalid")
    if lock.get("model_version_sha256") != expected_model_version:
        raise ValueError("prospective lock model_version_sha256 aggregate mismatch")
    if require_training_admission:
        _validate_training_admission(lock)
    return {
        "model_version_sha256": expected_model_version,
        "lock_identity_sha256": lock_identity_fingerprint(lock),
        "locked_at": locked_at.isoformat(),
        "evaluation_window_started_at": window_started.isoformat(),
    }


def read_validated_lock(
    path: Path | str,
    *,
    repository_root: Path | None = None,
    require_training_admission: bool = True,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Read a lock through a no-follow, stable snapshot and validate it."""

    lock_path = require_lock_path(path, field="lock_path", must_exist=True)
    payload = _read_regular_nofollow(lock_path)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("prospective lock is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("prospective lock root must be an object")
    identity = validate_lock_identity(
        value,
        repository_root=repository_root,
        require_training_admission=require_training_admission,
    )
    bytes_sha256 = hashlib.sha256(payload).hexdigest()
    identity = {
        **identity,
        "path": str(lock_path),
        # ``sha256`` is retained for compatibility with existing evidence;
        # these explicit aliases make clear that the digest covers the exact
        # lock bytes, not merely the semantic model identity.
        "sha256": bytes_sha256,
        "bytes_sha256": bytes_sha256,
        "lock_bytes_sha256": bytes_sha256,
        "fingerprint": identity["lock_identity_sha256"],
    }
    return value, identity


def assert_lock_identities_match(
    candidate: Mapping[str, Any],
    approved: Mapping[str, Any],
) -> None:
    """Fail closed when a candidate is not the approved active lock identity."""

    candidate_fingerprint = candidate.get("lock_identity_sha256")
    approved_fingerprint = approved.get("lock_identity_sha256")
    if not isinstance(candidate_fingerprint, str) or not isinstance(approved_fingerprint, str):
        raise ValueError("candidate/approved lock identity is missing")
    if candidate_fingerprint != approved_fingerprint:
        raise ValueError("candidate/approved lock identity mismatch")
    # A semantic match alone is not enough for an auditable hand-off.  The
    # evidence must identify the exact path and bytes that were inspected.
    # Validated identities always carry both fields and therefore take this
    # stricter branch; legacy callers with only a fingerprint retain the
    # historical comparison above.
    for field in ("path", "sha256"):
        candidate_value = candidate.get(field)
        approved_value = approved.get(field)
        if not isinstance(candidate_value, str) or not isinstance(approved_value, str):
            raise ValueError("candidate/approved lock identity is missing")
        if candidate_value != approved_value:
            raise ValueError(f"candidate/approved lock {field} mismatch")


def _require_durable_path(
    value: Path | str,
    *,
    field: str,
    expected: str,
    must_exist: bool,
) -> Path:
    path = Path(value)
    if expected not in {"file", "directory"}:
        raise ValueError(f"{field} has an unsupported path contract")
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute durable path")
    if ".." in path.parts:
        raise ValueError(f"{field} must not contain parent traversal")
    current = Path(path.anchor)
    missing = False
    missing_index: int | None = None
    for index, part in enumerate(path.parts[1:], start=1):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing = True
            missing_index = index
            break
        except OSError as exc:
            raise ValueError(f"{field} cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{field} must not contain a symlink")
        is_last = index == len(path.parts) - 1
        if not is_last and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{field} has a non-directory path component")
        if is_last and expected == "directory" and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{field} must be an existing durable directory")
        if is_last and expected == "file" and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{field} must be an existing durable regular file")
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(Path("/dev/shm").resolve(strict=True))
    except ValueError:
        pass
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{field} must be a resolvable durable path") from exc
    else:
        raise ValueError(f"{field} must be on durable storage, not /dev/shm")
    if must_exist and missing:
        label = "directory" if expected == "directory" else "regular file"
        raise ValueError(f"{field} must be an existing durable {label}")
    if not must_exist and missing_index is not None and missing_index != len(path.parts) - 1:
        raise ValueError(f"{field} parent must be an existing durable directory")
    return path


def require_lock_path(
    value: Path | str,
    *,
    field: str = "lock_path",
    must_exist: bool = True,
) -> Path:
    """Resolve and validate a lock path at a trust boundary.

    Lock files are control-plane inputs, not runtime pointers. Callers may
    pass a relative path for local compatibility, but the path is normalized
    to an absolute, symlink-free regular file before it is opened. Keeping
    this check in the lock module lets the cycle and direct readers enforce
    the same durable-path contract.
    """

    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return _require_durable_path(
        path,
        field=field,
        expected="file",
        must_exist=must_exist,
    )


def _read_regular_nofollow(path: Path, *, max_bytes: int = 1024 * 1024) -> bytes:
    try:
        parent_descriptor = _open_absolute_directory_nofollow(path.parent)
    except OSError as exc:
        raise ValueError(f"receipt path contains a missing or symlinked directory: {path}") from exc
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ValueError(f"receipt path is missing or is a symlink: {path}") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size < 1
                or before.st_size > max_bytes
            ):
                raise ValueError(f"receipt is not a bounded regular file: {path}")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"receipt exceeds its size limit: {path}")
            after = os.fstat(descriptor)
            if (
                total != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError(f"receipt changed while being read: {path}")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _load_history_receipt(raw_root: Path, receipt_path: Path) -> dict[str, Any]:
    payload = _read_regular_nofollow(receipt_path)
    try:
        receipt = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenFootball history receipt is not valid JSON") from exc
    if not isinstance(receipt, dict) or set(receipt) != _HISTORY_RECEIPT_FIELDS:
        raise ValueError("OpenFootball history receipt fields are invalid")
    digest = hashlib.sha256(payload).hexdigest()
    immutable = (
        raw_root
        / RECEIPT_ARCHIVE_NAME
        / "sha256"
        / digest[:2]
        / f"{digest}.json"
    )
    if _read_regular_nofollow(immutable) != payload:
        raise ValueError("OpenFootball history receipt immutable copy is missing or changed")
    if payload != _canonical(receipt) + b"\n":
        raise ValueError("OpenFootball history receipt is not canonical producer output")
    return receipt


def _history_evidence(
    *,
    raw_root: Path,
    receipt_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt = _load_history_receipt(raw_root, receipt_path)
    source_ids = sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("status") != "verified_raw_history"
        or receipt.get("source_count") != 66
        or receipt.get("source_ids") != source_ids
        or receipt.get("errors") != []
    ):
        raise ValueError("OpenFootball history receipt contract is invalid")
    for field in (
        "admission_sha256",
        "rows_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
    ):
        if not _valid_sha256(receipt.get(field)):
            raise ValueError(f"OpenFootball history receipt {field} is invalid")
    for field in (
        "parsed_fixture_count",
        "finished_fixture_count",
        "unfinished_fixture_count",
    ):
        value = receipt.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"OpenFootball history receipt {field} is invalid")
    if receipt["parsed_fixture_count"] != (
        receipt["finished_fixture_count"] + receipt["unfinished_fixture_count"]
    ):
        raise ValueError("OpenFootball history receipt row counts do not reconcile")
    observed_before = _utc(str(receipt.get("observed_at")))
    raw_candidate = load_verified_openfootball_archive(
        raw_root,
        source_ids=source_ids,
        observed_before=observed_before,
    )
    source = VerifiedOpenFootballHistorySource(
        raw_root,
        observed_before=observed_before,
        source_ids=source_ids,
    )
    admission = source.admission_manifest()
    comparisons = {
        "admission_sha256": raw_candidate.get("admission_sha256"),
        "rows_sha256": raw_candidate.get("rows_sha256"),
        "source_manifest_sha256": raw_candidate.get("source_manifest_sha256"),
        "parser_contract_sha256": raw_candidate.get("parser_contract_sha256"),
    }
    if any(receipt.get(field) != value for field, value in comparisons.items()):
        raise ValueError("OpenFootball history receipt does not match raw replay")
    counts = admission.get("counts")
    if not isinstance(counts, Mapping) or (
        counts.get("raw_rows") != receipt["parsed_fixture_count"]
        or counts.get("finished_admitted") != receipt["finished_fixture_count"]
        or counts.get("upcoming_isolated") != receipt["unfinished_fixture_count"]
    ):
        raise ValueError("OpenFootball history receipt does not match domain projection")
    if (
        admission.get("status") != "training_admitted"
        or admission.get("training_admitted") is not True
        or admission.get("observed_before") != observed_before.isoformat()
        or admission.get("source_ids") != source_ids
        or admission.get("raw_admission_sha256") != raw_candidate.get("admission_sha256")
        or admission.get("source_manifest_sha256")
        != raw_candidate.get("source_manifest_sha256")
        or admission.get("parser_contract_sha256")
        != raw_candidate.get("parser_contract_sha256")
        or admission.get("raw_rows_sha256") != raw_candidate.get("rows_sha256")
    ):
        raise ValueError("OpenFootball verified training admission is invalid")
    for field in ("admission_sha256", "domain_rows_sha256"):
        if not _valid_sha256(admission.get(field)):
            raise ValueError(f"OpenFootball training admission {field} is invalid")
    return receipt, admission


def build_v260_lock(
    lock: Mapping[str, Any],
    *,
    repository_root: Path,
    raw_archive_dir: Path | str,
    history_receipt_path: Path | str,
    locked_at: datetime,
    migration: str,
    reason: str,
) -> dict[str, Any]:
    """Build, but never write, a v260 lock from replayed raw-history evidence."""

    if POLICY_VERSION != "v260":
        raise ValueError("source-rights policy version is not v260")
    locked, window_started = _migration_window(
        lock,
        locked_at=locked_at,
        migration=migration,
        reason=reason,
    )
    # Reject an unavailable/volatile evidence root before touching the model
    # inventory.  A lock candidate must never spend time replaying history (or
    # report an unrelated missing code file) when its durable raw evidence is
    # not even eligible for inspection.
    raw_root = _require_durable_path(
        raw_archive_dir,
        field="raw_archive_dir",
        expected="directory",
        must_exist=True,
    )
    receipt_path = _require_durable_path(
        history_receipt_path,
        field="history_receipt_path",
        expected="file",
        must_exist=True,
    )
    repository = _require_durable_path(
        repository_root,
        field="repository_root",
        expected="directory",
        must_exist=True,
    )
    files = _v260_inventory(lock, repository_root=repository)
    receipt, admission = _history_evidence(
        raw_root=raw_root,
        receipt_path=receipt_path,
    )
    refreshed = _refreshed_lock(
        lock,
        locked=locked,
        window_started=window_started,
        migration=migration,
        reason=reason,
        files=files,
    )
    source_ids = sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    refreshed.update(
        {
            "policy_version": "v260",
            "observed_before": receipt["observed_at"],
            "source_ids": source_ids,
            "training_admission": {
                "schema_version": V260_TRAINING_ADMISSION_SCHEMA,
                "status": "training_admitted",
                "policy_version": "v260",
                "observed_before": receipt["observed_at"],
                "source_ids": source_ids,
                "training_admission_sha256": admission["admission_sha256"],
                "raw_admission_sha256": admission["raw_admission_sha256"],
                "source_manifest_sha256": admission["source_manifest_sha256"],
                "parser_contract_sha256": admission["parser_contract_sha256"],
                "raw_rows_sha256": admission["raw_rows_sha256"],
                "domain_rows_sha256": admission["domain_rows_sha256"],
            },
        }
    )
    return refreshed


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    parent_descriptor = _open_absolute_directory_nofollow(path.parent)
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        try:
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("candidate lock write made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise FileExistsError(f"output lock already exists: {path}") from exc
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_created = False
        os.fsync(parent_descriptor)
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def create_v260_lock_candidate(
    *,
    input_lock_path: Path | str,
    output_lock_path: Path | str,
    repository_root: Path,
    raw_archive_dir: Path | str,
    history_receipt_path: Path | str,
    locked_at: datetime,
    migration: str,
    reason: str,
) -> dict[str, Any]:
    """Create a new-path v260 candidate without replacing either lock path."""

    input_path = _require_durable_path(
        input_lock_path,
        field="input_lock_path",
        expected="file",
        must_exist=True,
    )
    output_path = _require_durable_path(
        output_lock_path,
        field="output_lock_path",
        expected="file",
        must_exist=False,
    )
    if input_path == output_path:
        raise ValueError("input and output lock paths must be different")
    if output_path.exists():
        raise FileExistsError(f"output lock already exists: {output_path}")
    raw_root = _require_durable_path(
        raw_archive_dir,
        field="raw_archive_dir",
        expected="directory",
        must_exist=True,
    )
    receipt_path = _require_durable_path(
        history_receipt_path,
        field="history_receipt_path",
        expected="file",
        must_exist=True,
    )
    original_bytes = _read_regular_nofollow(input_path)
    try:
        original = json.loads(original_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input lock is not valid JSON") from exc
    if not isinstance(original, dict):
        raise ValueError("input lock must contain a JSON object")
    candidate = build_v260_lock(
        original,
        repository_root=repository_root,
        raw_archive_dir=raw_root,
        history_receipt_path=receipt_path,
        locked_at=locked_at,
        migration=migration,
        reason=reason,
    )
    if _read_regular_nofollow(input_path) != original_bytes:
        raise RuntimeError("input lock changed while creating candidate")
    _write_json_exclusive(output_path, candidate)
    return candidate


def _migration_window(
    lock: Mapping[str, Any],
    *,
    locked_at: datetime,
    migration: str,
    reason: str,
) -> tuple[datetime, datetime]:
    if lock.get("status") != "pending_prospective_window":
        raise ValueError("only a pending prospective lock can be migrated")
    if not migration.strip() or len(migration) > 120:
        raise ValueError("migration must be a bounded non-empty label")
    if not reason.strip() or len(reason) > 600:
        raise ValueError("reason must be a bounded non-empty explanation")
    locked = _utc(locked_at)
    old_locked_value = lock.get("locked_at")
    if old_locked_value is not None and locked <= _utc(str(old_locked_value)):
        raise ValueError("new locked_at must be later than the previous lock")
    return locked, locked + timedelta(seconds=1)


def _refreshed_lock(
    lock: Mapping[str, Any],
    *,
    locked: datetime,
    window_started: datetime,
    migration: str,
    reason: str,
    files: list[dict[str, str]],
) -> dict[str, Any]:
    model_version = _model_version(lock, files)

    refreshed = dict(lock)
    refreshed.update(
        {
            "status": "pending_prospective_window",
            "locked_at": locked.isoformat(),
            "evaluation_window_started_at": window_started.isoformat(),
            "model_version_sha256": model_version,
            "model_files": files,
            "results_not_used_for_selection": True,
            "sample_requirements_met": False,
            "all_required_targets_scored": False,
            "prediction_freezes_verified": False,
        }
    )
    for key in (
        "prospective_evaluation_completed_at",
        "prospective_evaluation_generated_at",
        "prospective_evaluation_scored_n",
        "prospective_evaluation_digest",
    ):
        refreshed.pop(key, None)
    old_window = str(lock.get("evaluation_window") or "prospective window")
    refreshed["evaluation_window"] = f"{migration}: {reason}; {old_window}"
    notes = list(lock.get("notes") or [])
    notes.append(
        f"{migration} explicit lock migration: {reason} "
        "Previous archive rows remain append-only legacy records and are excluded "
        "from the new independent window."
    )
    refreshed["notes"] = notes
    return refreshed


def refresh_lock(
    lock: Mapping[str, Any],
    *,
    repository_root: Path,
    locked_at: datetime,
    migration: str,
    reason: str,
    additional_model_files: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a new pending lock and leave the input mapping untouched.

    A migration is valid only for an unfinished pending lock.  The old rows
    are not rewritten; changing the hash and window identity makes the
    evaluator classify them as legacy automatically.
    """

    locked, window_started = _migration_window(
        lock,
        locked_at=locked_at,
        migration=migration,
        reason=reason,
    )
    files = _inventory(
        lock,
        repository_root=repository_root,
        additional_model_files=additional_model_files,
    )
    return _refreshed_lock(
        lock,
        locked=locked,
        window_started=window_started,
        migration=migration,
        reason=reason,
        files=files,
    )


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    """Write one lock artifact atomically, keeping a failed write recoverable."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a new-path v260 prospective lock candidate."
    )
    parser.add_argument("--input-lock", type=Path, required=True)
    parser.add_argument("--output-lock", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--openfootball-raw-archive", type=Path, required=True)
    parser.add_argument("--history-receipt", type=Path, required=True)
    parser.add_argument("--migration", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--locked-at", required=True, help="timezone-aware ISO-8601 timestamp")
    args = parser.parse_args(argv)
    try:
        locked_at = _utc(args.locked_at)
    except (TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "stage": "input_validation", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        input_path = _require_durable_path(
            args.input_lock,
            field="input_lock_path",
            expected="file",
            must_exist=True,
        )
        output_path = _require_durable_path(
            args.output_lock,
            field="output_lock_path",
            expected="file",
            must_exist=False,
        )
        repository_root = _require_durable_path(
            args.repository_root,
            field="repository_root",
            expected="directory",
            must_exist=True,
        )
        raw_root = _require_durable_path(
            args.openfootball_raw_archive,
            field="raw_archive_dir",
            expected="directory",
            must_exist=True,
        )
        receipt_path = _require_durable_path(
            args.history_receipt,
            field="history_receipt_path",
            expected="file",
            must_exist=True,
        )
        if input_path == output_path:
            raise ValueError("input and output lock paths must be different")
        if output_path.exists():
            raise FileExistsError(f"output lock already exists: {output_path}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "stage": "path_validation", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        candidate = create_v260_lock_candidate(
            input_lock_path=input_path,
            output_lock_path=output_path,
            repository_root=repository_root,
            raw_archive_dir=raw_root,
            history_receipt_path=receipt_path,
            locked_at=locked_at,
            migration=args.migration,
            reason=args.reason,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "stage": "evidence_validation", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": "candidate_created",
                "output_lock": str(output_path),
                "locked_at": candidate["locked_at"],
                "model_version_sha256": candidate["model_version_sha256"],
                "training_admission_sha256": candidate["training_admission"][
                    "training_admission_sha256"
                ],
                "model_file_count": len(candidate["model_files"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "V260_MANDATORY_MODEL_ADMISSION_FILES",
    "build_v260_lock",
    "canonical_model_version_sha256",
    "create_v260_lock_candidate",
    "assert_lock_identities_match",
    "lock_identity_fingerprint",
    "main",
    "read_validated_lock",
    "require_lock_path",
    "refresh_lock",
    "validate_lock_identity",
    "write_json_atomic",
]
