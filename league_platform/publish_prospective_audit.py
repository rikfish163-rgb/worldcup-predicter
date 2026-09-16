"""Publish a probability-free prospective evidence snapshot to Sites.

This is an audit lane, not a prediction-publication lane.  It binds one
validated model lock, cycle, evaluation, live snapshot, and immutable
prediction archive by raw SHA-256 and emits only counts, timestamps, stage
identities, and release-gate booleans.  Model probabilities, markets, feature
values, recommendations, local paths, and arbitrary diagnostic text are never
part of the wire contract.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
from typing import Any

from league_platform.prospective_archive import (
    logical_freeze_key,
    validate_lock_integrity,
    validate_prediction,
)
from league_platform.publish_raw_artifacts import (
    DEFAULT_TIMEOUT_SECONDS,
    INGEST_TOKEN_ENV,
    PRODUCER_KEY_ID_ENV,
    PRODUCER_NAME,
    PRODUCER_SIGNING_SECRET_ENV,
    PRODUCER_VERSION,
    _normalized_endpoint,
    _send_raw_upload,
    canonical_json_bytes,
)


PROSPECTIVE_AUDIT_SCHEMA = "matchline.prospective_audit_snapshot.v1"
PROSPECTIVE_AUDIT_ATTESTATION_SCHEMA = "matchline.prospective_audit_attestation.v1"
PROSPECTIVE_AUDIT_ENDPOINT_ENV = "MATCHLINE_PROSPECTIVE_AUDIT_ENDPOINT"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_AUDIT_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
ATTESTATION_TTL_SECONDS = 300
_HEX = frozenset("0123456789abcdef")
_STAGES = (
    "t_minus_24h",
    "t_minus_6h",
    "t_minus_90m",
    "lineup_confirmation",
)
_TARGETS = ("three_way", "total_goals", "half_full", "scoreline")
_AUDIT_STATUSES = {
    "pending_prospective_window",
    "passed",
    "research_only_underperforms_market",
    "blocked_selection_debt",
    "partial_with_explicit_blocks",
}
_MAX_FREEZE_RECORDS = 1024

AuditSender = Callable[..., Mapping[str, Any]]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and set(value) <= _HEX
        and value != "0" * 64
    )


def _read_regular(path: Path | str, *, maximum: int, label: str) -> bytes:
    candidate = Path(path)
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"cannot open {label}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            raise ValueError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if total > maximum or total != before.st_size:
            raise ValueError(f"{label} exceeds its size limit")
        if (
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
            raise ValueError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _iso(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _count(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _boolean(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _digest(value: object, *, field: str) -> str:
    if not _valid_sha256(value):
        raise ValueError(f"{field} must be a non-zero lowercase SHA-256")
    return str(value)


def _current_archive_records(
    raw: bytes,
    *,
    lock: Mapping[str, Any],
) -> tuple[list[dict[str, str]], dict[str, int]]:
    if not raw.endswith(b"\n"):
        raise ValueError("prediction archive must be newline-terminated JSONL")
    model_sha = _digest(lock.get("model_version_sha256"), field="model version")
    window_started = _iso(
        lock.get("evaluation_window_started_at"),
        field="evaluation_window_started_at",
    )
    stage_counts = {stage: 0 for stage in _STAGES}
    freeze_keys: set[str] = set()
    records: list[dict[str, str]] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        if not raw_line:
            raise ValueError(f"prediction archive has a blank line at {line_number}")
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"prediction archive is invalid at line {line_number}") from exc
        if not isinstance(row, Mapping):
            raise ValueError(f"prediction archive row {line_number} is not an object")
        if row.get("model_version_sha256") != model_sha:
            continue
        if row.get("model_lock_window_started_at") != lock.get("evaluation_window_started_at"):
            raise ValueError("prediction archive current row has a mismatched lock window")
        if row.get("conflict") is True:
            raise ValueError("prediction archive current row is conflicted")
        prediction = row.get("prediction")
        if not isinstance(prediction, Mapping):
            raise ValueError("prediction archive current row is missing prediction")
        validate_prediction(prediction, lock=lock)
        stage = prediction.get("freeze_stage")
        if stage not in stage_counts:
            raise ValueError("prediction archive current row has an invalid freeze stage")
        key = logical_freeze_key(prediction)
        if key in freeze_keys:
            raise ValueError("prediction archive contains duplicate current freeze identities")
        freeze_keys.add(key)
        stage_counts[str(stage)] += 1
        fixture_id = prediction.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id.startswith("openfootball:"):
            raise ValueError("prediction archive current row has a non-OpenFootball fixture id")
        kickoff_at = _iso(prediction.get("kickoff_at"), field="prediction.kickoff_at")
        cutoff_at = _iso(prediction.get("freeze_cutoff_at"), field="prediction.freeze_cutoff_at")
        observed_at = _iso(
            prediction.get("prediction_observed_at")
            or prediction.get("as_of")
            or row.get("captured_at"),
            field="prediction.observed_at",
        )
        records.append(
            {
                "fixtureId": fixture_id,
                "competitionId": str(prediction.get("competition_id")),
                "kickoffAt": kickoff_at,
                "freezeStage": str(stage),
                "cutoffAt": cutoff_at,
                "observedAt": observed_at,
            }
        )
        if (
            _iso(
                row.get("model_lock_window_started_at"),
                field="model_lock_window_started_at",
            )
            != window_started
        ):
            raise ValueError("prediction archive current row has a mismatched lock window")
    if not records:
        raise ValueError("prediction archive contains no current-lock freezes")
    if len(records) > _MAX_FREEZE_RECORDS:
        raise ValueError("prediction archive current-lock freeze records exceed the bounded limit")
    records.sort(key=lambda item: (
        item["kickoffAt"], item["cutoffAt"], item["fixtureId"], item["freezeStage"],
    ))
    return records, stage_counts


def _current_archive_counts(
    raw: bytes,
    *,
    lock: Mapping[str, Any],
) -> tuple[int, dict[str, int]]:
    records, stage_counts = _current_archive_records(raw, lock=lock)
    return len(records), stage_counts


def _sample_requirements(value: object) -> dict[str, dict[str, int | bool]]:
    if not isinstance(value, Mapping) or set(value) != set(_TARGETS):
        raise ValueError("evaluation target sample requirements are incomplete")
    result: dict[str, dict[str, int | bool]] = {}
    for target in _TARGETS:
        raw = value.get(target)
        if not isinstance(raw, Mapping) or set(raw) != {
            "sample_n",
            "minimum_n",
            "shortfall_n",
            "met",
        }:
            raise ValueError(f"evaluation target {target} has an invalid schema")
        sample_n = _count(raw.get("sample_n"), field=f"{target}.sample_n")
        minimum_n = _count(raw.get("minimum_n"), field=f"{target}.minimum_n")
        shortfall_n = _count(raw.get("shortfall_n"), field=f"{target}.shortfall_n")
        met = _boolean(raw.get("met"), field=f"{target}.met")
        if shortfall_n != max(0, minimum_n - sample_n) or met != (sample_n >= minimum_n):
            raise ValueError(f"evaluation target {target} counts are inconsistent")
        result[target] = {
            "sampleN": sample_n,
            "minimumN": minimum_n,
            "shortfallN": shortfall_n,
            "met": met,
        }
    return result


def _next_freezes(capture: Mapping[str, Any]) -> list[dict[str, str]]:
    diagnostics = capture.get("blocked_diagnostics")
    if not isinstance(diagnostics, Mapping):
        return []
    items = diagnostics.get("items")
    if not isinstance(items, list):
        raise ValueError("capture blocked diagnostics items are invalid")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in items:
        if not isinstance(raw, Mapping) or raw.get("reason") != (
            "no_freeze_cutoff_observed_before_as_of"
        ):
            continue
        competition = raw.get("competition_id")
        fixture_id = raw.get("next_fixture_id")
        stage = raw.get("next_freeze_stage")
        if (
            not isinstance(competition, str)
            or not competition.strip()
            or len(competition) > 80
            or not isinstance(fixture_id, str)
            or not fixture_id.startswith("openfootball:")
            or len(fixture_id) > 200
            or stage not in _STAGES
        ):
            raise ValueError("next freeze identity is invalid")
        cutoff = _iso(raw.get("next_freeze_cutoff_at"), field="next freeze cutoff")
        kickoff = _iso(raw.get("next_kickoff_at"), field="next freeze kickoff")
        identity = (competition, str(stage), cutoff)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            {
                "competitionId": competition,
                "fixtureId": fixture_id,
                "freezeStage": str(stage),
                "cutoffAt": cutoff,
                "kickoffAt": kickoff,
            }
        )
    return sorted(result, key=lambda item: (item["cutoffAt"], item["competitionId"]))[:8]


def build_prospective_audit_snapshot(
    *,
    lock_path: Path | str,
    cycle_path: Path | str,
    evaluation_path: Path | str,
    live_snapshot_path: Path | str,
    prediction_archive_path: Path | str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one closed audit snapshot from raw artifact bytes."""

    lock_raw = _read_regular(lock_path, maximum=MAX_JSON_BYTES, label="model lock")
    cycle_raw = _read_regular(cycle_path, maximum=MAX_JSON_BYTES, label="cycle receipt")
    evaluation_raw = _read_regular(
        evaluation_path,
        maximum=MAX_JSON_BYTES,
        label="evaluation receipt",
    )
    live_raw = _read_regular(
        live_snapshot_path,
        maximum=MAX_JSON_BYTES,
        label="live snapshot",
    )
    archive_raw = _read_regular(
        prediction_archive_path,
        maximum=MAX_ARCHIVE_BYTES,
        label="prediction archive",
    )
    lock = _json_object(lock_raw, label="model lock")
    cycle = _json_object(cycle_raw, label="cycle receipt")
    evaluation = _json_object(evaluation_raw, label="evaluation receipt")
    live = _json_object(live_raw, label="live snapshot")

    validate_lock_integrity(lock)
    lock_status = lock.get("status")
    cycle_status = cycle.get("status")
    evaluation_status = evaluation.get("status")
    if (
        lock_status not in _AUDIT_STATUSES
        or cycle_status not in _AUDIT_STATUSES
        or evaluation_status != cycle_status
    ):
        raise ValueError("prospective audit statuses are not mutually compatible")
    cycle_evaluation = cycle.get("evaluation")
    if not isinstance(cycle_evaluation, Mapping) or any(
        evaluation.get(key) != value for key, value in cycle_evaluation.items()
    ):
        raise ValueError("cycle receipt is not bound to the evaluation receipt")
    evaluation_lock_identity = evaluation.get("lock_identity")
    if evaluation_lock_identity is not None:
        cycle_lock_identity = cycle.get("lock_identity")
        if not isinstance(cycle_lock_identity, Mapping) or not isinstance(
            evaluation_lock_identity, Mapping
        ) or evaluation_lock_identity != cycle_lock_identity:
            raise ValueError("evaluation lock identity is not bound to the cycle receipt")
    # The runtime writer adds storage/publication diagnostics to the mirrored
    # evaluation pointer after the cycle embeds its immutable scoring result.
    # Those four closed audit sections may be appended, but no cycle-owned
    # scoring field may disappear or change.
    evaluation_extras = set(evaluation) - set(cycle_evaluation)
    if not evaluation_extras <= {
        "degraded",
        "lock_identity",
        "lock_promotion",
        "publication",
        "storage",
    }:
        raise ValueError("evaluation receipt contains unbound top-level fields")

    sync = cycle.get("sync")
    capture = cycle.get("capture")
    if not isinstance(sync, Mapping) or not isinstance(capture, Mapping):
        raise ValueError("cycle receipt is missing sync or capture evidence")
    live_sha = _sha256(live_raw)
    if sync.get("snapshot_sha256") != live_sha:
        raise ValueError("live snapshot hash does not match the cycle receipt")
    live_as_of = _iso(live.get("as_of"), field="live snapshot as_of")
    cycle_as_of = _iso(sync.get("as_of"), field="cycle sync as_of")
    if live_as_of != cycle_as_of:
        raise ValueError("live snapshot time does not match the cycle receipt")

    openfootball = live.get("openfootball_current")
    admission = (
        openfootball.get("raw_archive_admission") if isinstance(openfootball, Mapping) else None
    )
    if (
        not isinstance(admission, Mapping)
        or admission.get("status") != "verified_current_raw"
        or admission.get("policy_version") != "v260"
    ):
        raise ValueError("live snapshot lacks verified v260 raw admission")
    source_ids = admission.get("source_ids")
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or any(
            not isinstance(value, str) or not value.startswith("openfootball:")
            for value in source_ids
        )
    ):
        raise ValueError("live snapshot raw source inventory is invalid")
    raw_identity = {
        "admissionSha256": _digest(
            admission.get("admission_sha256"), field="raw admission digest"
        ),
        "rowsSha256": _digest(admission.get("rows_sha256"), field="raw rows digest"),
        "sourceManifestSha256": _digest(
            admission.get("source_manifest_sha256"),
            field="raw source manifest digest",
        ),
    }

    freeze_records, stage_counts = _current_archive_records(archive_raw, lock=lock)
    current_freezes = len(freeze_records)
    scored_n = _count(evaluation.get("scored_n"), field="evaluation.scored_n")
    pending_n = _count(evaluation.get("pending_n"), field="evaluation.pending_n")
    if scored_n + pending_n != current_freezes:
        raise ValueError("evaluation counts do not match the current prediction archive")

    payload: dict[str, Any] = {
        "schemaVersion": PROSPECTIVE_AUDIT_SCHEMA,
        # The artifact identity must be retry-stable.  Bind generation time to
        # the completed cycle unless a test/replay explicitly supplies it.
        "generatedAt": _iso(
            generated_at.isoformat() if generated_at is not None else cycle.get("finished_at"),
            field="generated_at",
        ),
        "sourcePolicyIds": ["openfootball_current"],
        "model": {
            "versionSha256": _digest(lock.get("model_version_sha256"), field="model version"),
            "status": str(lock_status),
            "lockedAt": _iso(lock.get("locked_at"), field="locked_at"),
            "evaluationWindowStartedAt": _iso(
                lock.get("evaluation_window_started_at"),
                field="evaluation_window_started_at",
            ),
            "parametersMutable": False,
        },
        "artifacts": {
            "modelLockRawSha256": _sha256(lock_raw),
            "cycleRawSha256": _sha256(cycle_raw),
            "evaluationRawSha256": _sha256(evaluation_raw),
            "liveSnapshotRawSha256": live_sha,
            "predictionArchiveRawSha256": _sha256(archive_raw),
            **raw_identity,
        },
        "cycle": {
            "status": str(cycle_status),
            "startedAt": _iso(cycle.get("started_at"), field="cycle.started_at"),
            "finishedAt": _iso(cycle.get("finished_at"), field="cycle.finished_at"),
            "asOf": cycle_as_of,
            "fixtureCount": _count(sync.get("fixtures"), field="cycle fixture count"),
        },
        "capture": {
            "upcomingFixtures": _count(
                capture.get("upcoming_fixtures"), field="capture.upcoming_fixtures"
            ),
            "predictionsGenerated": _count(
                capture.get("predictions"), field="capture.predictions"
            ),
            "predictionsAppended": _count(capture.get("appended"), field="capture.appended"),
            "duplicatesSkipped": _count(
                capture.get("skipped_duplicate"), field="capture.skipped_duplicate"
            ),
            "lineupObserved": _count(
                capture.get("lineup_observed"), field="capture.lineup_observed"
            ),
            "blocked": _count(capture.get("blocked"), field="capture.blocked"),
            "currentFreezeCount": current_freezes,
            "stageCounts": stage_counts,
        },
        "evaluation": {
            "scoredN": scored_n,
            "pendingN": pending_n,
            "resultConflicts": _count(
                evaluation.get("result_conflicts"),
                field="evaluation.result_conflicts",
            ),
            "sampleRequirementsMet": _boolean(
                evaluation.get("sample_requirements_met"),
                field="evaluation.sample_requirements_met",
            ),
            "allRequiredTargetsScored": _boolean(
                evaluation.get("all_required_targets_scored"),
                field="evaluation.all_required_targets_scored",
            ),
            "predictionFreezesVerified": _boolean(
                evaluation.get("prediction_freezes_verified"),
                field="evaluation.prediction_freezes_verified",
            ),
            "promotionEligible": _boolean(
                evaluation.get("promotion_eligible"),
                field="evaluation.promotion_eligible",
            ),
            "productionAllowed": _boolean(
                evaluation.get("production_allowed"),
                field="evaluation.production_allowed",
            ),
            "targets": _sample_requirements(evaluation.get("target_sample_requirements")),
        },
        "nextFreezes": _next_freezes(capture),
        # This is deliberately metadata-only: it lets the public facts row
        # prove that a research freeze exists without publishing any model
        # probability, market, feature, or recommendation value.
        "freezeRecords": freeze_records,
    }
    if len(canonical_json_bytes(payload)) > MAX_AUDIT_BYTES:
        raise ValueError("prospective audit snapshot exceeds 128 KiB")
    return payload


def prospective_audit_attestation_headers(
    body: bytes,
    *,
    endpoint: str,
    key_id: str,
    signing_secret: str,
    payload: Mapping[str, Any],
    issued_at: datetime | None = None,
) -> dict[str, str]:
    normalized_key = key_id.strip()
    secret = signing_secret.encode("utf-8")
    if not normalized_key or len(normalized_key) > 120 or len(secret) < 32 or len(secret) > 4096:
        raise ValueError("prospective audit signing identity is invalid")
    if payload.get("schemaVersion") != PROSPECTIVE_AUDIT_SCHEMA:
        raise ValueError("prospective audit payload schema is invalid")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("prospective audit artifact identity is missing")
    source_policy_ids = payload.get("sourcePolicyIds")
    if source_policy_ids != ["openfootball_current"]:
        raise ValueError("prospective audit source policy identity is invalid")
    now = (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expires = (
        (now + timedelta(seconds=ATTESTATION_TTL_SECONDS))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    attestation = {
        "bodySha256": _sha256(body),
        "cycleRawSha256": _digest(artifacts.get("cycleRawSha256"), field="cycle digest"),
        "destination": _normalized_endpoint(endpoint),
        "evaluationRawSha256": _digest(
            artifacts.get("evaluationRawSha256"), field="evaluation digest"
        ),
        "expiresAt": expires,
        "issuedAt": issued,
        "keyId": normalized_key,
        "liveSnapshotRawSha256": _digest(
            artifacts.get("liveSnapshotRawSha256"), field="live snapshot digest"
        ),
        "modelLockRawSha256": _digest(
            artifacts.get("modelLockRawSha256"), field="model lock digest"
        ),
        "predictionArchiveRawSha256": _digest(
            artifacts.get("predictionArchiveRawSha256"),
            field="prediction archive digest",
        ),
        "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
        "rightsUseCase": "audit_only",
        "schemaVersion": PROSPECTIVE_AUDIT_ATTESTATION_SCHEMA,
        "sourcePolicyIds": ["openfootball_current"],
        "stream": "prospective_audit_snapshot",
    }
    encoded_body = canonical_json_bytes(attestation)
    encoded = base64.urlsafe_b64encode(encoded_body).decode("ascii").rstrip("=")
    signature = hmac.new(secret, encoded_body, hashlib.sha256).hexdigest()
    return {
        "X-Matchline-Producer-Attestation": encoded,
        "X-Matchline-Producer-Signature": f"v1={signature}",
    }


def validate_prospective_audit_ack(
    payload: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    expected = {
        "status",
        "created",
        "auditId",
        "snapshotSha256",
        "modelVersionSha256",
        "currentFreezeCount",
        "scoredN",
        "pendingN",
    }
    model = payload.get("model")
    capture = payload.get("capture")
    evaluation = payload.get("evaluation")
    valid = (
        isinstance(response, Mapping)
        and set(response) == expected
        and response.get("status") == "ok"
        and response.get("created") in {0, 1}
        and isinstance(response.get("auditId"), int)
        and not isinstance(response.get("auditId"), bool)
        and int(response["auditId"]) > 0
        and response.get("snapshotSha256") == _sha256(canonical_json_bytes(payload))
        and isinstance(model, Mapping)
        and response.get("modelVersionSha256") == model.get("versionSha256")
        and isinstance(capture, Mapping)
        and response.get("currentFreezeCount") == capture.get("currentFreezeCount")
        and isinstance(evaluation, Mapping)
        and response.get("scoredN") == evaluation.get("scoredN")
        and response.get("pendingN") == evaluation.get("pendingN")
    )
    if not valid:
        raise ValueError("prospective audit response contract is invalid")
    return dict(response)


def publish_prospective_audit_snapshot(
    *,
    lock_path: Path | str,
    cycle_path: Path | str,
    evaluation_path: Path | str,
    live_snapshot_path: Path | str,
    prediction_archive_path: Path | str,
    endpoint: str,
    token: str,
    key_id: str,
    signing_secret: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    sender: AuditSender | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload = build_prospective_audit_snapshot(
        lock_path=lock_path,
        cycle_path=cycle_path,
        evaluation_path=evaluation_path,
        live_snapshot_path=live_snapshot_path,
        prediction_archive_path=prediction_archive_path,
    )
    body = canonical_json_bytes(payload)
    model = payload["model"]
    capture = payload["capture"]
    evaluation = payload["evaluation"]
    summary = {
        "status": "dry_run" if dry_run else "ok",
        "requests": 0,
        "snapshotSha256": _sha256(body),
        "modelVersionSha256": model["versionSha256"],
        "currentFreezeCount": capture["currentFreezeCount"],
        "scoredN": evaluation["scoredN"],
        "pendingN": evaluation["pendingN"],
        "productionAllowed": False,
    }
    if dry_run:
        return summary
    if not token:
        raise ValueError("prospective audit ingest token is required")
    normalized_endpoint = _normalized_endpoint(endpoint)
    headers = prospective_audit_attestation_headers(
        body,
        endpoint=normalized_endpoint,
        key_id=key_id,
        signing_secret=signing_secret,
        payload=payload,
    )
    send = sender or _send_raw_upload
    response = send(
        endpoint=normalized_endpoint,
        body=body,
        headers=headers,
        token=token,
        timeout=timeout,
    )
    ack = validate_prospective_audit_ack(payload, response)
    return {**summary, **ack, "requests": 1, "productionAllowed": False}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--cycle", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--live-snapshot", type=Path, required=True)
    parser.add_argument("--prediction-archive", type=Path, required=True)
    parser.add_argument("--endpoint", default=os.environ.get(PROSPECTIVE_AUDIT_ENDPOINT_ENV))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    endpoint = (args.endpoint or "").strip()
    if not endpoint:
        raise ValueError(f"--endpoint or {PROSPECTIVE_AUDIT_ENDPOINT_ENV} is required")
    result = publish_prospective_audit_snapshot(
        lock_path=args.lock,
        cycle_path=args.cycle,
        evaluation_path=args.evaluation,
        live_snapshot_path=args.live_snapshot,
        prediction_archive_path=args.prediction_archive,
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
    "PROSPECTIVE_AUDIT_ATTESTATION_SCHEMA",
    "PROSPECTIVE_AUDIT_SCHEMA",
    "build_prospective_audit_snapshot",
    "canonical_json_bytes",
    "main",
    "prospective_audit_attestation_headers",
    "publish_prospective_audit_snapshot",
    "validate_prospective_audit_ack",
]
