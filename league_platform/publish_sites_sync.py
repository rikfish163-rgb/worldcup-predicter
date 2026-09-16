"""Receipt-backed orchestration for Python-to-Sites evidence uploads.

The coordinator keeps research audit evidence and token-only private system
evidence independent from the unavailable formal publication lane.  A batch is
fully preflighted before the first request, each strict acknowledgement is
checkpointed, and a completed receipt is written only after every planned item
has been acknowledged.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_CURRENT_SOURCE_IDS,
    OPENFOOTBALL_SOURCES,
)
from league_platform.platform_verification import strict_report_v260_failures
from league_platform.publish_prospective_audit import (
    PROSPECTIVE_AUDIT_ENDPOINT_ENV,
    PROSPECTIVE_AUDIT_SCHEMA,
    build_prospective_audit_snapshot,
    prospective_audit_attestation_headers,
    validate_prospective_audit_ack,
)
from league_platform.publish_raw_artifacts import (
    DEFAULT_TIMEOUT_SECONDS,
    INGEST_TOKEN_ENV,
    PRODUCER_KEY_ID_ENV,
    PRODUCER_SIGNING_SECRET_ENV,
    RAW_ENDPOINT_ENV,
    RAW_UPLOAD_SCHEMA,
    _normalized_endpoint,
    _send_raw_upload,
    build_raw_artifact_uploads,
    canonical_json_bytes,
    raw_artifact_attestation_headers,
    validate_raw_artifact_ack,
)
from league_platform.publish_research_fixtures import (
    RESEARCH_FIXTURE_SCHEMA,
    RESEARCH_FIXTURE_ENDPOINT_ENV,
    build_verified_research_fixture_snapshot,
    research_fixture_attestation_headers,
    validate_research_fixture_ack,
)
from league_platform.publish_structured_artifacts import (
    build_private_system_evidence,
    build_research_artifact_summaries,
    build_structured_artifact_upload,
    structured_artifact_attestation_headers,
    validate_structured_artifact_ack,
)


BATCH_MANIFEST_SCHEMA = "matchline.sites_sync_batch_manifest.v1"
CURSOR_SCHEMA = "matchline.sites_sync_cursor.v1"
RECEIPT_SCHEMA = "matchline.sites_sync_receipt.v1"
SOURCE_POLICY_IDS = ("openfootball_current",)
FORMAL_BLOCKED_STATUS = "blocked_independent_verifier_unavailable"
FACTS_ONLY_LANE: Literal["research_facts_only"] = "research_facts_only"
FACTS_ONLY_BOUNDARY = "raw_backed_research_facts_only"
STRUCTURED_ARTIFACT_ENDPOINT_ENV = "MATCHLINE_ARTIFACT_ENDPOINT"
SITES_SYNC_CURSOR_ENV = "MATCHLINE_SITES_SYNC_CURSOR"
SITES_SYNC_RECEIPT_ENV = "MATCHLINE_SITES_SYNC_RECEIPT"
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_LOCAL_JSON_BYTES = 32 * 1024 * 1024

Lane = Literal[
    "research_audit",
    "private_evidence",
    "formal",
    "research_facts_only",
]
Mode = Literal["full", "incremental"]
Stream = Literal[
    "raw_artifact",
    "research_fixture",
    "prospective_audit",
    "structured_artifact",
]
SyncSender = Callable[..., Mapping[str, Any]]

_SHA256_CHARS = frozenset("0123456789abcdef")
_CURRENT_SOURCE_IDS = frozenset(OPENFOOTBALL_CURRENT_SOURCE_IDS)
_AUTHORIZED_SOURCE_IDS = frozenset(OPENFOOTBALL_SOURCES)
_FACTS_ONLY_FORBIDDEN_KEY_PARTS = (
    "prediction",
    "probability",
    "market",
    "xg",
    "feature",
    "factortrace",
    "strict",
    "formal",
    "prospective",
    "private",
    "evaluation",
    "model",
    "lock",
)


@dataclass(frozen=True)
class PreparedSitesSyncInputs:
    """Already locally verified inputs for one Sites sync preflight."""

    archive_manifest_sha256: str
    admission_sha256: str
    source_ids: tuple[str, ...]
    raw_uploads: tuple[dict[str, Any], ...] = ()
    research_fixture: dict[str, Any] | None = None
    prospective_audit: dict[str, Any] | None = None
    structured_contents: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SyncItem:
    stream: Stream
    item_key: str
    endpoint: str
    payload: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True)
class SitesSyncPlan:
    status: str
    lane: Lane
    mode: Mode
    batch_manifest: dict[str, Any]
    batch_manifest_sha256: str
    inventory: dict[str, Any]
    items: tuple[SyncItem, ...]
    # Incremental plans send only the delta, but a completed receipt covers
    # the complete desired inventory.  Carry prior ACKs into the next receipt
    # so the receipt cannot claim success for unacknowledged old objects.
    baseline_completed_items: tuple[tuple[str, dict[str, str]], ...] = ()
    # A legacy incremental receipt may contain several ACKs for contiguous
    # segments of one raw object.  When those ACKs are proven to cover the
    # current canonical upload, preflight marks the plan for a local-only
    # receipt/cursor rewrite.  No network request is needed for that rewrite.
    normalized_completed_items: tuple[tuple[str, dict[str, str]], ...] = ()
    normalized_completed_at: str | None = None
    # Incremental execution may finish a batch whose baseline receipt carries
    # legacy raw segments.  Keep the complete, locally validated inputs on the
    # plan so the post-ACK checkpoint can collapse those segments to the
    # canonical full-inventory identities before writing the receipt.
    normalization_expected_payload_digests: tuple[tuple[str, str], ...] = ()
    normalization_raw_uploads: tuple[dict[str, Any], ...] = ()
    normalization_historical_manifest_sha: str | None = None


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value != "0" * 64
        and set(value) <= _SHA256_CHARS
    )


def _digest(value: object, *, field: str) -> str:
    if not _is_digest(value):
        raise ValueError(f"{field} must be a non-zero lowercase SHA-256")
    return str(value)


def _content_digest(value: object) -> str:
    return _sha256(canonical_json_bytes(value))


def _canonical_lane(lane: Lane | str, *, facts_only: bool = False) -> Lane:
    """Normalize the explicit facts-only spelling before planning or receipt IO."""

    if not isinstance(facts_only, bool):
        raise ValueError("Sites sync facts_only flag must be boolean")
    value = lane.strip() if isinstance(lane, str) else str(lane)
    if facts_only:
        if value not in {"research_audit", FACTS_ONLY_LANE, "facts_only"}:
            raise ValueError("--facts-only is only valid for a research lane")
        return FACTS_ONLY_LANE
    if value in {FACTS_ONLY_LANE, "facts_only"}:
        return FACTS_ONLY_LANE
    if value == "research_audit":
        return "research_audit"
    if value == "private_evidence":
        return "private_evidence"
    if value == "formal":
        return "formal"
    raise ValueError("Sites sync lane is invalid")


def _assert_facts_only_shape(value: object, *, path: str = "payload", depth: int = 0) -> None:
    """Reject model/publication fields before a facts-only request is built.

    The raw and research publishers already emit closed schemas.  This guard
    protects the lower-level preflight API too, where callers can construct a
    ``PreparedSitesSyncInputs`` object directly and accidentally smuggle a
    model artifact through an otherwise valid facts-only lane.
    """

    if depth > 64:
        raise ValueError("facts-only payload is too deeply nested")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"facts-only payload has an invalid key at {path}")
            normalized = "".join(character for character in key.casefold() if character.isalnum())
            if any(part in normalized for part in _FACTS_ONLY_FORBIDDEN_KEY_PARTS):
                raise ValueError(f"facts-only payload contains forbidden field {path}.{key}")
            _assert_facts_only_shape(child, path=f"{path}.{key}", depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_facts_only_shape(child, path=f"{path}[{index}]", depth=depth + 1)


def _assert_facts_only_raw_bytes(uploads: Sequence[Mapping[str, Any]]) -> None:
    """Inspect JSON raw objects when possible, without rewriting immutable bytes."""

    for upload in uploads:
        raw = upload.get("raw")
        if not isinstance(raw, Mapping):
            continue
        encoded = raw.get("base64")
        if not isinstance(encoded, str):
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        try:
            parsed = json.loads(decoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        _assert_facts_only_shape(parsed, path="raw")


def _normalize_source_ids(values: tuple[str, ...]) -> list[str]:
    normalized = sorted(set(values))
    if (
        not normalized
        or len(normalized) > 64
        or normalized != list(values)
        or any(value not in _CURRENT_SOURCE_IDS for value in normalized)
    ):
        raise ValueError("sync source_ids must be sorted unique current OpenFootball IDs")
    return normalized


def _validate_raw_uploads(
    uploads: tuple[dict[str, Any], ...],
    *,
    archive_manifest_sha256: str,
) -> tuple[dict[str, str], list[str], list[str]]:
    observations: dict[str, str] = {}
    raw_objects: set[str] = set()
    for upload in uploads:
        if (
            not isinstance(upload, dict)
            or upload.get("schemaVersion") != RAW_UPLOAD_SCHEMA
            or upload.get("archiveManifestSha256") != archive_manifest_sha256
            or set(upload)
            != {
                "schemaVersion",
                "archiveManifestSha256",
                "raw",
                "observations",
            }
        ):
            raise ValueError("raw upload is not bound to the sync archive manifest")
        raw = upload.get("raw")
        rows = upload.get("observations")
        if not isinstance(raw, Mapping) or set(raw) != {
            "sha256",
            "sizeBytes",
            "contentType",
            "base64",
        }:
            raise ValueError("raw upload object contract is invalid")
        raw_sha = _digest(raw.get("sha256"), field="raw.sha256")
        size = raw.get("sizeBytes")
        encoded = raw.get("base64")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(encoded, str)
        ):
            raise ValueError("raw upload object contract is invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("raw upload base64 is invalid") from exc
        if len(content) != size or _sha256(content) != raw_sha:
            raise ValueError("raw upload bytes do not match their content identity")
        if not isinstance(rows, list) or not rows:
            raise ValueError("raw upload must contain at least one observation")
        raw_objects.add(raw_sha)
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {
                "archiveRecord",
                "manifestPrefixSha256",
            }:
                raise ValueError("raw observation upload contract is invalid")
            _digest(row.get("manifestPrefixSha256"), field="manifestPrefixSha256")
            record = row.get("archiveRecord")
            if not isinstance(record, Mapping):
                raise ValueError("raw observation archive record is invalid")
            observation_id = _digest(record.get("observation_id"), field="observation_id")
            source_id = record.get("source_id")
            if (
                not isinstance(source_id, str)
                or not source_id.startswith("openfootball:")
                or source_id not in _AUTHORIZED_SOURCE_IDS
                or record.get("raw_sha256") != raw_sha
            ):
                raise ValueError("raw observation is outside the verified sync inventory")
            row_digest = _content_digest(row)
            if observation_id in observations:
                raise ValueError("raw observation identities must be unique")
            observations[observation_id] = row_digest
    return observations, sorted(raw_objects), sorted(observations)


def _validate_research_fixture(
    payload: Mapping[str, Any],
    *,
    archive_manifest_sha256: str,
    admission_sha256: str,
    source_ids: list[str],
) -> None:
    if (
        payload.get("schemaVersion") != RESEARCH_FIXTURE_SCHEMA
        or payload.get("archiveManifestSha256") != archive_manifest_sha256
        or payload.get("rawAdmissionSha256") != admission_sha256
        or payload.get("sourcePolicyIds") != list(SOURCE_POLICY_IDS)
    ):
        raise ValueError("research fixture is outside the sync admission gate")
    fixtures = payload.get("fixtures")
    fixture_count = payload.get("fixtureCount")
    if (
        not isinstance(fixtures, list)
        or isinstance(fixture_count, bool)
        or not isinstance(fixture_count, int)
        or fixture_count != len(fixtures)
    ):
        raise ValueError("research fixture count is invalid")
    for fixture in fixtures:
        source = fixture.get("source") if isinstance(fixture, Mapping) else None
        if not isinstance(source, Mapping) or source.get("sourceId") not in source_ids:
            raise ValueError("research fixture source is outside the sync inventory")


def _validate_prospective_audit(payload: Mapping[str, Any]) -> None:
    if payload.get("schemaVersion") != PROSPECTIVE_AUDIT_SCHEMA or payload.get(
        "sourcePolicyIds"
    ) != list(SOURCE_POLICY_IDS):
        raise ValueError("prospective audit policy identity is invalid")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("prospective audit artifact identities are missing")
    for field in (
        "modelLockRawSha256",
        "cycleRawSha256",
        "evaluationRawSha256",
        "liveSnapshotRawSha256",
        "predictionArchiveRawSha256",
        "admissionSha256",
        "rowsSha256",
        "sourceManifestSha256",
    ):
        _digest(artifacts.get(field), field=f"prospective_audit.artifacts.{field}")
    for field in ("model", "cycle", "capture", "evaluation"):
        if not isinstance(payload.get(field), Mapping):
            raise ValueError(f"prospective audit {field} contract is invalid")


def _structured_inventory(
    contents: tuple[dict[str, Any], ...],
    *,
    lane: Lane,
) -> tuple[dict[str, str], list[tuple[str, dict[str, Any]]]]:
    result: dict[str, str] = {}
    ordered: list[tuple[str, dict[str, Any]]] = []
    for content in contents:
        if not isinstance(content, dict):
            raise ValueError("structured artifact content must be an object")
        artifact_kind = content.get("artifactKind")
        if not isinstance(artifact_kind, str) or not artifact_kind:
            raise ValueError("structured artifact kind is required")
        identity = f"structured:{lane}:{artifact_kind}"
        if identity in result:
            raise ValueError("structured artifact kinds must be unique per batch")
        result[identity] = _content_digest(content)
        ordered.append((artifact_kind, content))
    ordered.sort(key=lambda item: item[0])
    return result, ordered


def _required_endpoints(
    inputs: PreparedSitesSyncInputs,
    *,
    lane: Lane,
    endpoints: Mapping[str, str],
) -> dict[str, str]:
    streams: list[str]
    if lane == "research_audit":
        streams = [
            "raw_artifact",
            "research_fixture",
            "prospective_audit",
            "structured_artifact",
        ]
    elif lane == FACTS_ONLY_LANE:
        streams = ["raw_artifact", "research_fixture"]
    elif lane == "private_evidence":
        streams = ["structured_artifact"]
    else:  # pragma: no cover - formal returns before endpoint preflight
        streams = []
    normalized: dict[str, str] = {}
    for stream in streams:
        raw = endpoints.get(stream)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"Sites endpoint for {stream} is required")
        normalized[stream] = _normalized_endpoint(raw.strip())
    return normalized


def _inventory(
    *,
    raw_observation_digests: Mapping[str, str],
    raw_objects: list[str],
    content_digests: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "rawObservationIds": sorted(raw_observation_digests),
        "rawObservationDigests": dict(sorted(raw_observation_digests.items())),
        "rawObjectSha256s": sorted(raw_objects),
        "contentDigests": dict(sorted(content_digests.items())),
    }


def _blocked_formal_plan(*, mode: Mode) -> SitesSyncPlan:
    manifest = {
        "schemaVersion": BATCH_MANIFEST_SCHEMA,
        "lane": "formal",
        "status": FORMAL_BLOCKED_STATUS,
        "reason": "publication_snapshot_independent_verifier_unavailable",
    }
    digest = _content_digest(manifest)
    return SitesSyncPlan(
        status=FORMAL_BLOCKED_STATUS,
        lane="formal",
        mode=mode,
        batch_manifest=manifest,
        batch_manifest_sha256=digest,
        inventory=_inventory(raw_observation_digests={}, raw_objects=[], content_digests={}),
        items=(),
    )


def _read_local_json(
    path: Path | str,
    *,
    label: str,
    maximum: int = MAX_LOCAL_JSON_BYTES,
) -> tuple[dict[str, Any], str]:
    candidate = Path(path)
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise ValueError(f"cannot open Sites sync {label}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            raise ValueError(f"Sites sync {label} is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if total != before.st_size or (
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
            raise ValueError(f"Sites sync {label} changed while being read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Sites sync {label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Sites sync {label} root must be an object")
    return value, _sha256(raw)


def _required_path(value: Path | str | None, *, label: str) -> Path:
    if value is None or not str(value).strip():
        raise ValueError(f"Sites sync {label} path is required")
    return Path(value)


def _research_fixture_source_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("verified research fixture contains no source inventory")
    source_ids: set[str] = set()
    for fixture in fixtures:
        source = fixture.get("source") if isinstance(fixture, Mapping) else None
        source_id = source.get("sourceId") if isinstance(source, Mapping) else None
        if not isinstance(source_id, str) or source_id not in _CURRENT_SOURCE_IDS:
            raise ValueError("verified research fixture source inventory is invalid")
        source_ids.add(source_id)
    return tuple(sorted(source_ids))


def prepare_sites_sync_inputs(
    *,
    lane: Lane,
    archive_root: Path | str | None = None,
    snapshot_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    cycle_path: Path | str | None = None,
    evaluation_path: Path | str | None = None,
    live_snapshot_path: Path | str | None = None,
    prediction_archive_path: Path | str | None = None,
    strict_report_path: Path | str | None = None,
    offline_snapshot_path: Path | str | None = None,
    facts_only: bool = False,
) -> PreparedSitesSyncInputs | None:
    """Compose existing verified publishers into one all-local preflight."""

    lane = _canonical_lane(lane, facts_only=facts_only)
    if lane == "formal":
        return None

    archive = _required_path(archive_root, label="raw archive")
    snapshot = _required_path(snapshot_path, label="research fixture snapshot")

    if lane == FACTS_ONLY_LANE:
        research_fixture = build_verified_research_fixture_snapshot(snapshot, archive)
        if not isinstance(research_fixture, dict):
            raise ValueError("verified research fixture builder returned an invalid payload")
        archive_sha = _digest(
            research_fixture.get("archiveManifestSha256"),
            field="research_fixture.archiveManifestSha256",
        )
        admission_sha = _digest(
            research_fixture.get("rawAdmissionSha256"),
            field="research_fixture.rawAdmissionSha256",
        )
        source_ids = _research_fixture_source_ids(research_fixture)
        raw_uploads = build_raw_artifact_uploads(archive)
        _assert_facts_only_shape(research_fixture, path="research_fixture")
        _assert_facts_only_shape(raw_uploads, path="raw_uploads")
        _assert_facts_only_raw_bytes(raw_uploads)
        return PreparedSitesSyncInputs(
            archive_manifest_sha256=archive_sha,
            admission_sha256=admission_sha,
            source_ids=source_ids,
            raw_uploads=tuple(raw_uploads),
            research_fixture=research_fixture,
        )

    lock = _required_path(lock_path, label="prospective lock")
    cycle = _required_path(cycle_path, label="prospective cycle")
    evaluation = _required_path(
        evaluation_path,
        label="prospective evaluation",
    )
    live_snapshot = _required_path(
        live_snapshot_path,
        label="live snapshot",
    )
    prediction_archive = _required_path(
        prediction_archive_path,
        label="local prediction archive",
    )
    strict_path = _required_path(strict_report_path, label="strict report")

    research_fixture = build_verified_research_fixture_snapshot(snapshot, archive)
    if not isinstance(research_fixture, dict):
        raise ValueError("verified research fixture builder returned an invalid payload")
    archive_sha = _digest(
        research_fixture.get("archiveManifestSha256"),
        field="research_fixture.archiveManifestSha256",
    )
    admission_sha = _digest(
        research_fixture.get("rawAdmissionSha256"),
        field="research_fixture.rawAdmissionSha256",
    )
    source_ids = _research_fixture_source_ids(research_fixture)

    prospective_audit = build_prospective_audit_snapshot(
        lock_path=lock,
        cycle_path=cycle,
        evaluation_path=evaluation,
        live_snapshot_path=live_snapshot,
        prediction_archive_path=prediction_archive,
    )
    artifacts = prospective_audit.get("artifacts")
    if (
        not isinstance(artifacts, Mapping)
        or artifacts.get("admissionSha256") != admission_sha
        or artifacts.get("rowsSha256") != research_fixture.get("rawRowsSha256")
    ):
        raise ValueError("prospective audit admission identity does not match research")

    strict_report, strict_sha = _read_local_json(
        strict_path,
        label="strict report",
    )
    strict_failures = strict_report_v260_failures(strict_report)
    if strict_failures:
        raise ValueError(f"strict report closed provenance contract failed: {strict_failures[0]}")

    if lane == "private_evidence":
        private = build_private_system_evidence(
            prospective_audit=prospective_audit,
            strict_report=strict_report,
            strict_report_sha256=strict_sha,
        )
        return PreparedSitesSyncInputs(
            archive_manifest_sha256=archive_sha,
            admission_sha256=admission_sha,
            source_ids=source_ids,
            structured_contents=tuple(private),
        )

    offline_path = _required_path(offline_snapshot_path, label="offline snapshot")
    offline_snapshot, offline_sha = _read_local_json(
        offline_path,
        label="offline snapshot",
    )
    if offline_snapshot.get("schema_version") != "matchline.offline_snapshot.v1":
        raise ValueError("offline snapshot closed schema is invalid")
    summaries = build_research_artifact_summaries(
        prospective_audit=prospective_audit,
        strict_report=strict_report,
        strict_report_sha256=strict_sha,
        offline_snapshot=offline_snapshot,
        offline_snapshot_sha256=offline_sha,
    )
    raw_uploads = build_raw_artifact_uploads(archive)
    return PreparedSitesSyncInputs(
        archive_manifest_sha256=archive_sha,
        admission_sha256=admission_sha,
        source_ids=source_ids,
        raw_uploads=tuple(raw_uploads),
        research_fixture=research_fixture,
        prospective_audit=prospective_audit,
        structured_contents=tuple(summaries),
    )


def _read_state(path: Path, *, label: str) -> dict[str, Any] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot open Sites sync {label}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAX_STATE_BYTES
        ):
            raise ValueError(f"Sites sync {label} is not a bounded regular file")
        raw = os.read(descriptor, MAX_STATE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (
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
            raise ValueError(f"Sites sync {label} changed while being read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Sites sync {label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Sites sync {label} root must be an object")
    return value


def _validate_inventory(value: object) -> dict[str, Any]:
    expected = {
        "rawObservationIds",
        "rawObservationDigests",
        "rawObjectSha256s",
        "contentDigests",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("Sites sync receipt inventory is invalid")
    ids = value.get("rawObservationIds")
    observation_digests = value.get("rawObservationDigests")
    raw_objects = value.get("rawObjectSha256s")
    content_digests = value.get("contentDigests")
    observation_keys = (
        list(observation_digests.keys()) if isinstance(observation_digests, Mapping) else []
    )
    if (
        not isinstance(ids, list)
        or ids != sorted(set(ids))
        or not all(_is_digest(item) for item in ids)
        or not isinstance(observation_digests, Mapping)
        or any(not isinstance(item, str) for item in observation_keys)
        or sorted(observation_keys) != ids
        or not all(_is_digest(item) for item in observation_digests.values())
        or not isinstance(raw_objects, list)
        or raw_objects != sorted(set(raw_objects))
        or not all(_is_digest(item) for item in raw_objects)
        or not isinstance(content_digests, Mapping)
        or not all(
            isinstance(key, str) and key and _is_digest(item)
            for key, item in content_digests.items()
        )
        or (ids and not raw_objects)
    ):
        raise ValueError("Sites sync receipt inventory is invalid")
    return {
        "rawObservationIds": list(ids),
        "rawObservationDigests": dict(observation_digests),
        "rawObjectSha256s": list(raw_objects),
        "contentDigests": dict(content_digests),
    }


def _validate_receipt_ack_coverage(
    *,
    lane: Lane,
    inventory: Mapping[str, Any],
    completed_items: Mapping[str, Mapping[str, Any]],
) -> None:
    """Require ACK keys to cover every object and content identity claimed."""

    keys = set(completed_items)
    raw_objects = set(inventory["rawObjectSha256s"])
    raw_seen: set[str] = set()
    content_identities: set[str] = set()
    for key in keys:
        if key.startswith("raw:"):
            parts = key.split(":")
            if (
                lane not in {"research_audit", FACTS_ONLY_LANE}
                or len(parts) != 3
                or not _is_digest(parts[1])
                or not _is_digest(parts[2])
            ):
                raise ValueError("Sites sync completed receipt raw ACK identity is invalid")
            raw_seen.add(parts[1])
            continue
        if key.startswith("research-fixture:"):
            digest = key.removeprefix("research-fixture:")
            if not _is_digest(digest):
                raise ValueError("Sites sync completed receipt content ACK identity is invalid")
            content_identities.add("research_fixture")
            continue
        if key.startswith("prospective-audit:"):
            digest = key.removeprefix("prospective-audit:")
            if not _is_digest(digest):
                raise ValueError("Sites sync completed receipt content ACK identity is invalid")
            content_identities.add("prospective_audit")
            continue
        if key.startswith("structured:"):
            parts = key.split(":")
            if (
                lane == FACTS_ONLY_LANE
                or len(parts) != 4
                or not _is_digest(parts[3])
            ):
                raise ValueError("Sites sync completed receipt content ACK identity is invalid")
            identity = ":".join(parts[:3])
            if parts[1] != lane:
                raise ValueError("Sites sync completed receipt content ACK lane is invalid")
            content_identities.add(identity)
            continue
        raise ValueError("Sites sync completed receipt item identity is invalid")

    if raw_seen != raw_objects:
        raise ValueError("Sites sync completed receipt ACK coverage is incomplete")

    expected_content_identities = set(inventory["contentDigests"])
    if not content_identities.issubset(expected_content_identities):
        raise ValueError("Sites sync completed receipt content ACK identity is invalid")
    if content_identities != expected_content_identities:
        raise ValueError("Sites sync completed receipt ACK coverage is incomplete")
    for identity, digest in inventory["contentDigests"].items():
        if identity == "research_fixture":
            expected_key = f"research-fixture:{digest}"
        elif identity == "prospective_audit":
            expected_key = f"prospective-audit:{digest}"
        elif identity.startswith(f"structured:{lane}:"):
            expected_key = f"{identity}:{digest}"
        else:
            raise ValueError("Sites sync receipt content identity is invalid")
        if expected_key not in keys:
            raise ValueError("Sites sync completed receipt ACK coverage is incomplete")
        if (
            identity in {"research_fixture", "prospective_audit"}
            and completed_items[expected_key]["payloadSha256"] != digest
        ):
            raise ValueError("Sites sync completed receipt content digest is invalid")


def _load_completed_receipt(path: Path) -> dict[str, Any] | None:
    value = _read_state(path, label="completed receipt")
    if value is None:
        return None
    expected = {
        "schemaVersion",
        "status",
        "lane",
        "batchManifest",
        "batchManifestSha256",
        "inventory",
        "completedItems",
        "completedAt",
    }
    manifest = value.get("batchManifest")
    receipt_lane = value.get("lane")
    if receipt_lane == FACTS_ONLY_LANE:
        expected.add("factsOnly")
    if (
        set(value) != expected
        or value.get("schemaVersion") != RECEIPT_SCHEMA
        or value.get("status") != "completed"
        or value.get("lane")
        not in {"research_audit", "private_evidence", FACTS_ONLY_LANE}
        or (
            receipt_lane == FACTS_ONLY_LANE
            and value.get("factsOnly") is not True
        )
        or (
            receipt_lane != FACTS_ONLY_LANE
            and "factsOnly" in value
        )
        or not isinstance(manifest, Mapping)
        or not _is_digest(value.get("batchManifestSha256"))
        or _content_digest(manifest) != value.get("batchManifestSha256")
        or not isinstance(value.get("completedAt"), str)
    ):
        raise ValueError("Sites sync completed receipt is invalid")
    inventory = _validate_inventory(value.get("inventory"))
    receipt_lane = value["lane"]
    manifest_keys = {
        "schemaVersion",
        "lane",
        "archiveManifestSha256",
        "admissionSha256",
        "sourceIds",
        "sourcePolicyIds",
        "destinationDigests",
        "inventory",
    }
    if receipt_lane == FACTS_ONLY_LANE:
        manifest_keys.add("factsOnly")
    if (
        set(manifest) != manifest_keys
        or manifest.get("schemaVersion") != BATCH_MANIFEST_SCHEMA
        or manifest.get("lane") != receipt_lane
        or not _is_digest(manifest.get("archiveManifestSha256"))
        or not _is_digest(manifest.get("admissionSha256"))
        or manifest.get("sourcePolicyIds") != list(SOURCE_POLICY_IDS)
        or not isinstance(manifest.get("sourceIds"), list)
        or not all(isinstance(item, str) for item in manifest.get("sourceIds", []))
        or manifest.get("sourceIds") != sorted(set(manifest.get("sourceIds", [])))
        or any(item not in _CURRENT_SOURCE_IDS for item in manifest.get("sourceIds", []))
        or not isinstance(manifest.get("destinationDigests"), Mapping)
        or not all(_is_digest(item) for item in manifest["destinationDigests"].values())
        or _validate_inventory(manifest.get("inventory")) != inventory
        or (
            receipt_lane == FACTS_ONLY_LANE
            and manifest.get("factsOnly") is not True
        )
    ):
        raise ValueError("Sites sync completed receipt manifest is invalid")
    completed_items = value.get("completedItems")
    if not isinstance(completed_items, Mapping):
        raise ValueError("Sites sync completed receipt is invalid")
    for key, item in completed_items.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(item, Mapping)
            or set(item) != {"payloadSha256", "responseSha256"}
            or not _is_digest(item.get("payloadSha256"))
            or not _is_digest(item.get("responseSha256"))
        ):
            raise ValueError("Sites sync completed receipt is invalid")
    _validate_receipt_ack_coverage(
        lane=value["lane"],
        inventory=inventory,
        completed_items=completed_items,
    )
    return value


def _compatible_baseline(
    receipt: Mapping[str, Any] | None,
    *,
    lane: Lane,
    destination_digests: Mapping[str, str],
) -> bool:
    if receipt is None or receipt.get("lane") != lane:
        return False
    manifest = receipt.get("batchManifest")
    return (
        isinstance(manifest, Mapping) and manifest.get("destinationDigests") == destination_digests
    )


def _item(
    *,
    stream: Stream,
    item_key: str,
    endpoint: str,
    payload: dict[str, Any],
) -> SyncItem:
    return SyncItem(
        stream=stream,
        item_key=item_key,
        endpoint=endpoint,
        payload=payload,
        payload_sha256=_content_digest(payload),
    )


def _expected_full_item_payload_digests(
    *,
    inputs: PreparedSitesSyncInputs,
    lane: Lane,
    content_digests: Mapping[str, str],
    structured_uploads: Sequence[tuple[str, str, dict[str, Any]]],
) -> dict[str, str]:
    """Derive request-body digests used to verify an exact completed receipt."""

    expected: dict[str, str] = {}
    if lane in {"research_audit", FACTS_ONLY_LANE}:
        for upload in sorted(
            inputs.raw_uploads,
            key=lambda value: str(value["raw"]["sha256"]),
        ):
            observation_ids = sorted(
                row["archiveRecord"]["observation_id"] for row in upload["observations"]
            )
            raw_sha = str(upload["raw"]["sha256"])
            observation_set_sha = _content_digest(observation_ids)
            expected[f"raw:{raw_sha}:{observation_set_sha}"] = _content_digest(upload)

        assert inputs.research_fixture is not None
        research_digest = content_digests["research_fixture"]
        expected[f"research-fixture:{research_digest}"] = _content_digest(inputs.research_fixture)
        if lane == "research_audit":
            assert inputs.prospective_audit is not None
            audit_digest = content_digests["prospective_audit"]
            expected[f"prospective-audit:{audit_digest}"] = _content_digest(
                inputs.prospective_audit
            )

    for identity, artifact_kind, upload in structured_uploads:
        content_sha = content_digests[identity]
        expected[f"structured:{lane}:{artifact_kind}:{content_sha}"] = _content_digest(upload)
    return expected


def _validate_exact_receipt_payload_digests(
    completed_items: Mapping[str, Mapping[str, Any]],
    expected_payload_digests: Mapping[str, str],
) -> None:
    if set(completed_items) != set(expected_payload_digests):
        raise ValueError("Sites sync completed receipt ACK coverage is incomplete")
    for key, expected_digest in expected_payload_digests.items():
        if completed_items[key]["payloadSha256"] != expected_digest:
            raise ValueError("Sites sync completed receipt content digest is invalid")


def _legacy_raw_ack_ranges(
    upload: Mapping[str, Any],
    completed_items: Mapping[str, Mapping[str, Any]],
    *,
    require_complete: bool = True,
    extra_manifest_shas: Collection[str] = (),
) -> dict[str, tuple[int, int]]:
    """Resolve legacy raw ACK keys to unique contiguous observation ranges.

    Older incremental runs acknowledged only the newly appended observations
    for a raw object.  The current item identity is the complete object, so a
    prior receipt can contain multiple ``raw:<sha>:<observation-set>`` keys.
    Receipt entries retain only digests, not the request body; reconstructing
    each candidate body from the current verified upload lets us retain an ACK
    only when both its set identity and payload digest match exactly.  We
    intentionally consider contiguous ranges only: the archive manifest is
    append-only and the publisher preserves manifest order.  Ambiguous or
    non-partitioning ranges fail closed rather than guessing.  During
    incremental preflight ``require_complete=False`` is used to validate a
    carried baseline whose newly appended ranges have not been ACKed yet;
    execution performs the complete partition check after those ACKs arrive.
    """

    raw = upload.get("raw")
    rows = upload.get("observations")
    if not isinstance(raw, Mapping) or not isinstance(rows, list) or not rows:
        raise ValueError("Sites sync raw ACK normalization input is invalid")
    raw_sha = raw.get("sha256")
    if not _is_digest(raw_sha):
        raise ValueError("Sites sync raw ACK normalization identity is invalid")
    observation_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Sites sync raw ACK normalization observation is invalid")
        record = row.get("archiveRecord")
        observation_id = record.get("observation_id") if isinstance(record, Mapping) else None
        if not _is_digest(observation_id):
            raise ValueError("Sites sync raw ACK normalization observation identity is invalid")
        observation_ids.append(str(observation_id))

    grouped = {
        key: value
        for key, value in completed_items.items()
        if key.startswith(f"raw:{raw_sha}:")
    }
    if not grouped:
        raise ValueError("Sites sync completed receipt ACK coverage is incomplete")

    # A legacy delta payload carries the archive manifest hash that was current
    # when that segment was acknowledged.  The receipt intentionally stores
    # only payload/response digests, but every verified row also carries the
    # immutable manifest-prefix digest that existed at that point.  Trying
    # those prefix hashes lets us reconstruct a prior segment body after the
    # append-only manifest has advanced, while still requiring an exact body
    # digest match (unknown or forged historical hashes remain blocked).
    invalid_extra_manifest = [
        value for value in extra_manifest_shas if not _is_digest(value)
    ]
    if invalid_extra_manifest:
        raise ValueError("Sites sync raw ACK normalization manifest identity is invalid")
    manifest_candidates = {
        str(candidate)
        for candidate in (
            upload.get("archiveManifestSha256"),
            *(row.get("manifestPrefixSha256") for row in rows if isinstance(row, Mapping)),
            *extra_manifest_shas,
        )
        if _is_digest(candidate)
    }
    if not manifest_candidates:
        raise ValueError("Sites sync raw ACK normalization manifest identity is invalid")

    resolved: dict[str, tuple[int, int]] = {}
    for key, ack in sorted(grouped.items()):
        parts = key.split(":")
        if len(parts) != 3 or parts[0] != "raw" or parts[1] != raw_sha:
            raise ValueError("Sites sync raw ACK normalization identity is invalid")
        target_observation_set = parts[2]
        candidates: list[tuple[int, int]] = []
        for start in range(len(rows)):
            for end in range(start + 1, len(rows) + 1):
                subset_ids = sorted(observation_ids[start:end])
                if _content_digest(subset_ids) != target_observation_set:
                    continue
                subset_upload = dict(upload)
                subset_upload["observations"] = rows[start:end]
                for manifest_sha in sorted(manifest_candidates):
                    subset_upload["archiveManifestSha256"] = manifest_sha
                    if _content_digest(subset_upload) == ack["payloadSha256"]:
                        candidates.append((start, end))
        if len(candidates) != 1:
            raise ValueError("Sites sync raw ACK identity cannot be uniquely mapped")
        resolved[key] = candidates[0]

    ordered_ranges = sorted(resolved.values())
    cursor = 0
    for start, end in ordered_ranges:
        if start < cursor or (require_complete and start != cursor):
            raise ValueError("Sites sync raw ACK ranges overlap or leave a gap")
        cursor = max(cursor, end)
    if require_complete and cursor != len(rows):
        raise ValueError("Sites sync raw ACK coverage is incomplete")
    return resolved


def _aggregate_raw_ack_response_digest(
    raw_sha: str,
    completed_items: Mapping[str, Mapping[str, Any]],
    ranges: Mapping[str, tuple[int, int]],
) -> str:
    """Commit deterministically to every validated segment ACK."""

    segments = [
        {
            "itemKey": key,
            "payloadSha256": str(completed_items[key]["payloadSha256"]),
            "responseSha256": str(completed_items[key]["responseSha256"]),
            "range": [start, end],
        }
        for key, (start, end) in sorted(ranges.items(), key=lambda item: item[1])
    ]
    return _content_digest(
        {
            "schemaVersion": "matchline.sites_sync_ack_aggregate.v1",
            "rawSha256": raw_sha,
            "segments": segments,
        }
    )


def _validate_incremental_raw_baseline(
    *,
    raw_uploads: Sequence[Mapping[str, Any]],
    completed_items: Mapping[str, Mapping[str, Any]],
    historical_manifest_sha: str | None = None,
) -> None:
    """Prove carried raw ACKs before an incremental request is sent.

    A baseline may cover only a prefix of the current append-only upload, so
    a complete partition cannot be required until the delta ACKs arrive.
    Every carried segment is nevertheless resolved now; an unknown or
    ambiguous historical payload aborts preflight and therefore causes zero
    sender calls.  New raw objects simply have no baseline group yet.
    """

    current_shas: set[str] = set()
    for upload in raw_uploads:
        raw = upload.get("raw")
        raw_sha = raw.get("sha256") if isinstance(raw, Mapping) else None
        if not _is_digest(raw_sha) or str(raw_sha) in current_shas:
            raise ValueError("Sites sync raw ACK normalization input is invalid")
        current_shas.add(str(raw_sha))

    baseline_shas = {
        key.split(":")[1]
        for key in completed_items
        if key.startswith("raw:") and len(key.split(":")) >= 2
    }
    if not baseline_shas <= current_shas:
        raise ValueError("Sites sync completed receipt ACK coverage is incomplete")

    for upload in raw_uploads:
        raw = upload["raw"]
        raw_sha = str(raw["sha256"])
        group = {
            key: value
            for key, value in completed_items.items()
            if key.startswith(f"raw:{raw_sha}:")
        }
        if not group:
            continue
        rows = upload.get("observations")
        if not isinstance(rows, list) or not rows:
            raise ValueError("Sites sync raw ACK normalization input is invalid")
        observation_ids = sorted(
            row["archiveRecord"]["observation_id"]
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("archiveRecord"), Mapping)
        )
        full_key = f"raw:{raw_sha}:{_content_digest(observation_ids)}"
        full_payload = _content_digest(upload)
        if (
            len(group) == 1
            and full_key in group
            and group[full_key].get("payloadSha256") == full_payload
        ):
            continue
        _legacy_raw_ack_ranges(
            upload,
            completed_items,
            require_complete=False,
            extra_manifest_shas=(historical_manifest_sha,) if historical_manifest_sha else (),
        )


def _normalize_completed_items(
    *,
    completed_items: Mapping[str, Mapping[str, Any]],
    expected_payload_digests: Mapping[str, str],
    raw_uploads: Sequence[Mapping[str, Any]],
    historical_manifest_sha: str | None = None,
    preserve_nonraw_keys: Collection[str] = (),
) -> tuple[dict[str, dict[str, str]], bool]:
    """Collapse proven legacy segment ACKs into current canonical identities.

    The function is deliberately strict: every current raw object must be
    represented by a unique, non-overlapping partition of old ACK ranges, and
    every non-raw expected item must have an exact payload digest.  Extra old
    content identities are discarded only after the complete expected set has
    been proven.  No response is invented from payload data; the aggregate
    response digest commits to the original response digests and ranges.
    """

    expected = dict(expected_payload_digests)
    expected_raw = {
        key: digest for key, digest in expected.items() if key.startswith("raw:")
    }
    uploads_by_sha: dict[str, Mapping[str, Any]] = {}
    for upload in raw_uploads:
        raw = upload.get("raw")
        raw_sha = raw.get("sha256") if isinstance(raw, Mapping) else None
        if not _is_digest(raw_sha) or str(raw_sha) in uploads_by_sha:
            raise ValueError("Sites sync raw ACK normalization input is invalid")
        uploads_by_sha[str(raw_sha)] = upload

    expected_raw_shas = {key.split(":")[1] for key in expected_raw}
    if expected_raw_shas != set(uploads_by_sha):
        raise ValueError("Sites sync raw ACK normalization inventory is inconsistent")

    old_raw_keys = {key for key in completed_items if key.startswith("raw:")}
    old_raw_shas = {key.split(":")[1] for key in old_raw_keys}
    if old_raw_shas != expected_raw_shas:
        raise ValueError("Sites sync completed receipt ACK coverage is incomplete")

    normalized: dict[str, dict[str, str]] = {}
    for raw_sha, upload in sorted(uploads_by_sha.items()):
        expected_keys = [key for key in expected_raw if key.split(":")[1] == raw_sha]
        if len(expected_keys) != 1:
            raise ValueError("Sites sync raw ACK normalization identity is invalid")
        expected_key = expected_keys[0]
        expected_payload = expected[expected_key]
        if _content_digest(upload) != expected_payload:
            raise ValueError("Sites sync raw ACK normalization payload drifted")
        group = {
            key: value
            for key, value in completed_items.items()
            if key.startswith(f"raw:{raw_sha}:")
        }
        if (
            len(group) == 1
            and expected_key in group
            and group[expected_key]["payloadSha256"] == expected_payload
        ):
            normalized[expected_key] = {
                "payloadSha256": expected_payload,
                "responseSha256": str(group[expected_key]["responseSha256"]),
            }
            continue
        ranges = _legacy_raw_ack_ranges(
            upload,
            completed_items,
            extra_manifest_shas=(historical_manifest_sha,) if historical_manifest_sha else (),
        )
        normalized[expected_key] = {
            "payloadSha256": expected_payload,
            "responseSha256": _aggregate_raw_ack_response_digest(
                raw_sha,
                completed_items,
                ranges,
            ),
        }

    for key, expected_payload in expected.items():
        if key.startswith("raw:"):
            continue
        ack = completed_items.get(key)
        if not isinstance(ack, Mapping) or not _is_digest(ack.get("responseSha256")):
            raise ValueError("Sites sync completed receipt content digest is invalid")
        payload_sha = ack.get("payloadSha256")
        if payload_sha != expected_payload and key not in preserve_nonraw_keys:
            raise ValueError("Sites sync completed receipt content digest is invalid")
        if not _is_digest(payload_sha):
            raise ValueError("Sites sync completed receipt content digest is invalid")
        normalized[key] = {
            "payloadSha256": str(payload_sha),
            "responseSha256": str(ack["responseSha256"]),
        }

    if set(normalized) != set(expected):
        raise ValueError("Sites sync completed receipt ACK coverage is incomplete")
    return normalized, dict(normalized) != dict(completed_items)


def preflight_sites_sync(
    *,
    inputs: PreparedSitesSyncInputs | None,
    lane: Lane,
    mode: Mode,
    endpoints: Mapping[str, str],
    previous_receipt_path: Path | str,
    facts_only: bool = False,
) -> SitesSyncPlan:
    """Validate a complete desired batch and produce a zero-I/O request plan."""

    if mode not in {"full", "incremental"}:
        raise ValueError("Sites sync mode must be full or incremental")
    lane = _canonical_lane(lane, facts_only=facts_only)
    if lane == "formal":
        return _blocked_formal_plan(mode=mode)
    if inputs is None:
        raise ValueError("prepared Sites sync inputs are required")

    archive_sha = _digest(inputs.archive_manifest_sha256, field="archive_manifest_sha256")
    admission_sha = _digest(inputs.admission_sha256, field="admission_sha256")
    source_ids = _normalize_source_ids(inputs.source_ids)
    normalized_endpoints = _required_endpoints(inputs, lane=lane, endpoints=endpoints)
    destination_digests = {
        stream: _sha256(endpoint.encode("utf-8"))
        for stream, endpoint in sorted(normalized_endpoints.items())
    }

    structured_digests, structured_contents = _structured_inventory(
        inputs.structured_contents,
        lane=lane,
    )
    if lane != FACTS_ONLY_LANE and not structured_contents:
        raise ValueError("Sites sync requires structured artifact content")
    if lane == FACTS_ONLY_LANE:
        if structured_contents:
            raise ValueError("facts-only sync cannot contain structured artifacts")
        if inputs.prospective_audit is not None:
            raise ValueError("facts-only sync cannot contain prospective audit content")

    raw_observation_digests: dict[str, str] = {}
    raw_objects: list[str] = []
    raw_observation_ids: list[str] = []
    content_digests: dict[str, str] = dict(structured_digests)
    if lane in {"research_audit", FACTS_ONLY_LANE}:
        if (
            not inputs.raw_uploads
            or inputs.research_fixture is None
            or (lane == "research_audit" and inputs.prospective_audit is None)
        ):
            raise ValueError("facts/research sync inputs are incomplete")
        (
            raw_observation_digests,
            raw_objects,
            raw_observation_ids,
        ) = _validate_raw_uploads(
            inputs.raw_uploads,
            archive_manifest_sha256=archive_sha,
        )
        _validate_research_fixture(
            inputs.research_fixture,
            archive_manifest_sha256=archive_sha,
            admission_sha256=admission_sha,
            source_ids=source_ids,
        )
        if lane == FACTS_ONLY_LANE:
            _assert_facts_only_shape(inputs.research_fixture, path="research_fixture")
            _assert_facts_only_shape(inputs.raw_uploads, path="raw_uploads")
            _assert_facts_only_raw_bytes(inputs.raw_uploads)
        else:
            assert inputs.prospective_audit is not None
            _validate_prospective_audit(inputs.prospective_audit)
        content_digests["research_fixture"] = _content_digest(inputs.research_fixture)
        if lane == "research_audit":
            assert inputs.prospective_audit is not None
            content_digests["prospective_audit"] = _content_digest(inputs.prospective_audit)

    inventory = _inventory(
        raw_observation_digests=raw_observation_digests,
        raw_objects=raw_objects,
        content_digests=content_digests,
    )
    batch_manifest: dict[str, Any] = {
        "schemaVersion": BATCH_MANIFEST_SCHEMA,
        "lane": lane,
        "archiveManifestSha256": archive_sha,
        "admissionSha256": admission_sha,
        "sourceIds": source_ids,
        "sourcePolicyIds": list(SOURCE_POLICY_IDS),
        "destinationDigests": destination_digests,
        "inventory": inventory,
    }
    if lane == FACTS_ONLY_LANE:
        batch_manifest["factsOnly"] = True
    batch_sha = _content_digest(batch_manifest)

    structured_uploads: list[tuple[str, str, dict[str, Any]]] = []
    visibility: Literal["research_audit", "private_evidence"] | None = None
    if lane == "research_audit":
        visibility = "research_audit"
    elif lane == "private_evidence":
        visibility = "private_evidence"
    if visibility is not None:
        for artifact_kind, content in structured_contents:
            upload = build_structured_artifact_upload(
                content,
                visibility=visibility,
                artifact_kind=artifact_kind,
                batch_manifest_sha256=batch_sha,
                archive_manifest_sha256=archive_sha,
                admission_sha256=admission_sha,
                source_ids=source_ids,
            )
            identity = f"structured:{lane}:{artifact_kind}"
            if upload.get("contentSha256") != structured_digests[identity]:
                raise ValueError("structured artifact content identity drifted")
            structured_uploads.append((identity, artifact_kind, upload))

    expected_full_item_payload_digests = _expected_full_item_payload_digests(
        inputs=inputs,
        lane=lane,
        content_digests=content_digests,
        structured_uploads=structured_uploads,
    )

    receipt_path = Path(previous_receipt_path)
    previous = _load_completed_receipt(receipt_path)
    if previous is not None and previous.get("batchManifestSha256") == batch_sha:
        if previous.get("batchManifest") != batch_manifest:
            raise ValueError("Sites sync completed receipt has a hash collision")
        previous_completed_items = previous.get("completedItems")
        if not isinstance(
            previous_completed_items, Mapping
        ):  # pragma: no cover - loader invariant
            raise ValueError("Sites sync completed receipt is invalid")
        normalized_completed_items, needs_normalization = _normalize_completed_items(
            completed_items=previous_completed_items,
            expected_payload_digests=expected_full_item_payload_digests,
            raw_uploads=inputs.raw_uploads,
        )
        return SitesSyncPlan(
            status="already_complete",
            lane=lane,
            mode=mode,
            batch_manifest=batch_manifest,
            batch_manifest_sha256=batch_sha,
            inventory=inventory,
            items=(),
            normalized_completed_items=(
                tuple(sorted(normalized_completed_items.items())) if needs_normalization else ()
            ),
            normalized_completed_at=(
                str(previous["completedAt"]) if needs_normalization else None
            ),
        )

    baseline_inventory: dict[str, Any] | None = None
    baseline_completed_items: tuple[tuple[str, dict[str, str]], ...] = ()
    if mode == "incremental" and _compatible_baseline(
        previous,
        lane=lane,
        destination_digests=destination_digests,
    ):
        assert previous is not None
        baseline_inventory = _validate_inventory(previous.get("inventory"))
        previous_completed_items = previous.get("completedItems")
        if not isinstance(
            previous_completed_items, Mapping
        ):  # pragma: no cover - loader invariant
            raise ValueError("Sites sync completed receipt is invalid")
        baseline_completed_items = tuple(
            sorted(
                (
                    str(key),
                    {
                        "payloadSha256": str(item["payloadSha256"]),
                        "responseSha256": str(item["responseSha256"]),
                    },
                )
                for key, item in previous_completed_items.items()
            )
        )

    previous_observations: set[str] = set()
    previous_content: dict[str, str] = {}
    previous_manifest_sha: str | None = None
    if baseline_inventory is not None:
        previous_observations = set(baseline_inventory["rawObservationIds"])
        previous_content = dict(baseline_inventory["contentDigests"])
        previous_manifest = previous.get("batchManifest") if previous else None
        if isinstance(previous_manifest, Mapping):
            candidate = previous_manifest.get("archiveManifestSha256")
            previous_manifest_sha = str(candidate) if _is_digest(candidate) else None
        if not previous_observations <= set(raw_observation_ids):
            raise ValueError("incremental raw archive removed completed observations")
        # The local completed receipt is the only checkpoint from which an
        # incremental batch may advance.  Observation ids alone are not a
        # sufficient checkpoint: an append-only manifest must also preserve
        # the exact archive record and its manifest-prefix identity for every
        # observation already acknowledged.  Refuse to use a receipt when a
        # previously completed row was edited, reordered, or otherwise
        # replayed with different bytes; silently treating it as complete
        # would make the delta non-deterministic and could hide a provenance
        # fork behind the old receipt.
        previous_observation_digests = dict(baseline_inventory["rawObservationDigests"])
        for observation_id in sorted(previous_observations):
            if previous_observation_digests.get(observation_id) != raw_observation_digests.get(
                observation_id
            ):
                raise ValueError("incremental raw archive changed completed observations")
        if lane in {"research_audit", FACTS_ONLY_LANE}:
            _validate_incremental_raw_baseline(
                raw_uploads=inputs.raw_uploads,
                completed_items=dict(baseline_completed_items),
                historical_manifest_sha=previous_manifest_sha,
            )

    items: list[SyncItem] = []
    if lane in {"research_audit", FACTS_ONLY_LANE}:
        raw_candidates: list[dict[str, Any]] = []
        for upload in sorted(
            inputs.raw_uploads,
            key=lambda value: str(value["raw"]["sha256"]),
        ):
            selected = [
                row
                for row in upload["observations"]
                if row["archiveRecord"]["observation_id"] not in previous_observations
            ]
            if selected:
                raw_candidates.append({**upload, "observations": selected})
        if (
            baseline_inventory is not None
            and previous_manifest_sha != archive_sha
            and not raw_candidates
        ):
            anchor = min(
                inputs.raw_uploads,
                key=lambda value: str(value["raw"]["sha256"]),
            )
            raw_candidates.append({**anchor, "observations": [anchor["observations"][0]]})

        for upload in raw_candidates:
            observation_ids = sorted(
                row["archiveRecord"]["observation_id"] for row in upload["observations"]
            )
            observation_set_sha = _content_digest(observation_ids)
            raw_sha = str(upload["raw"]["sha256"])
            items.append(
                _item(
                    stream="raw_artifact",
                    item_key=f"raw:{raw_sha}:{observation_set_sha}",
                    endpoint=normalized_endpoints["raw_artifact"],
                    payload=upload,
                )
            )

        assert inputs.research_fixture is not None
        research_digest = content_digests["research_fixture"]
        if previous_content.get("research_fixture") != research_digest:
            items.append(
                _item(
                    stream="research_fixture",
                    item_key=f"research-fixture:{research_digest}",
                    endpoint=normalized_endpoints["research_fixture"],
                    payload=inputs.research_fixture,
                )
            )
        if lane == "research_audit":
            assert inputs.prospective_audit is not None
            audit_digest = content_digests["prospective_audit"]
            if previous_content.get("prospective_audit") != audit_digest:
                items.append(
                    _item(
                        stream="prospective_audit",
                        item_key=f"prospective-audit:{audit_digest}",
                        endpoint=normalized_endpoints["prospective_audit"],
                        payload=inputs.prospective_audit,
                    )
                )

    for identity, artifact_kind, upload in structured_uploads:
        content_sha = structured_digests[identity]
        if previous_content.get(identity) == content_sha:
            continue
        items.append(
            _item(
                stream="structured_artifact",
                item_key=f"structured:{lane}:{artifact_kind}:{content_sha}",
                endpoint=normalized_endpoints["structured_artifact"],
                payload=upload,
            )
        )

    return SitesSyncPlan(
        status="planned",
        lane=lane,
        mode=mode,
        batch_manifest=batch_manifest,
        batch_manifest_sha256=batch_sha,
        inventory=inventory,
        items=tuple(items),
        baseline_completed_items=baseline_completed_items,
        normalization_expected_payload_digests=(
            tuple(sorted(expected_full_item_payload_digests.items()))
            if baseline_inventory is not None and lane in {"research_audit", FACTS_ONLY_LANE}
            else ()
        ),
        normalization_raw_uploads=(
            tuple(inputs.raw_uploads)
            if baseline_inventory is not None and lane in {"research_audit", FACTS_ONLY_LANE}
            else ()
        ),
        normalization_historical_manifest_sha=(
            previous_manifest_sha
            if baseline_inventory is not None and lane in {"research_audit", FACTS_ONLY_LANE}
            else None
        ),
    )


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    body = canonical_json_bytes(value) + b"\n"
    if len(body) > MAX_STATE_BYTES:
        raise ValueError("Sites sync state exceeds its size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and not stat.S_ISREG(current.st_mode):
        raise ValueError("Sites sync state target must be a regular file")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _rewrite_normalized_completion(
    plan: SitesSyncPlan,
    *,
    receipt_file: Path,
    cursor_file: Path,
) -> None:
    """Persist a proven canonical receipt and discard legacy cursor segments."""

    normalized = dict(plan.normalized_completed_items)
    if not normalized:
        return
    if receipt_file == cursor_file:
        raise ValueError("Sites sync receipt and cursor paths must be distinct")
    _validate_receipt_ack_coverage(
        lane=plan.lane,
        inventory=plan.inventory,
        completed_items=normalized,
    )
    current = _load_completed_receipt(receipt_file)
    if (
        current is None
        or current.get("batchManifestSha256") != plan.batch_manifest_sha256
        or current.get("batchManifest") != plan.batch_manifest
    ):
        raise ValueError("Sites sync completed receipt changed before normalization")
    completed: dict[str, Any] = {
        "schemaVersion": RECEIPT_SCHEMA,
        "status": "completed",
        "lane": plan.lane,
        "batchManifest": plan.batch_manifest,
        "batchManifestSha256": plan.batch_manifest_sha256,
        "inventory": plan.inventory,
        "completedItems": dict(sorted(normalized.items())),
        "completedAt": plan.normalized_completed_at or str(current["completedAt"]),
    }
    if plan.lane == FACTS_ONLY_LANE:
        completed["factsOnly"] = True
    _atomic_write_json(receipt_file, completed)
    _atomic_write_json(
        cursor_file,
        {
            "schemaVersion": CURSOR_SCHEMA,
            "batchManifestSha256": plan.batch_manifest_sha256,
            "ackedItems": dict(sorted(normalized.items())),
        },
    )


def _load_cursor(
    path: Path,
    plan: SitesSyncPlan,
    *,
    carried_items: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    value = _read_state(path, label="cursor")
    if value is None:
        return {}
    if (
        set(value) != {"schemaVersion", "batchManifestSha256", "ackedItems"}
        or value.get("schemaVersion") != CURSOR_SCHEMA
        or not _is_digest(value.get("batchManifestSha256"))
        or not isinstance(value.get("ackedItems"), Mapping)
    ):
        raise ValueError("Sites sync cursor is invalid")
    if value.get("batchManifestSha256") != plan.batch_manifest_sha256:
        return {}
    planned = {item.item_key: item.payload_sha256 for item in plan.items}
    carried = carried_items or {}
    result: dict[str, dict[str, str]] = {}
    for key, item in value["ackedItems"].items():
        expected_payload = planned.get(key)
        if expected_payload is None:
            carried_item = carried.get(key)
            if not isinstance(carried_item, Mapping):
                raise ValueError("Sites sync cursor is invalid")
            expected_payload = carried_item.get("payloadSha256")
        if (
            not isinstance(item, Mapping)
            or set(item) != {"payloadSha256", "responseSha256"}
            or item.get("payloadSha256") != expected_payload
            or not _is_digest(item.get("responseSha256"))
        ):
            raise ValueError("Sites sync cursor is invalid")
        result[str(key)] = {
            "payloadSha256": str(item["payloadSha256"]),
            "responseSha256": str(item["responseSha256"]),
        }
    return result


def _headers_for_item(
    item: SyncItem,
    *,
    key_id: str,
    signing_secret: str,
) -> dict[str, str]:
    body = canonical_json_bytes(item.payload)
    if _sha256(body) != item.payload_sha256:
        raise ValueError("Sites sync item changed after preflight")
    if item.stream == "raw_artifact":
        source_policy_ids = sorted(
            {
                (
                    "openfootball_current"
                    if row["archiveRecord"]["source_id"] in _CURRENT_SOURCE_IDS
                    else "openfootball_historical"
                )
                for row in item.payload["observations"]
            }
        )
        return raw_artifact_attestation_headers(
            body,
            endpoint=item.endpoint,
            key_id=key_id,
            signing_secret=signing_secret,
            source_policy_ids=source_policy_ids,
            archive_manifest_sha256=str(item.payload["archiveManifestSha256"]),
        )
    if item.stream == "research_fixture":
        return research_fixture_attestation_headers(
            body,
            endpoint=item.endpoint,
            key_id=key_id,
            signing_secret=signing_secret,
            archive_manifest_sha256=str(item.payload["archiveManifestSha256"]),
        )
    if item.stream == "prospective_audit":
        return prospective_audit_attestation_headers(
            body,
            endpoint=item.endpoint,
            key_id=key_id,
            signing_secret=signing_secret,
            payload=item.payload,
        )
    return structured_artifact_attestation_headers(
        body,
        endpoint=item.endpoint,
        key_id=key_id,
        signing_secret=signing_secret,
        upload=item.payload,
    )


def _validated_ack(item: SyncItem, response: Mapping[str, Any]) -> dict[str, Any]:
    if item.stream == "raw_artifact":
        return validate_raw_artifact_ack(item.payload, response)
    if item.stream == "research_fixture":
        return validate_research_fixture_ack(item.payload, response)
    if item.stream == "prospective_audit":
        return validate_prospective_audit_ack(item.payload, response)
    return validate_structured_artifact_ack(item.payload, response)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def execute_sites_sync(
    plan: SitesSyncPlan,
    *,
    token: str,
    key_id: str,
    signing_secret: str,
    cursor_path: Path | str,
    receipt_path: Path | str,
    sender: SyncSender | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute one preflighted batch, checkpointing only strict ACKs."""

    if plan.status == FORMAL_BLOCKED_STATUS:
        return {"status": FORMAL_BLOCKED_STATUS, "requests": 0}
    if plan.status == "already_complete":
        _rewrite_normalized_completion(
            plan,
            receipt_file=Path(receipt_path),
            cursor_file=Path(cursor_path),
        )
        return {
            "status": "already_complete",
            "requests": 0,
            "batchManifestSha256": plan.batch_manifest_sha256,
        }
    if plan.status != "planned":
        raise ValueError("Sites sync plan status is invalid")
    if not token.strip():
        raise ValueError("Sites sync ingest token is required")
    if not key_id.strip() or not 32 <= len(signing_secret.encode("utf-8")) <= 4096:
        raise ValueError("Sites sync signing identity is invalid")
    if timeout <= 0:
        raise ValueError("Sites sync timeout must be positive")

    cursor_file = Path(cursor_path)
    receipt_file = Path(receipt_path)
    planned_payloads = {item.item_key: item.payload_sha256 for item in plan.items}
    baseline_items = {key: dict(value) for key, value in plan.baseline_completed_items}
    # An item identity is intentionally stable across retries.  If a manifest
    # changed while keeping the same observation set (the registration-anchor
    # case), the payload digest changes and the old ACK must not suppress the
    # new request.
    for key in list(baseline_items):
        if (
            key in planned_payloads
            and baseline_items[key]["payloadSha256"] != planned_payloads[key]
        ):
            del baseline_items[key]
    cursor_items = _load_cursor(cursor_file, plan, carried_items=baseline_items)
    acked_items = {**baseline_items, **cursor_items}
    initially_acked = sum(
        1
        for key, payload_sha256 in planned_payloads.items()
        if acked_items.get(key, {}).get("payloadSha256") == payload_sha256
    )
    requests = 0
    send = sender or _send_raw_upload

    prepared_requests: list[tuple[SyncItem, bytes, dict[str, str]]] = []
    for item in plan.items:
        if acked_items.get(item.item_key, {}).get("payloadSha256") == item.payload_sha256:
            continue
        body = canonical_json_bytes(item.payload)
        headers = _headers_for_item(
            item,
            key_id=key_id,
            signing_secret=signing_secret,
        )
        prepared_requests.append((item, body, headers))

    # All request bodies and attestations are constructed before the first
    # network call.  A late payload/header failure therefore cannot leave a
    # partially uploaded batch.
    for item, body, headers in prepared_requests:
        response = send(
            endpoint=item.endpoint,
            body=body,
            headers=headers,
            token=token,
            timeout=timeout,
        )
        ack = _validated_ack(item, response)
        requests += 1
        acked_items[item.item_key] = {
            "payloadSha256": item.payload_sha256,
            "responseSha256": _content_digest(ack),
        }
        _atomic_write_json(
            cursor_file,
            {
                "schemaVersion": CURSOR_SCHEMA,
                "batchManifestSha256": plan.batch_manifest_sha256,
                "ackedItems": {
                    key: acked_items[key] for key in sorted(planned_payloads) if key in acked_items
                },
            },
        )

    if any(
        acked_items.get(key, {}).get("payloadSha256") != payload_sha256
        for key, payload_sha256 in planned_payloads.items()
    ):  # pragma: no cover - loop invariant
        raise ValueError("Sites sync did not acknowledge every planned item")

    # An incremental receipt may carry legacy segment identities from prior
    # batches.  Once this batch's delta ACKs are present, prove the complete
    # current inventory and collapse all segments/content versions locally.
    # The helper is deliberately fail-closed: no canonical ACK is emitted
    # unless every segment body digest maps uniquely and partitions each raw
    # object without overlap or gaps.
    normalization_expected = dict(plan.normalization_expected_payload_digests)
    if normalization_expected:
        normalized_items, _ = _normalize_completed_items(
            completed_items=acked_items,
            expected_payload_digests=normalization_expected,
            raw_uploads=plan.normalization_raw_uploads,
            historical_manifest_sha=plan.normalization_historical_manifest_sha,
            preserve_nonraw_keys={
                key for key in baseline_items if not key.startswith("raw:")
            },
        )
        _validate_receipt_ack_coverage(
            lane=plan.lane,
            inventory=plan.inventory,
            completed_items=normalized_items,
        )
        acked_items = normalized_items
        # Checkpoint the canonical set as well as the completed receipt.  This
        # removes stale segment keys even when the next invocation is a retry.
        _atomic_write_json(
            cursor_file,
            {
                "schemaVersion": CURSOR_SCHEMA,
                "batchManifestSha256": plan.batch_manifest_sha256,
                "ackedItems": dict(sorted(acked_items.items())),
            },
        )

    completed: dict[str, Any] = {
        "schemaVersion": RECEIPT_SCHEMA,
        "status": "completed",
        "lane": plan.lane,
        "batchManifest": plan.batch_manifest,
        "batchManifestSha256": plan.batch_manifest_sha256,
        "inventory": plan.inventory,
        "completedItems": dict(sorted(acked_items.items())),
        "completedAt": _utc_now(),
    }
    if plan.lane == FACTS_ONLY_LANE:
        completed["factsOnly"] = True
    _atomic_write_json(receipt_file, completed)
    return {
        "status": "completed",
        "requests": requests,
        "resumedItems": initially_acked,
        "plannedItems": len(plan.items),
        "batchManifestSha256": plan.batch_manifest_sha256,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lane",
        choices=("research_audit", "private_evidence", "formal", FACTS_ONLY_LANE, "facts_only"),
        required=True,
    )
    parser.add_argument(
        "--facts-only",
        action="store_true",
        help="use the explicit raw-backed research facts-only lane",
    )
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=os.environ.get("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"),
    )
    parser.add_argument(
        "--snapshot", type=Path, default=os.environ.get("MATCHLINE_RESEARCH_FIXTURE_SNAPSHOT")
    )
    parser.add_argument("--lock", type=Path, default=os.environ.get("MATCHLINE_PROSPECTIVE_LOCK"))
    parser.add_argument("--cycle", type=Path)
    parser.add_argument("--evaluation", type=Path)
    parser.add_argument("--live-snapshot", type=Path)
    parser.add_argument("--prediction-archive", type=Path)
    parser.add_argument("--strict-report", type=Path)
    parser.add_argument("--offline-snapshot", type=Path)
    parser.add_argument("--raw-endpoint", default=os.environ.get(RAW_ENDPOINT_ENV))
    parser.add_argument(
        "--research-endpoint",
        default=os.environ.get(RESEARCH_FIXTURE_ENDPOINT_ENV),
    )
    parser.add_argument(
        "--audit-endpoint",
        default=os.environ.get(PROSPECTIVE_AUDIT_ENDPOINT_ENV),
    )
    parser.add_argument(
        "--structured-endpoint",
        default=os.environ.get(STRUCTURED_ARTIFACT_ENDPOINT_ENV),
    )
    parser.add_argument("--cursor", type=Path, default=os.environ.get(SITES_SYNC_CURSOR_ENV))
    parser.add_argument("--receipt", type=Path, default=os.environ.get(SITES_SYNC_RECEIPT_ENV))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cursor is None or args.receipt is None:
        raise ValueError("--cursor and --receipt are required")
    lane = _canonical_lane(args.lane, facts_only=args.facts_only)
    prepared = prepare_sites_sync_inputs(
        lane=lane,
        archive_root=args.archive_root,
        snapshot_path=args.snapshot,
        lock_path=args.lock,
        cycle_path=args.cycle,
        evaluation_path=args.evaluation,
        live_snapshot_path=args.live_snapshot,
        prediction_archive_path=args.prediction_archive,
        strict_report_path=args.strict_report,
        offline_snapshot_path=args.offline_snapshot,
    )
    endpoints = {
        key: value
        for key, value in {
            "raw_artifact": args.raw_endpoint,
            "research_fixture": args.research_endpoint,
            "prospective_audit": args.audit_endpoint,
            "structured_artifact": args.structured_endpoint,
        }.items()
        if isinstance(value, str) and value.strip()
    }
    plan = preflight_sites_sync(
        inputs=prepared,
        lane=lane,
        mode=args.mode,
        endpoints=endpoints,
        previous_receipt_path=args.receipt,
    )
    if args.dry_run and plan.status != FORMAL_BLOCKED_STATUS:
        result: dict[str, Any] = {
            "status": "dry_run" if plan.status == "planned" else plan.status,
            "lane": plan.lane,
            "mode": plan.mode,
            "plannedRequests": len(plan.items),
            "batchManifestSha256": plan.batch_manifest_sha256,
            "inventory": {
                "rawObservations": len(plan.inventory["rawObservationIds"]),
                "rawObjects": len(plan.inventory["rawObjectSha256s"]),
                "contentArtifacts": len(plan.inventory["contentDigests"]),
            },
        }
    else:
        result = execute_sites_sync(
            plan,
            token=os.environ.get(INGEST_TOKEN_ENV, ""),
            key_id=os.environ.get(PRODUCER_KEY_ID_ENV, ""),
            signing_secret=os.environ.get(PRODUCER_SIGNING_SECRET_ENV, ""),
            cursor_path=args.cursor,
            receipt_path=args.receipt,
            timeout=args.timeout,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "BATCH_MANIFEST_SCHEMA",
    "CURSOR_SCHEMA",
    "FACTS_ONLY_BOUNDARY",
    "FACTS_ONLY_LANE",
    "FORMAL_BLOCKED_STATUS",
    "PreparedSitesSyncInputs",
    "RECEIPT_SCHEMA",
    "SitesSyncPlan",
    "SyncItem",
    "execute_sites_sync",
    "main",
    "preflight_sites_sync",
    "prepare_sites_sync_inputs",
]


if __name__ == "__main__":
    raise SystemExit(main())
