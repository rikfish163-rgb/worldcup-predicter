"""Plan and perform a non-destructive migration of append-only runtime data.

The scheduled cycle produces a mixture of snapshots, raw Crawl4AI pages,
ledgers and receipts.  Those files grow independently from the source tree and
can exhaust the project filesystem even when the archive filesystem still has
space.  This helper deliberately keeps migration explicit:

* ``--dry-run`` inventories files, hashes and destination conflicts;
  it also reports whether the destination (or its existing parent) is
  writable by the service user;
* ``--apply`` requires both ``--operator-approved`` and ``--cycle-stopped``;
* source files are never removed or rewritten;
* lock and temporary files are not copied, so a stale process lock cannot be
  carried into a new runtime root.

It is an operational aid, not part of the prediction or source-ingestion
model.  The existing storage guard remains unchanged after a cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from league_platform.runtime_paths import DEFAULT_RUNTIME_DIR


DEFAULT_MIN_FREE_BYTES = 1 << 30


def _excluded(path: Path) -> bool:
    """Return whether a transient runtime file must not be migrated."""

    return (
        path.name.endswith(".lock")
        or path.name.endswith(".tmp")
        or path.name.startswith(".") and ".runtime." in path.name
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class RuntimeEntry:
    relative_path: str
    kind: str
    size_bytes: int
    sha256: str | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class MigrationPlan:
    source_root: str
    target_root: str
    source_status: str
    entries: tuple[RuntimeEntry, ...]
    skipped_transient: tuple[str, ...]
    bytes_to_copy: int
    destination_free_bytes: int
    destination_writable: bool
    required_free_bytes: int
    conflicts: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return (
            self.source_status == "ok"
            and not self.conflicts
            and self.destination_writable
            and self.destination_free_bytes >= self.required_free_bytes
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_root": self.source_root,
            "target_root": self.target_root,
            "source_status": self.source_status,
            "entries": [asdict(entry) for entry in self.entries],
            "skipped_transient": list(self.skipped_transient),
            "bytes_to_copy": self.bytes_to_copy,
            "destination_free_bytes": self.destination_free_bytes,
            "destination_writable": self.destination_writable,
            "required_free_bytes": self.required_free_bytes,
            "conflicts": list(self.conflicts),
            "ready": self.ready,
            "source_preserved": True,
            "lock_files_copied": False,
        }


def _root_pair(source_root: Path | str, target_root: Path | str) -> tuple[Path, Path]:
    source = Path(source_root).absolute()
    target = Path(target_root).absolute()
    if source == target:
        raise ValueError("source and target runtime roots must differ")
    # Refuse nested roots: copying into a child would recursively copy the
    # destination, while copying a parent would make the source ambiguous.
    try:
        target.relative_to(source)
        raise ValueError("target runtime root cannot be inside source root")
    except ValueError as exc:
        if str(exc) == "target runtime root cannot be inside source root":
            raise
    try:
        source.relative_to(target)
        raise ValueError("source runtime root cannot be inside target root")
    except ValueError as exc:
        if str(exc) == "source runtime root cannot be inside target root":
            raise
    return source, target


def _iter_entries(source: Path) -> tuple[str, list[RuntimeEntry], list[str]]:
    entries: list[RuntimeEntry] = []
    skipped: list[str] = []
    if not source.exists():
        return "missing", entries, skipped
    if not source.is_dir():
        return "not_a_directory", entries, skipped
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if _excluded(path):
            if path.is_file() or path.is_symlink():
                skipped.append(relative)
            continue
        if path.is_symlink():
            entries.append(RuntimeEntry(relative, "symlink", 0, link_target=os.readlink(path)))
        elif path.is_file():
            entries.append(RuntimeEntry(relative, "file", path.stat().st_size, _sha256(path)))
    return "ok", entries, skipped


def _destination_state(target: Path, entries: Iterable[RuntimeEntry]) -> tuple[int, bool, tuple[str, ...]]:
    conflicts: list[str] = []
    for entry in entries:
        destination = target / entry.relative_path
        if not destination.exists() and not destination.is_symlink():
            continue
        if entry.kind == "symlink":
            if not destination.is_symlink() or os.readlink(destination) != entry.link_target:
                conflicts.append(entry.relative_path)
        elif not destination.is_file() or _sha256(destination) != entry.sha256:
            conflicts.append(entry.relative_path)
    probe = target if target.exists() else target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    writable = os.access(probe, os.W_OK | os.X_OK)
    return shutil.disk_usage(probe).free, writable, tuple(conflicts)


def plan_migration(
    source_root: Path | str = DEFAULT_RUNTIME_DIR,
    target_root: Path | str = "/mnt/matchline-runtime",
    *,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> MigrationPlan:
    """Inventory a runtime root without creating or changing any files."""

    source, target = _root_pair(source_root, target_root)
    source_status, entries, skipped = _iter_entries(source)
    destination_free, destination_writable, conflicts = _destination_state(target, entries)
    existing = 0
    for entry in entries:
        destination = target / entry.relative_path
        if entry.kind == "file" and destination.is_file() and destination.stat().st_size == entry.size_bytes:
            existing += entry.size_bytes
    bytes_to_copy = max(0, sum(entry.size_bytes for entry in entries if entry.kind == "file") - existing)
    required = int(min_free_bytes) + bytes_to_copy
    return MigrationPlan(
        source_root=str(source),
        target_root=str(target),
        source_status=source_status,
        entries=tuple(entries),
        skipped_transient=tuple(skipped),
        bytes_to_copy=bytes_to_copy,
        destination_free_bytes=destination_free,
        destination_writable=destination_writable,
        required_free_bytes=required,
        conflicts=conflicts,
    )


def _copy_entry(source: Path, target: Path, entry: RuntimeEntry) -> None:
    source_path = source / entry.relative_path
    target_path = target / entry.relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() or target_path.is_symlink():
        # The plan has already compared the destination.  Re-check here so a
        # concurrent edit cannot silently overwrite an operator's file.
        if entry.kind == "symlink" and target_path.is_symlink() and os.readlink(target_path) == entry.link_target:
            return
        if entry.kind == "file" and target_path.is_file() and _sha256(target_path) == entry.sha256:
            return
        raise FileExistsError(f"destination conflict: {entry.relative_path}")
    if entry.kind == "symlink":
        os.symlink(entry.link_target or "", target_path)
        return
    temporary = target_path.with_name(f".{target_path.name}.migration.{os.getpid()}.tmp")
    try:
        shutil.copy2(source_path, temporary)
        if _sha256(temporary) != entry.sha256:
            raise IOError(f"source changed while copying: {entry.relative_path}")
        temporary.replace(target_path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_migration(plan: MigrationPlan, *, operator_approved: bool, cycle_stopped: bool) -> dict[str, Any]:
    """Copy a previously inspected plan, preserving the source tree."""

    if not operator_approved or not cycle_stopped:
        raise PermissionError("apply requires operator_approved and cycle_stopped")
    if not plan.ready:
        raise RuntimeError("migration plan is not ready: resolve conflicts and destination space first")
    source = Path(plan.source_root)
    target = Path(plan.target_root)
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    for entry in plan.entries:
        destination = target / entry.relative_path
        before = destination.exists() or destination.is_symlink()
        _copy_entry(source, target, entry)
        if before:
            skipped += 1
        else:
            copied += 1
    return {
        "status": "copied",
        "source_root": str(source),
        "target_root": str(target),
        "copied_entries": copied,
        "already_matching_entries": skipped,
        "source_preserved": True,
        "lock_files_copied": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--operator-approved", action="store_true")
    parser.add_argument("--cycle-stopped", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan = plan_migration(args.source, args.target, min_free_bytes=args.min_free_bytes)
    result: dict[str, Any] = {"mode": "apply" if args.apply else "dry_run", "plan": plan.as_dict()}
    if args.apply:
        result["apply"] = apply_migration(
            plan,
            operator_approved=args.operator_approved,
            cycle_stopped=args.cycle_stopped,
        )
    else:
        result["status"] = "ready" if plan.ready else "blocked"
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if (not args.apply and plan.ready) or (args.apply and result.get("apply", {}).get("status") == "copied") else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
