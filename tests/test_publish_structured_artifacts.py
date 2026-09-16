from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from league_platform import publish_structured_artifacts as publisher


ARCHIVE_SHA = "a" * 64
ADMISSION_SHA = "b" * 64
BATCH_SHA = "c" * 64


def _summary() -> dict[str, object]:
    return {
        "schemaVersion": "matchline.research_artifact_summary.v1",
        "artifactKind": "prospective_evaluation",
        "researchOnly": True,
        "live": False,
        "productionReady": False,
        "observedAt": "2026-08-28T02:00:00.000Z",
        "status": "pending_prospective_window",
        "evidenceSha256": "d" * 64,
        "reasonCodes": ["production_not_allowed", "sample_requirements_not_met"],
        "metrics": {
            "scoredN": 1,
            "pendingN": 8,
            "resultConflicts": 0,
            "sampleRequirementsMet": False,
            "allRequiredTargetsScored": False,
            "predictionFreezesVerified": True,
            "promotionEligible": False,
            "productionAllowed": False,
        },
    }


def test_research_summary_wire_is_flat_content_addressed_and_path_free() -> None:
    summary = _summary()
    request = publisher.build_structured_artifact_upload(
        summary,
        visibility="research_audit",
        artifact_kind="prospective_evaluation",
        batch_manifest_sha256=BATCH_SHA,
        archive_manifest_sha256=ARCHIVE_SHA,
        admission_sha256=ADMISSION_SHA,
        source_ids=("openfootball:football.json:2026-27:en.1",),
    )

    content = publisher.canonical_json_bytes(summary)
    content_sha = hashlib.sha256(content).hexdigest()
    assert request == {
        "schemaVersion": "matchline.sites_blob_upload.v1",
        "visibility": "research_audit",
        "artifactKind": "prospective_evaluation",
        "batchId": f"sites-sync-{BATCH_SHA[:24]}",
        "batchManifestSha256": BATCH_SHA,
        "objectKey": (
            "artifacts/research_audit/prospective_evaluation/sha256/"
            f"{content_sha[:2]}/{content_sha}.json"
        ),
        "contentType": "application/json",
        "contentSha256": content_sha,
        "sizeBytes": len(content),
        "base64": base64.b64encode(content).decode("ascii"),
        "observedAt": "2026-08-28T02:00:00.000Z",
        "sourceIds": ["openfootball:football.json:2026-27:en.1"],
        "sourcePolicyIds": ["openfootball_current"],
        "rightsUseCase": "audit_only",
        "archiveManifestSha256": ARCHIVE_SHA,
        "admissionSha256": ADMISSION_SHA,
        "publicationEpoch": None,
        "publicationManifestSha256": None,
    }
    wire = publisher.canonical_json_bytes(request).decode("utf-8")
    assert "/home/" not in wire
    assert "/dev/shm" not in wire
    assert "secret" not in wire.lower()
    assert "probabilit" not in wire.lower()


def test_canonical_json_matches_sites_number_spelling() -> None:
    assert publisher.canonical_json_bytes({"elo_k": 20.0, "weight": 0.2}) == (
        b'{"elo_k":20,"weight":0.2}'
    )


def test_research_summary_rejects_candidate_probability_or_local_path() -> None:
    with pytest.raises(ValueError, match="closed schema"):
        publisher.build_structured_artifact_upload(
            {**_summary(), "probabilities": {"home": 0.5}},
            visibility="research_audit",
            artifact_kind="prospective_evaluation",
            batch_manifest_sha256=BATCH_SHA,
            archive_manifest_sha256=ARCHIVE_SHA,
            admission_sha256=ADMISSION_SHA,
            source_ids=("openfootball:football.json:2026-27:en.1",),
        )
    with pytest.raises(ValueError, match="closed schema"):
        publisher.build_structured_artifact_upload(
            {**_summary(), "reasonCodes": ["/home/operator/private.json"]},
            visibility="research_audit",
            artifact_kind="prospective_evaluation",
            batch_manifest_sha256=BATCH_SHA,
            archive_manifest_sha256=ARCHIVE_SHA,
            admission_sha256=ADMISSION_SHA,
            source_ids=("openfootball:football.json:2026-27:en.1",),
        )


def test_structured_attestation_binds_every_gate_and_exact_body() -> None:
    request = publisher.build_structured_artifact_upload(
        _summary(),
        visibility="research_audit",
        artifact_kind="prospective_evaluation",
        batch_manifest_sha256=BATCH_SHA,
        archive_manifest_sha256=ARCHIVE_SHA,
        admission_sha256=ADMISSION_SHA,
        source_ids=("openfootball:football.json:2026-27:en.1",),
    )
    body = publisher.canonical_json_bytes(request)
    headers = publisher.structured_artifact_attestation_headers(
        body,
        endpoint="https://predict.example/api/artifacts",
        key_id="publisher-key-1",
        signing_secret="test-producer-signing-secret-at-least-32-bytes",
        upload=request,
        issued_at=datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc),
    )

    encoded = headers["X-Matchline-Producer-Attestation"]
    canonical = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    attestation = json.loads(canonical)
    assert set(attestation) == {
        "schemaVersion",
        "producer",
        "keyId",
        "issuedAt",
        "expiresAt",
        "stream",
        "visibility",
        "artifactKind",
        "rightsUseCase",
        "sourcePolicyIds",
        "batchManifestSha256",
        "archiveManifestSha256",
        "publicationEpoch",
        "publicationManifestSha256",
        "admissionSha256",
        "destination",
        "bodySha256",
    }
    assert attestation["schemaVersion"] == "matchline.sites_blob_attestation.v1"
    assert attestation["stream"] == "structured_artifact_snapshot"
    assert attestation["bodySha256"] == hashlib.sha256(body).hexdigest()
    assert attestation["publicationEpoch"] is None
    assert attestation["publicationManifestSha256"] is None
    signature = hmac.new(
        b"test-producer-signing-secret-at-least-32-bytes",
        canonical,
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-Matchline-Producer-Signature"] == f"v1={signature}"


def test_ack_must_exactly_bind_object_content_and_size() -> None:
    request = publisher.build_structured_artifact_upload(
        _summary(),
        visibility="research_audit",
        artifact_kind="prospective_evaluation",
        batch_manifest_sha256=BATCH_SHA,
        archive_manifest_sha256=ARCHIVE_SHA,
        admission_sha256=ADMISSION_SHA,
        source_ids=("openfootball:football.json:2026-27:en.1",),
    )
    response = {
        "status": "ok",
        "objectKey": request["objectKey"],
        "contentSha256": request["contentSha256"],
        "sizeBytes": request["sizeBytes"],
        "created": 1,
    }
    assert publisher.validate_structured_artifact_ack(request, response) == response
    with pytest.raises(ValueError, match="response contract"):
        publisher.validate_structured_artifact_ack(
            request,
            {**response, "contentSha256": "e" * 64},
        )


def test_builds_five_closed_probability_free_research_summaries() -> None:
    prospective = {
        "model": {"status": "pending_prospective_window"},
        "artifacts": {
            "cycleRawSha256": "1" * 64,
            "evaluationRawSha256": "2" * 64,
        },
        "cycle": {
            "status": "pending_prospective_window",
            "startedAt": "2026-08-28T01:00:00.000Z",
            "finishedAt": "2026-08-28T02:00:00.000Z",
            "asOf": "2026-08-28T01:55:00.000Z",
            "fixtureCount": 844,
        },
        "capture": {"currentFreezeCount": 9, "blocked": 3},
        "evaluation": {
            "scoredN": 1,
            "pendingN": 8,
            "resultConflicts": 0,
            "sampleRequirementsMet": False,
            "allRequiredTargetsScored": False,
            "predictionFreezesVerified": True,
            "promotionEligible": False,
            "productionAllowed": False,
        },
    }
    strict = {
        "status": "research_only_underperforms_market",
        "generated_at": "2026-08-28T01:58:00+00:00",
        "combined": {"sample_n": 6200},
        "failures": ["market baseline unavailable"],
    }
    offline = {
        "schema_version": "matchline.offline_snapshot.v1",
        "as_of": "2026-08-28T01:55:00+00:00",
        "matches": [{"id": "one"}, {"id": "two"}],
        "competitions": [{"id": "premier-league"}],
        "current_data": {
            "source_registry": [
                {"status": "fresh", "runtime": {"status": "fresh"}},
                {"status": "degraded", "runtime": {"status": "degraded"}},
                {"status": "forbidden", "runtime": {"status": "rights_blocked"}},
            ]
        },
    }

    summaries = publisher.build_research_artifact_summaries(
        prospective_audit=prospective,
        strict_report=strict,
        strict_report_sha256="3" * 64,
        offline_snapshot=offline,
        offline_snapshot_sha256="4" * 64,
    )

    assert [row["artifactKind"] for row in summaries] == [
        "prospective_cycle",
        "prospective_evaluation",
        "strict_report",
        "source_health",
        "read_model_summary",
    ]
    assert summaries[0]["metrics"] == {
        "startedAt": "2026-08-28T01:00:00.000Z",
        "finishedAt": "2026-08-28T02:00:00.000Z",
        "asOf": "2026-08-28T01:55:00.000Z",
        "fixtureCount": 844,
        "currentFreezeCount": 9,
        "blockedCount": 3,
    }
    assert summaries[2]["metrics"] == {
        "evaluatedAt": "2026-08-28T01:58:00.000Z",
        "sampleN": 6200,
        "failureCount": 1,
        "reportSha256": "3" * 64,
        "productionAllowed": False,
    }
    assert summaries[3]["metrics"] == {
        "sourceCount": 3,
        "healthyCount": 1,
        "degradedCount": 1,
        "blockedCount": 1,
    }
    assert summaries[4]["metrics"] == {
        "asOf": "2026-08-28T01:55:00.000Z",
        "fixtureCount": 2,
        "competitionCount": 1,
        "sourceCount": 3,
        "staleSourceCount": 2,
    }
    encoded = publisher.canonical_json_bytes(summaries).lower()
    assert b"probabilities" not in encoded
    assert b"market baseline unavailable" not in encoded
    assert b"/home/" not in encoded


def test_private_system_evidence_redacts_paths_and_never_builds_candidate_archive() -> None:
    prospective = {
        "schemaVersion": "matchline.prospective_audit_snapshot.v1",
        "generatedAt": "2026-08-28T02:00:00.000Z",
        "model": {"status": "pending_prospective_window"},
        "artifacts": {
            "cycleRawSha256": "1" * 64,
            "evaluationRawSha256": "2" * 64,
        },
        "cycle": {"status": "pending_prospective_window"},
        "capture": {"currentFreezeCount": 9},
        "evaluation": {"scoredN": 1, "pendingN": 8},
        "nextFreezes": [],
    }
    strict = {
        "schema_version": "matchline.strict_report.v1",
        "generated_at": "2026-08-28T01:58:00+00:00",
        "candidate_evidence_dir": "/home/operator/private/evidence",
        "metrics": {"market_delta": 0.02},
    }

    artifacts = publisher.build_private_system_evidence(
        prospective_audit=prospective,
        strict_report=strict,
        strict_report_sha256="3" * 64,
    )

    assert [row["artifactKind"] for row in artifacts] == [
        "cycle_evidence",
        "evaluation_evidence",
        "strict_evidence",
    ]
    encoded = publisher.canonical_json_bytes(artifacts).decode("utf-8")
    assert "candidate_prediction_archive" not in encoded
    assert "/home/operator" not in encoded
    assert "redactedLocalPathSha256" in encoded
    assert all(row["schemaVersion"] == "matchline.private_system_evidence.v1" for row in artifacts)

    private_upload = publisher.build_structured_artifact_upload(
        artifacts[0],
        visibility="private_evidence",
        artifact_kind="cycle_evidence",
        batch_manifest_sha256=BATCH_SHA,
        archive_manifest_sha256=ARCHIVE_SHA,
        admission_sha256=ADMISSION_SHA,
        source_ids=("openfootball:football.json:2026-27:en.1",),
    )
    assert private_upload["rightsUseCase"] == "audit_only"
    assert private_upload["publicationEpoch"] is None
    assert private_upload["publicationManifestSha256"] is None
    with pytest.raises(ValueError, match="candidate prediction archive"):
        publisher.build_structured_artifact_upload(
            {
                "schemaVersion": "matchline.private_system_evidence.v1",
                "observedAt": "2026-08-28T02:00:00.000Z",
            },
            visibility="private_evidence",
            artifact_kind="candidate_prediction_archive",
            batch_manifest_sha256=BATCH_SHA,
            archive_manifest_sha256=ARCHIVE_SHA,
            admission_sha256=ADMISSION_SHA,
            source_ids=("openfootball:football.json:2026-27:en.1",),
        )


def test_formal_artifact_is_explicitly_blocked_before_wire_construction() -> None:
    with pytest.raises(ValueError, match="independent verifier unavailable"):
        publisher.build_structured_artifact_upload(
            {
                "schemaVersion": "matchline.publication_snapshot.v1",
                "observedAt": "2026-08-28T02:00:00.000Z",
            },
            visibility="publication_member",
            artifact_kind="publication_snapshot",
            batch_manifest_sha256=BATCH_SHA,
            archive_manifest_sha256=ARCHIVE_SHA,
            admission_sha256=ADMISSION_SHA,
            source_ids=("openfootball:football.json:2026-27:en.1",),
            publication_epoch="epoch-1",
            publication_manifest_sha256="e" * 64,
        )
