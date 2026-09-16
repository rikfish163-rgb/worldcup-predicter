"""Create and verify a bounded durable archive of a volatile runtime.

The prospective cycle deliberately writes to a fast temporary filesystem when
the configured external volume is read-only.  That keeps the live collector
available, but it also means a reboot can discard the append-only ledgers.  A
packed archive is a *backup/read-only recovery artifact*: it never replaces the
runtime directory, never deletes source rows, and is never treated as a live
model input until a separate restore has been verified.

The command first estimates the compressed size, enforces a post-write free
space reserve, then writes an archive and manifest atomically.  A second
inventory after packing detects a source that changed during the snapshot;
such a pack is rejected rather than advertised as a complete archive.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping


SCHEMA_VERSION = "matchline.runtime_archive.v1"
VERIFICATION_SCHEMA_VERSION = "matchline.runtime_archive_verification.v1"
DEFAULT_RUNTIME_DIR = Path("data/live")
DEFAULT_ARCHIVE_DIR = Path("data/runtime-archive")
DEFAULT_MIN_FREE_BYTES = 1_073_741_824
DEFAULT_KEEP = 2
DEFAULT_COMPRESSION = "zstd"
MAX_SOURCE_FILES = 100_000


class SourceChangedError(RuntimeError):
    """Raised when an append-only runtime changes during archive creation."""


class ArchiveBusyError(RuntimeError):
    """Raised when another archive operation already owns the pack lock."""


class SourceBusyError(RuntimeError):
    """Raised when a live writer owns the runtime mutation lock."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_path(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


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


def _is_excluded(path: Path) -> bool:
    name = path.name
    return name.endswith(".lock") or name.endswith(".tmp") or name.startswith(".")


def _inventory(runtime_dir: Path) -> list[dict[str, Any]]:
    """Return a deterministic, metadata-only inventory of regular files."""

    runtime_dir = runtime_dir.resolve()
    if not runtime_dir.is_dir():
        raise FileNotFoundError(f"runtime directory does not exist: {runtime_dir}")
    rows: list[dict[str, Any]] = []
    for root, dirs, files in os.walk(runtime_dir, topdown=True, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not _is_excluded(Path(name)))
        for name in sorted(files):
            path = Path(root) / name
            if _is_excluded(path) or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                raise SourceChangedError(f"source disappeared during inventory: {path}") from None
            if not path.is_file():
                continue
            relative = path.relative_to(runtime_dir).as_posix()
            rows.append({
                "path": relative,
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            })
            if len(rows) > MAX_SOURCE_FILES:
                raise ValueError(f"runtime inventory exceeds {MAX_SOURCE_FILES} files")
    return rows


def _inventory_digest(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(rows)).hexdigest()


def _source_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_count": len(rows),
        "bytes": sum(int(row["size"]) for row in rows),
        "state_sha256": _inventory_digest(rows),
        "files": rows,
    }


def _free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _tar_command(runtime_dir: Path) -> list[str]:
    # GNU tar is available on the Linux runtime.  Stable ordering makes the
    # archive reproducible for an unchanged inventory; source timestamps are
    # still recorded in the external manifest.
    return [
        "/usr/bin/tar",
        "--sort=name",
        "--numeric-owner",
        "--owner=0",
        "--group=0",
        "--exclude=*.lock",
        "--exclude=*.tmp",
        "--exclude=./.*",
        "-C",
        str(runtime_dir),
        "-cf",
        "-",
        ".",
    ]


def _compress_command(compression: str) -> list[str]:
    if compression == "zstd":
        if not shutil.which("zstd"):
            raise FileNotFoundError("zstd executable is not available")
        return ["zstd", "-3", "-T0", "-q", "-c"]
    if compression == "gzip":
        return ["gzip", "-6", "-c"]
    raise ValueError(f"unsupported compression: {compression}")


def _stream_archive(runtime_dir: Path, destination: BinaryIO, compression: str) -> int:
    """Stream tar + compressor into ``destination`` and return bytes written."""

    tar_process = subprocess.Popen(
        _tar_command(runtime_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert tar_process.stdout is not None
    compressor = subprocess.Popen(
        _compress_command(compression),
        stdin=tar_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    tar_process.stdout.close()
    assert compressor.stdout is not None
    written = 0
    try:
        while chunk := compressor.stdout.read(1024 * 1024):
            destination.write(chunk)
            written += len(chunk)
    finally:
        compressor.stdout.close()
    compressor_stderr = compressor.stderr.read().decode("utf-8", errors="replace") if compressor.stderr else ""
    tar_stderr = tar_process.stderr.read().decode("utf-8", errors="replace") if tar_process.stderr else ""
    compressor_return = compressor.wait()
    tar_return = tar_process.wait()
    if compressor_return != 0:
        raise RuntimeError(f"compression failed ({compressor_return}): {compressor_stderr[-800:]}")
    if tar_return != 0:
        # GNU tar returns 1 for a non-fatal snapshot mutation.  The wording is
        # localized on this host, so do not depend on an English substring;
        # any exit-1 archive warning is treated as a source-change retry/fail-
        # closed result, while exit-2 remains a hard archive error.
        if tar_return == 1 or "file changed as we read it" in tar_stderr or "file removed before we read it" in tar_stderr:
            raise SourceChangedError(tar_stderr[-800:])
        raise RuntimeError(f"tar failed ({tar_return}): {tar_stderr[-800:]}")
    return written


def _estimate(runtime_dir: Path, compression: str) -> int:
    with open(os.devnull, "wb") as sink:
        return _stream_archive(runtime_dir, sink, compression)


@contextmanager
def _pack_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveBusyError(str(path)) from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _source_lock(path: Path) -> Iterator[None]:
    """Exclude prospective/live writers while inventorying and packing."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SourceBusyError(str(path)) from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_failure_receipt(archive_dir: Path, receipt: Mapping[str, Any]) -> None:
    """Persist a failed attempt without invalidating the last good archive."""

    _atomic_json(archive_dir / "runtime-archive-last-failure.json", dict(receipt))


def _archive_suffix(compression: str) -> str:
    return "tar.zst" if compression == "zstd" else "tar.gz"


def _archive_files(archive_dir: Path, suffix: str) -> list[Path]:
    return sorted(archive_dir.glob(f"runtime-snapshot-*.{suffix}"), key=lambda path: path.stat().st_mtime_ns, reverse=True)


def _prune(archive_dir: Path, suffix: str, keep: int) -> list[str]:
    retained = _archive_files(archive_dir, suffix)[: max(1, keep)]
    retained_names = {path.name for path in retained}
    removed: list[str] = []
    for path in _archive_files(archive_dir, suffix)[max(1, keep):]:
        if path.name in retained_names:
            continue
        path.unlink(missing_ok=True)
        removed.append(path.name)
    return removed


def pack_runtime(
    runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
    archive_dir: Path | str = DEFAULT_ARCHIVE_DIR,
    *,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    keep: int = DEFAULT_KEEP,
    compression: str = DEFAULT_COMPRESSION,
) -> dict[str, Any]:
    """Pack one stable runtime snapshot and return a machine-readable receipt."""

    runtime = Path(runtime_dir).resolve()
    destination = Path(archive_dir).resolve()
    if destination == runtime or destination.is_relative_to(runtime):
        raise ValueError("archive directory must not be inside the volatile runtime directory")
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "runtime-archive-manifest.json"
    lock_path = destination / ".runtime-archive.lock"
    started = _now()
    try:
        with _pack_lock(lock_path), _source_lock(runtime / "prospective-cycle.lock"):
            rows = _inventory(runtime)
            summary = _source_summary(rows)
            if not rows:
                receipt = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "empty",
                    "started_at": started,
                    "finished_at": _now(),
                    "runtime_dir": str(runtime),
                    "archive_dir": str(destination),
                    "source": summary,
                    "runtime_persistence": "durable_packed_archive",
                }
                _atomic_json(manifest_path, receipt)
                return receipt
            previous = _load_manifest(manifest_path)
            previous_archive = destination / str(previous.get("archive_file")) if previous and previous.get("archive_file") else None
            if previous and previous.get("source", {}).get("state_sha256") == summary["state_sha256"] and previous_archive and previous_archive.exists():
                return {
                    **previous,
                    "status": "unchanged",
                    "checked_at": _now(),
                }
            free_before = _free_bytes(destination)
            estimated = _estimate(runtime, compression)
            reserve = max(0, int(min_free_bytes))
            safety_margin = max(16 * 1024 * 1024, estimated // 20)
            if free_before - estimated - safety_margin < reserve:
                receipt = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "blocked_storage",
                    "started_at": started,
                    "finished_at": _now(),
                    "runtime_dir": str(runtime),
                    "archive_dir": str(destination),
                    "source": summary,
                    "estimated_archive_bytes": estimated,
                    "free_bytes_before": free_before,
                    "required_free_bytes_after": reserve,
                    "safety_margin_bytes": safety_margin,
                    "reason": "insufficient_free_space_for_atomic_packed_archive",
                    "runtime_persistence": "durable_packed_archive_not_written",
                }
                _write_failure_receipt(destination, receipt)
                return receipt
            suffix = _archive_suffix(compression)
            final_name = f"runtime-snapshot-{summary['state_sha256'][:20]}.{suffix}"
            final_path = destination / final_name
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=".runtime-snapshot-",
                suffix=".tmp",
                dir=destination,
            )
            # ``mkstemp`` returns an open descriptor.  The archive is opened
            # separately below, so close the descriptor immediately to avoid
            # leaking one descriptor per scheduled pack attempt.
            os.close(temporary_fd)
            temporary = Path(temporary_name)
            try:
                with temporary.open("wb") as stream:
                    _stream_archive(runtime, stream, compression)
                    stream.flush()
                    os.fsync(stream.fileno())
                after_rows = _inventory(runtime)
                if _inventory_digest(after_rows) != summary["state_sha256"]:
                    raise SourceChangedError("runtime changed while archive was being written")
                temporary.replace(final_path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            archive_sha256 = _sha256_path(final_path)
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "status": "archived",
                "started_at": started,
                "finished_at": _now(),
                "runtime_dir": str(runtime),
                "archive_dir": str(destination),
                "archive_file": final_name,
                "archive_sha256": archive_sha256,
                "compression": compression,
                "source": summary,
                "estimated_archive_bytes": estimated,
                "archive_bytes": final_path.stat().st_size,
                "free_bytes_before": free_before,
                # This first value is the conservative post-write amount
                # while the previous retained snapshot still exists.  The
                # final manifest is rewritten below after pruning and records
                # the actual post-rotation amount.
                "free_bytes_after": _free_bytes(destination),
                "retained_archive_count": max(1, int(keep)),
                "runtime_persistence": "durable_packed_archive",
                "restore_boundary": "read_only_recovery_artifact; restore and verify before model admission",
            }
            _atomic_json(manifest_path, receipt)
            receipt["pruned_archives"] = _prune(destination, suffix, keep)
            receipt["free_bytes_before_prune"] = receipt["free_bytes_after"]
            receipt["free_bytes_after"] = _free_bytes(destination)
            _atomic_json(manifest_path, receipt)
            return receipt
    except ArchiveBusyError:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "busy",
            "started_at": started,
            "finished_at": _now(),
            "runtime_dir": str(runtime),
            "archive_dir": str(destination),
            "reason": "another_archive_operation_holds_the_pack_lock",
        }
    except SourceBusyError:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "source_busy",
            "started_at": started,
            "finished_at": _now(),
            "runtime_dir": str(runtime),
            "archive_dir": str(destination),
            "reason": "runtime_mutation_lock_is_held",
        }
    except SourceChangedError as exc:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "source_changed",
            "started_at": started,
            "finished_at": _now(),
            "runtime_dir": str(runtime),
            "archive_dir": str(destination),
            "reason": str(exc),
            "runtime_persistence": "durable_packed_archive_not_written",
        }
        _write_failure_receipt(destination, receipt)
        return receipt


def verify_archive(manifest_path: Path | str) -> dict[str, Any]:
    """Verify the manifest hash and reject unsafe archive member paths."""

    manifest_file = Path(manifest_path).resolve()
    manifest = _load_manifest(manifest_file)
    if not manifest:
        return {"schema_version": SCHEMA_VERSION, "status": "missing_manifest", "manifest": str(manifest_file)}
    archive_file = manifest.get("archive_file")
    if not isinstance(archive_file, str) or not archive_file:
        return {**manifest, "status": "not_verifiable", "reason": "archive_file_missing"}
    archive = manifest_file.parent / archive_file
    if not archive.is_file():
        return {**manifest, "status": "missing_archive", "archive": str(archive)}
    actual_sha256 = _sha256_path(archive)
    expected_sha256 = manifest.get("archive_sha256")
    if not isinstance(expected_sha256, str) or actual_sha256 != expected_sha256:
        return {**manifest, "status": "hash_mismatch", "actual_archive_sha256": actual_sha256}
    members = 0
    unsafe: list[str] = []

    def scan_tar(tar: tarfile.TarFile) -> None:
        nonlocal members
        for member in tar:
            name = member.name
            candidate = Path(name)
            if candidate.is_absolute() or ".." in candidate.parts:
                unsafe.append(name)
            if member.isfile():
                members += 1

    compression = manifest.get("compression", "gzip")
    decoder: subprocess.Popen[bytes] | None = None
    try:
        if compression == "zstd":
            # Python 3.12's tarfile module has no zstd reader.  Stream the
            # decompressed tar through the system zstd binary and use tarfile's
            # streaming mode so verification does not materialize another
            # copy of the archive.
            for attempt in range(2):
                members_before = members
                unsafe_before = len(unsafe)
                decoder = subprocess.Popen(
                    ["zstd", "-q", "-d", "-c", str(archive)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                assert decoder.stdout is not None
                try:
                    with tarfile.open(fileobj=decoder.stdout, mode="r|") as tar:
                        scan_tar(tar)
                finally:
                    decoder.stdout.close()
                decoder_stderr = decoder.stderr.read().decode("utf-8", errors="replace") if decoder.stderr else ""
                decoder_return = decoder.wait()
                if decoder_return == 0:
                    break
                members = members_before
                del unsafe[unsafe_before:]
                # A decoder killed by a transient pipe or I/O condition can
                # exit without a diagnostic even after the archive hash and
                # tar stream were readable. Retry that ambiguous case exactly
                # once; explicit zstd errors still fail immediately.
                if attempt == 0 and not decoder_stderr.strip():
                    continue
                reason = decoder_stderr[-700:].strip()
                return {
                    **manifest,
                    "status": "archive_unreadable",
                    "reason": (
                        f"zstd decoder exited with status {decoder_return}"
                        + (f": {reason}" if reason else "")
                    ),
                }
        elif compression in {"gzip", ""}:
            with tarfile.open(archive, mode="r:*") as tar:
                scan_tar(tar)
        else:
            return {**manifest, "status": "not_verifiable", "reason": f"unsupported compression: {compression}"}
    except (OSError, tarfile.TarError) as exc:
        if decoder is not None:
            if decoder.poll() is None:
                decoder.kill()
            decoder.wait()
        return {**manifest, "status": "archive_unreadable", "reason": str(exc)}
    if unsafe:
        return {**manifest, "status": "unsafe_members", "unsafe_members": unsafe[:20]}
    return {
        **manifest,
        "status": "verified",
        "verified_at": _now(),
        "actual_archive_sha256": actual_sha256,
        "archive_member_files": members,
    }


def _verification_receipt(
    result: Mapping[str, Any], manifest_path: Path
) -> dict[str, Any]:
    """Return a bounded receipt bound to one exact manifest and archive."""

    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    status = result.get("status") if isinstance(result.get("status"), str) else "unknown"
    receipt: dict[str, Any] = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "status": status,
        "verified_at": result.get("verified_at") if status == "verified" else None,
        "manifest_file": manifest_path.name,
        "archive_file": result.get("archive_file"),
        "archive_sha256": result.get("actual_archive_sha256") or result.get("archive_sha256"),
        "source_state_sha256": source.get("state_sha256"),
        "archive_member_files": result.get("archive_member_files"),
    }
    if status != "verified":
        receipt["checked_at"] = _now()
        receipt["reason"] = result.get("reason")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--verification-receipt", type=Path)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--compression", choices=("zstd", "gzip"), default=DEFAULT_COMPRESSION)
    args = parser.parse_args(argv)
    if args.verify:
        manifest_path = args.manifest or (args.archive_dir / "runtime-archive-manifest.json")
        result = verify_archive(manifest_path)
        if args.verification_receipt is not None:
            _atomic_json(
                args.verification_receipt,
                _verification_receipt(result, Path(manifest_path)),
            )
    else:
        result = pack_runtime(
            args.runtime_dir,
            args.archive_dir,
            min_free_bytes=args.min_free_bytes,
            keep=max(1, args.keep),
            compression=args.compression,
        )
    # Do not dump the complete source inventory to systemd/journal output:
    # manifests remain available on disk, while scheduled runs should emit a
    # bounded receipt that is useful to an operator.
    printable = dict(result)
    source = printable.get("source")
    if isinstance(source, dict):
        printable["source"] = {
            key: source.get(key)
            for key in ("file_count", "bytes", "state_sha256")
            if key in source
        }
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {
        "archived",
        "unchanged",
        "verified",
        "empty",
        "busy",
        "source_busy",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "pack_runtime",
    "verify_archive",
    "main",
    "SCHEMA_VERSION",
    "VERIFICATION_SCHEMA_VERSION",
]
