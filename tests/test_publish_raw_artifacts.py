from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import shutil
import subprocess
import tempfile
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
)
from league_platform import publish_raw_artifacts as publisher


SOURCE_ID = "openfootball:football.json:2026-27:en.1"


def _archive(tmp_path: Path) -> Path:
    root = tmp_path / "openfootball-raw"
    config = OPENFOOTBALL_SOURCES[SOURCE_ID]
    archive = OpenFootballRawArchive(root)
    payload = b'{"matches":[]}\n'
    for minute in (0, 5):
        archive.store(
            OpenFootballRawObservation(
                source_id=SOURCE_ID,
                url=config["url"],
                retrieved_at=datetime(2026, 8, 27, 10, minute, tzinfo=timezone.utc),
                payload=payload,
                source_format=config["format"],
                competition_id=config["competition_id"],
                season=config["season"],
                timezone_name=config["timezone"],
                license="CC0-1.0",
            )
        )
    return root


def test_verified_archive_is_grouped_by_content_without_losing_observations(
    tmp_path: Path,
) -> None:
    root = _archive(tmp_path)
    batches = publisher.build_raw_artifact_uploads(root)

    assert len(batches) == 1
    batch = batches[0]
    assert batch["schemaVersion"] == "matchline.raw_artifact_upload.v1"
    assert batch["raw"]["contentType"] == "application/json"
    assert len(batch["observations"]) == 2
    assert len({row["archiveRecord"]["observation_id"] for row in batch["observations"]}) == 2
    raw = base64.b64decode(batch["raw"]["base64"], validate=True)
    assert hashlib.sha256(raw).hexdigest() == batch["raw"]["sha256"]
    assert (
        batch["archiveManifestSha256"]
        == hashlib.sha256((root / "manifest.jsonl").read_bytes()).hexdigest()
    )


def test_attestation_is_canonical_and_binds_manifest_body_and_destination(tmp_path: Path) -> None:
    batch = publisher.build_raw_artifact_uploads(_archive(tmp_path))[0]
    body = publisher.canonical_json_bytes(batch)
    issued_at = datetime(2026, 8, 27, 11, 0, tzinfo=timezone.utc)
    headers = publisher.raw_artifact_attestation_headers(
        body,
        endpoint="https://matchline.example/api/raw-artifacts",
        key_id="publisher-key-1",
        signing_secret="local-test-hmac-material-" * 2,
        source_policy_ids=("openfootball_current",),
        archive_manifest_sha256=batch["archiveManifestSha256"],
        issued_at=issued_at,
    )

    encoded = headers["X-Matchline-Producer-Attestation"]
    canonical = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    attestation = json.loads(canonical)
    assert attestation == {
        "archiveManifestSha256": batch["archiveManifestSha256"],
        "bodySha256": hashlib.sha256(body).hexdigest(),
        "destination": "https://matchline.example/api/raw-artifacts",
        "expiresAt": (issued_at + timedelta(seconds=300))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "issuedAt": issued_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "keyId": "publisher-key-1",
        "producer": {"name": "matchline-python-publisher", "version": "v260"},
        "rightsUseCase": "redistribution",
        "schemaVersion": "matchline.raw_artifact_attestation.v1",
        "sourcePolicyIds": ["openfootball_current"],
        "stream": "raw_artifact",
    }
    expected = hmac.new(
        ("local-test-hmac-material-" * 2).encode(), canonical, hashlib.sha256
    ).hexdigest()
    assert headers["X-Matchline-Producer-Signature"] == f"v1={expected}"


def test_publisher_requires_durable_archive_before_any_request() -> None:
    if not Path("/dev/shm").is_dir():
        pytest.skip("the production volatile-path contract requires /dev/shm")
    with tempfile.TemporaryDirectory(prefix="matchline-raw-test-", dir="/dev/shm") as directory:
        root = _archive(Path(directory))
        calls: list[object] = []

        with pytest.raises(ValueError, match="durable"):
            publisher.upload_openfootball_raw_archive(
                root,
                endpoint="https://matchline.example/api/raw-artifacts",
                token=hashlib.sha256(b"fixture-auth").hexdigest(),
                key_id="publisher-key-1",
                signing_secret="local-test-hmac-material-" * 2,
                sender=lambda *args, **kwargs: calls.append((args, kwargs)) or {},
            )

        assert calls == []


def test_strict_ack_rejects_partial_or_wrong_object_response(tmp_path: Path) -> None:
    batch = publisher.build_raw_artifact_uploads(_archive(tmp_path))[0]
    body = publisher.canonical_json_bytes(batch)
    observation_ids = sorted(
        item["archiveRecord"]["observation_id"] for item in batch["observations"]
    )
    expected = {
        "status": "ok",
        "acceptedObservations": 2,
        "createdObject": 1,
        "createdObservations": 2,
        "duplicateObservations": 0,
        "rawSha256": batch["raw"]["sha256"],
        "objectKey": (
            f"openfootball/raw/sha256/{batch['raw']['sha256'][:2]}/{batch['raw']['sha256']}.raw"
        ),
        "observationSetSha256": hashlib.sha256(
            publisher.canonical_json_bytes(observation_ids)
        ).hexdigest(),
        "archiveManifestSha256": batch["archiveManifestSha256"],
    }
    publisher.validate_raw_artifact_ack(batch, expected)
    del expected["acceptedObservations"]
    with pytest.raises(ValueError, match="response contract"):
        publisher.validate_raw_artifact_ack(batch, expected)
    assert body


def test_http_error_includes_bounded_safe_response_context_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = hashlib.sha256(b"fixture-auth").hexdigest()
    body = json.dumps(
        {
            "error": "prospective_audit_validation_failed",
            "reason": "cycle_digest_mismatch",
            "token": token,
        }
    ).encode("utf-8")

    class FailingOpener:
        def open(self, *args: object, **kwargs: object) -> object:
            raise urllib.error.HTTPError(
                "https://matchline.example/api/prospective-audit",
                422,
                "Unprocessable Entity",
                {},
                io.BytesIO(body),
            )

    monkeypatch.setattr(
        publisher.urllib.request,
        "build_opener",
        lambda handler: FailingOpener(),
    )

    with pytest.raises(ValueError) as raised:
        publisher._send_raw_upload(
            endpoint="https://matchline.example/api/prospective-audit",
            body=b"{}",
            headers={},
            token=token,
            timeout=1,
        )

    message = str(raised.value)
    assert "HTTP 422" in message
    assert "prospective_audit_validation_failed" in message
    assert "cycle_digest_mismatch" in message
    assert token not in message


def test_python_raw_attestation_is_accepted_by_sites_typescript_verifier(tmp_path: Path) -> None:
    node = shutil.which("node")
    sites_root = Path(__file__).resolve().parents[1] / "matchline_sites"
    if node is None or not (sites_root / "node_modules" / "tsx").exists():
        pytest.skip("Sites Node/tsx runtime is unavailable")
    batch = publisher.build_raw_artifact_uploads(_archive(tmp_path))[0]
    body = publisher.canonical_json_bytes(batch)
    endpoint = "https://predict.example/api/raw-artifacts"
    headers = publisher.raw_artifact_attestation_headers(
        body,
        endpoint=endpoint,
        key_id="publisher-key-1",
        signing_secret="test-producer-signing-secret-at-least-32-bytes",
        source_policy_ids=("openfootball_current",),
        archive_manifest_sha256=batch["archiveManifestSha256"],
        issued_at=datetime.fromisoformat("2026-08-27T11:00:00+00:00"),
    )
    node_program = """
import fs from "node:fs";
import { verifyRawArtifactProducerAttestation } from "./app/api/d1-source-rights-policy.ts";
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const decision = await verifyRawArtifactProducerAttestation({
  body: input.body,
  destination: input.endpoint,
  env: {
    MATCHLINE_PRODUCER_KEY_ID: input.keyId,
    MATCHLINE_PRODUCER_SIGNING_SECRET: input.secret,
  },
  expectedArchiveManifestSha256: input.manifestSha256,
  expectedSourcePolicyIds: ["openfootball_current"],
  headers: new Headers(input.headers),
  now: Date.parse("2026-08-27T11:01:00.000Z"),
});
process.stdout.write(JSON.stringify(decision));
"""
    completed = subprocess.run(
        [node, "--import", "tsx", "--input-type=module", "-e", node_program],
        cwd=sites_root,
        input=json.dumps(
            {
                "body": body.decode("utf-8"),
                "endpoint": endpoint,
                "keyId": "publisher-key-1",
                "secret": "test-producer-signing-secret-at-least-32-bytes",
                "manifestSha256": batch["archiveManifestSha256"],
                "headers": headers,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    decision = json.loads(completed.stdout)
    assert decision["allowed"] is True
    assert decision["archiveManifestSha256"] == batch["archiveManifestSha256"]
