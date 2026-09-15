"""Produce a durable, replay-verified OpenFootball historical receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
    fetch_openfootball_history,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawArchiveError,
    load_verified_openfootball_archive,
)


RECEIPT_SCHEMA_VERSION = "matchline.openfootball_history_refresh.v1"
RECEIPT_ARCHIVE_NAME = "history-refresh-receipts"
OPENFOOTBALL_RAW_ARCHIVE_ENV = "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"
HISTORY_SOURCE_IDS = tuple(sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS))
_RAW_RECEIPT_FIELDS = {
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


class OpenFootballHistorySyncError(RuntimeError):
    """Raised before the mutable receipt pointer can be updated."""

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.errors = [dict(error) for error in errors or []]


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OpenFootballHistorySyncError("receipt value is not canonical JSON") from exc


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OpenFootballHistorySyncError("observed_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value != "0" * 64


def _raw_archive_path_error(message: str) -> OpenFootballHistorySyncError:
    return OpenFootballHistorySyncError(
        message,
        errors=[{"stage": "raw_archive_path", "error": message}],
    )


def _validate_durable_raw_root(value: Path | str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise _raw_archive_path_error(
            "OpenFootball raw archive path must be explicit and absolute"
        )
    if path == Path(path.anchor):
        raise _raw_archive_path_error(
            "OpenFootball raw archive path must not be a filesystem root"
        )
    if ".." in path.parts:
        raise _raw_archive_path_error(
            "OpenFootball raw archive path must not contain parent traversal"
        )

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise _raw_archive_path_error(
                "OpenFootball raw archive path cannot be inspected"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise _raw_archive_path_error(
                "OpenFootball raw archive path must not contain a symlink"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise _raw_archive_path_error("OpenFootball raw archive path must be a directory")

    try:
        resolved = path.resolve(strict=False)
        volatile_root = Path("/dev/shm").resolve(strict=True)
        resolved.relative_to(volatile_root)
    except ValueError:
        return path
    except OSError as exc:
        raise _raw_archive_path_error("OpenFootball raw archive path cannot be resolved") from exc
    raise _raw_archive_path_error("OpenFootball raw archive must not be under /dev/shm")


def _raw_receipts_by_source(
    value: object,
    *,
    observed_at: datetime,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(HISTORY_SOURCE_IDS):
        raise OpenFootballHistorySyncError(
            "history fetch did not return one raw receipt for every fixed source"
        )
    expected_time = observed_at.isoformat()
    receipts: dict[str, dict[str, Any]] = {}
    for item in value:
        source_id = item.get("source_id") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item) != _RAW_RECEIPT_FIELDS
            or item.get("status") != "raw_observation_archived"
            or not isinstance(source_id, str)
            or source_id not in HISTORY_SOURCE_IDS
            or source_id in receipts
            or item.get("retrieved_at") != expected_time
            or any(
                not _valid_sha256(item.get(field))
                for field in (
                    "observation_id",
                    "raw_sha256",
                    "record_sha256",
                    "manifest_sha256",
                )
            )
            or isinstance(item.get("size_bytes"), bool)
            or not isinstance(item.get("size_bytes"), int)
            or item["size_bytes"] < 0
            or not isinstance(item.get("raw_path"), str)
            or not item["raw_path"]
            or not isinstance(item.get("duplicate"), bool)
        ):
            raise OpenFootballHistorySyncError("history raw receipt is invalid")
        receipts[source_id] = dict(item)
    if sorted(receipts) != list(HISTORY_SOURCE_IDS):
        raise OpenFootballHistorySyncError("history raw receipt source set is incomplete")
    return receipts


def _rows_identity(rows: object) -> tuple[list[str], str]:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise OpenFootballHistorySyncError("history rows must be a list of objects")
    fixture_ids: list[str] = []
    canonical_rows: list[dict[str, Any]] = []
    for row in rows:
        fixture_id = row.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise OpenFootballHistorySyncError("history row id is invalid")
        fixture_ids.append(fixture_id)
        canonical_rows.append(row)
    if len(set(fixture_ids)) != len(fixture_ids):
        raise OpenFootballHistorySyncError("history rows contain a duplicate fixture id")
    ordered = [
        row
        for _fixture_id, row in sorted(
            zip(fixture_ids, canonical_rows, strict=True),
            key=lambda item: item[0],
        )
    ]
    return sorted(fixture_ids), hashlib.sha256(_canonical_bytes(ordered)).hexdigest()


def _verify_fetch_matches_replay(
    fetched: Mapping[str, Any],
    replayed: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    history = fetched.get("history")
    unfinished = fetched.get("unfinished_fixtures")
    replayed_rows = replayed.get("rows")
    if not isinstance(replayed_rows, list):
        raise OpenFootballHistorySyncError("verified raw replay omitted rows")
    replayed_finished = [row for row in replayed_rows if row.get("status") == "finished"]
    replayed_unfinished = [row for row in replayed_rows if row.get("status") != "finished"]
    if _rows_identity(history) != _rows_identity(replayed_finished) or _rows_identity(
        unfinished
    ) != _rows_identity(replayed_unfinished):
        raise OpenFootballHistorySyncError(
            "history fetch rows do not match the verified raw replay",
            errors=[
                {
                    "stage": "fetch_replay_comparison",
                    "error": "fetch rows do not match verified raw replay rows",
                }
            ],
        )
    return replayed_finished, replayed_unfinished


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    max_bytes: int = 1024 * 1024,
) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise OpenFootballHistorySyncError("cannot open immutable history receipt") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise OpenFootballHistorySyncError("immutable history receipt is invalid")
        payload = b""
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        if len(payload) != metadata.st_size or len(payload) > max_bytes:
            raise OpenFootballHistorySyncError("immutable history receipt is invalid")
        return payload
    finally:
        os.close(descriptor)


def _open_absolute_directory_nofollow(path: Path) -> int:
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


def _open_or_create_directory_at(parent_descriptor: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        created = True
    except FileExistsError:
        pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    if created:
        os.fsync(parent_descriptor)
    return descriptor


def _write_content_addressed_receipt(raw_root: Path, payload: bytes) -> Path:
    digest = hashlib.sha256(payload).hexdigest()
    parent = raw_root / RECEIPT_ARCHIVE_NAME / "sha256" / digest[:2]
    target = parent / f"{digest}.json"
    parent_descriptor = _open_absolute_directory_nofollow(raw_root)
    try:
        for part in (RECEIPT_ARCHIVE_NAME, "sha256", digest[:2]):
            child = _open_or_create_directory_at(parent_descriptor, part)
            os.close(parent_descriptor)
            parent_descriptor = child
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(
                target.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            if _read_regular_at(parent_descriptor, target.name) != payload:
                raise OpenFootballHistorySyncError(
                    "content-addressed history receipt has conflicting bytes"
                )
            return target
        except OSError as exc:
            raise OpenFootballHistorySyncError("cannot create immutable history receipt") from exc
        try:
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OpenFootballHistorySyncError("history receipt write made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent_descriptor)
        return target
    finally:
        os.close(parent_descriptor)


def _write_pointer_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_openfootball_history(
    *,
    raw_archive_dir: Path | str,
    receipt_path: Path | str,
    now: datetime | None = None,
    opener: object | None = None,
) -> dict[str, Any]:
    """Fetch, durably archive, replay, then publish one verified receipt."""

    if len(HISTORY_SOURCE_IDS) != 66:
        raise OpenFootballHistorySyncError("fixed OpenFootball history source count is not 66")
    observed_at = _utc(now or datetime.now(timezone.utc))
    raw_root = _validate_durable_raw_root(raw_archive_dir)
    pointer = Path(receipt_path)
    archive = OpenFootballRawArchive(raw_root)
    fetched = fetch_openfootball_history(
        now=observed_at,
        source_ids=HISTORY_SOURCE_IDS,
        opener=opener,
        raw_sink=archive,
    )
    if not isinstance(fetched, dict):
        raise OpenFootballHistorySyncError("history fetch result is not an object")
    errors = fetched.get("errors")
    if not isinstance(errors, list):
        raise OpenFootballHistorySyncError(
            "history fetch errors are not a list",
            errors=[{"stage": "fetch_or_parse", "error": "invalid errors field"}],
        )
    if errors:
        raise OpenFootballHistorySyncError(
            "one or more history sources failed",
            errors=[dict(error) for error in errors if isinstance(error, dict)],
        )
    receipts = _raw_receipts_by_source(
        fetched.get("raw_archive_receipts"),
        observed_at=observed_at,
    )
    try:
        replayed = load_verified_openfootball_archive(
            raw_root,
            source_ids=HISTORY_SOURCE_IDS,
            observed_before=observed_at,
        )
    except OpenFootballRawArchiveError as exc:
        raise OpenFootballHistorySyncError(
            "verified history replay failed",
            errors=[
                {
                    "stage": "verified_replay",
                    "error": "verified OpenFootball raw archive replay failed",
                }
            ],
        ) from exc
    selected_records = replayed.get("selected_records")
    if not isinstance(selected_records, list) or len(selected_records) != 66:
        raise OpenFootballHistorySyncError(
            "verified history replay selected-record set is invalid"
        )
    for selected in selected_records:
        source_id = selected.get("source_id") if isinstance(selected, dict) else None
        receipt = receipts.get(source_id) if isinstance(source_id, str) else None
        if receipt is None or any(
            selected.get(field) != receipt.get(field)
            for field in ("source_id", "retrieved_at", "record_sha256", "raw_sha256")
        ):
            raise OpenFootballHistorySyncError(
                "verified history replay does not match this-run raw receipts"
            )
    finished, unfinished = _verify_fetch_matches_replay(fetched, replayed)
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "verified_raw_history",
        "observed_at": observed_at.isoformat(),
        "source_count": len(HISTORY_SOURCE_IDS),
        "source_ids": list(HISTORY_SOURCE_IDS),
        "parsed_fixture_count": len(finished) + len(unfinished),
        "finished_fixture_count": len(finished),
        "unfinished_fixture_count": len(unfinished),
        "admission_sha256": replayed["admission_sha256"],
        "rows_sha256": replayed["rows_sha256"],
        "source_manifest_sha256": replayed["source_manifest_sha256"],
        "parser_contract_sha256": replayed["parser_contract_sha256"],
        "errors": [],
    }
    payload = _canonical_bytes(receipt) + b"\n"
    try:
        _write_content_addressed_receipt(raw_root, payload)
    except (OSError, OpenFootballHistorySyncError) as exc:
        raise OpenFootballHistorySyncError(
            "immutable history receipt write failed",
            errors=[
                {
                    "stage": "receipt_archive",
                    "error": "immutable history receipt write failed",
                }
            ],
        ) from exc
    try:
        _write_pointer_atomic(pointer, payload)
    except (OSError, OpenFootballHistorySyncError) as exc:
        raise OpenFootballHistorySyncError(
            "history receipt pointer update failed",
            errors=[
                {
                    "stage": "receipt_pointer",
                    "error": "history receipt pointer update failed",
                }
            ],
        ) from exc
    return receipt


def _cli_raw_archive_root(explicit: str | None) -> Path:
    configured = explicit.strip() if isinstance(explicit, str) else ""
    if not configured:
        configured = os.environ.get(OPENFOOTBALL_RAW_ARCHIVE_ENV, "").strip()
    if not configured:
        raise _raw_archive_path_error(f"set --raw-archive-dir or {OPENFOOTBALL_RAW_ARCHIVE_ENV}")
    return Path(configured)


def _blocked_output(exc: OpenFootballHistorySyncError) -> dict[str, Any]:
    errors = exc.errors or [{"stage": "history_refresh", "error": str(exc)}]
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "blocked",
        "source_count": len(HISTORY_SOURCE_IDS),
        "source_ids": list(HISTORY_SOURCE_IDS),
        "errors": errors,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh replay-verified OpenFootball historical raw evidence."
    )
    parser.add_argument(
        "--raw-archive-dir",
        default=None,
        help=(
            f"explicit durable raw archive root; falls back only to {OPENFOOTBALL_RAW_ARCHIVE_ENV}"
        ),
    )
    parser.add_argument(
        "--receipt",
        required=True,
        help="mutable runtime pointer to the latest verified receipt",
    )
    args = parser.parse_args(argv)
    try:
        raw_root = _cli_raw_archive_root(args.raw_archive_dir)
        receipt = refresh_openfootball_history(
            raw_archive_dir=raw_root,
            receipt_path=Path(args.receipt),
        )
    except OpenFootballHistorySyncError as exc:
        sys.stderr.buffer.write(_canonical_bytes(_blocked_output(exc)) + b"\n")
        return 2
    sys.stdout.buffer.write(_canonical_bytes(receipt) + b"\n")
    return 0


__all__ = [
    "HISTORY_SOURCE_IDS",
    "OPENFOOTBALL_RAW_ARCHIVE_ENV",
    "OpenFootballHistorySyncError",
    "main",
    "refresh_openfootball_history",
]


if __name__ == "__main__":
    raise SystemExit(main())
