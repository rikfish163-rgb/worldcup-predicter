"""Publish a raw-backed fixture read model to Sites without model maturity.

This lane contains only OpenFootball CC0 schedule/result facts replayed from
the durable raw archive.  Its wire schema intentionally has no prediction,
market, feature, or recommendation fields.  A successful upload therefore
enables the research workbench without weakening the independent model-release
gate used by ``publish_cycle``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from league_platform.fixture_feed import fixture_rows
from league_platform.openfootball_raw_archive import (
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    verify_openfootball_snapshot_admission,
)
from league_platform.publish_raw_artifacts import (
    DEFAULT_TIMEOUT_SECONDS,
    INGEST_TOKEN_ENV,
    PRODUCER_KEY_ID_ENV,
    PRODUCER_SIGNING_SECRET_ENV,
    _normalized_endpoint,
    _require_durable_archive,
    _send_raw_upload,
    canonical_json_bytes,
    raw_artifact_attestation_headers,
)


RESEARCH_FIXTURE_SCHEMA = "matchline.research_fixture_snapshot.v1"
RESEARCH_FIXTURE_ENDPOINT_ENV = "MATCHLINE_RESEARCH_FIXTURE_ENDPOINT"
MAX_RESEARCH_FIXTURE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
_SHA256_CHARS = set("0123456789abcdef")

ResearchSender = Callable[..., Mapping[str, Any]]


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and set(value) <= _SHA256_CHARS
        and value != "0" * 64
    )


def _iso(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _source_iso(value: object, *, field: str) -> str:
    """Validate but preserve the archive observation timestamp byte-for-byte."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _text(value: object, *, field: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return value.strip()


def _optional_integer(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def _score(value: object, *, field: str) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"home", "away"}:
        raise ValueError(f"{field} must be a score object or null")
    home = _optional_integer(value.get("home"), field=f"{field}.home")
    away = _optional_integer(value.get("away"), field=f"{field}.away")
    if home is None or away is None or home > 99 or away > 99:
        raise ValueError(f"{field} is out of range")
    return {"home": home, "away": away}


def _source(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or value.get("name") != "OpenFootball":
        raise ValueError("every research fixture must use OpenFootball provenance")
    source_id = _text(value.get("source_id"), field="source.source_id", maximum=200)
    url = _text(value.get("url"), field="source.url", maximum=2048)
    retrieved_at = _source_iso(
        value.get("retrieved_at"), field="source.retrieved_at"
    )
    raw_sha256 = value.get("raw_sha256")
    if not _valid_sha256(raw_sha256):
        raise ValueError("source.raw_sha256 is invalid")
    if value.get("license") != "CC0-1.0":
        raise ValueError("OpenFootball research fixture license must be CC0-1.0")
    return {
        "name": "OpenFootball",
        "sourceId": source_id,
        "url": url,
        "retrievedAt": retrieved_at,
        "rawSha256": str(raw_sha256),
        "license": "CC0-1.0",
    }


def _lineage(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("research fixture lineage is required")
    if not isinstance(value.get("date_inherited"), bool) or not isinstance(
        value.get("time_inherited"), bool
    ):
        raise ValueError("research fixture inherited flags must be booleans")
    return {
        "parser": _text(value.get("parser"), field="lineage.parser", maximum=120),
        "recordIndex": _optional_integer(
            value.get("record_index"), field="lineage.record_index"
        ),
        "lineNumber": _optional_integer(
            value.get("line_number"), field="lineage.line_number"
        ),
        "recordPath": _text(
            value.get("record_path"), field="lineage.record_path", maximum=512
        ),
        "sourceRowId": _text(
            value.get("source_row_id"), field="lineage.source_row_id", maximum=1024
        ),
        "dateInherited": value["date_inherited"],
        "timeInherited": value["time_inherited"],
    }


def _fixture(value: Mapping[str, Any]) -> dict[str, object]:
    status = _text(value.get("status"), field="fixture.status", maximum=32)
    if status not in {"scheduled", "upcoming", "live", "finished", "postponed", "cancelled"}:
        raise ValueError("fixture.status is invalid")
    score = _score(value.get("score"), field="fixture.score")
    halftime_score = _score(value.get("halftime_score"), field="fixture.halftime_score")
    if status == "finished" and score is None:
        raise ValueError("finished research fixture requires a score")
    if status != "finished" and (score is not None or halftime_score is not None):
        raise ValueError("pre-match research fixture cannot contain a score")
    return {
        "id": _text(value.get("id"), field="fixture.id", maximum=200),
        "competitionId": _text(
            value.get("competition_id"), field="fixture.competition_id", maximum=80
        ),
        "season": _text(value.get("season"), field="fixture.season", maximum=40),
        "round": (
            None
            if value.get("round") is None
            else _text(value.get("round"), field="fixture.round", maximum=120)
        ),
        "kickoffAt": _iso(value.get("kickoff_at"), field="fixture.kickoff_at"),
        "kickoffTimeQuality": _text(
            value.get("kickoff_time_quality"),
            field="fixture.kickoff_time_quality",
            maximum=40,
        ),
        "kickoffTimeSource": _text(
            value.get("kickoff_time_source"),
            field="fixture.kickoff_time_source",
            maximum=40,
        ),
        "kickoffTimezone": _text(
            value.get("kickoff_timezone"),
            field="fixture.kickoff_timezone",
            maximum=80,
        ),
        "homeTeam": _text(value.get("home_team"), field="fixture.home_team", maximum=160),
        "awayTeam": _text(value.get("away_team"), field="fixture.away_team", maximum=160),
        "status": status,
        "score": score,
        "halftimeScore": halftime_score,
        "resultScope": (
            None
            if value.get("result_scope") is None
            else _text(
                value.get("result_scope"), field="fixture.result_scope", maximum=60
            )
        ),
        "source": _source(value.get("source")),
        "lineage": _lineage(value.get("lineage")),
    }


def build_research_fixture_snapshot(
    snapshot: Mapping[str, Any],
    *,
    archive_manifest_sha256: str,
    verified_admission: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the closed fact-only payload from an already verified replay."""

    if not _valid_sha256(archive_manifest_sha256):
        raise ValueError("archive manifest SHA-256 is invalid")
    if verified_admission.get("status") != "verified_current_raw":
        raise ValueError("verified current raw admission is required")
    for field in ("admission_sha256", "rows_sha256"):
        if not _valid_sha256(verified_admission.get(field)):
            raise ValueError(f"verified admission {field} is invalid")
    source_ids = verified_admission.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids or any(
        not isinstance(source_id, str) or not source_id for source_id in source_ids
    ):
        raise ValueError("verified admission source_ids are invalid")
    rows = fixture_rows(snapshot)
    if not rows:
        raise ValueError("verified snapshot contains no exact fixtures")
    if verified_admission.get("published_fixture_count") != len(rows):
        raise ValueError("verified admission fixture count does not match snapshot")
    fixtures = [_fixture(row) for row in rows]
    fixture_ids = [str(row["id"]) for row in fixtures]
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("research fixture identities must be unique")
    actual_source_ids: set[str] = set()
    for fixture in fixtures:
        source = fixture.get("source")
        if not isinstance(source, Mapping) or not isinstance(
            source.get("sourceId"), str
        ):
            raise ValueError("research fixture source identity is invalid")
        actual_source_ids.add(source["sourceId"])
    if not actual_source_ids <= set(source_ids):
        raise ValueError("research fixture source is outside verified admission")
    payload = {
        "schemaVersion": RESEARCH_FIXTURE_SCHEMA,
        "asOf": _iso(snapshot.get("as_of"), field="snapshot.as_of"),
        "archiveManifestSha256": archive_manifest_sha256,
        "rawAdmissionSha256": str(verified_admission["admission_sha256"]),
        "rawRowsSha256": str(verified_admission["rows_sha256"]),
        "sourcePolicyIds": ["openfootball_current"],
        "fixtureCount": len(fixtures),
        "fixtures": fixtures,
    }
    if len(canonical_json_bytes(payload)) > MAX_RESEARCH_FIXTURE_BYTES:
        raise ValueError("research fixture snapshot exceeds 2 MiB")
    return payload


def _read_manifest(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("cannot open OpenFootball raw manifest") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_MANIFEST_BYTES:
            raise ValueError("OpenFootball raw manifest is not a bounded regular file")
        value = os.read(descriptor, MAX_MANIFEST_BYTES + 1)
        after = os.fstat(descriptor)
        if len(value) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("OpenFootball raw manifest changed while being read")
        return value
    finally:
        os.close(descriptor)


def build_verified_research_fixture_snapshot(
    snapshot_path: Path | str,
    archive_root: Path | str,
) -> dict[str, Any]:
    snapshot_file = Path(snapshot_path)
    try:
        snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("current snapshot is not readable JSON") from exc
    if not isinstance(snapshot, Mapping):
        raise ValueError("current snapshot root must be an object")
    durable_root = _require_durable_archive(archive_root)
    manifest_path = durable_root / MANIFEST_NAME
    before = _read_manifest(manifest_path)
    admission = verify_openfootball_snapshot_admission(
        snapshot,
        archive_root=durable_root,
    )
    after = _read_manifest(manifest_path)
    if before != after:
        raise ValueError("OpenFootball raw manifest changed during snapshot replay")
    return build_research_fixture_snapshot(
        snapshot,
        archive_manifest_sha256=hashlib.sha256(before).hexdigest(),
        verified_admission=admission,
    )


def research_fixture_attestation_headers(
    body: bytes,
    *,
    endpoint: str,
    key_id: str,
    signing_secret: str,
    archive_manifest_sha256: str,
    issued_at: datetime | None = None,
) -> dict[str, str]:
    return raw_artifact_attestation_headers(
        body,
        endpoint=endpoint,
        key_id=key_id,
        signing_secret=signing_secret,
        source_policy_ids=("openfootball_current",),
        archive_manifest_sha256=archive_manifest_sha256,
        stream="research_fixture_snapshot",
        issued_at=issued_at,
    )


def validate_research_fixture_ack(
    payload: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    expected_keys = {
        "status",
        "created",
        "snapshotId",
        "snapshotSha256",
        "fixtureCount",
        "objectKey",
        "archiveManifestSha256",
    }
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    valid = (
        set(response) == expected_keys
        and response.get("status") == "ok"
        and response.get("created") in {0, 1}
        and isinstance(response.get("snapshotId"), int)
        and not isinstance(response.get("snapshotId"), bool)
        and int(response["snapshotId"]) > 0
        and response.get("snapshotSha256") == digest
        and response.get("fixtureCount") == payload.get("fixtureCount")
        and response.get("objectKey")
        == f"research-fixtures/sha256/{digest[:2]}/{digest}.json"
        and response.get("archiveManifestSha256")
        == payload.get("archiveManifestSha256")
    )
    if not valid:
        raise ValueError("research fixture response contract is invalid")
    return dict(response)


def publish_research_fixture_snapshot(
    snapshot_path: Path | str,
    archive_root: Path | str,
    *,
    endpoint: str,
    token: str,
    key_id: str,
    signing_secret: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    sender: ResearchSender | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload = build_verified_research_fixture_snapshot(snapshot_path, archive_root)
    body = canonical_json_bytes(payload)
    summary = {
        "status": "dry_run" if dry_run else "ok",
        "requests": 0,
        "fixtures": payload["fixtureCount"],
        "asOf": payload["asOf"],
        "archiveManifestSha256": payload["archiveManifestSha256"],
        "snapshotSha256": hashlib.sha256(body).hexdigest(),
    }
    if dry_run:
        return summary
    if not token:
        raise ValueError("research fixture ingest token is required")
    normalized_endpoint = _normalized_endpoint(endpoint)
    headers = research_fixture_attestation_headers(
        body,
        endpoint=normalized_endpoint,
        key_id=key_id,
        signing_secret=signing_secret,
        archive_manifest_sha256=str(payload["archiveManifestSha256"]),
    )
    send = sender or _send_raw_upload
    response = send(
        endpoint=normalized_endpoint,
        body=body,
        headers=headers,
        token=token,
        timeout=timeout,
    )
    ack = validate_research_fixture_ack(payload, response)
    return {**summary, **ack, "requests": 1}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--endpoint", default=os.environ.get(RESEARCH_FIXTURE_ENDPOINT_ENV))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    endpoint = (args.endpoint or "").strip()
    if not endpoint:
        raise ValueError(
            f"--endpoint or {RESEARCH_FIXTURE_ENDPOINT_ENV} is required"
        )
    result = publish_research_fixture_snapshot(
        args.snapshot,
        args.archive_root,
        endpoint=endpoint,
        token=os.environ.get(INGEST_TOKEN_ENV, ""),
        key_id=os.environ.get(PRODUCER_KEY_ID_ENV, ""),
        signing_secret=os.environ.get(PRODUCER_SIGNING_SECRET_ENV, ""),
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RESEARCH_FIXTURE_SCHEMA",
    "build_research_fixture_snapshot",
    "build_verified_research_fixture_snapshot",
    "main",
    "publish_research_fixture_snapshot",
    "research_fixture_attestation_headers",
    "validate_research_fixture_ack",
]
