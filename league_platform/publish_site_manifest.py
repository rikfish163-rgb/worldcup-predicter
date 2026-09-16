"""Commit one complete producer publication epoch to the Sites D1 ledger.

The individual fixture, intelligence and forecast endpoints are append-only.
This final signed request is the commit marker consumed by the public read
model; it is never sent when an earlier phase is partial or blocked.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
from typing import Any

from league_platform.publish_intelligence import (
    PublicationMaturityContext,
    ProducerAttestationContext,
    build_producer_attestation_context,
    publish_payload,
    require_publication_maturity,
    resolve_publication_epoch,
)


SITE_PUBLICATION_SCHEMA = "matchline.site_publication.v1"
SOURCE_POLICY_IDS = ("openfootball_current",)
INDEPENDENT_VERIFIER_PROOF_SCHEMA = "matchline.independent_verifier_proof.v1"
INDEPENDENT_VERIFIER_NAME = "matchline-independent-verifier"
INDEPENDENT_VERIFIER_VERSION = "v260"
INDEPENDENT_VERIFIER_STREAM = "publication_manifest"
INDEPENDENT_VERIFIER_RIGHTS_USE_CASE = "audit_only"
INDEPENDENT_VERIFIER_KEY_ID_ENV = "MATCHLINE_INDEPENDENT_VERIFIER_KEY_ID"
INDEPENDENT_VERIFIER_SIGNING_SECRET_ENV = "MATCHLINE_INDEPENDENT_VERIFIER_SIGNING_SECRET"
INDEPENDENT_VERIFIER_TTL_SECONDS = 240
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_digest(value: object, *, name: str) -> str:
    normalized = value.lower() if isinstance(value, str) else ""
    if not _SHA256_RE.fullmatch(normalized) or normalized == "0" * 64:
        raise ValueError(f"{name} must be a non-placeholder SHA-256 digest")
    return normalized


def _normalise_source_policy_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() or item != item.strip()
        for item in value
    ):
        raise ValueError("independent verifier source policy ids are malformed")
    source_ids = list(value)
    if source_ids != sorted(set(source_ids)) or tuple(source_ids) != SOURCE_POLICY_IDS:
        raise ValueError("independent verifier source policy ids are not the closed inventory")
    return source_ids


def _independent_verifier_admission_identity(
    payload: Mapping[str, Any],
    *,
    manifest_sha256: str,
    producer: ProducerAttestationContext,
) -> dict[str, Any]:
    """Return the exact identity object hashed by the Sites verifier.

    Keep this flat and closed: every value is reconstructed from the validated
    manifest or the producer attestation that was just signed for this request.
    """

    source_policy_ids = _normalise_source_policy_ids(payload.get("sourcePolicyIds"))
    if payload.get("schemaVersion") != SITE_PUBLICATION_SCHEMA:
        raise ValueError("independent verifier payload schema is invalid")
    if payload.get("status") != "complete":
        raise ValueError("independent verifier proof requires a complete payload")
    producer_key_id = producer.key_id.strip() if isinstance(producer.key_id, str) else ""
    if not producer_key_id or len(producer_key_id) > 120:
        raise ValueError("independent verifier producer key id is invalid")
    maturity_sha = _require_digest(
        producer.maturity_receipt_raw_sha256,
        name="independent verifier maturity receipt digest",
    )
    live_sha = _require_digest(
        producer.live_snapshot_raw_sha256,
        name="independent verifier live snapshot digest",
    )
    return {
        "schemaVersion": payload["schemaVersion"],
        "publicationEpoch": payload["publicationEpoch"],
        "status": payload["status"],
        "sourceAsOf": payload["sourceAsOf"],
        "modelVersion": payload["modelVersion"],
        "modelLockSha256": _require_digest(
            payload.get("modelLockSha256"),
            name="independent verifier model lock digest",
        ),
        "sourcePolicyIds": source_policy_ids,
        "counts": dict(payload["counts"]),
        "phases": {
            str(key): dict(value) if isinstance(value, Mapping) else value
            for key, value in dict(payload["phases"]).items()
        },
        "manifestSha256": _require_digest(
            manifest_sha256,
            name="independent verifier manifest digest",
        ),
        "producerKeyId": producer_key_id,
        "maturityReceiptRawSha256": maturity_sha,
        "liveSnapshotRawSha256": live_sha,
        "stream": INDEPENDENT_VERIFIER_STREAM,
        "rightsUseCase": INDEPENDENT_VERIFIER_RIGHTS_USE_CASE,
    }


def _independent_verifier_rights_identity(source_policy_ids: list[str]) -> dict[str, Any]:
    return {
        "policyVersion": "v260",
        "sourcePolicyIds": list(source_policy_ids),
        "stream": INDEPENDENT_VERIFIER_STREAM,
        "rightsUseCase": INDEPENDENT_VERIFIER_RIGHTS_USE_CASE,
    }


def _independent_verifier_identity() -> dict[str, str]:
    return {
        "schemaVersion": INDEPENDENT_VERIFIER_PROOF_SCHEMA,
        "name": INDEPENDENT_VERIFIER_NAME,
        "version": INDEPENDENT_VERIFIER_VERSION,
    }


def build_independent_verifier_proof_headers(
    payload: Mapping[str, Any],
    *,
    endpoint: str,
    producer_attestation: ProducerAttestationContext,
    key_id: str | None = None,
    signing_secret: str | None = None,
    issued_at: datetime | None = None,
) -> dict[str, str]:
    """Build the separately keyed proof headers required for a complete write."""

    resolved_key_id = (
        key_id if key_id is not None else os.environ.get(INDEPENDENT_VERIFIER_KEY_ID_ENV, "")
    ).strip()
    resolved_secret = (
        signing_secret
        if signing_secret is not None
        else os.environ.get(INDEPENDENT_VERIFIER_SIGNING_SECRET_ENV, "")
    )
    if (
        not resolved_key_id
        or len(resolved_key_id) > 120
        or len(resolved_secret.encode("utf-8")) < 32
        or len(resolved_secret.encode("utf-8")) > 4_096
    ):
        raise ValueError("independent verifier signing identity is required outside dry-run")
    producer_key_id = (
        producer_attestation.key_id.strip()
        if isinstance(producer_attestation.key_id, str)
        else ""
    )
    if not producer_key_id or resolved_key_id == producer_key_id:
        raise ValueError("independent verifier key must differ from producer key")
    producer_secret = producer_attestation.signing_secret
    if not isinstance(producer_secret, bytes) or not producer_secret:
        raise ValueError("independent verifier producer secret binding is invalid")
    if hmac.compare_digest(
        resolved_secret.encode("utf-8"),
        producer_secret,
    ):
        raise ValueError("independent verifier secret must differ from producer secret")

    manifest_sha256 = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    admission_identity = _independent_verifier_admission_identity(
        payload,
        manifest_sha256=manifest_sha256,
        producer=producer_attestation,
    )
    source_policy_ids = _normalise_source_policy_ids(payload.get("sourcePolicyIds"))
    now = (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expires = (now + timedelta(seconds=INDEPENDENT_VERIFIER_TTL_SECONDS)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    proof = {
        "admissionSha256": hashlib.sha256(
            _canonical_json(admission_identity).encode("utf-8")
        ).hexdigest(),
        "destination": _normalised_destination(endpoint),
        "expiresAt": expires,
        "formalProbabilities": False,
        "issuedAt": issued,
        "keyId": resolved_key_id,
        "manifestSha256": manifest_sha256,
        "modelLockSha256": _require_digest(
            payload.get("modelLockSha256"),
            name="independent verifier model lock digest",
        ),
        "productionReady": False,
        "publicationEpoch": payload["publicationEpoch"],
        "rightsDigestSha256": hashlib.sha256(
            _canonical_json(_independent_verifier_rights_identity(source_policy_ids)).encode("utf-8")
        ).hexdigest(),
        "schemaVersion": INDEPENDENT_VERIFIER_PROOF_SCHEMA,
        "sourcePolicyIds": source_policy_ids,
        "status": "verified",
        "stream": INDEPENDENT_VERIFIER_STREAM,
        # The proof's nested verifier object intentionally omits the
        # schemaVersion; Sites expects the exact {name, version} shape.
        "verifier": {
            "name": INDEPENDENT_VERIFIER_NAME,
            "version": INDEPENDENT_VERIFIER_VERSION,
        },
        "verifierSha256": hashlib.sha256(
            _canonical_json(_independent_verifier_identity()).encode("utf-8")
        ).hexdigest(),
    }
    canonical = _canonical_json(proof).encode("utf-8")
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        resolved_secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Matchline-Independent-Verifier-Proof": encoded,
        "X-Matchline-Independent-Verifier-Signature": f"v1={signature}",
    }


def _normalised_destination(endpoint: str) -> str:
    """Match the Sites URL normalizer for HTTPS publication endpoints."""

    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(endpoint)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("independent verifier publication destination must be HTTPS")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("independent verifier publication destination has an invalid port") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunparse(("https", netloc, parsed.path or "/", "", parsed.query, ""))


def _non_negative_integer(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _records(result: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _reason(result: Mapping[str, Any], fallback: str) -> str:
    for key in ("reason", "error", "status"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:240]
    return fallback


def build_site_publication_payload(
    *,
    publication_epoch: str,
    source_as_of: str,
    model_version: str,
    model_lock_sha256: str,
    fixtures: Mapping[str, Any],
    intelligence: Mapping[str, Any],
    source_health: Mapping[str, Any],
    lineups: Mapping[str, Any],
    forecast: Mapping[str, Any],
    stage_evaluations: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the closed v260 manifest from already successful phase results."""

    epoch = resolve_publication_epoch(publication_epoch, dry_run=False)
    try:
        parsed_as_of = datetime.fromisoformat(source_as_of.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("site publication source_as_of is invalid") from exc
    if parsed_as_of.tzinfo is None or parsed_as_of.utcoffset() is None:
        raise ValueError("site publication source_as_of must include a timezone")
    source_as_of = (
        parsed_as_of.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    normalized_lock = model_lock_sha256.lower()
    if (
        len(normalized_lock) != 64
        or any(character not in "0123456789abcdef" for character in normalized_lock)
        or normalized_lock == "0" * 64
    ):
        raise ValueError("site publication model lock digest is invalid")
    if not model_version.endswith(f"@lock-{normalized_lock[:16]}"):
        raise ValueError("site publication model version is not lock-bound")

    fixture_count = _records(fixtures, "fixtures")
    intelligence_count = _records(intelligence, "rows")
    prediction_groups = _records(forecast, "groups")
    prediction_stages = _records(forecast, "stageRows")
    evaluation_rows = _records(stage_evaluations, "stageRows")
    source_health_records = _records(source_health, "published", "rows", "recordsSeen")
    lineup_records = _records(lineups, "published", "rows", "recordsSeen")

    source_health_degraded = source_health.get("status") == "degraded"
    lineup_degraded = lineups.get("status") == "degraded"
    return {
        "schemaVersion": SITE_PUBLICATION_SCHEMA,
        "publicationEpoch": epoch,
        "status": "complete",
        "sourceAsOf": source_as_of,
        "modelVersion": model_version,
        "modelLockSha256": normalized_lock,
        "sourcePolicyIds": list(SOURCE_POLICY_IDS),
        "counts": {
            "fixtures": fixture_count,
            "intelligenceObservations": intelligence_count,
            "predictionGroups": prediction_groups,
            "predictionStages": prediction_stages,
            "stageEvaluations": evaluation_rows,
        },
        "phases": {
            "fixtures": {
                "status": "complete",
                "records": fixture_count,
                "reason": None,
            },
            "intelligence": {
                "status": "complete",
                "records": intelligence_count,
                "reason": None,
            },
            "sourceHealth": {
                "status": "degraded" if source_health_degraded else "complete",
                "records": source_health_records,
                "reason": (
                    _reason(source_health, "source health coverage degraded")
                    if source_health_degraded
                    else None
                ),
            },
            "lineups": {
                "status": "degraded" if lineup_degraded else "complete",
                "records": lineup_records,
                "reason": (
                    _reason(lineups, "lineup coverage degraded") if lineup_degraded else None
                ),
            },
            "forecast": {
                "status": "complete" if prediction_groups else "not_applicable",
                "records": prediction_groups,
                "reason": None if prediction_groups else "no frozen prediction groups",
            },
            "stageEvaluations": {
                "status": "complete" if evaluation_rows else "not_applicable",
                "records": evaluation_rows,
                "reason": None if evaluation_rows else "no scored stage rows",
            },
        },
    }


def build_site_publication_barrier(
    *,
    publication_epoch: str,
    source_as_of: str,
    model_version: str,
    model_lock_sha256: str,
) -> dict[str, Any]:
    """Build the zero-row marker that hides D1 while an epoch is publishing."""

    payload = build_site_publication_payload(
        publication_epoch=publication_epoch,
        source_as_of=source_as_of,
        model_version=model_version,
        model_lock_sha256=model_lock_sha256,
        fixtures={},
        intelligence={},
        source_health={},
        lineups={},
        forecast={},
        stage_evaluations={},
    )
    payload["status"] = "publishing"
    payload["counts"] = {key: 0 for key in payload["counts"]}
    payload["phases"] = {
        key: {
            "status": "pending",
            "records": 0,
            "reason": "publication in progress",
        }
        for key in payload["phases"]
    }
    return payload


def _validate_response(
    response: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
) -> None:
    expected_digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if (
        response.get("status") != "ok"
        or response.get("accepted") != 1
        or response.get("created") not in {0, 1}
        or response.get("publicationEpoch") != payload.get("publicationEpoch")
        or response.get("manifestSha256") != expected_digest
        or isinstance(response.get("publicationId"), bool)
        or not isinstance(response.get("publicationId"), int)
        or int(response["publicationId"]) <= 0
    ):
        raise ValueError("site publication response acknowledgement mismatch")


def publish_site_manifest(
    payload: Mapping[str, Any],
    *,
    endpoint: str,
    token: str,
    publication_epoch: str,
    maturity_context: PublicationMaturityContext | None,
    dry_run: bool,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
    independent_verifier_key_id: str | None = None,
    independent_verifier_signing_secret: str | None = None,
    publish_payload_fn: Callable[..., dict] = publish_payload,
) -> dict[str, Any]:
    """Publish the final commit marker and require an exact idempotent ACK."""

    epoch = resolve_publication_epoch(publication_epoch, dry_run=dry_run)
    if payload.get("publicationEpoch") != epoch:
        raise ValueError("site publication epoch does not match the cycle")
    maturity = require_publication_maturity(maturity_context, dry_run=dry_run)
    if dry_run:
        return {
            "status": "dry_run",
            "published": 0,
            "requests": 0,
            "manifestSha256": hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
        }
    producer_attestation = build_producer_attestation_context(
        maturity_context=maturity_context,
        maturity_verification=maturity,
        stream="publication_manifest",
        rights_use_case="audit_only",
        source_policy_ids=SOURCE_POLICY_IDS,
        publication_epoch=epoch,
        dry_run=False,
    )
    if producer_attestation is None:
        raise ValueError("producer attestation is required outside dry-run")
    independent_headers = (
        build_independent_verifier_proof_headers(
            payload,
            endpoint=endpoint,
            producer_attestation=producer_attestation,
            key_id=independent_verifier_key_id,
            signing_secret=independent_verifier_signing_secret,
        )
        if payload.get("status") == "complete"
        else None
    )
    publish_kwargs: dict[str, Any] = {
        "endpoint": endpoint,
        "token": token,
        "timeout": timeout,
        "max_retries": max_retries,
        "retry_base_seconds": retry_base_seconds,
        "error_label": "site publication",
        "producer_attestation": producer_attestation,
    }
    if independent_headers is not None:
        publish_kwargs["independent_verifier_headers"] = independent_headers
    response = publish_payload_fn(
        dict(payload),
        **publish_kwargs,
    )
    if not isinstance(response, Mapping):
        raise ValueError("site publication response must be a JSON object")
    _validate_response(response, payload=payload)
    return {
        "status": "ok",
        "published": 1,
        "requests": 1,
        "publicationId": response["publicationId"],
        "publicationEpoch": response["publicationEpoch"],
        "manifestSha256": response["manifestSha256"],
        "created": response["created"],
    }


__all__ = [
    "INDEPENDENT_VERIFIER_KEY_ID_ENV",
    "INDEPENDENT_VERIFIER_PROOF_SCHEMA",
    "INDEPENDENT_VERIFIER_SIGNING_SECRET_ENV",
    "SITE_PUBLICATION_SCHEMA",
    "build_independent_verifier_proof_headers",
    "build_site_publication_barrier",
    "build_site_publication_payload",
    "publish_site_manifest",
]
