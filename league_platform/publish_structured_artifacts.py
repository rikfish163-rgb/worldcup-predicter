"""Build probability-free, content-addressed research artifacts for Sites.

The public ``research_audit`` lane accepts five closed summary schemas.  Wire
documents contain content identities and rights/admission gates, never local
producer paths or credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from league_platform.publish_raw_artifacts import (
    PRODUCER_NAME,
    PRODUCER_VERSION,
    _normalized_endpoint,
)


UPLOAD_SCHEMA = "matchline.sites_blob_upload.v1"
ATTESTATION_SCHEMA = "matchline.sites_blob_attestation.v1"
RESEARCH_SUMMARY_SCHEMA = "matchline.research_artifact_summary.v1"
ATTESTATION_TTL_SECONDS = 300
MAX_STRUCTURED_CONTENT_BYTES = 8 * 1024 * 1024
SOURCE_POLICY_IDS = ("openfootball_current",)

Visibility = Literal["private_evidence", "research_audit", "publication_member"]
PRIVATE_SYSTEM_SCHEMA = "matchline.private_system_evidence.v1"
PRIVATE_SYSTEM_KINDS = {
    "cycle_evidence",
    "evaluation_evidence",
    "strict_evidence",
}

RESEARCH_ARTIFACT_METRICS: dict[str, tuple[str, ...]] = {
    "prospective_cycle": (
        "startedAt",
        "finishedAt",
        "asOf",
        "fixtureCount",
        "currentFreezeCount",
        "blockedCount",
    ),
    "prospective_evaluation": (
        "scoredN",
        "pendingN",
        "resultConflicts",
        "sampleRequirementsMet",
        "allRequiredTargetsScored",
        "predictionFreezesVerified",
        "promotionEligible",
        "productionAllowed",
    ),
    "strict_report": (
        "evaluatedAt",
        "sampleN",
        "failureCount",
        "reportSha256",
        "productionAllowed",
    ),
    "source_health": (
        "sourceCount",
        "healthyCount",
        "degradedCount",
        "blockedCount",
    ),
    "read_model_summary": (
        "asOf",
        "fixtureCount",
        "competitionCount",
        "sourceCount",
        "staleSourceCount",
    ),
}
_SUMMARY_KEYS = {
    "schemaVersion",
    "artifactKind",
    "researchOnly",
    "live",
    "productionReady",
    "observedAt",
    "status",
    "evidenceSha256",
    "reasonCodes",
    "metrics",
}
_UPLOAD_KEYS = {
    "schemaVersion",
    "visibility",
    "artifactKind",
    "batchId",
    "batchManifestSha256",
    "objectKey",
    "contentType",
    "contentSha256",
    "sizeBytes",
    "base64",
    "observedAt",
    "sourceIds",
    "sourcePolicyIds",
    "rightsUseCase",
    "archiveManifestSha256",
    "admissionSha256",
    "publicationEpoch",
    "publicationManifestSha256",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_STATUS_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,119}\Z")
_REASON_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,159}\Z")
_SOURCE_ID_RE = re.compile(r"openfootball:[a-zA-Z0-9._:/-]{1,190}\Z")
_LOCAL_PATH_RE = re.compile(r"(?:^|[\s'\"])/(?:home|root|dev/shm|tmp|media|mnt|var|etc)(?:/|\b)")
_SECRET_VALUE_RE = re.compile(
    r"(?:\bBearer\s+\S+|\b(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{12,}|"
    r"MATCHLINE_[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD)\s*=)",
    re.IGNORECASE,
)
_SECRET_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "bearertoken",
    "credential",
    "credentials",
    "password",
    "privatekey",
    "secret",
    "signingsecret",
    "token",
}
_PRIVATE_KEYS = {
    "schemaVersion",
    "artifactKind",
    "observedAt",
    "evidenceSha256",
    "systemGenerated",
    "candidateArchiveIncluded",
    "evidence",
}


def canonical_json_bytes(value: object) -> bytes:
    """Return canonical UTF-8 JSON bytes shared with the Sites verifier."""

    def normalize_numbers(item: object) -> object:
        # JSON.parse turns integral JSON numbers into JavaScript Numbers and
        # JSON.stringify emits them without a trailing ``.0``.  Python keeps
        # that distinction for floats, so normalize the safe integral subset
        # before serializing the shared wire representation.
        if isinstance(item, float):
            if math.isfinite(item) and item.is_integer() and abs(item) <= 2**53 - 1:
                return int(item)
            return item
        if isinstance(item, Mapping):
            return {key: normalize_numbers(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize_numbers(child) for child in item]
        if isinstance(item, tuple):
            return [normalize_numbers(child) for child in item]
        return item

    try:
        return json.dumps(
            normalize_numbers(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("structured artifact is not canonical JSON") from exc


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value) or value == "0" * 64:
        raise ValueError(f"{field} must be a non-zero lowercase SHA-256")
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


def _status(value: object, *, fallback: str = "unknown") -> str:
    if isinstance(value, str):
        normalized = value.strip().lower().replace(" ", "_")
        if _STATUS_RE.fullmatch(normalized):
            return normalized
    return fallback


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _validate_research_summary(content: Mapping[str, Any], *, artifact_kind: str) -> str:
    if set(content) != _SUMMARY_KEYS:
        raise ValueError("research artifact does not match its closed schema")
    if (
        content.get("schemaVersion") != RESEARCH_SUMMARY_SCHEMA
        or content.get("artifactKind") != artifact_kind
        or artifact_kind not in RESEARCH_ARTIFACT_METRICS
        or content.get("researchOnly") is not True
        or content.get("live") is not False
        or content.get("productionReady") is not False
    ):
        raise ValueError("research artifact does not match its closed schema")
    observed_at = _iso(content.get("observedAt"), field="observedAt")
    if observed_at != content.get("observedAt"):
        raise ValueError("research artifact does not match its closed schema")
    status = content.get("status")
    if not isinstance(status, str) or not _STATUS_RE.fullmatch(status):
        raise ValueError("research artifact does not match its closed schema")
    _digest(content.get("evidenceSha256"), field="evidenceSha256")
    reasons = content.get("reasonCodes")
    if (
        not isinstance(reasons, list)
        or len(reasons) > 64
        or reasons != sorted(set(reasons))
        or any(
            not isinstance(reason, str) or not _REASON_RE.fullmatch(reason) for reason in reasons
        )
    ):
        raise ValueError("research artifact does not match its closed schema")
    metrics = content.get("metrics")
    expected_metrics = RESEARCH_ARTIFACT_METRICS[artifact_kind]
    if not isinstance(metrics, Mapping) or set(metrics) != set(expected_metrics):
        raise ValueError("research artifact does not match its closed schema")

    timestamp_fields = {
        "prospective_cycle": {"startedAt", "finishedAt", "asOf"},
        "strict_report": {"evaluatedAt"},
        "read_model_summary": {"asOf"},
    }.get(artifact_kind, set())
    digest_fields = {"reportSha256"} if artifact_kind == "strict_report" else set()
    boolean_fields = {
        "sampleRequirementsMet",
        "allRequiredTargetsScored",
        "predictionFreezesVerified",
        "promotionEligible",
        "productionAllowed",
    }
    for field in expected_metrics:
        value = metrics.get(field)
        if field in timestamp_fields:
            if _iso(value, field=f"metrics.{field}") != value:
                raise ValueError("research artifact does not match its closed schema")
        elif field in digest_fields:
            _digest(value, field=f"metrics.{field}")
        elif field in boolean_fields:
            _boolean(value, field=f"metrics.{field}")
        else:
            _count(value, field=f"metrics.{field}")
    if metrics.get("productionAllowed") is True:
        raise ValueError("research artifact does not match its closed schema")
    if artifact_kind == "strict_report" and metrics.get("reportSha256") != content.get(
        "evidenceSha256"
    ):
        raise ValueError("research artifact does not match its closed schema")
    return observed_at


def _summary(
    *,
    artifact_kind: str,
    observed_at: object,
    status: object,
    evidence_sha256: object,
    reason_codes: Sequence[str],
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "schemaVersion": RESEARCH_SUMMARY_SCHEMA,
        "artifactKind": artifact_kind,
        "researchOnly": True,
        "live": False,
        "productionReady": False,
        "observedAt": _iso(observed_at, field=f"{artifact_kind}.observedAt"),
        "status": _status(status),
        "evidenceSha256": _digest(
            evidence_sha256,
            field=f"{artifact_kind}.evidenceSha256",
        ),
        "reasonCodes": sorted(set(reason_codes)),
        "metrics": dict(metrics),
    }
    _validate_research_summary(value, artifact_kind=artifact_kind)
    return value


def build_research_artifact_summaries(
    *,
    prospective_audit: Mapping[str, Any],
    strict_report: Mapping[str, Any],
    strict_report_sha256: str,
    offline_snapshot: Mapping[str, Any],
    offline_snapshot_sha256: str,
) -> list[dict[str, Any]]:
    """Project local evidence into the five frozen probability-free summaries."""

    model = _mapping(prospective_audit.get("model"))
    artifacts = _mapping(prospective_audit.get("artifacts"))
    cycle = _mapping(prospective_audit.get("cycle"))
    capture = _mapping(prospective_audit.get("capture"))
    evaluation = _mapping(prospective_audit.get("evaluation"))
    cycle_status = _status(cycle.get("status"))
    model_status = _status(model.get("status"))
    cycle_reasons = [] if cycle_status == "passed" else [f"cycle_status:{cycle_status}"]
    evaluation_reasons = [
        code
        for field, code in (
            ("sampleRequirementsMet", "sample_requirements_not_met"),
            ("allRequiredTargetsScored", "required_targets_not_scored"),
            ("predictionFreezesVerified", "prediction_freezes_unverified"),
            ("promotionEligible", "promotion_not_eligible"),
        )
        if evaluation.get(field) is not True
    ]

    if evaluation.get("productionAllowed") is not True:
        evaluation_reasons.append("production_not_allowed")

    strict_status = _status(strict_report.get("status"))
    strict_failures = strict_report.get("failures")
    if not isinstance(strict_failures, list):
        strict_failures = strict_report.get("errors")
    failure_count = len(strict_failures) if isinstance(strict_failures, list) else 0
    if strict_status not in {"passed", "pass", "ok"} and failure_count == 0:
        failure_count = 1
    strict_combined = _mapping(strict_report.get("combined"))
    strict_overall = _mapping(strict_report.get("overall"))
    sample_n = strict_combined.get("sample_n", strict_overall.get("sample_n", 0))
    evaluated_at = strict_report.get(
        "generated_at",
        strict_report.get("evaluated_at", cycle.get("finishedAt")),
    )

    current_data = _mapping(offline_snapshot.get("current_data"))
    registry = _rows(current_data.get("source_registry"))
    healthy = 0
    degraded = 0
    blocked = 0
    for source in registry:
        runtime = _mapping(source.get("runtime"))
        source_status = _status(runtime.get("status", source.get("status")))
        rights_status = _status(
            runtime.get("rights_status", source.get("rights_status")),
            fallback="",
        )
        if source_status in {
            "rights_blocked",
            "blocked",
            "forbidden",
            "unknown",
        } or rights_status in {"rights_blocked", "blocked", "forbidden", "unknown"}:
            blocked += 1
        elif source_status in {"fresh", "ok", "available"}:
            healthy += 1
        else:
            degraded += 1
    source_count = len(registry)
    stale_source_count = source_count - healthy
    source_status = "blocked" if blocked else "degraded" if degraded else "ok"
    source_reasons = []
    if blocked:
        source_reasons.append("source_rights_blocked")
    if degraded:
        source_reasons.append("source_health_degraded")

    matches = _rows(offline_snapshot.get("matches"))
    competitions = _rows(offline_snapshot.get("competitions"))
    offline_as_of = offline_snapshot.get("as_of")
    read_status = "degraded" if stale_source_count else "ok"
    read_reasons = ["stale_or_unavailable_sources"] if stale_source_count else []

    return [
        _summary(
            artifact_kind="prospective_cycle",
            observed_at=cycle.get("asOf"),
            status=cycle_status,
            evidence_sha256=artifacts.get("cycleRawSha256"),
            reason_codes=cycle_reasons,
            metrics={
                "startedAt": _iso(cycle.get("startedAt"), field="cycle.startedAt"),
                "finishedAt": _iso(cycle.get("finishedAt"), field="cycle.finishedAt"),
                "asOf": _iso(cycle.get("asOf"), field="cycle.asOf"),
                "fixtureCount": _count(cycle.get("fixtureCount"), field="cycle.fixtureCount"),
                "currentFreezeCount": _count(
                    capture.get("currentFreezeCount"),
                    field="capture.currentFreezeCount",
                ),
                "blockedCount": _count(capture.get("blocked"), field="capture.blocked"),
            },
        ),
        _summary(
            artifact_kind="prospective_evaluation",
            observed_at=cycle.get("finishedAt"),
            status=model_status,
            evidence_sha256=artifacts.get("evaluationRawSha256"),
            reason_codes=evaluation_reasons,
            metrics={
                "scoredN": _count(evaluation.get("scoredN"), field="evaluation.scoredN"),
                "pendingN": _count(evaluation.get("pendingN"), field="evaluation.pendingN"),
                "resultConflicts": _count(
                    evaluation.get("resultConflicts"),
                    field="evaluation.resultConflicts",
                ),
                "sampleRequirementsMet": _boolean(
                    evaluation.get("sampleRequirementsMet"),
                    field="evaluation.sampleRequirementsMet",
                ),
                "allRequiredTargetsScored": _boolean(
                    evaluation.get("allRequiredTargetsScored"),
                    field="evaluation.allRequiredTargetsScored",
                ),
                "predictionFreezesVerified": _boolean(
                    evaluation.get("predictionFreezesVerified"),
                    field="evaluation.predictionFreezesVerified",
                ),
                "promotionEligible": _boolean(
                    evaluation.get("promotionEligible"),
                    field="evaluation.promotionEligible",
                ),
                "productionAllowed": False,
            },
        ),
        _summary(
            artifact_kind="strict_report",
            observed_at=evaluated_at,
            status=strict_status,
            evidence_sha256=strict_report_sha256,
            reason_codes=[] if failure_count == 0 else ["strict_report_failures_present"],
            metrics={
                "evaluatedAt": _iso(evaluated_at, field="strict_report.evaluatedAt"),
                "sampleN": _count(sample_n, field="strict_report.sampleN"),
                "failureCount": failure_count,
                "reportSha256": _digest(
                    strict_report_sha256,
                    field="strict_report.reportSha256",
                ),
                "productionAllowed": False,
            },
        ),
        _summary(
            artifact_kind="source_health",
            observed_at=offline_as_of,
            status=source_status,
            evidence_sha256=offline_snapshot_sha256,
            reason_codes=source_reasons,
            metrics={
                "sourceCount": source_count,
                "healthyCount": healthy,
                "degradedCount": degraded,
                "blockedCount": blocked,
            },
        ),
        _summary(
            artifact_kind="read_model_summary",
            observed_at=offline_as_of,
            status=read_status,
            evidence_sha256=offline_snapshot_sha256,
            reason_codes=read_reasons,
            metrics={
                "asOf": _iso(offline_as_of, field="offline_snapshot.asOf"),
                "fixtureCount": len(matches),
                "competitionCount": len(competitions),
                "sourceCount": source_count,
                "staleSourceCount": stale_source_count,
            },
        ),
    ]


def _sanitise_private_value(
    value: object,
    *,
    path: str = "evidence",
    depth: int = 0,
    budget: list[int] | None = None,
) -> object:
    """Copy JSON evidence while redacting paths and rejecting credentials."""

    counter = budget if budget is not None else [0]
    counter[0] += 1
    if counter[0] > 200_000 or depth > 64:
        raise ValueError("private system evidence is too complex")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, str):
        if _SECRET_VALUE_RE.search(value):
            raise ValueError(f"{path} contains credential-like material")
        if _LOCAL_PATH_RE.search(value):
            return {"redactedLocalPathSha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
        return value
    if isinstance(value, list):
        return [
            _sanitise_private_value(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                budget=counter,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 240:
                raise ValueError(f"{path} contains an invalid key")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _SECRET_KEYS:
                raise ValueError(f"{path}.{key} contains a credential field")
            result[key] = _sanitise_private_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                budget=counter,
            )
        return result
    raise ValueError(f"{path} contains a non-JSON value")


def _private_envelope(
    *,
    artifact_kind: str,
    observed_at: object,
    evidence_sha256: object,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "schemaVersion": PRIVATE_SYSTEM_SCHEMA,
        "artifactKind": artifact_kind,
        "observedAt": _iso(observed_at, field=f"{artifact_kind}.observedAt"),
        "evidenceSha256": _digest(
            evidence_sha256,
            field=f"{artifact_kind}.evidenceSha256",
        ),
        "systemGenerated": True,
        "candidateArchiveIncluded": False,
        "evidence": _sanitise_private_value(evidence),
    }
    _validate_private_system_evidence(value, artifact_kind=artifact_kind)
    return value


def build_private_system_evidence(
    *,
    prospective_audit: Mapping[str, Any],
    strict_report: Mapping[str, Any],
    strict_report_sha256: str,
) -> list[dict[str, Any]]:
    """Build three private system artifacts; candidate archives are excluded."""

    artifacts = _mapping(prospective_audit.get("artifacts"))
    observed_at = prospective_audit.get("generatedAt")
    cycle_keys = (
        "schemaVersion",
        "generatedAt",
        "sourcePolicyIds",
        "model",
        "artifacts",
        "cycle",
        "capture",
        "nextFreezes",
    )
    evaluation_keys = (
        "schemaVersion",
        "generatedAt",
        "sourcePolicyIds",
        "model",
        "artifacts",
        "evaluation",
    )
    cycle_evidence = {
        key: prospective_audit.get(key) for key in cycle_keys if key in prospective_audit
    }
    evaluation_evidence = {
        key: prospective_audit.get(key) for key in evaluation_keys if key in prospective_audit
    }
    strict_observed_at = strict_report.get(
        "generated_at",
        strict_report.get("evaluated_at", observed_at),
    )
    return [
        _private_envelope(
            artifact_kind="cycle_evidence",
            observed_at=observed_at,
            evidence_sha256=artifacts.get("cycleRawSha256"),
            evidence=cycle_evidence,
        ),
        _private_envelope(
            artifact_kind="evaluation_evidence",
            observed_at=observed_at,
            evidence_sha256=artifacts.get("evaluationRawSha256"),
            evidence=evaluation_evidence,
        ),
        _private_envelope(
            artifact_kind="strict_evidence",
            observed_at=strict_observed_at,
            evidence_sha256=strict_report_sha256,
            evidence=strict_report,
        ),
    ]


def _validate_private_system_evidence(content: Mapping[str, Any], *, artifact_kind: str) -> str:
    if (
        set(content) != _PRIVATE_KEYS
        or content.get("schemaVersion") != PRIVATE_SYSTEM_SCHEMA
        or content.get("artifactKind") != artifact_kind
        or artifact_kind not in PRIVATE_SYSTEM_KINDS
        or content.get("systemGenerated") is not True
        or content.get("candidateArchiveIncluded") is not False
        or not isinstance(content.get("evidence"), Mapping)
    ):
        raise ValueError("private system evidence does not match its closed schema")
    observed_at = _iso(content.get("observedAt"), field="observedAt")
    if observed_at != content.get("observedAt"):
        raise ValueError("private system evidence does not match its closed schema")
    _digest(content.get("evidenceSha256"), field="evidenceSha256")
    encoded = canonical_json_bytes(content).decode("utf-8")
    if _LOCAL_PATH_RE.search(encoded) or _SECRET_VALUE_RE.search(encoded):
        raise ValueError("private system evidence contains forbidden producer data")
    return observed_at


def _source_ids(values: Sequence[str]) -> list[str]:
    normalized = sorted(set(values))
    if (
        not normalized
        or len(normalized) > 256
        or any(
            not isinstance(value, str) or not _SOURCE_ID_RE.fullmatch(value)
            for value in normalized
        )
    ):
        raise ValueError("structured artifact sourceIds are invalid")
    return normalized


def build_structured_artifact_upload(
    content: Mapping[str, Any],
    *,
    visibility: Visibility,
    artifact_kind: str,
    batch_manifest_sha256: str,
    archive_manifest_sha256: str,
    admission_sha256: str,
    source_ids: Sequence[str],
    publication_epoch: str | None = None,
    publication_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Build one flat v1 research/private upload after closed preflight."""

    if visibility == "publication_member":
        raise ValueError("publication artifact independent verifier unavailable")
    if visibility == "private_evidence" and artifact_kind == "candidate_prediction_archive":
        raise ValueError("candidate prediction archive upload requires explicit opt-in")
    if visibility == "research_audit":
        observed_at = _validate_research_summary(content, artifact_kind=artifact_kind)
    elif visibility == "private_evidence":
        observed_at = _validate_private_system_evidence(
            content,
            artifact_kind=artifact_kind,
        )
    else:
        raise ValueError("structured artifact visibility is invalid")
    if publication_epoch is not None or publication_manifest_sha256 is not None:
        raise ValueError("non-public artifact publication fields must be null")
    batch_sha = _digest(batch_manifest_sha256, field="batchManifestSha256")
    archive_sha = _digest(archive_manifest_sha256, field="archiveManifestSha256")
    admission_sha = _digest(admission_sha256, field="admissionSha256")
    content_bytes = canonical_json_bytes(content)
    if not content_bytes or len(content_bytes) > MAX_STRUCTURED_CONTENT_BYTES:
        raise ValueError("structured artifact content exceeds its size limit")
    content_sha = hashlib.sha256(content_bytes).hexdigest()
    request = {
        "schemaVersion": UPLOAD_SCHEMA,
        "visibility": visibility,
        "artifactKind": artifact_kind,
        "batchId": f"sites-sync-{batch_sha[:24]}",
        "batchManifestSha256": batch_sha,
        "objectKey": (
            f"artifacts/{visibility}/{artifact_kind}/sha256/{content_sha[:2]}/{content_sha}.json"
        ),
        "contentType": "application/json",
        "contentSha256": content_sha,
        "sizeBytes": len(content_bytes),
        "base64": base64.b64encode(content_bytes).decode("ascii"),
        "observedAt": observed_at,
        "sourceIds": _source_ids(source_ids),
        "sourcePolicyIds": list(SOURCE_POLICY_IDS),
        "rightsUseCase": "audit_only",
        "archiveManifestSha256": archive_sha,
        "admissionSha256": admission_sha,
        "publicationEpoch": None,
        "publicationManifestSha256": None,
    }
    if set(request) != _UPLOAD_KEYS:  # pragma: no cover - construction invariant
        raise AssertionError("structured artifact upload schema drifted")
    return request


def structured_artifact_attestation_headers(
    body: bytes,
    *,
    endpoint: str,
    key_id: str,
    signing_secret: str,
    upload: Mapping[str, Any],
    issued_at: datetime | None = None,
) -> dict[str, str]:
    """Sign every flat routing and admission gate for one exact request body."""

    normalized_key = key_id.strip()
    secret = signing_secret.encode("utf-8")
    if not normalized_key or len(normalized_key) > 120 or not 32 <= len(secret) <= 4096:
        raise ValueError("structured artifact signing identity is invalid")
    if set(upload) != _UPLOAD_KEYS or upload.get("schemaVersion") != UPLOAD_SCHEMA:
        raise ValueError("structured artifact upload contract is invalid")
    now = (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    attestation = {
        "schemaVersion": ATTESTATION_SCHEMA,
        "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
        "keyId": normalized_key,
        "issuedAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=ATTESTATION_TTL_SECONDS))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "stream": "structured_artifact_snapshot",
        "visibility": upload.get("visibility"),
        "artifactKind": upload.get("artifactKind"),
        "rightsUseCase": upload.get("rightsUseCase"),
        "sourcePolicyIds": upload.get("sourcePolicyIds"),
        "batchManifestSha256": upload.get("batchManifestSha256"),
        "archiveManifestSha256": upload.get("archiveManifestSha256"),
        "publicationEpoch": upload.get("publicationEpoch"),
        "publicationManifestSha256": upload.get("publicationManifestSha256"),
        "admissionSha256": upload.get("admissionSha256"),
        "destination": _normalized_endpoint(endpoint),
        "bodySha256": hashlib.sha256(body).hexdigest(),
    }
    canonical = canonical_json_bytes(attestation)
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return {
        "X-Matchline-Producer-Attestation": encoded,
        "X-Matchline-Producer-Signature": f"v1={signature}",
    }


def validate_structured_artifact_ack(
    upload: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    """Require an exact content-addressed acknowledgement."""

    expected_keys = {
        "status",
        "objectKey",
        "contentSha256",
        "sizeBytes",
        "created",
    }
    if (
        not isinstance(response, Mapping)
        or set(response) != expected_keys
        or response.get("status") != "ok"
        or response.get("objectKey") != upload.get("objectKey")
        or response.get("contentSha256") != upload.get("contentSha256")
        or response.get("sizeBytes") != upload.get("sizeBytes")
        or response.get("created") not in {0, 1}
        or isinstance(response.get("created"), bool)
    ):
        raise ValueError("structured artifact response contract is invalid")
    return dict(response)


__all__ = [
    "ATTESTATION_SCHEMA",
    "RESEARCH_SUMMARY_SCHEMA",
    "UPLOAD_SCHEMA",
    "build_private_system_evidence",
    "build_research_artifact_summaries",
    "build_structured_artifact_upload",
    "canonical_json_bytes",
    "structured_artifact_attestation_headers",
    "validate_structured_artifact_ack",
]
