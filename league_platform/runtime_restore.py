"""Safely restore a verified Matchline runtime archive into volatile storage.

The durable archive is never extracted over an active runtime.  Restoration
first verifies the compressed artifact, stages every regular file on the same
filesystem as the destination, validates the staged file set against the
manifest, and only then performs an atomic directory rename.  A partial
post-boot directory can be quarantined without deleting any of its evidence.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, IO, Iterator, Mapping

from league_platform.runtime_archive import verify_archive
from league_platform.runtime_checkpoint import MANIFEST_NAME as CHECKPOINT_MANIFEST_NAME
from league_platform.runtime_checkpoint import stage_checkpoint, verify_checkpoint


SCHEMA_VERSION = "matchline.runtime_restore.v1"
DEFAULT_MANIFEST = Path("data/runtime-archive/runtime-archive-manifest.json")
DEFAULT_DESTINATION = Path("/dev/shm/matchline-live-runtime")
DEFAULT_MIN_FREE_BYTES = 512 * 1024 * 1024
REQUIRED_RUNTIME_FILES = frozenset({"current.json"})
MAX_RESTORE_FILES = 100_000


class RestoreBusyError(RuntimeError):
    """Raised when the archive pack/restore lock is already held."""


class UnsafeArchiveError(RuntimeError):
    """Raised when an archive contains a member that must never be restored."""


class ArchiveInventoryError(RuntimeError):
    """Raised when archive contents do not match the signed manifest inventory."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ArchiveInventoryError("manifest must be a JSON object")
    return value


def _normal_member_name(raw: str) -> str | None:
    if not raw or "\x00" in raw:
        raise UnsafeArchiveError("archive member has an empty or NUL-containing path")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise UnsafeArchiveError(f"unsafe archive member path: {raw}")
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if not parts:
        return None
    return PurePosixPath(*parts).as_posix()


def _expected_files(manifest: Mapping[str, Any]) -> dict[str, int]:
    source = manifest.get("source")
    rows = source.get("files") if isinstance(source, dict) else None
    if not isinstance(rows, list):
        raise ArchiveInventoryError("manifest source.files is missing")
    if len(rows) > MAX_RESTORE_FILES:
        raise ArchiveInventoryError(
            f"manifest inventory exceeds the {MAX_RESTORE_FILES}-file restore limit"
        )
    expected: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ArchiveInventoryError("manifest file row is not an object")
        raw_path = row.get("path")
        size = row.get("size")
        if not isinstance(raw_path, str) or not isinstance(size, int) or size < 0:
            raise ArchiveInventoryError("manifest file row has an invalid path or size")
        normalized = _normal_member_name(raw_path)
        if normalized is None or normalized != raw_path:
            raise ArchiveInventoryError(f"manifest contains a non-canonical path: {raw_path}")
        if normalized in expected:
            raise ArchiveInventoryError(f"manifest contains a duplicate path: {raw_path}")
        expected[normalized] = size
    declared_count = source.get("file_count") if isinstance(source, dict) else None
    declared_bytes = source.get("bytes") if isinstance(source, dict) else None
    if declared_count != len(expected) or declared_bytes != sum(expected.values()):
        raise ArchiveInventoryError("manifest inventory totals are inconsistent")
    missing_required = sorted(REQUIRED_RUNTIME_FILES - expected.keys())
    if missing_required:
        raise ArchiveInventoryError(
            f"manifest is missing required runtime files: {', '.join(missing_required)}"
        )
    return expected


@contextmanager
def _archive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RestoreBusyError(str(path)) from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _open_archive(archive: Path, compression: str) -> Iterator[tarfile.TarFile]:
    decoder: subprocess.Popen[bytes] | None = None
    try:
        if compression == "zstd":
            if shutil.which("zstd") is None:
                raise FileNotFoundError("zstd executable is not available")
            decoder = subprocess.Popen(
                ["zstd", "-q", "-d", "-c", str(archive)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert decoder.stdout is not None
            with tarfile.open(fileobj=decoder.stdout, mode="r|") as tar:
                yield tar
            decoder.stdout.close()
            stderr = decoder.stderr.read().decode("utf-8", errors="replace") if decoder.stderr else ""
            return_code = decoder.wait()
            if return_code != 0:
                raise tarfile.ReadError(f"zstd decoder failed ({return_code}): {stderr[-400:]}")
        elif compression in {"gzip", ""}:
            with tarfile.open(archive, mode="r:*") as tar:
                yield tar
        else:
            raise ArchiveInventoryError(f"unsupported compression: {compression}")
    except Exception:
        if decoder is not None:
            if decoder.stdout is not None:
                decoder.stdout.close()
            if decoder.poll() is None:
                decoder.kill()
            decoder.wait()
        raise


def _copy_member(source: IO[bytes], destination: Path, expected_size: int) -> None:
    written = 0
    with destination.open("xb") as stream:
        while chunk := source.read(1024 * 1024):
            stream.write(chunk)
            written += len(chunk)
        stream.flush()
    if written != expected_size:
        raise ArchiveInventoryError(
            f"restored size mismatch for {destination.name}: {written} != {expected_size}"
        )


def _extract_archive(
    archive: Path,
    compression: str,
    staging: Path,
    expected: Mapping[str, int],
) -> None:
    seen: set[str] = set()
    allowed_directories: set[str] = set()
    for path in expected:
        for parent in PurePosixPath(path).parents:
            if parent != PurePosixPath("."):
                allowed_directories.add(parent.as_posix())
    seen_directories: set[str] = set()
    member_count = 0
    maximum_members = len(expected) + len(allowed_directories) + 1
    with _open_archive(archive, compression) as tar:
        for member in tar:
            member_count += 1
            if member_count > maximum_members:
                raise ArchiveInventoryError("archive contains members absent from the manifest tree")
            normalized = _normal_member_name(member.name)
            if normalized is None:
                if not member.isdir():
                    raise UnsafeArchiveError("archive root member is not a directory")
                continue
            target = staging.joinpath(*PurePosixPath(normalized).parts)
            if member.isdir():
                if normalized not in allowed_directories:
                    raise ArchiveInventoryError(
                        f"archive directory is absent from the manifest tree: {normalized}"
                    )
                if normalized in seen_directories:
                    raise ArchiveInventoryError(
                        f"archive contains a duplicate directory: {normalized}"
                    )
                target.mkdir(parents=True, exist_ok=True)
                seen_directories.add(normalized)
                continue
            if not member.isfile():
                raise UnsafeArchiveError(
                    f"unsupported archive member type for {normalized}; only files and directories are allowed"
                )
            if normalized in seen:
                raise ArchiveInventoryError(f"archive contains a duplicate file: {normalized}")
            expected_size = expected.get(normalized)
            if expected_size is None:
                raise ArchiveInventoryError(f"archive file is absent from manifest: {normalized}")
            if member.size != expected_size:
                raise ArchiveInventoryError(
                    f"archive size mismatch for {normalized}: {member.size} != {expected_size}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ArchiveInventoryError(f"archive file cannot be read: {normalized}")
            with extracted:
                _copy_member(extracted, target, expected_size)
            os.chmod(target, member.mode & 0o777)
            os.utime(target, (int(member.mtime), int(member.mtime)))
            seen.add(normalized)
    missing = sorted(set(expected) - seen)
    if missing:
        preview = ", ".join(missing[:5])
        raise ArchiveInventoryError(f"archive is missing {len(missing)} manifest files: {preview}")


def _staged_inventory(root: Path) -> dict[str, int]:
    rows: dict[str, int] = {}
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise UnsafeArchiveError(f"staging contains a non-regular file: {path}")
            relative = path.relative_to(root).as_posix()
            rows[relative] = path.stat().st_size
    return rows


def _usable_current(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and isinstance(value.get("as_of"), str) and bool(value["as_of"])


def _directory_nonempty(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def _utc_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ArchiveInventoryError(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArchiveInventoryError(f"{field} must be a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArchiveInventoryError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _is_file_prefix(prefix: Path, whole: Path) -> bool:
    if prefix.stat().st_size > whole.stat().st_size:
        return False
    with prefix.open("rb") as left, whole.open("rb") as right:
        while chunk := left.read(1024 * 1024):
            if right.read(len(chunk)) != chunk:
                return False
    return True


def _stage_newer_checkpoint(
    *,
    checkpoint_root: Path | str | None,
    archive_manifest: Mapping[str, Any],
    staging: Path,
) -> dict[str, Any]:
    if checkpoint_root is None:
        return {"status": "not_configured"}
    root = Path(checkpoint_root).resolve()
    if not (root / CHECKPOINT_MANIFEST_NAME).is_file():
        return {"status": "not_available", "checkpoint_root": str(root)}

    verified = verify_checkpoint(root)
    archive_runtime = archive_manifest.get("runtime_dir")
    checkpoint_runtime = verified.get("runtime_dir")
    if not isinstance(archive_runtime, str) or not isinstance(checkpoint_runtime, str):
        raise ArchiveInventoryError("archive/checkpoint runtime identity is missing")
    if Path(archive_runtime).resolve() != Path(checkpoint_runtime).resolve():
        raise ArchiveInventoryError("checkpoint belongs to a different runtime lane")

    checkpoint_time = _utc_timestamp(verified.get("created_at"), field="checkpoint.created_at")
    archive_time = _utc_timestamp(archive_manifest.get("finished_at"), field="archive.finished_at")
    if checkpoint_time <= archive_time:
        return {
            "status": "skipped_not_newer",
            "checkpoint_root": str(root),
            "state_sha256": verified.get("state_sha256"),
            "checkpoint_created_at": checkpoint_time.isoformat(),
            "archive_finished_at": archive_time.isoformat(),
        }

    checkpoint_dir = Path(verified["checkpoint_dir"])
    for row in verified["files"]:
        relative = Path(row["path"])
        archived = staging / relative
        newer = checkpoint_dir / relative
        if relative.suffix == ".jsonl" and archived.exists() and not _is_file_prefix(archived, newer):
            raise ArchiveInventoryError(
                f"newer checkpoint would rewrite or shrink append-only ledger: {relative.as_posix()}"
            )
    staged = stage_checkpoint(root, staging)
    return {
        "status": "applied",
        "checkpoint_root": str(root),
        "state_sha256": staged.get("state_sha256"),
        "checkpoint_created_at": checkpoint_time.isoformat(),
        "archive_finished_at": archive_time.isoformat(),
        "file_count": staged.get("file_count"),
        "files": staged.get("files"),
    }


def _failure_receipt(manifest: Path, receipt: Mapping[str, Any]) -> None:
    try:
        _atomic_json(manifest.parent / "runtime-restore-last-failure.json", receipt)
    except OSError:
        # The bounded CLI receipt remains available even if the durable disk
        # is too full to accept a second diagnostic file.
        pass


def restore_runtime(
    manifest_path: Path | str = DEFAULT_MANIFEST,
    destination: Path | str = DEFAULT_DESTINATION,
    *,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    quarantine_incomplete_target: bool = False,
    checkpoint_root: Path | str | None = None,
) -> dict[str, Any]:
    """Restore one verified archive and atomically install its runtime tree."""

    started_at = _now()
    manifest_file = Path(manifest_path).resolve()
    target = Path(destination).resolve()
    if _usable_current(target / "current.json"):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "not_needed",
            "started_at": started_at,
            "finished_at": _now(),
            "destination": str(target),
            "reason": "usable_current_runtime_already_exists",
        }
    staging: Path | None = None
    old_target: Path | None = None
    try:
        manifest = _load_manifest(manifest_file)
        archive_name = manifest.get("archive_file")
        if not isinstance(archive_name, str) or Path(archive_name).name != archive_name:
            raise ArchiveInventoryError("archive_file must be a plain filename")
        archive = manifest_file.parent / archive_name
        lock_path = manifest_file.parent / ".runtime-archive.lock"
        with _archive_lock(lock_path):
            verified = verify_archive(manifest_file)
            archive_status = verified.get("status")
            if archive_status != "verified":
                receipt = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "blocked_archive_verification",
                    "archive_status": archive_status,
                    "started_at": started_at,
                    "finished_at": _now(),
                    "manifest": str(manifest_file),
                    "destination": str(target),
                }
                _failure_receipt(manifest_file, receipt)
                return receipt
            expected = _expected_files(manifest)
            target.parent.mkdir(parents=True, exist_ok=True)
            required_free = sum(expected.values()) + max(0, int(min_free_bytes))
            free_bytes = int(shutil.disk_usage(target.parent).free)
            if free_bytes < required_free:
                receipt = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "blocked_storage",
                    "archive_status": archive_status,
                    "started_at": started_at,
                    "finished_at": _now(),
                    "manifest": str(manifest_file),
                    "destination": str(target),
                    "free_bytes": free_bytes,
                    "required_free_bytes": required_free,
                }
                _failure_receipt(manifest_file, receipt)
                return receipt
            if target.exists() and _directory_nonempty(target) and not quarantine_incomplete_target:
                receipt = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "blocked_destination_not_empty",
                    "archive_status": archive_status,
                    "started_at": started_at,
                    "finished_at": _now(),
                    "manifest": str(manifest_file),
                    "destination": str(target),
                    "reason": "incomplete destination preserved; opt in to quarantine before restore",
                }
                _failure_receipt(manifest_file, receipt)
                return receipt
            staging = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
            )
            _extract_archive(
                archive,
                str(manifest.get("compression", "gzip")),
                staging,
                expected,
            )
            staged = _staged_inventory(staging)
            if staged != expected:
                raise ArchiveInventoryError("staged file inventory does not match manifest")
            if not _usable_current(staging / "current.json"):
                raise ArchiveInventoryError("restored current.json is not a usable runtime snapshot")
            checkpoint = _stage_newer_checkpoint(
                checkpoint_root=checkpoint_root,
                archive_manifest=manifest,
                staging=staging,
            )
            final_expected = dict(expected)
            if checkpoint.get("status") == "applied":
                for row in checkpoint.get("files", []):
                    final_expected[str(row["path"])] = int(row["bytes"])
            staged = _staged_inventory(staging)
            if staged != final_expected:
                raise ArchiveInventoryError("checkpointed staged inventory does not match expected files")
            if target.exists():
                if _directory_nonempty(target):
                    old_target = target.parent / f".{target.name}.pre-restore-{_stamp()}-{os.getpid()}"
                    target.replace(old_target)
                else:
                    target.rmdir()
            try:
                staging.replace(target)
                staging = None
            except Exception:
                if old_target is not None and not target.exists():
                    old_target.replace(target)
                    old_target = None
                raise
            quarantined: str | None = None
            if old_target is not None:
                quarantine = target / "recovery" / "boot-partials" / old_target.name.lstrip(".")
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                old_target.replace(quarantine)
                old_target = None
                quarantined = str(quarantine)
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "status": "restored",
                "archive_status": archive_status,
                "started_at": started_at,
                "finished_at": _now(),
                "manifest": str(manifest_file),
                "archive_file": archive_name,
                "archive_sha256": manifest.get("archive_sha256"),
                "source_state_sha256": manifest.get("source", {}).get("state_sha256"),
                "destination": str(target),
                "restored_file_count": len(staged),
                "restored_bytes": sum(staged.values()),
                "checkpoint_status": checkpoint.get("status"),
                "checkpoint_state_sha256": checkpoint.get("state_sha256"),
                "quarantined_incomplete_target": quarantined,
                "model_admission": "not_granted; fresh causal cycle and normal gates still required",
            }
            _atomic_json(target / "runtime-restore-receipt.json", receipt)
            return receipt
    except RestoreBusyError:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "busy",
            "started_at": started_at,
            "finished_at": _now(),
            "manifest": str(manifest_file),
            "destination": str(target),
            "reason": "archive pack or restore lock is already held",
        }
        _failure_receipt(manifest_file, receipt)
        return receipt
    except (UnsafeArchiveError, ArchiveInventoryError) as exc:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked_archive_content",
            "started_at": started_at,
            "finished_at": _now(),
            "manifest": str(manifest_file),
            "destination": str(target),
            "error": type(exc).__name__,
            "reason": str(exc)[:800],
        }
        _failure_receipt(manifest_file, receipt)
        return receipt
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "restore_failed",
            "started_at": started_at,
            "finished_at": _now(),
            "manifest": str(manifest_file),
            "destination": str(target),
            "error": type(exc).__name__,
            "reason": str(exc)[:800],
        }
        _failure_receipt(manifest_file, receipt)
        return receipt
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        # If the restored tree was installed but moving the preserved partial
        # tree into its quarantine failed, keep the sibling directory intact.
        # Never delete it during cleanup.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--quarantine-incomplete-target", action="store_true")
    parser.add_argument("--checkpoint-root", type=Path, default=None)
    args = parser.parse_args(argv)
    result = restore_runtime(
        args.manifest,
        args.destination,
        min_free_bytes=args.min_free_bytes,
        quarantine_incomplete_target=args.quarantine_incomplete_target,
        checkpoint_root=args.checkpoint_root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"restored", "not_needed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["restore_runtime", "main", "SCHEMA_VERSION"]
