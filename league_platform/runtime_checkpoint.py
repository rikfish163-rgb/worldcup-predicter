"""Durably checkpoint the small ledgers that cannot be regenerated after reboot.

This is deliberately narrower than :mod:`league_platform.runtime_archive`.
The packed archive remains the complete recovery artifact; this checkpoint
closes the sub-hour loss window for append-only prospective and intelligence
ledgers without copying the multi-gigabyte raw-response archive every cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = "matchline.runtime_checkpoint.v1"
MANIFEST_NAME = "runtime-checkpoint-manifest.json"
DEFAULT_RETAIN_CHECKPOINTS = 4
_CHECKPOINT_DIRECTORY = re.compile(r"checkpoint-[0-9a-f]{20}")
CRITICAL_LEDGER_PATHS = (
    "prospective_predictions.jsonl",
    "prospective_market_baselines.jsonl",
    "prospective-cycle-latest.json",
    "prospective-evaluation-current.json",
    "runtime-only-cycle-latest.json",
    "runtime-only-evaluation-current.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory(runtime: Path, relative_paths: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relative in relative_paths:
        source = runtime / relative
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"critical ledger must be a regular file: {relative}")
        rows.append({"path": relative, "bytes": source.stat().st_size, "sha256": _sha256(source)})
    return rows


def _state_sha256(files: list[dict[str, Any]]) -> str:
    payload = json.dumps(files, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_relative_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("checkpoint manifest file path must be a non-empty string")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"unsafe checkpoint manifest path: {value!r}")
    return Path(*relative.parts)


def _load_manifest(checkpoint_root: Path | str) -> tuple[Path, Path, dict[str, Any], list[dict[str, Any]]]:
    root = Path(checkpoint_root).resolve()
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid runtime checkpoint manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported runtime checkpoint manifest")

    checkpoint_name = manifest.get("checkpoint_dir")
    if (
        not isinstance(checkpoint_name, str)
        or not checkpoint_name
        or Path(checkpoint_name).name != checkpoint_name
    ):
        raise ValueError("unsafe checkpoint directory in manifest")
    checkpoint_candidate = root / checkpoint_name
    checkpoint = checkpoint_candidate.resolve()
    if checkpoint.parent != root or checkpoint_candidate.is_symlink() or not checkpoint.is_dir():
        raise ValueError("checkpoint directory is missing or outside checkpoint root")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("checkpoint manifest has no files")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ValueError("invalid checkpoint manifest file entry")
        relative = _validated_relative_path(raw.get("path"))
        relative_text = relative.as_posix()
        size = raw.get("bytes")
        sha256 = raw.get("sha256")
        if relative_text in seen:
            raise ValueError(f"duplicate checkpoint manifest path: {relative_text}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid checkpoint size: {relative_text}")
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise ValueError(f"invalid checkpoint hash: {relative_text}")
        seen.add(relative_text)
        files.append({"path": relative_text, "bytes": size, "sha256": sha256})

    if manifest.get("file_count") != len(files):
        raise ValueError("checkpoint file count mismatch")
    state_sha256 = manifest.get("state_sha256")
    if state_sha256 != _state_sha256(files):
        raise ValueError("checkpoint state hash mismatch")
    return root, checkpoint, manifest, files


def _verify_files(checkpoint: Path, files: list[dict[str, Any]]) -> None:
    for row in files:
        relative = _validated_relative_path(row["path"])
        candidate = checkpoint / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError(f"checkpoint file missing or not regular: {relative.as_posix()}")
        if candidate.stat().st_size != row["bytes"]:
            raise OSError(f"checkpoint file size mismatch: {relative.as_posix()}")
        if _sha256(candidate) != row["sha256"]:
            raise OSError(f"checkpoint file hash mismatch: {relative.as_posix()}")


def _prune_checkpoint_generations(
    checkpoint_root: Path,
    *,
    current: Path,
    retain: int,
) -> int:
    """Bound content-addressed generations without following untrusted paths."""

    if isinstance(retain, bool) or not isinstance(retain, int) or retain < 1:
        raise ValueError("checkpoint retention must be a positive integer")
    candidates: list[Path] = []
    for candidate in checkpoint_root.iterdir():
        if candidate == current or _CHECKPOINT_DIRECTORY.fullmatch(candidate.name) is None:
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if candidate.resolve().parent != checkpoint_root:
            continue
        candidates.append(candidate)
    candidates.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    removed = 0
    for obsolete in candidates[max(0, retain - 1) :]:
        shutil.rmtree(obsolete)
        removed += 1
    return removed


def verify_checkpoint(checkpoint_root: Path | str) -> dict[str, Any]:
    """Verify the latest manifest and every listed checkpoint file."""

    root, checkpoint, manifest, files = _load_manifest(checkpoint_root)
    _verify_files(checkpoint, files)
    return {
        **manifest,
        "status": "verified",
        "checkpoint_root": str(root),
        "checkpoint_dir": str(checkpoint),
    }


def restore_checkpoint(checkpoint_root: Path | str, runtime_dir: Path | str) -> dict[str, Any]:
    """Restore only manifest-listed ledgers into a new or empty runtime directory."""

    verified = verify_checkpoint(checkpoint_root)
    checkpoint = Path(verified["checkpoint_dir"])
    files = verified["files"]
    target = Path(runtime_dir).resolve()
    root = Path(checkpoint_root).resolve()
    if target == root or root in target.parents or target == checkpoint or checkpoint in target.parents:
        raise ValueError("runtime restore target must be outside the checkpoint root")
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise ValueError("runtime restore target must be a directory")
        if any(target.iterdir()):
            raise FileExistsError("runtime restore target must be empty")

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    try:
        for row in files:
            relative = _validated_relative_path(row["path"])
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(checkpoint / relative, destination)
            if destination.stat().st_size != row["bytes"] or _sha256(destination) != row["sha256"]:
                raise OSError(f"restored checkpoint hash mismatch: {relative.as_posix()}")
        if target.exists():
            target.rmdir()
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "restored",
        "restored_at": datetime.now(UTC).isoformat(),
        "checkpoint_root": str(root),
        "checkpoint_dir": str(checkpoint),
        "runtime_dir": str(target),
        "state_sha256": verified["state_sha256"],
        "recovery_scope": "critical_ledgers_only",
        "file_count": len(files),
        "files": files,
    }


def stage_checkpoint(checkpoint_root: Path | str, staging_dir: Path | str) -> dict[str, Any]:
    """Copy verified ledger files into a caller-owned, non-live staging tree.

    This helper deliberately does not install the tree.  ``runtime_restore``
    uses it before its single atomic directory rename, so a failed copy cannot
    leave the live runtime with a mixture of archive and checkpoint states.
    """

    verified = verify_checkpoint(checkpoint_root)
    checkpoint = Path(verified["checkpoint_dir"])
    root = Path(checkpoint_root).resolve()
    staging = Path(staging_dir).resolve()
    if staging == root or root in staging.parents or staging == checkpoint or checkpoint in staging.parents:
        raise ValueError("checkpoint staging directory must be outside the checkpoint root")
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("checkpoint staging target must be an existing regular directory")

    for row in verified["files"]:
        relative = _validated_relative_path(row["path"])
        source = checkpoint / relative
        destination = staging / relative
        if destination.exists() and (destination.is_symlink() or not destination.is_file()):
            raise ValueError(f"checkpoint staging target is not a regular file: {relative.as_posix()}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if destination.stat().st_size != row["bytes"] or _sha256(destination) != row["sha256"]:
            raise OSError(f"staged checkpoint hash mismatch: {relative.as_posix()}")
    return {
        **verified,
        "status": "staged",
        "staging_dir": str(staging),
    }


def create_checkpoint(
    runtime_dir: Path | str,
    checkpoint_root: Path | str,
    *,
    relative_paths: Iterable[str] = CRITICAL_LEDGER_PATHS,
    retain: int = DEFAULT_RETAIN_CHECKPOINTS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Copy the current critical-ledger state into one atomic durable directory."""

    runtime = Path(runtime_dir).resolve()
    destination = Path(checkpoint_root).resolve()
    if not runtime.is_dir():
        raise FileNotFoundError(runtime)
    if destination == runtime or runtime in destination.parents:
        raise ValueError("checkpoint root must not be inside the runtime directory")
    if isinstance(retain, bool) or not isinstance(retain, int) or retain < 1:
        raise ValueError("checkpoint retention must be a positive integer")

    files = _inventory(runtime, relative_paths)
    if not files:
        raise FileNotFoundError("no critical runtime ledgers are available")
    state_sha256 = _state_sha256(files)
    checkpoint = destination / f"checkpoint-{state_sha256[:20]}"
    destination.mkdir(parents=True, exist_ok=True)

    if not checkpoint.exists():
        staging = Path(tempfile.mkdtemp(prefix=".runtime-checkpoint-", dir=destination))
        try:
            for row in files:
                relative = Path(row["path"])
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(runtime / relative, target)
                if _sha256(target) != row["sha256"]:
                    raise OSError(f"checkpoint copy hash mismatch: {relative}")
            if _inventory(runtime, (row["path"] for row in files)) != files:
                raise OSError("critical ledger changed while checkpointing")
            os.replace(staging, checkpoint)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    else:
        _verify_files(checkpoint, files)

    created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "checkpointed",
        "created_at": created_at,
        "runtime_dir": str(runtime),
        "checkpoint_dir": checkpoint.name,
        "state_sha256": state_sha256,
        "recovery_scope": "critical_ledgers_only",
        "retention_limit": retain,
        "file_count": len(files),
        "files": files,
    }
    _atomic_json(destination / MANIFEST_NAME, manifest)
    pruned = _prune_checkpoint_generations(
        destination,
        current=checkpoint,
        retain=retain,
    )
    return {
        **manifest,
        "checkpoint_dir": str(checkpoint),
        "pruned_checkpoint_count": pruned,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create or reuse a content-addressed checkpoint")
    create.add_argument("--runtime-dir", type=Path, required=True)
    create.add_argument("--checkpoint-root", type=Path, required=True)
    create.add_argument("--retain", type=int, default=DEFAULT_RETAIN_CHECKPOINTS)

    verify = commands.add_parser("verify", help="verify the latest checkpoint manifest and files")
    verify.add_argument("--checkpoint-root", type=Path, required=True)

    restore = commands.add_parser("restore", help="restore listed ledgers to a new or empty runtime")
    restore.add_argument("--checkpoint-root", type=Path, required=True)
    restore.add_argument("--runtime-dir", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "create":
        result = create_checkpoint(
            args.runtime_dir,
            args.checkpoint_root,
            retain=args.retain,
        )
    elif args.command == "verify":
        result = verify_checkpoint(args.checkpoint_root)
    else:
        result = restore_checkpoint(args.checkpoint_root, args.runtime_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CRITICAL_LEDGER_PATHS",
    "DEFAULT_RETAIN_CHECKPOINTS",
    "MANIFEST_NAME",
    "SCHEMA_VERSION",
    "create_checkpoint",
    "main",
    "restore_checkpoint",
    "stage_checkpoint",
    "verify_checkpoint",
]
