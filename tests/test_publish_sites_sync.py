from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from league_platform import publish_sites_sync as sync
from league_platform.publish_raw_artifacts import canonical_json_bytes


SOURCE_ID = "openfootball:football.json:2026-27:en.1"
ENDPOINTS = {
    "raw_artifact": "https://predict.example/api/raw-artifacts",
    "research_fixture": "https://predict.example/api/research-fixtures",
    "prospective_audit": "https://predict.example/api/prospective-audit",
    "structured_artifact": "https://predict.example/api/artifacts",
}


def _observation(identity: str, raw_sha: str) -> dict[str, object]:
    return {
        "archiveRecord": {
            "observation_id": identity,
            "source_id": SOURCE_ID,
            "raw_sha256": raw_sha,
        },
        "manifestPrefixSha256": "9" * 64,
    }


def _raw_upload(manifest_sha: str, raw_sha: str, *observations: str) -> dict[str, Any]:
    raw = canonical_json_bytes({"fixtureMarker": raw_sha})
    content_sha = hashlib.sha256(raw).hexdigest()
    return {
        "schemaVersion": "matchline.raw_artifact_upload.v1",
        "archiveManifestSha256": manifest_sha,
        "raw": {
            "sha256": content_sha,
            "sizeBytes": len(raw),
            "contentType": "application/json",
            "base64": base64.b64encode(raw).decode("ascii"),
        },
        "observations": [_observation(identity, content_sha) for identity in observations],
    }


def _research(manifest_sha: str, admission_sha: str) -> dict[str, Any]:
    return {
        "schemaVersion": "matchline.research_fixture_snapshot.v1",
        "asOf": "2026-08-28T02:00:00.000Z",
        "archiveManifestSha256": manifest_sha,
        "rawAdmissionSha256": admission_sha,
        "rawRowsSha256": "8" * 64,
        "sourcePolicyIds": ["openfootball_current"],
        "fixtureCount": 1,
        "fixtures": [{"source": {"sourceId": SOURCE_ID}}],
    }


def _audit() -> dict[str, Any]:
    return {
        "schemaVersion": "matchline.prospective_audit_snapshot.v1",
        "generatedAt": "2026-08-28T02:00:00.000Z",
        "sourcePolicyIds": ["openfootball_current"],
        "model": {
            "versionSha256": "7" * 64,
            "status": "pending_prospective_window",
        },
        "artifacts": {
            "modelLockRawSha256": "3" * 64,
            "cycleRawSha256": "6" * 64,
            "evaluationRawSha256": "5" * 64,
            "liveSnapshotRawSha256": "2" * 64,
            "predictionArchiveRawSha256": "1" * 64,
            "admissionSha256": "b" * 64,
            "rowsSha256": "8" * 64,
            "sourceManifestSha256": "9" * 64,
        },
        "cycle": {
            "status": "pending_prospective_window",
            "startedAt": "2026-08-28T01:00:00.000Z",
            "finishedAt": "2026-08-28T02:00:00.000Z",
            "asOf": "2026-08-28T01:55:00.000Z",
            "fixtureCount": 1,
        },
        "capture": {"currentFreezeCount": 1, "blocked": 0},
        "evaluation": {
            "scoredN": 0,
            "pendingN": 1,
            "resultConflicts": 0,
            "sampleRequirementsMet": False,
            "allRequiredTargetsScored": False,
            "predictionFreezesVerified": True,
            "promotionEligible": False,
            "productionAllowed": False,
        },
        "nextFreezes": [],
    }


def _strict_report() -> dict[str, Any]:
    return {
        "schema_version": "matchline.strict_report.v260",
        "status": "research_only_underperforms_market",
        "generated_at": "2026-08-28T01:58:00+00:00",
        "combined": {"sample_n": 6200},
        "failures": ["market baseline unavailable"],
        "candidate_evidence_dir": "/home/operator/private/evidence",
    }


def _offline_snapshot() -> dict[str, Any]:
    return {
        "schema_version": "matchline.offline_snapshot.v1",
        "as_of": "2026-08-28T01:55:00+00:00",
        "matches": [{"id": "one"}],
        "competitions": [{"id": "premier-league"}],
        "current_data": {"source_registry": [{"status": "fresh", "runtime": {"status": "fresh"}}]},
    }


def _summary() -> dict[str, Any]:
    return {
        "schemaVersion": "matchline.research_artifact_summary.v1",
        "artifactKind": "source_health",
        "researchOnly": True,
        "live": False,
        "productionReady": False,
        "observedAt": "2026-08-28T02:00:00.000Z",
        "status": "ok",
        "evidenceSha256": "4" * 64,
        "reasonCodes": [],
        "metrics": {
            "sourceCount": 1,
            "healthyCount": 1,
            "degradedCount": 0,
            "blockedCount": 0,
        },
    }


def _private_content(kind: str, evidence_sha: str) -> dict[str, Any]:
    return {
        "schemaVersion": "matchline.private_system_evidence.v1",
        "artifactKind": kind,
        "observedAt": "2026-08-28T02:00:00.000Z",
        "evidenceSha256": evidence_sha,
        "systemGenerated": True,
        "candidateArchiveIncluded": False,
        "evidence": {"status": "pending_prospective_window"},
    }


def _inputs(
    *,
    manifest_sha: str = "a" * 64,
    observation_ids: tuple[str, ...] = ("1" * 64, "2" * 64),
) -> sync.PreparedSitesSyncInputs:
    admission_sha = "b" * 64
    return sync.PreparedSitesSyncInputs(
        archive_manifest_sha256=manifest_sha,
        admission_sha256=admission_sha,
        source_ids=(SOURCE_ID,),
        raw_uploads=(
            _raw_upload(manifest_sha, "c" * 64, observation_ids[0]),
            *(
                (_raw_upload(manifest_sha, "d" * 64, observation_ids[1]),)
                if len(observation_ids) > 1
                else ()
            ),
        ),
        research_fixture=_research(manifest_sha, admission_sha),
        prospective_audit=_audit(),
        structured_contents=(_summary(),),
    )


class FakeSitesServer:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **request: Any) -> dict[str, Any]:
        payload = json.loads(request["body"])
        self.calls.append(payload)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise ValueError("fake server injected failure")
        schema = payload["schemaVersion"]
        if schema == "matchline.raw_artifact_upload.v1":
            observations = sorted(
                item["archiveRecord"]["observation_id"] for item in payload["observations"]
            )
            raw_sha = payload["raw"]["sha256"]
            return {
                "status": "ok",
                "acceptedObservations": len(observations),
                "createdObject": 1,
                "createdObservations": len(observations),
                "duplicateObservations": 0,
                "rawSha256": raw_sha,
                "objectKey": f"openfootball/raw/sha256/{raw_sha[:2]}/{raw_sha}.raw",
                "observationSetSha256": hashlib.sha256(
                    canonical_json_bytes(observations)
                ).hexdigest(),
                "archiveManifestSha256": payload["archiveManifestSha256"],
            }
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if schema == "matchline.research_fixture_snapshot.v1":
            return {
                "status": "ok",
                "created": 1,
                "snapshotId": 7,
                "snapshotSha256": digest,
                "fixtureCount": payload["fixtureCount"],
                "objectKey": f"research-fixtures/sha256/{digest[:2]}/{digest}.json",
                "archiveManifestSha256": payload["archiveManifestSha256"],
            }
        if schema == "matchline.prospective_audit_snapshot.v1":
            return {
                "status": "ok",
                "created": 1,
                "auditId": 8,
                "snapshotSha256": digest,
                "modelVersionSha256": payload["model"]["versionSha256"],
                "currentFreezeCount": payload["capture"]["currentFreezeCount"],
                "scoredN": payload["evaluation"]["scoredN"],
                "pendingN": payload["evaluation"]["pendingN"],
            }
        assert schema == "matchline.sites_blob_upload.v1"
        return {
            "status": "ok",
            "objectKey": payload["objectKey"],
            "contentSha256": payload["contentSha256"],
            "sizeBytes": payload["sizeBytes"],
            "created": 1,
        }


def _execute(
    plan: sync.SitesSyncPlan,
    *,
    cursor: Path,
    receipt: Path,
    server: FakeSitesServer,
) -> dict[str, Any]:
    return sync.execute_sites_sync(
        plan,
        token=hashlib.sha256(b"fixture-auth").hexdigest(),
        key_id="publisher-key-1",
        signing_secret="test-producer-signing-secret-at-least-32-bytes",
        cursor_path=cursor,
        receipt_path=receipt,
        sender=server,
    )


def test_failure_keeps_cursor_but_writes_no_completed_receipt_then_resumes(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "completed.json"
    cursor = tmp_path / "cursor.json"
    plan = sync.preflight_sites_sync(
        inputs=_inputs(),
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    failing = FakeSitesServer(fail_at=2)
    with pytest.raises(ValueError, match="injected failure"):
        _execute(plan, cursor=cursor, receipt=receipt, server=failing)

    assert cursor.exists()
    assert not receipt.exists()
    cursor_value = json.loads(cursor.read_text())
    assert cursor_value["schemaVersion"] == "matchline.sites_sync_cursor.v1"
    assert len(cursor_value["ackedItems"]) == 1

    resumed = FakeSitesServer()
    result = _execute(plan, cursor=cursor, receipt=receipt, server=resumed)
    assert result["status"] == "completed"
    assert result["requests"] == len(plan.items) - 1
    completed = json.loads(receipt.read_text())
    assert completed["status"] == "completed"
    assert completed["batchManifestSha256"] == plan.batch_manifest_sha256
    encoded = receipt.read_text().lower()
    assert "/home/" not in encoded
    assert "local-test-token" not in encoded
    assert "signing-secret" not in encoded


def test_exact_completed_batch_is_zero_requests(tmp_path: Path) -> None:
    receipt = tmp_path / "completed.json"
    cursor = tmp_path / "cursor.json"
    first = sync.preflight_sites_sync(
        inputs=_inputs(),
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    _execute(first, cursor=cursor, receipt=receipt, server=FakeSitesServer())

    second = sync.preflight_sites_sync(
        inputs=_inputs(),
        lane="research_audit",
        mode="incremental",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    server = FakeSitesServer()
    result = _execute(second, cursor=cursor, receipt=receipt, server=server)
    assert second.status == "already_complete"
    assert result == {
        "status": "already_complete",
        "requests": 0,
        "batchManifestSha256": first.batch_manifest_sha256,
    }
    assert server.calls == []


def test_incremental_manifest_change_sends_only_new_raw_observation(tmp_path: Path) -> None:
    receipt = tmp_path / "completed.json"
    first = sync.preflight_sites_sync(
        inputs=_inputs(manifest_sha="a" * 64, observation_ids=("1" * 64,)),
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    _execute(
        first,
        cursor=tmp_path / "first-cursor.json",
        receipt=receipt,
        server=FakeSitesServer(),
    )

    changed = _inputs(
        manifest_sha="e" * 64,
        observation_ids=("1" * 64, "2" * 64),
    )
    plan = sync.preflight_sites_sync(
        inputs=changed,
        lane="research_audit",
        mode="incremental",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    raw_items = [item for item in plan.items if item.stream == "raw_artifact"]
    assert len(raw_items) == 1
    assert [
        item["archiveRecord"]["observation_id"] for item in raw_items[0].payload["observations"]
    ] == ["2" * 64]
    assert not any(item.stream == "structured_artifact" for item in plan.items), (
        "unchanged summary content must be carried by the prior completed receipt"
    )


def test_incremental_manifest_mutation_of_completed_observation_fails_closed(
    tmp_path: Path,
) -> None:
    """A receipt checkpoint binds bytes/prefixes, not just observation ids."""

    receipt = tmp_path / "completed.json"
    first = sync.preflight_sites_sync(
        inputs=_inputs(manifest_sha="a" * 64, observation_ids=("1" * 64,)),
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    _execute(
        first,
        cursor=tmp_path / "first-cursor.json",
        receipt=receipt,
        server=FakeSitesServer(),
    )

    changed = _inputs(manifest_sha="e" * 64, observation_ids=("1" * 64,))
    changed.raw_uploads[0]["observations"][0]["manifestPrefixSha256"] = "8" * 64
    with pytest.raises(ValueError, match="changed completed observations"):
        sync.preflight_sites_sync(
            inputs=changed,
            lane="research_audit",
            mode="incremental",
            endpoints=ENDPOINTS,
            previous_receipt_path=receipt,
        )


def test_formal_lane_is_blocked_with_zero_requests(tmp_path: Path) -> None:
    plan = sync.preflight_sites_sync(
        inputs=None,
        lane="formal",
        mode="incremental",
        endpoints={},
        previous_receipt_path=tmp_path / "formal.json",
    )
    server = FakeSitesServer()
    result = _execute(
        plan,
        cursor=tmp_path / "formal-cursor.json",
        receipt=tmp_path / "formal.json",
        server=server,
    )
    assert result["status"] == "blocked_independent_verifier_unavailable"
    assert result["requests"] == 0
    assert server.calls == []
    assert not (tmp_path / "formal.json").exists()


def test_private_system_lane_uploads_only_token_gated_evidence_and_is_idempotent(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "private-completed.json"
    inputs = sync.PreparedSitesSyncInputs(
        archive_manifest_sha256="a" * 64,
        admission_sha256="b" * 64,
        source_ids=(SOURCE_ID,),
        structured_contents=(
            _private_content("cycle_evidence", "1" * 64),
            _private_content("evaluation_evidence", "2" * 64),
            _private_content("strict_evidence", "3" * 64),
        ),
    )
    first = sync.preflight_sites_sync(
        inputs=inputs,
        lane="private_evidence",
        mode="incremental",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    server = FakeSitesServer()
    result = _execute(
        first,
        cursor=tmp_path / "private-cursor.json",
        receipt=receipt,
        server=server,
    )

    assert result["status"] == "completed"
    assert len(server.calls) == 3
    assert all(call["visibility"] == "private_evidence" for call in server.calls)
    assert all(call["rightsUseCase"] == "audit_only" for call in server.calls)
    encoded = json.dumps(server.calls, sort_keys=True)
    assert "candidate_prediction_archive" not in encoded
    assert "probabilities" not in encoded

    second = sync.preflight_sites_sync(
        inputs=inputs,
        lane="private_evidence",
        mode="incremental",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    assert second.status == "already_complete"
    assert (
        _execute(
            second,
            cursor=tmp_path / "private-cursor.json",
            receipt=receipt,
            server=FakeSitesServer(),
        )["requests"]
        == 0
    )


def test_changed_manifest_without_new_observation_sends_one_registration_anchor(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "completed.json"
    first = sync.preflight_sites_sync(
        inputs=_inputs(manifest_sha="a" * 64, observation_ids=("1" * 64,)),
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    _execute(
        first,
        cursor=tmp_path / "first-cursor.json",
        receipt=receipt,
        server=FakeSitesServer(),
    )

    changed = sync.preflight_sites_sync(
        inputs=_inputs(manifest_sha="e" * 64, observation_ids=("1" * 64,)),
        lane="research_audit",
        mode="incremental",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    raw = [item for item in changed.items if item.stream == "raw_artifact"]
    assert len(raw) == 1
    assert len(raw[0].payload["observations"]) == 1
    assert raw[0].payload["observations"][0]["archiveRecord"]["observation_id"] == "1" * 64


def test_corrupt_completed_receipt_fails_closed_before_requests(tmp_path: Path) -> None:
    receipt = tmp_path / "completed.json"
    receipt.write_text('{"status":"completed"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="completed receipt is invalid"):
        sync.preflight_sites_sync(
            inputs=_inputs(),
            lane="research_audit",
            mode="incremental",
            endpoints=ENDPOINTS,
            previous_receipt_path=receipt,
        )


def test_incomplete_completed_receipt_inventory_fails_closed_before_requests(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "completed.json"
    cursor = tmp_path / "cursor.json"
    first = sync.preflight_sites_sync(
        inputs=_inputs(),
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    _execute(first, cursor=cursor, receipt=receipt, server=FakeSitesServer())
    value = json.loads(receipt.read_text())
    raw_key = next(key for key in value["completedItems"] if key.startswith("raw:"))
    del value["completedItems"][raw_key]
    receipt.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="ACK coverage"):
        sync.preflight_sites_sync(
            inputs=_inputs(),
            lane="research_audit",
            mode="incremental",
            endpoints=ENDPOINTS,
            previous_receipt_path=receipt,
        )


def test_completed_receipt_rejects_structured_payload_digest_mismatch(tmp_path: Path) -> None:
    receipt = tmp_path / "completed.json"
    cursor = tmp_path / "cursor.json"
    first = sync.preflight_sites_sync(
        inputs=_inputs(),
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    _execute(first, cursor=cursor, receipt=receipt, server=FakeSitesServer())
    value = json.loads(receipt.read_text())
    structured_key = next(key for key in value["completedItems"] if key.startswith("structured:"))
    value["completedItems"][structured_key]["payloadSha256"] = "f" * 64
    receipt.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="content digest is invalid"):
        sync.preflight_sites_sync(
            inputs=_inputs(),
            lane="research_audit",
            mode="incremental",
            endpoints=ENDPOINTS,
            previous_receipt_path=receipt,
        )


def test_incremental_completed_receipt_carries_prior_ack_records(tmp_path: Path) -> None:
    receipt = tmp_path / "completed.json"
    first_inputs = _inputs(manifest_sha="a" * 64, observation_ids=("1" * 64,))
    # Keep an immutable prefix marker that lets the strict normalizer prove the
    # historical segment body after the append-only archive advances.
    first_inputs.raw_uploads[0]["observations"][0]["manifestPrefixSha256"] = "a" * 64
    first = sync.preflight_sites_sync(
        inputs=first_inputs,
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    _execute(
        first,
        cursor=tmp_path / "first-cursor.json",
        receipt=receipt,
        server=FakeSitesServer(),
    )

    changed = _inputs(
        manifest_sha="e" * 64,
        observation_ids=("1" * 64, "2" * 64),
    )
    changed.raw_uploads[0]["observations"][0]["manifestPrefixSha256"] = "a" * 64
    second = sync.preflight_sites_sync(
        inputs=changed,
        lane="research_audit",
        mode="incremental",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    result = _execute(
        second,
        cursor=tmp_path / "second-cursor.json",
        receipt=receipt,
        server=FakeSitesServer(),
    )
    assert result["status"] == "completed"
    completed = json.loads(receipt.read_text())
    inventory = completed["inventory"]
    raw_keys = [key for key in completed["completedItems"] if key.startswith("raw:")]
    assert {key.split(":", 2)[1] for key in raw_keys} == set(inventory["rawObjectSha256s"])
    assert set(inventory["contentDigests"]) == {
        "research_fixture",
        "prospective_audit",
        "structured:research_audit:source_health",
    }
    assert (
        f"research-fixture:{inventory['contentDigests']['research_fixture']}"
        in completed["completedItems"]
    )
    assert (
        f"prospective-audit:{inventory['contentDigests']['prospective_audit']}"
        in completed["completedItems"]
    )
    assert (
        f"structured:research_audit:source_health:{inventory['contentDigests']['structured:research_audit:source_health']}"
        in completed["completedItems"]
    )


def test_incremental_segmented_receipt_normalizes_without_requests(
    tmp_path: Path,
) -> None:
    """A carried prefix/suffix ACK receipt must not fail exact replay forever."""

    def facts_inputs(
        manifest_sha: str,
        observation_ids: tuple[str, ...],
        *,
        admission_sha: str = "b" * 64,
    ) -> sync.PreparedSitesSyncInputs:
        return sync.PreparedSitesSyncInputs(
            archive_manifest_sha256=manifest_sha,
            admission_sha256=admission_sha,
            source_ids=(SOURCE_ID,),
            raw_uploads=(_raw_upload(manifest_sha, "c" * 64, *observation_ids),),
            research_fixture=_research(manifest_sha, admission_sha),
        )

    receipt = tmp_path / "completed.json"
    cursor = tmp_path / "cursor.json"
    first = sync.preflight_sites_sync(
        inputs=facts_inputs("a" * 64, ("1" * 64, "2" * 64)),
        lane="research_facts_only",
        mode="full",
        endpoints={
            "raw_artifact": ENDPOINTS["raw_artifact"],
            "research_fixture": ENDPOINTS["research_fixture"],
        },
        previous_receipt_path=receipt,
    )
    _execute(first, cursor=cursor, receipt=receipt, server=FakeSitesServer())

    # Keep the raw archive manifest stable so the old ACK payloads can be
    # reconstructed exactly; changing the admission marker still produces a
    # distinct incremental batch and exercises carried segmented identities.
    second_inputs = facts_inputs(
        "a" * 64,
        ("1" * 64, "2" * 64, "3" * 64, "4" * 64),
        admission_sha="d" * 64,
    )
    second = sync.preflight_sites_sync(
        inputs=second_inputs,
        lane="research_facts_only",
        mode="incremental",
        endpoints={
            "raw_artifact": ENDPOINTS["raw_artifact"],
            "research_fixture": ENDPOINTS["research_fixture"],
        },
        previous_receipt_path=receipt,
    )
    _execute(second, cursor=cursor, receipt=receipt, server=FakeSitesServer())

    expected_plan = sync.preflight_sites_sync(
        inputs=second_inputs,
        lane="research_facts_only",
        mode="full",
        endpoints={
            "raw_artifact": ENDPOINTS["raw_artifact"],
            "research_fixture": ENDPOINTS["research_fixture"],
        },
        previous_receipt_path=tmp_path / "empty.json",
    )
    expected_keys = {item.item_key for item in expected_plan.items}

    # Planned incremental execution must converge the carried receipt before
    # returning.  Waiting for a later already-complete replay leaves stale
    # segment/content identities visible to Sites and caused the timer drift.
    completed_after_incremental = json.loads(receipt.read_text(encoding="utf-8"))
    assert set(completed_after_incremental["completedItems"]) == expected_keys

    replay = sync.preflight_sites_sync(
        inputs=second_inputs,
        lane="research_facts_only",
        mode="incremental",
        endpoints={
            "raw_artifact": ENDPOINTS["raw_artifact"],
            "research_fixture": ENDPOINTS["research_fixture"],
        },
        previous_receipt_path=receipt,
    )
    assert replay.status == "already_complete"
    server = FakeSitesServer()
    result = _execute(replay, cursor=cursor, receipt=receipt, server=server)
    assert result["status"] == "already_complete"
    assert result["requests"] == 0
    assert server.calls == []

    normalized = json.loads(receipt.read_text(encoding="utf-8"))
    assert set(normalized["completedItems"]) == expected_keys
    assert len([key for key in normalized["completedItems"] if key.startswith("raw:")]) == 1
    assert len(
        [key for key in normalized["completedItems"] if key.startswith("research-fixture:")]
    ) == 1
    cursor_value = json.loads(cursor.read_text(encoding="utf-8"))
    assert set(cursor_value["ackedItems"]) <= expected_keys


def test_incremental_segmented_receipt_reconstructs_historical_manifest_prefix(
    tmp_path: Path,
) -> None:
    """A changed append-only manifest can still prove old segment ACK bodies."""

    def facts_inputs(
        manifest_sha: str,
        observation_ids: tuple[str, ...],
        *,
        admission_sha: str,
    ) -> sync.PreparedSitesSyncInputs:
        return sync.PreparedSitesSyncInputs(
            archive_manifest_sha256=manifest_sha,
            admission_sha256=admission_sha,
            source_ids=(SOURCE_ID,),
            raw_uploads=(_raw_upload(manifest_sha, "c" * 64, *observation_ids),),
            research_fixture=_research(manifest_sha, admission_sha),
        )

    receipt = tmp_path / "completed.json"
    cursor = tmp_path / "cursor.json"
    first = sync.preflight_sites_sync(
        inputs=facts_inputs("9" * 64, ("1" * 64, "2" * 64), admission_sha="b" * 64),
        lane="research_facts_only",
        mode="full",
        endpoints={
            "raw_artifact": ENDPOINTS["raw_artifact"],
            "research_fixture": ENDPOINTS["research_fixture"],
        },
        previous_receipt_path=receipt,
    )
    _execute(first, cursor=cursor, receipt=receipt, server=FakeSitesServer())

    second_inputs = facts_inputs(
        "a" * 64,
        ("1" * 64, "2" * 64, "3" * 64, "4" * 64),
        admission_sha="d" * 64,
    )
    second = sync.preflight_sites_sync(
        inputs=second_inputs,
        lane="research_facts_only",
        mode="incremental",
        endpoints={
            "raw_artifact": ENDPOINTS["raw_artifact"],
            "research_fixture": ENDPOINTS["research_fixture"],
        },
        previous_receipt_path=receipt,
    )
    _execute(second, cursor=cursor, receipt=receipt, server=FakeSitesServer())

    replay = sync.preflight_sites_sync(
        inputs=second_inputs,
        lane="research_facts_only",
        mode="incremental",
        endpoints={
            "raw_artifact": ENDPOINTS["raw_artifact"],
            "research_fixture": ENDPOINTS["research_fixture"],
        },
        previous_receipt_path=receipt,
    )
    assert replay.status == "already_complete"
    server = FakeSitesServer()
    assert _execute(replay, cursor=cursor, receipt=receipt, server=server)["requests"] == 0
    assert server.calls == []


def test_request_preflight_failure_makes_zero_sender_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = sync.preflight_sites_sync(
        inputs=_inputs(),
        lane="research_audit",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=tmp_path / "receipt.json",
    )
    original = sync._headers_for_item
    seen = 0

    def fail_on_second(item: sync.SyncItem, **kwargs: Any) -> dict[str, str]:
        nonlocal seen
        seen += 1
        if seen == 2:
            raise ValueError("header preflight failure")
        return original(item, **kwargs)

    monkeypatch.setattr(sync, "_headers_for_item", fail_on_second)
    server = FakeSitesServer()
    with pytest.raises(ValueError, match="header preflight failure"):
        _execute(
            plan,
            cursor=tmp_path / "cursor.json",
            receipt=tmp_path / "receipt.json",
            server=server,
        )
    assert server.calls == []


def test_prepare_research_lane_reuses_existing_publishers_and_builds_five_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _inputs()
    strict = tmp_path / "strict.json"
    offline = tmp_path / "offline.json"
    strict.write_text(json.dumps(_strict_report()), encoding="utf-8")
    offline.write_text(json.dumps(_offline_snapshot()), encoding="utf-8")
    monkeypatch.setattr(
        sync,
        "build_raw_artifact_uploads",
        lambda _root: list(base.raw_uploads),
    )
    monkeypatch.setattr(
        sync,
        "build_verified_research_fixture_snapshot",
        lambda _snapshot, _root: dict(base.research_fixture or {}),
    )
    monkeypatch.setattr(
        sync,
        "build_prospective_audit_snapshot",
        lambda **_paths: dict(base.prospective_audit or {}),
    )
    monkeypatch.setattr(sync, "strict_report_v260_failures", lambda _report: [])

    prepared = sync.prepare_sites_sync_inputs(
        lane="research_audit",
        archive_root=tmp_path / "raw",
        snapshot_path=tmp_path / "current.json",
        lock_path=tmp_path / "lock.json",
        cycle_path=tmp_path / "cycle.json",
        evaluation_path=tmp_path / "evaluation.json",
        live_snapshot_path=tmp_path / "current.json",
        prediction_archive_path=tmp_path / "predictions.jsonl",
        strict_report_path=strict,
        offline_snapshot_path=offline,
    )

    assert prepared is not None
    assert prepared.archive_manifest_sha256 == "a" * 64
    assert prepared.admission_sha256 == "b" * 64
    assert prepared.raw_uploads == base.raw_uploads
    assert [row["artifactKind"] for row in prepared.structured_contents] == [
        "prospective_cycle",
        "prospective_evaluation",
        "strict_report",
        "source_health",
        "read_model_summary",
    ]
    encoded = canonical_json_bytes(prepared.structured_contents).lower()
    assert b"probabilities" not in encoded
    assert b"market baseline unavailable" not in encoded
    assert b"/home/" not in encoded


def test_prepare_private_lane_builds_only_three_system_evidence_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _inputs()
    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps(_strict_report()), encoding="utf-8")
    monkeypatch.setattr(
        sync,
        "build_raw_artifact_uploads",
        lambda _root: pytest.fail("private lane must not prepare raw uploads"),
    )
    monkeypatch.setattr(
        sync,
        "build_verified_research_fixture_snapshot",
        lambda _snapshot, _root: dict(base.research_fixture or {}),
    )
    monkeypatch.setattr(
        sync,
        "build_prospective_audit_snapshot",
        lambda **_paths: dict(base.prospective_audit or {}),
    )
    monkeypatch.setattr(sync, "strict_report_v260_failures", lambda _report: [])

    prepared = sync.prepare_sites_sync_inputs(
        lane="private_evidence",
        archive_root=tmp_path / "raw",
        snapshot_path=tmp_path / "current.json",
        lock_path=tmp_path / "lock.json",
        cycle_path=tmp_path / "cycle.json",
        evaluation_path=tmp_path / "evaluation.json",
        live_snapshot_path=tmp_path / "current.json",
        prediction_archive_path=tmp_path / "predictions.jsonl",
        strict_report_path=strict,
    )

    assert prepared is not None
    assert prepared.raw_uploads == ()
    assert prepared.research_fixture is None
    assert prepared.prospective_audit is None
    assert [row["artifactKind"] for row in prepared.structured_contents] == [
        "cycle_evidence",
        "evaluation_evidence",
        "strict_evidence",
    ]
    encoded = canonical_json_bytes(prepared.structured_contents).decode("utf-8")
    assert "candidate_prediction_archive" not in encoded
    assert "/home/operator" not in encoded
    assert "redactedLocalPathSha256" in encoded


def test_prepare_rejects_cross_gate_admission_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _inputs()
    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps(_strict_report()), encoding="utf-8")
    mismatched = _audit()
    mismatched["artifacts"]["admissionSha256"] = "f" * 64
    monkeypatch.setattr(
        sync,
        "build_verified_research_fixture_snapshot",
        lambda _snapshot, _root: dict(base.research_fixture or {}),
    )
    monkeypatch.setattr(
        sync,
        "build_prospective_audit_snapshot",
        lambda **_paths: mismatched,
    )
    monkeypatch.setattr(sync, "strict_report_v260_failures", lambda _report: [])

    with pytest.raises(ValueError, match="admission identity"):
        sync.prepare_sites_sync_inputs(
            lane="private_evidence",
            archive_root=tmp_path / "raw",
            snapshot_path=tmp_path / "current.json",
            lock_path=tmp_path / "lock.json",
            cycle_path=tmp_path / "cycle.json",
            evaluation_path=tmp_path / "evaluation.json",
            live_snapshot_path=tmp_path / "current.json",
            prediction_archive_path=tmp_path / "predictions.jsonl",
            strict_report_path=strict,
        )


def test_prepare_formal_lane_stops_before_reading_local_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync,
        "build_raw_artifact_uploads",
        lambda _root: pytest.fail("formal blocked lane must not read raw artifacts"),
    )
    assert sync.prepare_sites_sync_inputs(lane="formal") is None


def test_cli_formal_lane_reports_blocked_without_paths_credentials_or_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = tmp_path / "formal-receipt.json"
    cursor = tmp_path / "formal-cursor.json"
    assert (
        sync.main(
            [
                "--lane",
                "formal",
                "--mode",
                "incremental",
                "--receipt",
                str(receipt),
                "--cursor",
                str(cursor),
            ]
        )
        == 0
    )
    value = json.loads(capsys.readouterr().out)
    assert value == {
        "status": "blocked_independent_verifier_unavailable",
        "requests": 0,
    }
    assert not receipt.exists()
    assert not cursor.exists()


def test_cli_dry_run_reports_counts_without_credentials_or_local_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inputs = sync.PreparedSitesSyncInputs(
        archive_manifest_sha256="a" * 64,
        admission_sha256="b" * 64,
        source_ids=(SOURCE_ID,),
        structured_contents=(
            _private_content("cycle_evidence", "1" * 64),
            _private_content("evaluation_evidence", "2" * 64),
            _private_content("strict_evidence", "3" * 64),
        ),
    )
    monkeypatch.setattr(sync, "prepare_sites_sync_inputs", lambda **_kwargs: inputs)
    monkeypatch.setattr(
        sync,
        "execute_sites_sync",
        lambda *_args, **_kwargs: pytest.fail("dry run must not execute requests"),
    )
    assert (
        sync.main(
            [
                "--lane",
                "private_evidence",
                "--mode",
                "incremental",
                "--structured-endpoint",
                ENDPOINTS["structured_artifact"],
                "--receipt",
                str(tmp_path / "receipt.json"),
                "--cursor",
                str(tmp_path / "cursor.json"),
                "--dry-run",
            ]
        )
        == 0
    )
    raw = capsys.readouterr().out
    value = json.loads(raw)
    assert value["status"] == "dry_run"
    assert value["plannedRequests"] == 3
    assert value["inventory"] == {
        "contentArtifacts": 3,
        "rawObjects": 0,
        "rawObservations": 0,
    }
    assert "/home/" not in raw
    assert "candidate_prediction_archive" not in raw


def test_facts_only_preflight_accepts_archive_and_fixture_without_model_inputs(
    tmp_path: Path,
) -> None:
    base = _inputs()
    facts = sync.PreparedSitesSyncInputs(
        archive_manifest_sha256=base.archive_manifest_sha256,
        admission_sha256=base.admission_sha256,
        source_ids=base.source_ids,
        raw_uploads=base.raw_uploads,
        research_fixture=base.research_fixture,
    )

    plan = sync.preflight_sites_sync(
        inputs=facts,
        lane="research_facts_only",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=tmp_path / "facts-receipt.json",
    )

    assert plan.status == "planned"
    assert plan.lane == "research_facts_only"
    assert {item.stream for item in plan.items} == {
        "raw_artifact",
        "research_fixture",
    }
    assert not any(item.stream == "prospective_audit" for item in plan.items)
    assert plan.batch_manifest["factsOnly"] is True
    assert plan.batch_manifest["archiveManifestSha256"] == base.archive_manifest_sha256
    assert plan.batch_manifest["admissionSha256"] == base.admission_sha256
    assert plan.batch_manifest["sourceIds"] == [SOURCE_ID]
    encoded = json.dumps(plan.batch_manifest, sort_keys=True).lower()
    assert "prediction" not in encoded
    assert "probability" not in encoded
    assert "market" not in encoded
    assert "strict" not in encoded


def test_prepare_facts_only_does_not_require_or_read_model_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _inputs()
    calls: list[str] = []

    monkeypatch.setattr(
        sync,
        "build_verified_research_fixture_snapshot",
        lambda _snapshot, _root: (calls.append("research") or dict(base.research_fixture or {})),
    )
    monkeypatch.setattr(
        sync,
        "build_raw_artifact_uploads",
        lambda _root: (calls.append("raw") or list(base.raw_uploads)),
    )
    monkeypatch.setattr(
        sync,
        "build_prospective_audit_snapshot",
        lambda **_paths: pytest.fail("facts-only preparation must not read model artifacts"),
    )

    prepared = sync.prepare_sites_sync_inputs(
        lane="research_facts_only",
        archive_root=tmp_path / "raw",
        snapshot_path=tmp_path / "snapshot.json",
    )

    assert prepared is not None
    assert calls == ["research", "raw"]
    assert prepared.prospective_audit is None
    assert prepared.structured_contents == ()
    assert prepared.raw_uploads == base.raw_uploads
    assert prepared.research_fixture == base.research_fixture


def test_facts_only_execute_writes_complete_receipt_without_sensitive_streams(
    tmp_path: Path,
) -> None:
    base = _inputs()
    facts = sync.PreparedSitesSyncInputs(
        archive_manifest_sha256=base.archive_manifest_sha256,
        admission_sha256=base.admission_sha256,
        source_ids=base.source_ids,
        raw_uploads=base.raw_uploads,
        research_fixture=base.research_fixture,
    )
    receipt = tmp_path / "facts-receipt.json"
    plan = sync.preflight_sites_sync(
        inputs=facts,
        lane="research_facts_only",
        mode="full",
        endpoints=ENDPOINTS,
        previous_receipt_path=receipt,
    )
    server = FakeSitesServer()

    result = _execute(
        plan,
        cursor=tmp_path / "facts-cursor.json",
        receipt=receipt,
        server=server,
    )

    assert result["status"] == "completed"
    assert {payload["schemaVersion"] for payload in server.calls} == {
        "matchline.raw_artifact_upload.v1",
        "matchline.research_fixture_snapshot.v1",
    }
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert saved["lane"] == "research_facts_only"
    assert saved["factsOnly"] is True
    assert saved["batchManifest"]["factsOnly"] is True
    encoded = json.dumps(saved, sort_keys=True).lower()
    for forbidden in ("prediction", "probability", "market", "strict", "formal"):
        assert forbidden not in encoded

    second = sync.preflight_sites_sync(
        inputs=facts,
        lane="research_facts_only",
        mode="incremental",
        endpoints={
            "raw_artifact": ENDPOINTS["raw_artifact"],
            "research_fixture": ENDPOINTS["research_fixture"],
        },
        previous_receipt_path=receipt,
    )
    assert second.status == "already_complete"


def test_default_research_audit_still_requires_model_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="prospective lock"):
        sync.prepare_sites_sync_inputs(
            lane="research_audit",
            archive_root=tmp_path / "raw",
            snapshot_path=tmp_path / "snapshot.json",
        )


def test_facts_only_rejects_prospective_or_forbidden_nested_content(tmp_path: Path) -> None:
    base = _inputs()
    contaminated = dict(base.research_fixture or {})
    contaminated["researchPrediction"] = {"probability": 0.9}
    facts = sync.PreparedSitesSyncInputs(
        archive_manifest_sha256=base.archive_manifest_sha256,
        admission_sha256=base.admission_sha256,
        source_ids=base.source_ids,
        raw_uploads=base.raw_uploads,
        research_fixture=contaminated,
        prospective_audit=base.prospective_audit,
    )

    with pytest.raises(ValueError, match="facts-only sync cannot contain prospective"):
        sync.preflight_sites_sync(
            inputs=facts,
            lane="research_facts_only",
            mode="full",
            endpoints={
                "raw_artifact": ENDPOINTS["raw_artifact"],
                "research_fixture": ENDPOINTS["research_fixture"],
            },
            previous_receipt_path=tmp_path / "facts-receipt.json",
        )

    facts = sync.PreparedSitesSyncInputs(
        archive_manifest_sha256=base.archive_manifest_sha256,
        admission_sha256=base.admission_sha256,
        source_ids=base.source_ids,
        raw_uploads=base.raw_uploads,
        research_fixture=contaminated,
    )
    with pytest.raises(ValueError, match="forbidden field"):
        sync.preflight_sites_sync(
            inputs=facts,
            lane="research_facts_only",
            mode="full",
            endpoints={
                "raw_artifact": ENDPOINTS["raw_artifact"],
                "research_fixture": ENDPOINTS["research_fixture"],
            },
            previous_receipt_path=tmp_path / "facts-receipt-2.json",
        )


def test_facts_only_rejects_forbidden_fields_in_raw_archive_metadata(tmp_path: Path) -> None:
    base = _inputs()
    original = base.raw_uploads[0]
    contaminated = dict(original)
    observations = []
    for row in original["observations"]:
        assert isinstance(row, dict)
        record = row["archiveRecord"]
        assert isinstance(record, dict)
        observations.append(
            {
                **row,
                "archiveRecord": {**record, "prediction": {"home": 0.9}},
            }
        )
    contaminated["observations"] = observations
    facts = sync.PreparedSitesSyncInputs(
        archive_manifest_sha256=base.archive_manifest_sha256,
        admission_sha256=base.admission_sha256,
        source_ids=base.source_ids,
        raw_uploads=(contaminated, *base.raw_uploads[1:]),
        research_fixture=base.research_fixture,
    )

    with pytest.raises(ValueError, match="forbidden field"):
        sync.preflight_sites_sync(
            inputs=facts,
            lane="research_facts_only",
            mode="full",
            endpoints={
                "raw_artifact": ENDPOINTS["raw_artifact"],
                "research_fixture": ENDPOINTS["research_fixture"],
            },
            previous_receipt_path=tmp_path / "facts-raw-contaminated.json",
        )


def test_cli_facts_only_dry_run_never_executes_or_requires_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = _inputs()
    facts = sync.PreparedSitesSyncInputs(
        archive_manifest_sha256=base.archive_manifest_sha256,
        admission_sha256=base.admission_sha256,
        source_ids=base.source_ids,
        raw_uploads=base.raw_uploads,
        research_fixture=base.research_fixture,
    )
    monkeypatch.setattr(sync, "prepare_sites_sync_inputs", lambda **_kwargs: facts)
    monkeypatch.setattr(
        sync,
        "execute_sites_sync",
        lambda *_args, **_kwargs: pytest.fail("facts-only dry run must not execute requests"),
    )

    assert (
        sync.main(
            [
                "--lane",
                "research_facts_only",
                "--raw-endpoint",
                ENDPOINTS["raw_artifact"],
                "--research-endpoint",
                ENDPOINTS["research_fixture"],
                "--receipt",
                str(tmp_path / "facts-receipt.json"),
                "--cursor",
                str(tmp_path / "facts-cursor.json"),
                "--dry-run",
            ]
        )
        == 0
    )
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "dry_run"
    assert value["lane"] == "research_facts_only"
    assert value["plannedRequests"] == len(base.raw_uploads) + 1
    assert not (tmp_path / "facts-receipt.json").exists()
    assert not (tmp_path / "facts-cursor.json").exists()
