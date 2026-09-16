"""Immutable raw-evidence archive for allowlisted OpenFootball responses.

The archive is local-only.  Fetchers hand it the exact HTTP response bytes
before parsing; it writes those bytes once and appends one observation record
to a locked JSONL manifest.  A stored observation is evidence that bytes were
captured, not an assertion that any row was admitted to training or release.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


MANIFEST_NAME = "manifest.jsonl"
SCHEMA_VERSION = "1.0.0"
PUBLIC_ADMISSION_SCHEMA_VERSION = "matchline.openfootball_raw_admission.v1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RAW_BYTES = 4 * 1024 * 1024
PARSER_CONTRACT = {
    "json": "openfootball_json_v1",
    "txt": "openfootball_football_txt_v1",
}


class OpenFootballRawArchiveError(ValueError):
    """Raised when raw OpenFootball evidence cannot be stored or verified."""


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
        raise OpenFootballRawArchiveError("archive value is not canonical JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OpenFootballRawArchiveError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OpenFootballRawArchiveError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpenFootballRawArchiveError(f"{field} is not an ISO-8601 timestamp") from exc
    return _utc(parsed, field=field)


@dataclass(frozen=True, slots=True)
class OpenFootballRawObservation:
    """One immutable transport observation before any parser accepts rows."""

    source_id: str
    url: str
    retrieved_at: datetime
    payload: bytes
    source_format: str
    competition_id: str
    season: str
    timezone_name: str
    license: str

    def source_config(self) -> tuple[tuple[str, str], ...]:
        """Return the source contract as an immutable canonical tuple."""

        return tuple(
            sorted(
                {
                    "source_id": self.source_id,
                    "url": self.url,
                    "format": self.source_format,
                    "competition_id": self.competition_id,
                    "season": self.season,
                    "timezone": self.timezone_name,
                }.items()
            )
        )


def _allowed_source_config(source_id: str) -> dict[str, str]:
    # Imported lazily so the live adapter can use this module's observation
    # type without creating an import cycle.
    from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES

    config = OPENFOOTBALL_SOURCES.get(source_id)
    if config is None:
        raise OpenFootballRawArchiveError("OpenFootball source_id is not allowlisted")
    return dict(config)


def _validated_observation(observation: OpenFootballRawObservation) -> dict[str, Any]:
    if not isinstance(observation, OpenFootballRawObservation):
        raise TypeError("observation must be an OpenFootballRawObservation")
    if type(observation.payload) is not bytes:
        raise OpenFootballRawArchiveError("OpenFootball raw payload must be bytes")
    if len(observation.payload) > MAX_RAW_BYTES:
        raise OpenFootballRawArchiveError("OpenFootball raw payload exceeded 4 MiB")
    retrieved_at = _utc(observation.retrieved_at, field="retrieved_at")
    supplied_config = dict(observation.source_config())
    allowed_config = _allowed_source_config(observation.source_id)
    if supplied_config != allowed_config:
        raise OpenFootballRawArchiveError(
            "OpenFootball source configuration does not match the exact allowlist"
        )
    if observation.license != "CC0-1.0":
        raise OpenFootballRawArchiveError("OpenFootball license must be CC0-1.0")
    raw_sha256 = _sha256_bytes(observation.payload)
    identity = {
        "source_id": observation.source_id,
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": raw_sha256,
        "size_bytes": len(observation.payload),
        "source_config": supplied_config,
        "license": observation.license,
    }
    observation_id = _sha256_bytes(_canonical_bytes(identity))
    raw_path = Path("raw") / "sha256" / raw_sha256[:2] / f"{raw_sha256}.raw"
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": observation_id,
        **identity,
        "url": observation.url,
        "parser_contract": PARSER_CONTRACT[observation.source_format],
        "raw_path": raw_path.as_posix(),
    }
    record["record_sha256"] = _sha256_bytes(_canonical_bytes(record))
    return record


def _write_all(file_descriptor: int, payload: bytes) -> None:
    position = 0
    while position < len(payload):
        written = os.write(file_descriptor, payload[position:])
        if written <= 0:
            raise OpenFootballRawArchiveError("archive write made no progress")
        position += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _existing_directory(path: Path, *, label: str) -> bool:
    try:
        path_status = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OpenFootballRawArchiveError(f"cannot inspect OpenFootball {label}") from exc
    if stat.S_ISLNK(path_status.st_mode):
        raise OpenFootballRawArchiveError(f"OpenFootball {label} must not be a symlink")
    if not stat.S_ISDIR(path_status.st_mode):
        raise OpenFootballRawArchiveError(f"OpenFootball {label} must be a directory")
    return True


def _ensure_directory(path: Path, *, label: str) -> None:
    if _existing_directory(path, label=label):
        return
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        if not _existing_directory(path, label=label):
            raise OpenFootballRawArchiveError(f"cannot create OpenFootball {label}")
    except OSError as exc:
        raise OpenFootballRawArchiveError(f"cannot create OpenFootball {label}") from exc
    _fsync_directory(path.parent)


def _prepare_archive_directories(root: Path, raw_parent: Path) -> None:
    if not _existing_directory(root, label="archive root"):
        try:
            root.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OpenFootballRawArchiveError(
                "cannot create OpenFootball archive root parent"
            ) from exc
        _ensure_directory(root, label="archive root")
    try:
        relative_parent = raw_parent.relative_to(root)
    except ValueError as exc:
        raise OpenFootballRawArchiveError("OpenFootball raw path escapes archive root") from exc
    current = root
    for part in relative_parent.parts:
        if part in ("", ".", ".."):
            raise OpenFootballRawArchiveError("OpenFootball raw path escapes archive root")
        current = current / part
        _ensure_directory(current, label="archive raw directory")


def _read_regular_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    fsync_after_read: bool = False,
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        try:
            is_symlink = stat.S_ISLNK(path.lstat().st_mode)
        except OSError:
            is_symlink = False
        if is_symlink:
            raise OpenFootballRawArchiveError(
                f"OpenFootball {label} must not be a symlink"
            ) from exc
        raise OpenFootballRawArchiveError(f"cannot open OpenFootball {label}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OpenFootballRawArchiveError(f"OpenFootball {label} must be a regular file")
        if before.st_size < 0 or before.st_size > max_bytes:
            raise OpenFootballRawArchiveError(f"OpenFootball {label} exceeds its size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise OpenFootballRawArchiveError(f"OpenFootball {label} exceeds its size limit")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or total != before.st_size:
            raise OpenFootballRawArchiveError(f"OpenFootball {label} changed while being read")
        if fsync_after_read:
            os.fsync(descriptor)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_manifest_bytes(path: Path, *, fsync_after_read: bool = False) -> bytes:
    return _read_regular_file(
        path,
        max_bytes=MAX_MANIFEST_BYTES,
        label="archive manifest",
        fsync_after_read=fsync_after_read,
    )


def _validate_archive_member_path(root: Path, member: Path) -> None:
    try:
        root_status = root.lstat()
    except OSError as exc:
        raise OpenFootballRawArchiveError("OpenFootball archive root is missing") from exc
    if stat.S_ISLNK(root_status.st_mode):
        raise OpenFootballRawArchiveError("OpenFootball archive root must not be a symlink")
    if not stat.S_ISDIR(root_status.st_mode):
        raise OpenFootballRawArchiveError("OpenFootball archive root must be a directory")
    try:
        relative = member.relative_to(root)
    except ValueError as exc:
        raise OpenFootballRawArchiveError("OpenFootball archive path escapes its root") from exc
    if relative.is_absolute() or ".." in relative.parts:
        raise OpenFootballRawArchiveError("OpenFootball archive path escapes its root")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            current_status = current.lstat()
        except OSError as exc:
            raise OpenFootballRawArchiveError(
                "OpenFootball archive path component is missing"
            ) from exc
        if stat.S_ISLNK(current_status.st_mode):
            raise OpenFootballRawArchiveError(
                "OpenFootball archive path component must not be a symlink"
            )
        if not stat.S_ISDIR(current_status.st_mode):
            raise OpenFootballRawArchiveError(
                "OpenFootball archive path component must be a directory"
            )


def _read_manifest_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        text = _read_manifest_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenFootballRawArchiveError("OpenFootball archive manifest is not UTF-8") from exc
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise OpenFootballRawArchiveError(
                f"OpenFootball archive manifest has a blank line at {line_number}"
            )
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OpenFootballRawArchiveError(
                f"OpenFootball archive manifest is corrupt at line {line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise OpenFootballRawArchiveError("OpenFootball archive record must be an object")
        records.append(record)
    return records


def _valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_manifest_record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OpenFootballRawArchiveError("OpenFootball archive record must be an object")
    expected_keys = {
        "schema_version",
        "observation_id",
        "source_id",
        "retrieved_at",
        "raw_sha256",
        "size_bytes",
        "source_config",
        "license",
        "url",
        "parser_contract",
        "raw_path",
        "record_sha256",
    }
    if set(value) != expected_keys:
        raise OpenFootballRawArchiveError("OpenFootball archive record fields are invalid")
    if value["schema_version"] != SCHEMA_VERSION:
        raise OpenFootballRawArchiveError("OpenFootball archive schema is unsupported")
    if not _valid_sha256(value["observation_id"]):
        raise OpenFootballRawArchiveError("OpenFootball observation_id is invalid")
    if not _valid_sha256(value["raw_sha256"]):
        raise OpenFootballRawArchiveError("OpenFootball raw_sha256 is invalid")
    if not _valid_sha256(value["record_sha256"]):
        raise OpenFootballRawArchiveError("OpenFootball record_sha256 is invalid")
    size_bytes = value["size_bytes"]
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise OpenFootballRawArchiveError("OpenFootball archive size_bytes is invalid")
    source_id = value["source_id"]
    if not isinstance(source_id, str):
        raise OpenFootballRawArchiveError("OpenFootball archive source_id is invalid")
    allowed = _allowed_source_config(source_id)
    if value["source_config"] != allowed:
        raise OpenFootballRawArchiveError(
            "OpenFootball archive source configuration does not match the exact allowlist"
        )
    if value["url"] != allowed["url"]:
        raise OpenFootballRawArchiveError("OpenFootball archive URL is not allowlisted")
    if value["license"] != "CC0-1.0":
        raise OpenFootballRawArchiveError("OpenFootball archive license is invalid")
    if value["parser_contract"] != PARSER_CONTRACT[allowed["format"]]:
        raise OpenFootballRawArchiveError("OpenFootball archive parser contract is invalid")
    retrieved_at = _timestamp(value["retrieved_at"], field="record.retrieved_at")
    if value["retrieved_at"] != retrieved_at.isoformat():
        raise OpenFootballRawArchiveError("OpenFootball archive retrieved_at is not canonical UTC")
    raw_path = (
        Path("raw") / "sha256" / value["raw_sha256"][:2] / f"{value['raw_sha256']}.raw"
    ).as_posix()
    if value["raw_path"] != raw_path:
        raise OpenFootballRawArchiveError("OpenFootball archive raw_path is not content-addressed")
    identity = {
        "source_id": source_id,
        "retrieved_at": value["retrieved_at"],
        "raw_sha256": value["raw_sha256"],
        "size_bytes": size_bytes,
        "source_config": allowed,
        "license": value["license"],
    }
    if value["observation_id"] != _sha256_bytes(_canonical_bytes(identity)):
        raise OpenFootballRawArchiveError(
            "OpenFootball observation identity does not match record"
        )
    unsigned = {key: item for key, item in value.items() if key != "record_sha256"}
    if value["record_sha256"] != _sha256_bytes(_canonical_bytes(unsigned)):
        raise OpenFootballRawArchiveError("OpenFootball archive record hash does not match")
    return dict(value)


def _verified_manifest_records(path: Path) -> list[dict[str, Any]]:
    records = [_validate_manifest_record(record) for record in _read_manifest_records(path)]
    observations: set[str] = set()
    captures: dict[tuple[str, str], str] = {}
    for record in records:
        observation_id = record["observation_id"]
        if observation_id in observations:
            raise OpenFootballRawArchiveError(
                "OpenFootball archive has a duplicate observation_id"
            )
        observations.add(observation_id)
        capture = (record["source_id"], record["retrieved_at"])
        previous_hash = captures.get(capture)
        if previous_hash is not None and previous_hash != record["raw_sha256"]:
            raise OpenFootballRawArchiveError(
                "OpenFootball archive has conflicting bytes for the same source and retrieved_at"
            )
        captures[capture] = record["raw_sha256"]
    return records


def _verify_raw_bytes(
    root: Path,
    record: Mapping[str, Any],
    *,
    fsync_after_read: bool = False,
) -> bytes:
    raw_path = root / record["raw_path"]
    _validate_archive_member_path(root, raw_path)
    raw = _read_regular_file(
        raw_path,
        max_bytes=MAX_RAW_BYTES,
        label="raw object",
        fsync_after_read=fsync_after_read,
    )
    if len(raw) != record["size_bytes"]:
        raise OpenFootballRawArchiveError("OpenFootball raw object size does not match manifest")
    if _sha256_bytes(raw) != record["raw_sha256"]:
        raise OpenFootballRawArchiveError("OpenFootball raw object hash does not match manifest")
    return raw


def _selected_source_ids(source_ids: Iterable[str]) -> list[str]:
    if isinstance(source_ids, str):
        raise OpenFootballRawArchiveError("source_ids must be an iterable of source identifiers")
    selected = list(source_ids)
    if not selected:
        raise OpenFootballRawArchiveError("source_ids must not be empty")
    if any(not isinstance(source_id, str) for source_id in selected):
        raise OpenFootballRawArchiveError("source_ids contains a non-string value")
    if len(set(selected)) != len(selected):
        raise OpenFootballRawArchiveError("source_ids contains a duplicate")
    for source_id in selected:
        _allowed_source_config(source_id)
    return sorted(selected)


def _source_policy_manifest(source_ids: list[str]) -> list[dict[str, Any]]:
    from league_platform.live_sources.openfootball_live import (
        OPENFOOTBALL_CURRENT_SOURCE_IDS,
    )
    from league_platform.source_rights import SourceId, UseCase, require_source_rights

    current_ids = set(OPENFOOTBALL_CURRENT_SOURCE_IDS)
    manifest: list[dict[str, Any]] = []
    for source_id in source_ids:
        policy_source = (
            SourceId.OPENFOOTBALL_CURRENT
            if source_id in current_ids
            else SourceId.OPENFOOTBALL_HISTORICAL
        )
        manifest.append(
            {
                "source_id": source_id,
                "source_config": _allowed_source_config(source_id),
                "license": "CC0-1.0",
                "policy_source_id": policy_source.value,
                "policy_decisions": {
                    use_case.value: require_source_rights(policy_source, use_case).as_dict()
                    for use_case in (
                        UseCase.NETWORK_FETCH,
                        UseCase.SERVE_CURRENT,
                        UseCase.MODEL_INPUT,
                        UseCase.TRAINING,
                        UseCase.REDISTRIBUTION,
                    )
                },
            }
        )
    return manifest


def _validate_reparsed_row(
    row: object,
    *,
    record: Mapping[str, Any],
    config: Mapping[str, str],
) -> dict[str, Any]:
    from league_platform.live_sources import openfootball_live

    if not isinstance(row, dict):
        raise OpenFootballRawArchiveError("OpenFootball parser returned a non-object row")
    source = row.get("source")
    lineage = row.get("lineage")
    if not isinstance(source, dict) or not isinstance(lineage, dict):
        raise OpenFootballRawArchiveError("OpenFootball row source lineage is missing")
    expected_lineage_keys = {
        "source_id",
        "url",
        "retrieved_at",
        "raw_sha256",
        "hash",
        "license",
        "parser",
        "record_index",
        "line_number",
        "record_path",
        "source_row_id",
        "date_inherited",
        "time_inherited",
    }
    if set(lineage) != expected_lineage_keys:
        raise OpenFootballRawArchiveError("OpenFootball row lineage fields are invalid")
    expected_source = {
        "name": "OpenFootball",
        "source_id": record["source_id"],
        "url": record["url"],
        "retrieved_at": record["retrieved_at"],
        "raw_sha256": record["raw_sha256"],
        "hash": f"sha256:{record['raw_sha256']}",
        "license": record["license"],
    }
    if source != expected_source:
        raise OpenFootballRawArchiveError("OpenFootball row source does not match raw evidence")
    for key, expected in (
        ("source_id", record["source_id"]),
        ("url", record["url"]),
        ("retrieved_at", record["retrieved_at"]),
        ("raw_sha256", record["raw_sha256"]),
        ("hash", f"sha256:{record['raw_sha256']}"),
        ("license", record["license"]),
        ("parser", record["parser_contract"]),
    ):
        if lineage.get(key) != expected:
            raise OpenFootballRawArchiveError(
                f"OpenFootball row lineage {key} does not match raw evidence"
            )
    record_index = lineage["record_index"]
    line_number = lineage["line_number"]
    if (
        isinstance(record_index, bool)
        or not isinstance(record_index, int)
        or record_index < 0
        or isinstance(line_number, bool)
        or not isinstance(line_number, int)
        or line_number < 1
        or not isinstance(lineage["date_inherited"], bool)
        or not isinstance(lineage["time_inherited"], bool)
    ):
        raise OpenFootballRawArchiveError("OpenFootball row lineage pointer is invalid")
    expected_record_path = (
        f"$.matches[{record_index}]"
        if record["parser_contract"] == PARSER_CONTRACT["json"]
        else f"L{line_number}"
    )
    if (
        lineage["record_path"] != expected_record_path
        or lineage["source_row_id"] != f"{record['source_id']}#{expected_record_path}"
    ):
        raise OpenFootballRawArchiveError("OpenFootball row lineage pointer is invalid")
    home = row.get("home_team")
    away = row.get("away_team")
    if not isinstance(home, str) or not isinstance(away, str):
        raise OpenFootballRawArchiveError("OpenFootball row team identity is invalid")
    expected_fixture_id = openfootball_live._fixture_id(
        config=dict(config),
        home=home,
        away=away,
        round_name=row.get("round") if isinstance(row.get("round"), str) else None,
    )
    if row.get("id") != expected_fixture_id:
        raise OpenFootballRawArchiveError("OpenFootball row fixture id is not deterministic")
    if (
        row.get("competition_id") != config["competition_id"]
        or row.get("season") != config["season"]
        or row.get("provider_fixture_ids") != {"OpenFootball": expected_fixture_id}
    ):
        raise OpenFootballRawArchiveError(
            "OpenFootball row identity does not match allowlisted source configuration"
        )
    return row


def load_verified_openfootball_archive(
    root: Path | str,
    *,
    source_ids: Iterable[str],
    observed_before: datetime,
) -> dict[str, Any]:
    """Rebuild the latest raw observation per source at an inclusive cutoff."""

    from league_platform.live_sources.openfootball_live import (
        parse_openfootball_json,
        parse_openfootball_txt,
    )

    archive_root = Path(root)
    cutoff = _utc(observed_before, field="observed_before")
    selected_ids = _selected_source_ids(source_ids)
    manifest_path = archive_root / MANIFEST_NAME
    _validate_archive_member_path(archive_root, manifest_path)
    manifest_bytes = _read_manifest_bytes(manifest_path)
    records = _verified_manifest_records(manifest_path)
    raw_objects: dict[str, bytes] = {}
    for record in records:
        raw_sha256 = record["raw_sha256"]
        if raw_sha256 not in raw_objects:
            raw_objects[raw_sha256] = _verify_raw_bytes(archive_root, record)
    selected_records: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for source_id in selected_ids:
        eligible = [
            record
            for record in records
            if record["source_id"] == source_id
            and _timestamp(record["retrieved_at"], field="record.retrieved_at") <= cutoff
        ]
        if not eligible:
            raise OpenFootballRawArchiveError(
                f"OpenFootball archive has no observation for {source_id} at cutoff"
            )
        selected = max(
            eligible,
            key=lambda record: _timestamp(record["retrieved_at"], field="record.retrieved_at"),
        )
        raw = raw_objects[selected["raw_sha256"]]
        config = _allowed_source_config(source_id)
        parser = parse_openfootball_json if config["format"] == "json" else parse_openfootball_txt
        parsed_rows = parser(
            raw,
            source_id=source_id,
            retrieved_at=_timestamp(selected["retrieved_at"], field="record.retrieved_at"),
            url=config["url"],
        )
        rows.extend(
            _validate_reparsed_row(row, record=selected, config=config) for row in parsed_rows
        )
        selected_records.append(
            {
                "source_id": source_id,
                "retrieved_at": selected["retrieved_at"],
                "record_sha256": selected["record_sha256"],
                "raw_sha256": selected["raw_sha256"],
            }
        )

    fixture_identities: dict[str, str] = {}
    for row in rows:
        fixture_id = row.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise OpenFootballRawArchiveError("OpenFootball row fixture id is invalid")
        row_sha256 = _sha256_bytes(_canonical_bytes(row))
        previous = fixture_identities.get(fixture_id)
        if previous is not None:
            qualifier = "conflicting " if previous != row_sha256 else ""
            raise OpenFootballRawArchiveError(
                f"OpenFootball archive has a {qualifier}duplicate fixture id"
            )
        fixture_identities[fixture_id] = row_sha256

    source_manifest = _source_policy_manifest(selected_ids)
    source_manifest_sha256 = _sha256_bytes(_canonical_bytes(source_manifest))
    parser_contract_sha256 = _sha256_bytes(_canonical_bytes(PARSER_CONTRACT))
    rows_sha256 = _sha256_bytes(_canonical_bytes(rows))
    admission_identity = {
        "schema_version": SCHEMA_VERSION,
        "observed_before": cutoff.isoformat(),
        "source_ids": selected_ids,
        "selected_record_sha256": [row["record_sha256"] for row in selected_records],
        "source_manifest_sha256": source_manifest_sha256,
        "parser_contract_sha256": parser_contract_sha256,
        "rows_sha256": rows_sha256,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "verified_raw_candidate",
        "training_admitted": False,
        "observed_before": cutoff.isoformat(),
        "source_ids": selected_ids,
        "selected_records": selected_records,
        "source_manifest": source_manifest,
        "parser_contract": dict(PARSER_CONTRACT),
        "rows": rows,
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "source_manifest_sha256": source_manifest_sha256,
        "parser_contract_sha256": parser_contract_sha256,
        "rows_sha256": rows_sha256,
        "admission_sha256": _sha256_bytes(_canonical_bytes(admission_identity)),
    }


def verify_openfootball_snapshot_admission(
    snapshot: Mapping[str, Any],
    *,
    archive_root: Path | str,
) -> dict[str, Any]:
    """Rebuild and compare the OpenFootball rows used by a publish snapshot.

    A snapshot is mutable JSON and therefore cannot prove its own provenance.
    This boundary replays the selected raw observations from the durable
    archive, verifies the admission identity carried by the snapshot, and
    compares every exact fixture row's identity and source lineage with the
    replay.  Date-only rows may remain in the admission corpus but are never
    publishable fixture rows.
    """

    from league_platform.fixture_feed import fixture_feed_section, fixture_rows

    if not isinstance(snapshot, Mapping):
        raise OpenFootballRawArchiveError("OpenFootball publication snapshot must be an object")
    root = Path(archive_root)
    try:
        resolved_root = root.resolve(strict=False)
        resolved_root.relative_to(Path("/dev/shm").resolve(strict=True))
    except ValueError:
        pass
    except OSError as exc:
        raise OpenFootballRawArchiveError(
            "cannot resolve OpenFootball publication archive root"
        ) from exc
    else:
        raise OpenFootballRawArchiveError(
            "OpenFootball publication archive must be durable, not /dev/shm"
        )

    section = fixture_feed_section(snapshot)
    if section.get("provider") != "OpenFootball":
        raise OpenFootballRawArchiveError(
            "OpenFootball publication fixture feed provider is invalid"
        )
    admission = section.get("raw_archive_admission")
    if not isinstance(admission, Mapping):
        raise OpenFootballRawArchiveError("raw_archive_admission is required")
    required_admission_fields = (
        "schema_version",
        "status",
        "policy_version",
        "observed_before",
        "source_ids",
        "row_count",
        "selected_records",
        "admission_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
        "rows_sha256",
    )
    if any(field not in admission for field in required_admission_fields):
        raise OpenFootballRawArchiveError(
            "OpenFootball publication admission is incomplete"
        )
    admission_schema_version = admission.get("schema_version")
    if admission_schema_version not in {
        SCHEMA_VERSION,
        PUBLIC_ADMISSION_SCHEMA_VERSION,
    }:
        raise OpenFootballRawArchiveError(
            "OpenFootball publication admission schema_version is invalid"
        )
    if admission.get("status") != "verified_current_raw":
        raise OpenFootballRawArchiveError(
            "OpenFootball publication admission is not verified_current_raw"
        )
    from league_platform.source_rights import POLICY_VERSION

    if admission.get("policy_version") != POLICY_VERSION:
        raise OpenFootballRawArchiveError(
            "OpenFootball publication admission policy version is invalid"
        )
    source_ids = admission.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids or any(
        not isinstance(source_id, str) or not source_id for source_id in source_ids
    ) or len(set(source_ids)) != len(source_ids):
        raise OpenFootballRawArchiveError(
            "OpenFootball publication admission source_ids are invalid"
        )
    observed_before = _timestamp(admission.get("observed_before"), field="observed_before")
    verified = load_verified_openfootball_archive(
        root,
        source_ids=source_ids,
        observed_before=observed_before,
    )
    for field in (
        "observed_before",
        "source_ids",
        "selected_records",
        "admission_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
        "rows_sha256",
    ):
        if admission.get(field) != verified.get(field):
            raise OpenFootballRawArchiveError(
                f"OpenFootball publication admission {field} does not match replay"
            )
    if admission_schema_version == SCHEMA_VERSION and admission.get("schema_version") != verified.get(
        "schema_version"
    ):
        raise OpenFootballRawArchiveError(
            "OpenFootball publication admission schema_version does not match replay"
        )
    if admission.get("row_count") != len(verified["rows"]):
        raise OpenFootballRawArchiveError(
            "OpenFootball publication admission row_count does not match replay"
        )

    verified_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_row in verified["rows"]:
        fixture_id = raw_row.get("id") if isinstance(raw_row, Mapping) else None
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in verified_by_id:
            raise OpenFootballRawArchiveError(
                "OpenFootball publication replay has duplicate or invalid fixture IDs"
            )
        verified_by_id[fixture_id] = raw_row

    rows = fixture_rows(snapshot)
    if not rows:
        raise OpenFootballRawArchiveError(
            "OpenFootball publication fixture feed has no exact rows"
        )
    compared_fields = (
        "id",
        "competition_id",
        "season",
        "round",
        "kickoff_date",
        "kickoff_at",
        "kickoff_time_quality",
        "kickoff_time_source",
        "kickoff_timezone",
        "home_team",
        "away_team",
        "status",
        "score",
        "halftime_score",
        "result_scope",
        "provider_fixture_ids",
    )
    source_fields = (
        "name",
        "source_id",
        "url",
        "retrieved_at",
        "raw_sha256",
        "hash",
        "license",
    )
    seen_snapshot_ids: set[str] = set()
    for row in rows:
        fixture_id = row.get("id")
        if not isinstance(fixture_id, str) or fixture_id in seen_snapshot_ids:
            raise OpenFootballRawArchiveError(
                "OpenFootball publication snapshot has duplicate or invalid fixture IDs"
            )
        seen_snapshot_ids.add(fixture_id)
        expected = verified_by_id.get(fixture_id)
        if expected is None:
            raise OpenFootballRawArchiveError(
                "OpenFootball publication fixture is absent from raw replay"
            )
        if any(row.get(field) != expected.get(field) for field in compared_fields):
            raise OpenFootballRawArchiveError(
                "OpenFootball publication fixture facts do not match raw replay"
            )
        source = row.get("source")
        expected_source = expected.get("source")
        if not isinstance(source, Mapping) or not isinstance(expected_source, Mapping):
            raise OpenFootballRawArchiveError(
                "OpenFootball publication source lineage is missing"
            )
        if set(source) != set(expected_source) or any(
            source.get(field) != expected_source.get(field) for field in source_fields
        ):
            raise OpenFootballRawArchiveError(
                "OpenFootball publication source lineage does not match raw replay"
            )
        lineage = row.get("lineage")
        expected_lineage = expected.get("lineage")
        if not isinstance(lineage, Mapping) or not isinstance(expected_lineage, Mapping):
            raise OpenFootballRawArchiveError(
                "OpenFootball publication row lineage is missing"
            )
        if set(lineage) != set(expected_lineage):
            raise OpenFootballRawArchiveError(
                "OpenFootball publication row lineage does not match raw replay"
            )
        for field in (
            "source_id",
            "url",
            "retrieved_at",
            "raw_sha256",
            "hash",
            "license",
            "parser",
            "record_index",
            "line_number",
            "record_path",
            "source_row_id",
        ):
            if lineage.get(field) != expected_lineage.get(field):
                raise OpenFootballRawArchiveError(
                    "OpenFootball publication row lineage does not match raw replay"
                )
        for field in ("date_inherited", "time_inherited"):
            if lineage.get(field) != expected_lineage.get(field):
                raise OpenFootballRawArchiveError(
                    "OpenFootball publication row lineage does not match raw replay"
                )

    return {
        "status": "verified_current_raw",
        "schema_version": admission_schema_version,
        "observed_before": verified["observed_before"],
        "source_ids": list(verified["source_ids"]),
        "admission_sha256": verified["admission_sha256"],
        "source_manifest_sha256": verified["source_manifest_sha256"],
        "parser_contract_sha256": verified["parser_contract_sha256"],
        "rows_sha256": verified["rows_sha256"],
        "raw_row_count": len(verified["rows"]),
        "published_fixture_count": len(rows),
    }


def _receipt(
    record: Mapping[str, Any], *, manifest_sha256: str, duplicate: bool
) -> dict[str, Any]:
    return {
        "status": "raw_observation_archived",
        "observation_id": record["observation_id"],
        "source_id": record["source_id"],
        "retrieved_at": record["retrieved_at"],
        "raw_sha256": record["raw_sha256"],
        "size_bytes": record["size_bytes"],
        "raw_path": record["raw_path"],
        "record_sha256": record["record_sha256"],
        "manifest_sha256": manifest_sha256,
        "duplicate": duplicate,
    }


def validate_openfootball_raw_receipt(
    observation: OpenFootballRawObservation,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a receipt only when it exactly binds the supplied observation."""

    if not isinstance(receipt, Mapping):
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt must be an object")
    supplied = dict(receipt)
    expected_keys = {
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
    if set(supplied) != expected_keys:
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt fields are invalid")
    record = _validated_observation(observation)
    expected = _receipt(record, manifest_sha256=supplied["manifest_sha256"], duplicate=False)
    for key in expected_keys - {"manifest_sha256", "duplicate"}:
        if supplied[key] != expected[key]:
            raise OpenFootballRawArchiveError(
                f"OpenFootball raw sink receipt {key} does not match observation"
            )
    if not isinstance(supplied["duplicate"], bool):
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt duplicate is invalid")
    manifest_sha256 = supplied["manifest_sha256"]
    if not _valid_sha256(manifest_sha256) or manifest_sha256 == "0" * 64:
        raise OpenFootballRawArchiveError(
            "OpenFootball raw sink receipt manifest_sha256 is invalid"
        )
    return supplied


def verify_openfootball_raw_receipt(
    root: Path | str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one producer receipt to the durable manifest and raw object.

    A receipt is transport metadata supplied by the producer and is therefore
    not trusted merely because its hashes have the right shape.  This helper
    revalidates the append-only manifest, locates the exact observation record,
    verifies its content-addressed raw object, and checks that the receipt's
    manifest hash was a real prefix hash at or after that record.  Prefix
    matching is intentional: concurrent stores can return a manifest hash from
    the instant of their append, while later stores may have extended the file
    before the consumer runs.
    """

    if not isinstance(receipt, Mapping):
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt must be an object")
    supplied = dict(receipt)
    expected_keys = {
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
    if set(supplied) != expected_keys:
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt fields are invalid")
    if supplied.get("status") != "raw_observation_archived":
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt status is invalid")
    if not isinstance(supplied.get("source_id"), str):
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt source_id is invalid")
    if not _valid_sha256(supplied.get("observation_id")):
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt observation_id is invalid")
    if not _valid_sha256(supplied.get("raw_sha256")):
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt raw_sha256 is invalid")
    if not _valid_sha256(supplied.get("record_sha256")):
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt record_sha256 is invalid")
    if not _valid_sha256(supplied.get("manifest_sha256")) or supplied["manifest_sha256"] == "0" * 64:
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt manifest_sha256 is invalid")
    size_bytes = supplied.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt size_bytes is invalid")
    if not isinstance(supplied.get("raw_path"), str) or not supplied["raw_path"]:
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt raw_path is invalid")
    if not isinstance(supplied.get("retrieved_at"), str):
        raise OpenFootballRawArchiveError(
            "OpenFootball raw sink receipt retrieved_at is invalid"
        )
    _timestamp(supplied["retrieved_at"], field="receipt.retrieved_at")
    if not isinstance(supplied.get("duplicate"), bool):
        raise OpenFootballRawArchiveError("OpenFootball raw sink receipt duplicate is invalid")

    archive_root = Path(root)
    manifest_path = archive_root / MANIFEST_NAME
    _validate_archive_member_path(archive_root, manifest_path)
    manifest_bytes = _read_manifest_bytes(manifest_path, fsync_after_read=True)
    records = _verified_manifest_records(manifest_path)
    # The manifest is append-only but can be observed while another writer is
    # finishing its fsync.  Read it again and refuse a mixed view rather than
    # validating a receipt against bytes from two different versions.
    if _read_manifest_bytes(manifest_path, fsync_after_read=True) != manifest_bytes:
        raise OpenFootballRawArchiveError("OpenFootball archive manifest changed while being read")

    matches = [
        record
        for record in records
        if record.get("observation_id") == supplied["observation_id"]
    ]
    if len(matches) != 1:
        raise OpenFootballRawArchiveError(
            "OpenFootball raw sink receipt observation is absent or duplicated"
        )
    record = matches[0]
    for field in (
        "source_id",
        "retrieved_at",
        "raw_sha256",
        "size_bytes",
        "raw_path",
        "record_sha256",
    ):
        if supplied[field] != record[field]:
            raise OpenFootballRawArchiveError(
                f"OpenFootball raw sink receipt {field} does not match manifest"
            )

    _verify_raw_bytes(archive_root, record, fsync_after_read=True)
    lines = manifest_bytes.splitlines(keepends=True)
    if len(lines) != len(records):
        raise OpenFootballRawArchiveError("OpenFootball archive manifest line count changed")
    record_index = next(
        index
        for index, candidate in enumerate(records)
        if candidate["observation_id"] == supplied["observation_id"]
    )
    prefix = b""
    manifest_match = False
    for index, line in enumerate(lines):
        prefix += line
        expected_position = supplied["duplicate"] or index == record_index
        if index >= record_index and expected_position and _sha256_bytes(prefix) == supplied[
            "manifest_sha256"
        ]:
            manifest_match = True
            break
    if not manifest_match:
        raise OpenFootballRawArchiveError(
            "OpenFootball raw sink receipt manifest_sha256 does not match manifest"
        )
    return dict(record)


class OpenFootballRawArchive:
    """Locked, content-addressed sink for raw OpenFootball observations."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def __call__(self, observation: OpenFootballRawObservation) -> dict[str, Any]:
        return self.store(observation)

    def store(self, observation: OpenFootballRawObservation) -> dict[str, Any]:
        record = _validated_observation(observation)
        raw_parent = self.root / Path(record["raw_path"]).parent
        _prepare_archive_directories(self.root, raw_parent)
        manifest_path = self.root / MANIFEST_NAME
        lock_path = self.root / f"{MANIFEST_NAME}.lock"
        lock_flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            lock_descriptor = os.open(lock_path, lock_flags, 0o600)
        except OSError as exc:
            raise OpenFootballRawArchiveError("cannot open OpenFootball archive lock") from exc
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            records = _verified_manifest_records(manifest_path)
            for previous in records:
                same_capture = (
                    previous.get("source_id") == record["source_id"]
                    and previous.get("retrieved_at") == record["retrieved_at"]
                )
                if same_capture and previous.get("raw_sha256") != record["raw_sha256"]:
                    raise OpenFootballRawArchiveError(
                        "OpenFootball archive has conflicting bytes for the same source and retrieved_at"
                    )
                if previous.get("observation_id") == record["observation_id"]:
                    _verify_raw_bytes(self.root, previous, fsync_after_read=True)
                    manifest_sha256 = _sha256_bytes(
                        _read_manifest_bytes(manifest_path, fsync_after_read=True)
                    )
                    return _receipt(previous, manifest_sha256=manifest_sha256, duplicate=True)

            raw_path = self.root / record["raw_path"]
            _validate_archive_member_path(self.root, raw_path)
            raw_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                raw_descriptor = os.open(raw_path, raw_flags, 0o600)
            except FileExistsError:
                existing = _read_regular_file(
                    raw_path,
                    max_bytes=MAX_RAW_BYTES,
                    label="raw object",
                    fsync_after_read=True,
                )
                if (
                    _sha256_bytes(existing) != record["raw_sha256"]
                    or len(existing) != record["size_bytes"]
                ):
                    raise OpenFootballRawArchiveError(
                        "existing OpenFootball raw object does not match its content address"
                    )
            except OSError as exc:
                raise OpenFootballRawArchiveError("cannot create OpenFootball raw object") from exc
            else:
                try:
                    _write_all(raw_descriptor, observation.payload)
                    os.fsync(raw_descriptor)
                finally:
                    os.close(raw_descriptor)
                _fsync_directory(raw_parent)

            manifest_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                manifest_descriptor = os.open(manifest_path, manifest_flags, 0o600)
            except OSError as exc:
                raise OpenFootballRawArchiveError(
                    "cannot open OpenFootball archive manifest"
                ) from exc
            try:
                _write_all(manifest_descriptor, _canonical_bytes(record) + b"\n")
                os.fsync(manifest_descriptor)
            finally:
                os.close(manifest_descriptor)
            _fsync_directory(self.root)
            manifest_sha256 = _sha256_bytes(_read_manifest_bytes(manifest_path))
            return _receipt(record, manifest_sha256=manifest_sha256, duplicate=False)
        finally:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)


__all__ = [
    "MANIFEST_NAME",
    "OpenFootballRawArchive",
    "OpenFootballRawArchiveError",
    "OpenFootballRawObservation",
    "load_verified_openfootball_archive",
    "verify_openfootball_raw_receipt",
    "verify_openfootball_snapshot_admission",
    "validate_openfootball_raw_receipt",
]
