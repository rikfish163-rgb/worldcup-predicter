from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
import pytest

from league_platform import publish_research_fixtures as publisher


SOURCE_ID = "openfootball:football.json:2026-27:en.1"
SOURCE_URL = (
    "https://raw.githubusercontent.com/openfootball/football.json/"
    "master/2026-27/en.1.json"
)


def _fixture() -> dict:
    home = "Arsenal FC"
    away = "Chelsea FC"
    identity = hashlib.sha256(
        f"{SOURCE_ID}|2026-27|{home.casefold()}|{away.casefold()}".encode()
    ).hexdigest()[:24]
    return {
        "id": f"openfootball:premier-league:{identity}",
        "competition_id": "premier-league",
        "season": "2026-27",
        "round": "Matchday 1",
        "kickoff_date": "2026-08-29",
        "kickoff_at": "2026-08-29T14:00:00+00:00",
        "kickoff_time_quality": "exact",
        "kickoff_time_source": "explicit",
        "kickoff_timezone": "Europe/London",
        "home_team": home,
        "away_team": away,
        "status": "upcoming",
        "score": None,
        "halftime_score": None,
        "result_scope": "regulation_90",
        "provider_fixture_ids": {
            "OpenFootball": f"openfootball:premier-league:{identity}"
        },
        "source": {
            "name": "OpenFootball",
            "source_id": SOURCE_ID,
            "url": SOURCE_URL,
            "retrieved_at": "2026-08-27T14:00:00+00:00",
            "raw_sha256": "a" * 64,
            "hash": f"sha256:{'a' * 64}",
            "license": "CC0-1.0",
        },
        "lineage": {
            "source_id": SOURCE_ID,
            "url": SOURCE_URL,
            "retrieved_at": "2026-08-27T14:00:00+00:00",
            "raw_sha256": "a" * 64,
            "hash": f"sha256:{'a' * 64}",
            "license": "CC0-1.0",
            "parser": "openfootball_json_v1",
            "record_index": 0,
            "line_number": 1,
            "record_path": "$.matches[0]",
            "source_row_id": f"{SOURCE_ID}#$.matches[0]",
            "date_inherited": False,
            "time_inherited": False,
        },
    }


def _snapshot() -> dict:
    fixture = _fixture()
    return {
        "as_of": "2026-08-27T14:00:00+00:00",
        "fixture_feed": {
            "provider": "OpenFootball",
            "status": "ok",
            "fixtures": [fixture],
        },
    }


def _admission() -> dict:
    return {
        "status": "verified_current_raw",
        "admission_sha256": "b" * 64,
        "rows_sha256": "c" * 64,
        "source_ids": [SOURCE_ID],
        "published_fixture_count": 1,
    }


def test_compact_snapshot_contains_only_raw_backed_facts_and_no_prediction_fields() -> None:
    payload = publisher.build_research_fixture_snapshot(
        _snapshot(),
        archive_manifest_sha256="d" * 64,
        verified_admission=_admission(),
    )

    assert payload["schemaVersion"] == "matchline.research_fixture_snapshot.v1"
    assert payload["sourcePolicyIds"] == ["openfootball_current"]
    assert payload["fixtureCount"] == 1
    assert payload["fixtures"][0]["source"]["rawSha256"] == "a" * 64
    encoded = publisher.canonical_json_bytes(payload)
    assert b"prediction" not in encoded.lower()
    assert b"market" not in encoded.lower()


def test_non_openfootball_or_unverified_snapshot_is_rejected() -> None:
    snapshot = _snapshot()
    snapshot["fixture_feed"]["fixtures"][0]["source"]["name"] = "ESPN"
    with pytest.raises(ValueError, match="OpenFootball"):
        publisher.build_research_fixture_snapshot(
            snapshot,
            archive_manifest_sha256="d" * 64,
            verified_admission=_admission(),
        )
    with pytest.raises(ValueError, match="verified"):
        publisher.build_research_fixture_snapshot(
            _snapshot(),
            archive_manifest_sha256="d" * 64,
            verified_admission={**_admission(), "status": "candidate_only"},
        )


def test_research_attestation_uses_raw_archive_gate_not_maturity() -> None:
    payload = publisher.build_research_fixture_snapshot(
        _snapshot(),
        archive_manifest_sha256="d" * 64,
        verified_admission=_admission(),
    )
    body = publisher.canonical_json_bytes(payload)
    issued_at = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)
    headers = publisher.research_fixture_attestation_headers(
        body,
        endpoint="https://predict.example/api/research-fixtures",
        key_id="publisher-key-1",
        signing_secret="test-producer-signing-secret-at-least-32-bytes",
        archive_manifest_sha256="d" * 64,
        issued_at=issued_at,
    )
    canonical = base64.urlsafe_b64decode(
        headers["X-Matchline-Producer-Attestation"] + "="
        * (-len(headers["X-Matchline-Producer-Attestation"]) % 4)
    )
    attestation = json.loads(canonical)
    assert attestation["stream"] == "research_fixture_snapshot"
    assert "maturityReceiptRawSha256" not in attestation
    expected = hmac.new(
        b"test-producer-signing-secret-at-least-32-bytes",
        canonical,
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-Matchline-Producer-Signature"] == f"v1={expected}"


def test_strict_ack_binds_snapshot_identity() -> None:
    payload = publisher.build_research_fixture_snapshot(
        _snapshot(),
        archive_manifest_sha256="d" * 64,
        verified_admission=_admission(),
    )
    digest = hashlib.sha256(publisher.canonical_json_bytes(payload)).hexdigest()
    expected = {
        "status": "ok",
        "created": 1,
        "snapshotId": 7,
        "snapshotSha256": digest,
        "fixtureCount": 1,
        "objectKey": f"research-fixtures/sha256/{digest[:2]}/{digest}.json",
        "archiveManifestSha256": "d" * 64,
    }
    assert publisher.validate_research_fixture_ack(payload, expected) == expected
    expected["fixtureCount"] = 0
    with pytest.raises(ValueError, match="response contract"):
        publisher.validate_research_fixture_ack(payload, expected)
