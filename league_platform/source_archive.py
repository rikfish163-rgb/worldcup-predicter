"""Append-only storage and quality reports for current source snapshots.

The live synchroniser still keeps a small ``current.json`` pointer for the
application.  This module is the durable history behind that pointer.  It is
deliberately local-only: callers pass an in-memory payload or an existing JSON
file and no network client is imported or invoked here.

Each accepted payload is written once to ``raw/<content-sha256>.json``.  A
compact metadata record is appended to ``snapshots.jsonl`` and includes the
``as_of`` cut-off, provider status, coverage counts, and the content hash.
Repeated ingestion of the same bytes is idempotent and never overwrites the
previous raw file.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ARCHIVE_DIR = Path("data/live/archive")
MANIFEST_NAME = "snapshots.jsonl"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_HASH_LENGTH = 64
_PROVIDER_KEYS = ("espn", "espn_markets", "understat")
_OPTIONAL_PROVIDER_KEYS = ("news", "weather", "sofascore")
_PROVIDER_RECORD_KEYS = (
    "fixtures",
    "events",
    "markets",
    "odds",
    "team_features",
    "features",
    "xg",
    "items",
    "articles",
    "feeds",
    "forecasts",
    "forecast",
    "observations",
    "weather",
    "matches",
    "records",
    "data",
)


class SnapshotArchiveError(ValueError):
    """Raised when a source snapshot cannot be safely archived or read."""


def _reject_json_constant(value: str) -> None:
    raise SnapshotArchiveError(f"snapshot contains non-finite JSON number: {value}")


def _parse_json(raw: bytes) -> dict[str, Any]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotArchiveError("snapshot is not valid UTF-8 JSON") from exc
    try:
        payload = json.loads(decoded, parse_constant=_reject_json_constant)
    except SnapshotArchiveError:
        raise
    except (TypeError, json.JSONDecodeError) as exc:
        raise SnapshotArchiveError("snapshot JSON is corrupt") from exc
    if not isinstance(payload, dict):
        raise SnapshotArchiveError("snapshot JSON root must be an object")
    return payload


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SnapshotArchiveError("snapshot contains values that cannot be encoded as JSON") from exc


def _parse_datetime(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotArchiveError(f"snapshot {field} must be an ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SnapshotArchiveError(f"snapshot {field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotArchiveError(f"snapshot {field} must include a timezone")
    return parsed


def _reference_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        # A caller-supplied naive clock is interpreted as UTC.  The payload's
        # as_of is never relaxed: it must still contain an explicit timezone.
        return now.replace(tzinfo=timezone.utc)
    return now


def _validate_nested_times(value: Any, *, as_of: datetime, now: datetime, key: str = "") -> None:
    """Reject future source timestamps without treating fixture kickoffs as errors."""

    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            if child_key in {"retrieved_at", "as_of"} and child_value is not None:
                observed = _parse_datetime(child_value, field=child_key)
                if observed.astimezone(timezone.utc) > now.astimezone(timezone.utc):
                    raise SnapshotArchiveError(f"snapshot {child_key} is in the future")
                if child_key == "retrieved_at" and observed.astimezone(
                    timezone.utc
                ) > as_of.astimezone(timezone.utc):
                    raise SnapshotArchiveError("snapshot retrieved_at is later than as_of")
            _validate_nested_times(child_value, as_of=as_of, now=now, key=child_key)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _validate_nested_times(child, as_of=as_of, now=now, key=key)


def _load_input(
    snapshot: Mapping[str, Any] | bytes | bytearray | memoryview | Path | str,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], bytes, str]:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    source_name = "in-memory snapshot"
    if isinstance(snapshot, Mapping):
        payload = dict(snapshot)
        raw = _canonical_bytes(payload)
    else:
        if isinstance(snapshot, Path):
            path = snapshot
        elif isinstance(snapshot, str):
            candidate = Path(snapshot)
            try:
                is_path = candidate.exists()
            except OSError:
                is_path = False
            if is_path:
                path = candidate
            else:
                raw = snapshot.encode("utf-8")
                if len(raw) > max_bytes:
                    raise SnapshotArchiveError("snapshot exceeds the maximum accepted size")
                payload = _parse_json(raw)
                return payload, raw, source_name
        elif isinstance(snapshot, (bytes, bytearray, memoryview)):
            raw = bytes(snapshot)
            if len(raw) > max_bytes:
                raise SnapshotArchiveError("snapshot exceeds the maximum accepted size")
            payload = _parse_json(raw)
            return payload, raw, source_name
        else:
            raise TypeError("snapshot must be a mapping, JSON bytes/text, or a filesystem path")

        source_name = str(path)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise SnapshotArchiveError(f"cannot inspect snapshot input: {path}") from exc
        if size > max_bytes:
            raise SnapshotArchiveError("snapshot exceeds the maximum accepted size")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SnapshotArchiveError(f"cannot read snapshot input: {path}") from exc
        if len(raw) > max_bytes:
            raise SnapshotArchiveError("snapshot exceeds the maximum accepted size")
        payload = _parse_json(raw)

    if len(raw) > max_bytes:
        raise SnapshotArchiveError("snapshot exceeds the maximum accepted size")
    return payload, raw, source_name


def _first_collection(payload: Mapping[str, Any], *paths: tuple[str, ...]) -> list[Any]:
    for path in paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, Mapping):
                break
            current = current.get(key)
        else:
            if isinstance(current, list):
                return current
    return []


def _provider_section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    direct = payload.get(key)
    if isinstance(direct, Mapping):
        return direct
    providers = payload.get("providers")
    if isinstance(providers, Mapping):
        candidate = providers.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _section_records(section: Mapping[str, Any] | None, *keys: str) -> list[Any]:
    if section is None:
        return []
    for key in keys:
        value = section.get(key)
        if isinstance(value, list):
            return value
    return []


def _provider_records(section: Mapping[str, Any] | None) -> list[Any]:
    return _section_records(section, *_PROVIDER_RECORD_KEYS)


def _provider_status(section: Mapping[str, Any] | None, records: list[Any]) -> str:
    if section is None:
        return "missing"
    explicit = section.get("status")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    errors = section.get("errors")
    if isinstance(errors, list) and errors:
        return "degraded" if records else "unavailable"
    return "ok"


def summarize_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable provider status and coverage counts for one snapshot."""

    if not isinstance(payload, Mapping):
        raise SnapshotArchiveError("snapshot payload must be an object")

    fixtures = _first_collection(
        payload,
        ("espn", "fixtures"),
        ("providers", "espn", "fixtures"),
        ("fixtures",),
    )
    markets = _first_collection(
        payload,
        ("espn_markets", "markets"),
        ("providers", "espn_markets", "markets"),
        ("markets",),
        ("odds",),
    )
    xg = _first_collection(
        payload,
        ("understat", "team_features"),
        ("understat", "features"),
        ("providers", "understat", "team_features"),
        ("providers", "understat", "features"),
        ("team_features",),
        ("xg",),
        ("xg_features",),
    )

    records_by_provider = {
        "espn": _section_records(_provider_section(payload, "espn"), "fixtures", "events"),
        "espn_markets": _section_records(
            _provider_section(payload, "espn_markets"), "markets", "odds"
        ),
        "understat": _section_records(
            _provider_section(payload, "understat"), "team_features", "features", "xg"
        ),
    }
    provider_status = {
        key: _provider_status(_provider_section(payload, key), records_by_provider[key])
        for key in _PROVIDER_KEYS
    }
    provider_counts = {key: len(records) for key, records in records_by_provider.items()}
    for key in _OPTIONAL_PROVIDER_KEYS:
        section = _provider_section(payload, key)
        if section is not None:
            records = _provider_records(section)
            provider_status[key] = _provider_status(section, records)
            provider_counts[key] = len(records)
    # Generic provider payloads are accepted as well.  This keeps the report
    # useful for an offline fixture while preserving the current source names.
    providers = payload.get("providers")
    if isinstance(providers, Mapping):
        for key, section in providers.items():
            if key in provider_status or not isinstance(section, Mapping):
                continue
            records = _provider_records(section)
            provider_status[str(key)] = _provider_status(section, records)
            provider_counts[str(key)] = len(records)
    # Current synchronisation payloads use top-level provider sections.  Keep
    # newly added local providers (for example news/weather/sofascore) in the
    # report without requiring this module to import their network clients.
    for key, section in payload.items():
        if key in provider_status or key in {"providers", "roles", "summary"}:
            continue
        if not isinstance(section, Mapping) or not (
            "provider" in section or "errors" in section or "status" in section
        ):
            continue
        records = _provider_records(section)
        provider_status[str(key)] = _provider_status(section, records)
        provider_counts[str(key)] = len(records)

    provider_errors: dict[str, int] = {}
    for key in provider_status:
        section = _provider_section(payload, key)
        errors = section.get("errors") if section else None
        provider_errors[key] = len(errors) if isinstance(errors, list) else 0

    has_recognized_collection = any(
        key in payload for key in ("fixtures", "markets", "odds", "team_features", "xg", "xg_features")
    ) or any(
        isinstance(payload.get(key), Mapping)
        and any(name in payload[key] for name in ("fixtures", "markets", "team_features", "features", "xg"))
        for key in _PROVIDER_KEYS
    )
    if (
        not has_recognized_collection
        and not any(status != "missing" for status in provider_status.values())
    ):
        raise SnapshotArchiveError("snapshot contains no recognized source sections")

    counts = {
        "fixtures": len(fixtures),
        "markets": len(markets),
        "xg": len(xg),
        "xg_team_features": len(xg),
    }
    return {
        "provider_status": provider_status,
        "provider_errors": provider_errors,
        "provider_counts": provider_counts,
        "counts": counts,
        # Flat aliases make the JSONL useful to shell tools without requiring
        # callers to know the nested report shape.
        "fixture_count": counts["fixtures"],
        "market_count": counts["markets"],
        "xg_count": counts["xg"],
        "xg_team_count": counts["xg"],
    }


def _validate_payload(
    payload: dict[str, Any],
    *,
    now: datetime,
) -> datetime:
    if "as_of" not in payload:
        raise SnapshotArchiveError("snapshot is missing as_of")
    as_of = _parse_datetime(payload["as_of"], field="as_of")
    if as_of.astimezone(timezone.utc) > now.astimezone(timezone.utc):
        raise SnapshotArchiveError("snapshot as_of is in the future")
    _validate_nested_times(payload, as_of=as_of, now=now)
    summarize_snapshot(payload)
    return as_of


def _safe_relative_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise SnapshotArchiveError("archive record raw_path is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise SnapshotArchiveError("archive record raw_path escapes the archive")
    return path


def _valid_hash(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != _HASH_LENGTH:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SnapshotArchiveError("archive manifest record must be an object")
    for key in ("as_of", "content_sha256", "raw_path", "counts", "provider_status"):
        if key not in record:
            raise SnapshotArchiveError(f"archive manifest record is missing {key}")
    _parse_datetime(record["as_of"], field="archive as_of")
    if not _valid_hash(record["content_sha256"]):
        raise SnapshotArchiveError("archive manifest content hash is invalid")
    for alias in ("content_hash", "raw_sha256", "sha256"):
        if alias in record and record[alias] != record["content_sha256"]:
            raise SnapshotArchiveError(f"archive manifest {alias} does not match content hash")
    _safe_relative_path(record["raw_path"])
    counts = record["counts"]
    if not isinstance(counts, dict) or any(
        not isinstance(counts.get(key), int) or isinstance(counts.get(key), bool) or counts[key] < 0
        for key in ("fixtures", "markets", "xg")
    ):
        raise SnapshotArchiveError("archive manifest counts are invalid")
    statuses = record["provider_status"]
    if not isinstance(statuses, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in statuses.items()
    ):
        raise SnapshotArchiveError("archive manifest provider status is invalid")
    provider_counts = record.get("provider_counts")
    if provider_counts is not None and (
        not isinstance(provider_counts, dict)
        or any(
            not isinstance(key, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for key, value in provider_counts.items()
        )
    ):
        raise SnapshotArchiveError("archive manifest provider counts are invalid")
    return record


def _read_manifest(archive_dir: Path, *, max_manifest_bytes: int) -> list[dict[str, Any]]:
    manifest = archive_dir / MANIFEST_NAME
    if not manifest.exists():
        return []
    try:
        size = manifest.stat().st_size
    except OSError as exc:
        raise SnapshotArchiveError("cannot inspect archive manifest") from exc
    if size > max_manifest_bytes:
        raise SnapshotArchiveError("archive manifest exceeds the maximum accepted size")
    records: list[dict[str, Any]] = []
    try:
        with manifest.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    raise SnapshotArchiveError(f"archive manifest has a blank line at {line_number}")
                try:
                    decoded = json.loads(line, parse_constant=_reject_json_constant)
                except SnapshotArchiveError:
                    raise
                except (TypeError, json.JSONDecodeError) as exc:
                    raise SnapshotArchiveError(
                        f"archive manifest JSON is corrupt at line {line_number}"
                    ) from exc
                records.append(_validate_record(decoded))
    except UnicodeDecodeError as exc:
        raise SnapshotArchiveError("archive manifest is not valid UTF-8") from exc
    return records


def _raw_path_for(archive_dir: Path, record: Mapping[str, Any]) -> Path:
    relative = _safe_relative_path(record["raw_path"])
    path = archive_dir / relative
    try:
        path.resolve().relative_to(archive_dir.resolve())
    except ValueError as exc:
        raise SnapshotArchiveError("archive raw path escapes the archive") from exc
    return path


def _verify_raw(
    archive_dir: Path,
    record: Mapping[str, Any],
    *,
    max_bytes: int,
    now: datetime,
) -> bytes:
    path = _raw_path_for(archive_dir, record)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SnapshotArchiveError(f"archive raw snapshot is missing: {path}") from exc
    if size > max_bytes:
        raise SnapshotArchiveError("archive raw snapshot exceeds the maximum accepted size")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SnapshotArchiveError(f"cannot read archive raw snapshot: {path}") from exc
    if len(raw) != size:
        raise SnapshotArchiveError("archive raw snapshot changed while being read")
    if hashlib.sha256(raw).hexdigest() != record["content_sha256"]:
        raise SnapshotArchiveError("archive raw snapshot content hash does not match manifest")
    payload = _parse_json(raw)
    payload_as_of = _validate_payload(payload, now=now)
    expected_as_of = _parse_datetime(record["as_of"], field="archive as_of")
    if payload_as_of.astimezone(timezone.utc) != expected_as_of.astimezone(timezone.utc):
        raise SnapshotArchiveError("archive manifest as_of does not match raw snapshot")
    expected = summarize_snapshot(payload)
    for key in ("counts", "provider_status", "provider_errors"):
        if key in record and record[key] != expected[key]:
            raise SnapshotArchiveError(f"archive manifest {key} does not match raw snapshot")
    if "provider_counts" in record and record["provider_counts"] != expected["provider_counts"]:
        raise SnapshotArchiveError("archive manifest provider_counts does not match raw snapshot")
    if "content_size" in record and record["content_size"] != len(raw):
        raise SnapshotArchiveError("archive manifest content_size does not match raw snapshot")
    return raw


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise SnapshotArchiveError(f"cannot append archive manifest: {path}") from exc


def archive_source_snapshot(
    snapshot: Mapping[str, Any] | bytes | bytearray | memoryview | Path | str,
    archive_dir: Path | str = DEFAULT_ARCHIVE_DIR,
    *,
    now: datetime | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Validate and append one local source snapshot.

    ``snapshot`` may be an in-memory mapping, JSON bytes/text, or a path to an
    existing JSON file.  The function never performs network I/O and never
    replaces or deletes an existing raw snapshot.  Re-ingesting identical
    bytes returns the existing manifest record with ``duplicate=True``.
    """

    payload, raw, source_name = _load_input(snapshot, max_bytes=max_bytes)
    reference_now = _reference_now(now)
    as_of = _validate_payload(payload, now=reference_now)
    archive_root = Path(archive_dir)
    archive_root.mkdir(parents=True, exist_ok=True)
    raw_dir = archive_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    lock_path = archive_root / f"{MANIFEST_NAME}.lock"
    content_sha256 = hashlib.sha256(raw).hexdigest()
    raw_path = raw_dir / f"{content_sha256}.json"
    metadata = summarize_snapshot(payload)
    record: dict[str, Any] = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "archive_date": as_of.astimezone(timezone.utc).date().isoformat(),
        "content_sha256": content_sha256,
        "content_hash": content_sha256,
        "raw_sha256": content_sha256,
        "sha256": content_sha256,
        "content_size": len(raw),
        "raw_path": str(raw_path.relative_to(archive_root)),
        "source": source_name,
        **metadata,
    }

    try:
        with lock_path.open("a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
            except OSError as exc:
                raise SnapshotArchiveError("cannot lock snapshot archive") from exc

            existing = _read_manifest(
                archive_root,
                max_manifest_bytes=max(DEFAULT_MAX_BYTES, max_bytes) * 4,
            )
            for previous in existing:
                if previous["content_sha256"] == content_sha256:
                    _verify_raw(archive_root, previous, max_bytes=max_bytes, now=reference_now)
                    result = dict(previous)
                    result["duplicate"] = True
                    return result

            # Exclusive creation prevents a stale or concurrently created raw
            # file from being overwritten.  A pre-existing matching file is
            # only accepted after its bytes have been verified.
            try:
                with raw_path.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if hashlib.sha256(raw_path.read_bytes()).hexdigest() != content_sha256:
                    raise SnapshotArchiveError("archive raw path already contains different content")
            except OSError as exc:
                raise SnapshotArchiveError(f"cannot write archive raw snapshot: {raw_path}") from exc

            _append_jsonl(archive_root / MANIFEST_NAME, record)
            result = dict(record)
            result["duplicate"] = False
            return result
    finally:
        # Keep lock files as stable coordination points; only temporary files
        # would be cleaned here, and none are used for raw data.
        pass


def read_snapshot_archive(
    archive_dir: Path | str = DEFAULT_ARCHIVE_DIR,
    *,
    now: datetime | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Read and integrity-check all append-only archive records."""

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    archive_root = Path(archive_dir)
    reference_now = _reference_now(now)
    records = _read_manifest(
        archive_root,
        max_manifest_bytes=max(DEFAULT_MAX_BYTES, max_bytes) * 4,
    )
    verified: list[dict[str, Any]] = []
    for record in records:
        _verify_raw(archive_root, record, max_bytes=max_bytes, now=reference_now)
        verified.append(dict(record))
    return verified


def build_quality_report(
    archive_dir: Path | str = DEFAULT_ARCHIVE_DIR,
    *,
    start: datetime | date | str | None = None,
    end: datetime | date | str | None = None,
    now: datetime | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Build an offline provider/coverage trend report from archived records."""

    records = read_snapshot_archive(archive_dir, now=now, max_bytes=max_bytes)

    def bound(value: datetime | date | str | None, field: str) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return _parse_datetime(value, field=field)

    lower = bound(start, "report start")
    upper = bound(end, "report end")
    if lower and upper and lower > upper:
        raise ValueError("report start must not be later than end")
    selected = []
    for record in records:
        observed = _parse_datetime(record["as_of"], field="archive as_of")
        stamp = observed.astimezone(timezone.utc)
        if lower and stamp < lower.astimezone(timezone.utc):
            continue
        if upper and stamp > upper.astimezone(timezone.utc):
            continue
        selected.append(record)
    selected.sort(key=lambda item: _parse_datetime(item["as_of"], field="archive as_of"))

    status_counts: dict[str, dict[str, int]] = {}
    for record in selected:
        for provider, status in record["provider_status"].items():
            status_counts.setdefault(provider, {})[status] = (
                status_counts.setdefault(provider, {}).get(status, 0) + 1
            )

    trend = [
        {
            "as_of": record["as_of"],
            "archive_date": record.get("archive_date"),
            "content_sha256": record["content_sha256"],
            "provider_status": record["provider_status"],
            "provider_errors": record.get("provider_errors", {}),
            "provider_counts": record.get("provider_counts", {}),
            "fixture_count": record["fixture_count"],
            "market_count": record["market_count"],
            "xg_count": record["xg_count"],
        }
        for record in selected
    ]
    totals = {
        "fixtures": sum(item["fixture_count"] for item in selected),
        "markets": sum(item["market_count"] for item in selected),
        "xg": sum(item["xg_count"] for item in selected),
    }
    latest = trend[-1] if trend else None
    return {
        "schema_version": "1.0.0",
        "status": "ok" if selected else "empty",
        "snapshot_count": len(selected),
        "unique_content_count": len({item["content_sha256"] for item in selected}),
        "provider_status_counts": status_counts,
        "totals": totals,
        "latest": latest,
        "trend": trend,
    }


# Small aliases keep the API discoverable for callers that use either wording.
append_snapshot = archive_source_snapshot
archive_snapshot = archive_source_snapshot
quality_report = build_quality_report
load_snapshot_archive = read_snapshot_archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--input", type=Path, help="local JSON snapshot to append")
    parser.add_argument("--report", action="store_true", help="print an offline quality report")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    if args.input is None and not args.report:
        parser.error("provide --input or --report")
    if args.input is not None:
        result = archive_source_snapshot(
            args.input,
            args.archive_dir,
            max_bytes=args.max_bytes,
        )
    else:
        result = build_quality_report(args.archive_dir, max_bytes=args.max_bytes)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
