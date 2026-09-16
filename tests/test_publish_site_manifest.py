from __future__ import annotations

import hashlib
import json

import pytest

import league_platform.publish_site_manifest as publisher
from league_platform.publish_intelligence import ProducerAttestationContext
from league_platform.publish_site_manifest import (
    build_site_publication_barrier,
    build_site_publication_payload,
    publish_site_manifest,
)


LOCK = "a" * 64


def _payload() -> dict:
    return build_site_publication_payload(
        publication_epoch="cycle-2026-08-27T10:00:00Z",
        source_as_of="2026-08-27T09:59:00+00:00",
        model_version=f"strict-v260@lock-{LOCK[:16]}",
        model_lock_sha256=LOCK,
        fixtures={"status": "ok", "fixtures": 844},
        intelligence={"status": "ok", "rows": 0},
        source_health={"status": "degraded", "published": 12, "reason": "rights blocked"},
        lineups={"status": "degraded", "published": 6, "reason": "rights pending"},
        forecast={"groups": 0, "stageRows": 0, "published": 0, "skipped": []},
        stage_evaluations={"status": "ok", "stageRows": 0},
    )


def test_builder_produces_closed_counts_and_explicit_not_applicable_phases() -> None:
    payload = _payload()
    assert payload["sourceAsOf"] == "2026-08-27T09:59:00.000Z"
    assert payload["counts"] == {
        "fixtures": 844,
        "intelligenceObservations": 0,
        "predictionGroups": 0,
        "predictionStages": 0,
        "stageEvaluations": 0,
    }
    assert payload["phases"]["forecast"]["status"] == "not_applicable"
    assert payload["phases"]["sourceHealth"]["status"] == "degraded"


def test_barrier_is_a_zero_row_pending_manifest_for_the_same_epoch() -> None:
    payload = build_site_publication_barrier(
        publication_epoch="cycle-2026-08-27T10:00:00Z",
        source_as_of="2026-08-27T09:59:00+00:00",
        model_version=f"strict-v260@lock-{LOCK[:16]}",
        model_lock_sha256=LOCK,
    )
    assert payload["status"] == "publishing"
    assert set(payload["counts"].values()) == {0}
    assert {phase["status"] for phase in payload["phases"].values()} == {"pending"}
    assert {phase["records"] for phase in payload["phases"].values()} == {0}


def test_builder_rejects_unbound_model_identity() -> None:
    with pytest.raises(ValueError, match="not lock-bound"):
        build_site_publication_payload(
            publication_epoch="cycle-one",
            source_as_of="2026-08-27T09:59:00Z",
            model_version="mutable-model-name",
            model_lock_sha256=LOCK,
            fixtures={},
            intelligence={},
            source_health={},
            lineups={},
            forecast={},
            stage_evaluations={},
        )


def test_dry_run_hashes_manifest_without_network_or_maturity_context() -> None:
    payload = _payload()
    result = publish_site_manifest(
        payload,
        endpoint="https://invalid.example/api/publication/register",
        token="",
        publication_epoch=payload["publicationEpoch"],
        maturity_context=None,
        dry_run=True,
    )
    expected = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert result == {
        "status": "dry_run",
        "published": 0,
        "requests": 0,
        "manifestSha256": expected,
    }


def test_non_dry_run_emits_independent_verifier_headers_alongside_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    captured: dict[str, object] = {}

    def fake_publish(payload_value: dict, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        digest = hashlib.sha256(
            json.dumps(
                payload_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "status": "ok",
            "accepted": 1,
            "created": 1,
            "publicationId": 1,
            "publicationEpoch": payload_value["publicationEpoch"],
            "manifestSha256": digest,
        }

    monkeypatch.setattr(
        publisher,
        "require_publication_maturity",
        lambda *_args, **_kwargs: {"status": "pass", "receiptSha256": "c" * 64},
    )
    monkeypatch.setattr(
        publisher,
        "build_producer_attestation_context",
        lambda **_kwargs: ProducerAttestationContext(
            key_id="publisher-key-1",
            signing_secret=b"producer-signing-secret-material-123456",
            stream="publication_manifest",
            rights_use_case="audit_only",
            source_policy_ids=("openfootball_current",),
            publication_epoch=payload["publicationEpoch"],
            maturity_receipt_raw_sha256="c" * 64,
            live_snapshot_raw_sha256="d" * 64,
        ),
    )

    result = publisher.publish_site_manifest(
        payload,
        endpoint="https://predict.example/api/publication/register",
        token=hashlib.sha256(b"fixture-auth").hexdigest(),
        publication_epoch=payload["publicationEpoch"],
        maturity_context=object(),
        dry_run=False,
        independent_verifier_key_id="verifier-key-1",
        independent_verifier_signing_secret="independent-verifier-secret-material-123456",
        publish_payload_fn=fake_publish,
    )

    assert result["status"] == "ok"
    headers = captured.get("independent_verifier_headers")
    assert isinstance(headers, dict)
    assert "X-Matchline-Independent-Verifier-Proof" in headers
    assert "X-Matchline-Independent-Verifier-Signature" in headers
