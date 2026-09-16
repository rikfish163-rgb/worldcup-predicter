from __future__ import annotations

from datetime import datetime
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import unicodedata

import pytest

from league_platform.publish_forecast import (
    build_forecast_payload,
    build_registration_payload,
    load_archive_rows,
    publish_archive,
    publication_model_version,
    publication_schema_version,
    select_latest_stages,
)
import league_platform.publish_forecast as forecast_publisher
import league_platform.publish_intelligence as intelligence_publisher


_REAL_REQUIRE_PUBLICATION_MATURITY = (
    forecast_publisher.require_publication_maturity
)
_REAL_REQUIRE_PREDICTION_ARCHIVE_IDENTITY = (
    forecast_publisher.require_prediction_archive_identity
)
_REAL_REQUIRE_OPENFOOTBALL_RAW_PRODUCER_RECEIPT = (
    forecast_publisher.require_openfootball_raw_producer_receipt
)


@pytest.fixture(autouse=True)
def _validated_unit_maturity(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        forecast_publisher,
        "require_publication_maturity",
        lambda *_args, **_kwargs: {"status": "pass", "receiptSha256": "d" * 64},
    )
    monkeypatch.setattr(
        forecast_publisher,
        "build_producer_attestation_context",
        lambda *_args, **kwargs: None
        if kwargs.get("dry_run")
        else forecast_publisher.ProducerAttestationContext(
            key_id="test-key",
            signing_secret=b"s" * 32,
            stream=str(kwargs.get("stream")),
            rights_use_case=str(kwargs.get("rights_use_case")),
            source_policy_ids=tuple(
                sorted(kwargs.get("source_policy_ids") or ())
            ),
            publication_epoch="test",
            maturity_receipt_raw_sha256="d" * 64,
            live_snapshot_raw_sha256="e" * 64,
        ),
    )
    monkeypatch.setattr(
        forecast_publisher,
        "require_prediction_archive_identity",
        lambda *_args, **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        forecast_publisher,
        "require_openfootball_raw_producer_receipt",
        lambda _snapshot, *, dry_run: (
            "candidate_only_unverified_raw_provenance"
            if dry_run
            else "verified_test_fixture"
        ),
    )


def _prediction(stage: str, observed: str, cutoff: str) -> dict:
    return {
        "fixture_id": "espn:401882922",
        "competition_id": "la-liga",
        "season": "2026",
        "home_team": "Espanyol",
        "away_team": "Levante",
        "home_team_id": "la-liga:espanol",
        "away_team_id": "la-liga:levante",
        "provider_fixture_id": "401882922",
        "freeze_stage": stage,
        "freeze_cutoff_at": cutoff,
        "prediction_observed_at": observed,
        "model_version": "strict-test-v1",
        "feature_times": ["2026-08-16T16:27:30Z"],
        "live_feature_fields": ["market_1x2", "recent_xg"],
        "expected_goals": {"home": 1.5, "away": 1.2},
        "scoreline_probability": {"home": 0.43, "draw": 0.26, "away": 0.31},
        "scoreline_matrix": {"1-0": 0.2, "2-0": 0.15, "2-1": 0.08, "1-1": 0.16, "0-0": 0.1, "0-1": 0.18, "0-2": 0.13},
        "total_goals_probability": {"0": 0.1, "1": 0.38, "2": 0.44, "3": 0.08, "4": 0.0, "5+": 0.0},
        "half_full_probability": {"H/H": 0.2, "D/D": 0.2, "A/A": 0.6},
        "handicap_probability": None,
        "handicap_line": None,
        "market_probability": {"home": 0.45, "draw": 0.29, "away": 0.26},
        "probability_delta_model_minus_market": {"home": -0.02, "draw": -0.03, "away": 0.05},
        "source_name": "ESPN",
        "source_file": "data/live/current.json",
        "source_sha256": "a" * 64,
        "live_feature_sources": [],
        "model_training_cutoff": "2026-08-01T00:00:00Z",
        "lineup_confirmation_fingerprint": None,
    }


def _context() -> dict:
    return {
        "id": "espn:401882922",
        "season": "2026",
        "kickoff_at": "2026-08-16T17:00:00+00:00",
        "home_team": "Espanyol",
        "away_team": "Levante",
        "home_provider_team_id": "88",
        "away_provider_team_id": "1538",
        "venue": {"name": "RCDE Stadium", "country": "Spain"},
        "status": "finished",
        "source": {
            "name": "ESPN",
            "native_fixture_id": "401882922",
            "commercial_reuse_verified_by_code": True,
        },
    }


def _legacy_live(fixtures: list[dict], *, authorized: bool = True) -> dict:
    section = {
        "provider": "ESPN",
        "fixtures": fixtures,
    }
    if authorized:
        section.update(
            {
                "rights_status": "operator_authorization_reference_supplied",
                "access_allowed": True,
                "authorization_reference": "test-contract:espn-current-serving",
                "commercial_reuse_verified_by_code": True,
            }
        )
    return {"espn": section}


def _openfootball_prediction(stage: str = "t_minus_24h") -> dict:
    prediction = _prediction(
        stage,
        "2026-08-15T17:00:00Z",
        "2026-08-15T17:00:00Z",
    )
    source_id = "openfootball:espana:2026-27:1-liga"
    fixture_id = _openfootball_fixture_id(
        source_id,
        "la-liga",
        "2026-27",
        "Espanyol",
        "Levante",
    )
    prediction.update(
        {
            "fixture_id": fixture_id,
            "provider_fixture_id": fixture_id,
            "season": "2026-27",
            "source_name": "OpenFootball",
            "model_version_sha256": "a" * 64,
            "market_probability": None,
            "probability_delta_model_minus_market": None,
            "live_feature_fields": [],
            "live_feature_sources": [],
        }
    )
    return prediction


def _openfootball_context() -> dict:
    context = _context()
    source_id = "openfootball:espana:2026-27:1-liga"
    fixture_id = _openfootball_fixture_id(
        source_id,
        "la-liga",
        "2026-27",
        "Espanyol",
        "Levante",
    )
    context.update(
        {
            "id": fixture_id,
            "competition_id": "la-liga",
            "season": "2026-27",
            "source": {
                "name": "OpenFootball",
                "source_id": source_id,
                "url": "https://raw.githubusercontent.com/openfootball/espana/master/2026-27/1-liga.txt",
                "license": "CC0-1.0",
                "raw_sha256": "a" * 64,
            },
        }
    )
    context.pop("home_provider_team_id")
    context.pop("away_provider_team_id")
    return context


def _openfootball_fixture_id(
    source_id: str,
    competition_id: str,
    season: str,
    home: str,
    away: str,
) -> str:
    normalized_home = " ".join(
        unicodedata.normalize("NFKC", home).casefold().split()
    )
    normalized_away = " ".join(
        unicodedata.normalize("NFKC", away).casefold().split()
    )
    digest = hashlib.sha256(
        f"{source_id}|{season}|{normalized_home}|{normalized_away}".encode("utf-8")
    ).hexdigest()[:24]
    return f"openfootball:{competition_id}:{digest}"


def _openfootball_live() -> dict:
    return {
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [_openfootball_context()],
        }
    }


def _v260_forecast_payload(
    rows: list[dict],
    *,
    fixture_id: int = 7,
    feature_snapshot_ids: dict[str, int] | None = None,
    model_version_sha256: str | None = "a" * 64,
) -> dict:
    registration = build_registration_payload(
        rows,
        _openfootball_context(),
        model_version_sha256=model_version_sha256,
    )
    snapshots = registration["featureSnapshots"]
    if feature_snapshot_ids is None:
        feature_snapshot_ids = {
            snapshot["freezeStage"]: index + 31
            for index, snapshot in enumerate(snapshots)
        }
    return build_forecast_payload(
        rows,
        fixture_id=fixture_id,
        feature_snapshot_ids=feature_snapshot_ids,
        feature_snapshots=snapshots,
        model_version_sha256=model_version_sha256,
    )


def test_registration_uses_policy_authorized_identity_without_results():
    prediction = _openfootball_prediction()
    value = build_registration_payload(
        [prediction],
        _openfootball_context(),
        model_version_sha256="a" * 64,
    )
    assert value["fixture"]["providerFixtureId"] == prediction["fixture_id"]
    assert "actual" not in json.dumps(value).lower()
    assert value["observedAt"] == "2026-08-15T17:00:00Z"
    assert len(value["featureSnapshots"]) == 1
    snapshot = value["featureSnapshots"][0]
    assert snapshot["freezeStage"] == "t_minus_24h"
    assert snapshot["features"]["featureCoverage"] == 0.0
    assert snapshot["provenance"]["materialSources"] == [
        {
            "name": "OpenFootball",
            "url": "https://raw.githubusercontent.com/openfootball/espana/master/2026-27/1-liga.txt",
        }
    ]


def test_lock_digest_is_part_of_d1_publication_identity():
    digest = "A" * 64
    prediction = _openfootball_prediction()
    registration = build_registration_payload(
        [prediction],
        _openfootball_context(),
        model_version_sha256=digest,
    )
    forecast = _v260_forecast_payload(
        [prediction],
        feature_snapshot_ids={"t_minus_24h": 9},
        model_version_sha256=digest,
    )

    assert publication_model_version("strict-test-v1", digest) == "strict-test-v1@lock-aaaaaaaaaaaaaaaa"
    assert publication_schema_version(digest) == "prospective-forecast-v1@lock-aaaaaaaaaaaaaaaa"
    snapshot = registration["featureSnapshots"][0]
    assert snapshot["schemaVersion"] == "prospective-forecast-v1@lock-aaaaaaaaaaaaaaaa"
    assert snapshot["features"]["modelVersion"] == "strict-test-v1@lock-aaaaaaaaaaaaaaaa"
    assert snapshot["provenance"]["modelLockSha256"] == "a" * 64
    assert forecast["modelVersion"] == "strict-test-v1@lock-aaaaaaaaaaaaaaaa"
    assert forecast["modelLockSha256"] == "a" * 64


def test_lock_digest_identity_rejects_malformed_hash():
    try:
        publication_model_version("strict-test-v1", "not-a-digest")
    except ValueError as exc:
        assert "SHA-256 hex digest" in str(exc)
    else:
        raise AssertionError("malformed model lock digest was accepted")


def test_espn_row_metadata_cannot_self_authorize_model_input():
    prediction = _prediction("t_minus_24h", "2026-08-15T17:00:00Z", "2026-08-15T17:00:00Z")
    prediction["provider_fixture_id"] = None
    prediction["market_probability"] = None
    prediction["probability_delta_model_minus_market"] = None
    prediction["live_feature_fields"] = []
    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_schedule_summary:model_input",
    ):
        build_registration_payload(
            [prediction], _context(), model_version_sha256="a" * 64
        )


def test_openfootball_registration_uses_explicit_synthetic_aliases_without_fake_native_ids():
    prediction = _openfootball_prediction()
    context = _openfootball_context()

    value = build_registration_payload(
        [prediction], context, model_version_sha256="a" * 64
    )

    for entity in (value["homeTeam"], value["awayTeam"], value["fixture"]):
        assert entity["provider"] == "Matchline canonical alias"
        assert entity["identityKind"] == "synthetic_alias"
    assert value["homeTeam"]["providerId"] == "la-liga:espanol"
    assert value["awayTeam"]["providerId"] == "la-liga:levante"
    assert value["fixture"]["providerFixtureId"] == prediction["fixture_id"]
    assert value["featureSnapshots"][0]["provenance"]["sourceName"] == "OpenFootball"


def test_cfl_registration_blocks_unverified_commercial_reuse_despite_native_ids():
    prediction = _prediction(
        "t_minus_24h",
        "2026-08-15T17:00:00Z",
        "2026-08-15T17:00:00Z",
    )
    prediction.update(
        {
            "fixture_id": "cfl-official:csl:fixture-one",
            "competition_id": "csl",
            "provider_fixture_id": "fixture-one",
            "market_probability": None,
            "probability_delta_model_minus_market": None,
            "live_feature_fields": [],
        }
    )
    context = _context()
    context.update(
        {
            "id": "cfl-official:csl:fixture-one",
            "native_fixture_id": "fixture-one",
            "source": {
                "name": "CFL official",
                "commercial_reuse_verified": False,
            },
        }
    )

    with pytest.raises(ValueError, match="source_rights_blocked:cfl_official_current:model_input"):
        build_registration_payload(
            [prediction], context, model_version_sha256="a" * 64
        )


def test_blocked_live_feature_source_cannot_launder_through_openfootball():
    prediction = _openfootball_prediction()
    prediction["live_feature_fields"] = ["market_1x2"]
    prediction["live_feature_sources"] = [
        {
            "field": "market_1x2",
            "name": "ESPN",
            "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/fixtures",
            "observed_at": "2026-08-15T16:00:00+00:00",
        }
    ]
    prediction["market_probability"] = {
        "home": 0.45,
        "draw": 0.29,
        "away": 0.26,
    }

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_schedule_summary:model_input",
    ):
        build_registration_payload(
            [prediction],
            _openfootball_context(),
            model_version_sha256="a" * 64,
        )


def test_market_probability_without_source_provenance_is_quarantined():
    prediction = _openfootball_prediction()
    prediction["market_probability"] = {
        "home": 0.45,
        "draw": 0.29,
        "away": 0.26,
    }

    with pytest.raises(
        ValueError,
        match="forecast_feature_source_missing:market_probability",
    ):
        build_registration_payload(
            [prediction],
            _openfootball_context(),
            model_version_sha256="a" * 64,
        )


def test_registration_quarantines_unknown_provider_before_identity_projection():
    prediction = _prediction(
        "t_minus_24h",
        "2026-08-15T17:00:00Z",
        "2026-08-15T17:00:00Z",
    )
    prediction["provider_fixture_id"] = None
    prediction["market_probability"] = None
    prediction["probability_delta_model_minus_market"] = None
    prediction["live_feature_fields"] = []
    context = _context()
    context["id"] = ""
    context["source"] = {"name": "Authorized official", "commercial_reuse_verified": True}
    context.pop("home_provider_team_id")
    context.pop("away_provider_team_id")

    with pytest.raises(
        ValueError,
        match="source_rights_unknown_provider:Authorized official",
    ):
        build_registration_payload(
            [prediction], context, model_version_sha256="a" * 64
        )


def test_forecast_payload_groups_immutable_stages():
    rows = [
        _openfootball_prediction("t_minus_6h"),
        _openfootball_prediction("t_minus_24h"),
    ]
    rows[0]["prediction_observed_at"] = "2026-08-16T11:00:00Z"
    rows[0]["freeze_cutoff_at"] = "2026-08-16T11:00:00Z"
    value = _v260_forecast_payload(
        rows,
        feature_snapshot_ids={"t_minus_24h": 31, "t_minus_6h": 32},
    )
    assert value["fixtureId"] == 7
    assert [item["stage"] for item in value["stages"]] == ["t_minus_24h", "t_minus_6h"]
    assert [item["featureSnapshotId"] for item in value["stages"]] == [31, 32]
    assert set(value) == {
        "fixtureId",
        "modelVersion",
        "modelLockSha256",
        "state",
        "stages",
    }
    assert value["state"] == "research_only"
    assert "actual" not in json.dumps(value).lower()


def test_forecast_payload_rejects_stage_snapshot_source_or_time_drift():
    prediction = _openfootball_prediction()
    registration = build_registration_payload(
        [prediction],
        _openfootball_context(),
        model_version_sha256="a" * 64,
    )
    snapshots = json.loads(json.dumps(registration["featureSnapshots"]))
    snapshots[0]["provenance"]["materialSources"].append(
        {
            "name": "OpenFootball",
            "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
        }
    )
    with pytest.raises(ValueError, match="forecast_stage_material_sources_mismatch"):
        build_forecast_payload(
            [prediction],
            fixture_id=7,
            feature_snapshot_ids={"t_minus_24h": 31},
            feature_snapshots=snapshots,
            model_version_sha256="a" * 64,
        )

    snapshots = json.loads(json.dumps(registration["featureSnapshots"]))
    snapshots[0]["generatedAt"] = "2026-08-15T17:00:01Z"
    with pytest.raises(ValueError, match="forecast_stage_time_mismatch"):
        build_forecast_payload(
            [prediction],
            fixture_id=7,
            feature_snapshot_ids={"t_minus_24h": 31},
            feature_snapshots=snapshots,
            model_version_sha256="a" * 64,
        )


def test_python_v260_forecast_wire_and_hmac_are_accepted_by_sites() -> None:
    node = shutil.which("node")
    sites_root = Path(__file__).resolve().parents[1] / "matchline_sites"
    if node is None or not (sites_root / "node_modules" / "tsx").exists():
        pytest.skip("Sites Node/tsx runtime is unavailable")

    prediction = _openfootball_prediction()
    registration = build_registration_payload(
        [prediction],
        _openfootball_context(),
        model_version_sha256="a" * 64,
    )
    forecast = build_forecast_payload(
        [prediction],
        fixture_id=7,
        feature_snapshot_ids={"t_minus_24h": 31},
        feature_snapshots=registration["featureSnapshots"],
        model_version_sha256="a" * 64,
    )
    issued_at = datetime.fromisoformat("2026-08-25T10:00:00+00:00")
    secret = b"test-producer-signing-secret-at-least-32-bytes"

    def signed(stream: str, endpoint: str, body: bytes) -> dict[str, str]:
        return intelligence_publisher._producer_attestation_headers(
            body,
            endpoint=endpoint,
            context=intelligence_publisher.ProducerAttestationContext(
                key_id="publisher-key-1",
                signing_secret=secret,
                stream=stream,
                rights_use_case="model_input",
                source_policy_ids=("openfootball_current",),
                publication_epoch="epoch-2026-08-25",
                maturity_receipt_raw_sha256="c" * 64,
                live_snapshot_raw_sha256="d" * 64,
            ),
            issued_at=issued_at,
        )

    register_endpoint = "https://predict.example/api/forecast/register"
    forecast_endpoint = "https://predict.example/api/forecast"
    registration_body = intelligence_publisher._canonical_json(registration).encode(
        "utf-8"
    )
    forecast_body = intelligence_publisher._canonical_json(forecast).encode("utf-8")
    input_value = {
        "registrationBody": registration_body.decode("utf-8"),
        "forecastBody": forecast_body.decode("utf-8"),
        "registerEndpoint": register_endpoint,
        "forecastEndpoint": forecast_endpoint,
        "registrationHeaders": signed(
            "forecast_registration", register_endpoint, registration_body
        ),
        "forecastHeaders": signed(
            "forecast_prediction", forecast_endpoint, forecast_body
        ),
        "keyId": "publisher-key-1",
        "secret": secret.decode("utf-8"),
    }
    node_program = """
import fs from "node:fs";
import { validateForecastRegistration } from "./app/api/forecast/register/validation.ts";
import { validateForecastPayload } from "./app/api/forecast/validation.ts";
import { verifyProducerAttestation } from "./app/api/d1-source-rights-policy.ts";
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const registration = validateForecastRegistration(JSON.parse(input.registrationBody));
const forecast = validateForecastPayload(JSON.parse(input.forecastBody));
const env = {
  MATCHLINE_PRODUCER_KEY_ID: input.keyId,
  MATCHLINE_PRODUCER_SIGNING_SECRET: input.secret,
};
const registrationDecision = await verifyProducerAttestation({
  body: input.registrationBody,
  destination: input.registerEndpoint,
  env,
  expectedStream: "forecast_registration",
  expectedRightsUseCase: "model_input",
  expectedSourcePolicyIds: ["openfootball_current"],
  headers: new Headers(input.registrationHeaders),
  now: Date.parse("2026-08-25T10:01:00.000Z"),
});
const forecastDecision = await verifyProducerAttestation({
  body: input.forecastBody,
  destination: input.forecastEndpoint,
  env,
  expectedStream: "forecast_prediction",
  expectedRightsUseCase: "model_input",
  expectedSourcePolicyIds: ["openfootball_current"],
  headers: new Headers(input.forecastHeaders),
  now: Date.parse("2026-08-25T10:01:00.000Z"),
});
process.stdout.write(JSON.stringify({
  registrationAllowed: registrationDecision.allowed,
  forecastAllowed: forecastDecision.allowed,
  registrationStages: registration.featureSnapshots.map((row) => row.freezeStage),
  forecastSnapshotIds: forecast.stages.map((row) => row.featureSnapshotId),
}));
"""
    completed = subprocess.run(
        [node, "--import", "tsx", "--input-type=module", "-e", node_program],
        cwd=sites_root,
        input=json.dumps(input_value),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env={**os.environ, "TMPDIR": "/dev/shm"},
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "registrationAllowed": True,
        "forecastAllowed": True,
        "registrationStages": ["t_minus_24h"],
        "forecastSnapshotIds": [31],
    }


def test_forecast_payload_rejects_invalid_probability_contracts():
    invalid_matrix = _openfootball_prediction()
    invalid_matrix["scoreline_matrix"] = {"1-0": 0.8}
    try:
        _v260_forecast_payload([invalid_matrix])
    except ValueError as exc:
        assert "scoreline_matrix probabilities must sum to one" in str(exc)
    else:
        raise AssertionError("invalid scoreline mass was accepted")

    invalid_key = _openfootball_prediction()
    invalid_key["scoreline_matrix"] = {"1-x": 1.0}
    try:
        _v260_forecast_payload([invalid_key])
    except ValueError as exc:
        assert "scoreline_matrix" in str(exc)
    else:
        raise AssertionError("malformed scoreline key was accepted")

    invalid_total = _openfootball_prediction()
    invalid_total["total_goals_probability"] = {"0": 1.0}
    try:
        _v260_forecast_payload([invalid_total])
    except ValueError as exc:
        assert "total_goals_probability does not match scoreline_matrix" in str(exc)
    else:
        raise AssertionError("inconsistent total-goals distribution was accepted")

    invalid_half_full = _openfootball_prediction()
    invalid_half_full["half_full_probability"] = {"H/H": 0.6, "D/D": 0.1}
    try:
        _v260_forecast_payload([invalid_half_full])
    except ValueError as exc:
        assert "half_full_probability probabilities must sum to one" in str(exc)
    else:
        raise AssertionError("invalid half-full distribution was accepted")

    non_finite = _openfootball_prediction()
    non_finite["scoreline_probability"]["home"] = float("nan")
    try:
        _v260_forecast_payload([non_finite])
    except ValueError as exc:
        assert "1X2 probability" in str(exc)
    else:
        raise AssertionError("non-finite 1X2 probability was accepted")


def test_archive_selection_is_latest_per_stage(tmp_path: Path):
    path = tmp_path / "archive.jsonl"
    rows = [
        {"captured_at": "2026-08-16T10:00:00Z", "conflict": False, "prediction": _prediction("t_minus_24h", "2026-08-15T17:00:00Z", "2026-08-15T17:00:00Z")},
        {"captured_at": "2026-08-16T11:00:00Z", "conflict": False, "prediction": _prediction("t_minus_24h", "2026-08-15T17:00:00Z", "2026-08-15T17:00:00Z")},
        {"captured_at": "2026-08-16T11:00:00Z", "conflict": False, "prediction": _prediction("t_minus_6h", "2026-08-16T11:00:00Z", "2026-08-16T11:00:00Z")},
        {"captured_at": "2026-08-16T12:00:00Z", "conflict": True, "prediction": _prediction("t_minus_90m", "2026-08-16T15:30:00Z", "2026-08-16T15:30:00Z")},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    groups = select_latest_stages(load_archive_rows(path))
    assert len(groups) == 1
    assert {row.prediction["freeze_stage"] for row in groups[0]} == {"t_minus_24h", "t_minus_6h"}


def test_archive_selection_filters_same_model_name_to_current_lock_digest(tmp_path: Path):
    path = tmp_path / "archive.jsonl"
    older = _prediction("t_minus_24h", "2026-08-15T17:00:00Z", "2026-08-15T17:00:00Z")
    current = dict(older)
    current["scoreline_probability"] = {"home": 0.44, "draw": 0.25, "away": 0.31}
    current["scoreline_matrix"] = {"1-0": 0.21, "2-0": 0.15, "2-1": 0.08, "1-1": 0.15, "0-0": 0.1, "0-1": 0.18, "0-2": 0.13}
    rows = [
        {"captured_at": "2026-08-16T11:00:00Z", "conflict": False, "model_version_sha256": "a" * 64, "prediction": older},
        {"captured_at": "2026-08-16T12:00:00Z", "conflict": False, "model_version_sha256": "b" * 64, "prediction": current},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    groups = select_latest_stages(
        load_archive_rows(path),
        model_version="strict-test-v1",
        model_version_sha256="b" * 64,
    )

    assert len(groups) == 1
    assert groups[0][0].record["model_version_sha256"] == "b" * 64
    assert groups[0][0].prediction["scoreline_probability"]["home"] == 0.44


def test_dry_run_does_not_require_network(tmp_path: Path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text(json.dumps({"captured_at": "2026-08-16T11:00:00Z", "conflict": False, "prediction": _openfootball_prediction()}) + "\n", encoding="utf-8")
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    result = publish_archive(archive, live, register_endpoint="https://invalid.example/register", forecast_endpoint="https://invalid.example/forecast", token="", dry_run=True)
    assert result["published"] == 1
    assert result["skipped"] == []


def test_openfootball_forecast_requires_raw_producer_receipt_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        json.dumps(
            {
                "captured_at": "2026-08-16T11:00:00Z",
                "conflict": False,
                "prediction": _openfootball_prediction(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        forecast_publisher,
        "publish_payload",
        lambda _payload, *, endpoint, **_kwargs: calls.append(endpoint),
    )
    monkeypatch.setattr(
        forecast_publisher,
        "require_openfootball_raw_producer_receipt",
        _REAL_REQUIRE_OPENFOOTBALL_RAW_PRODUCER_RECEIPT,
    )

    with pytest.raises(ValueError, match="raw_producer_receipt_required"):
        publish_archive(
            archive,
            live,
            register_endpoint="https://example.com/api/forecast/register",
            forecast_endpoint="https://example.com/api/forecast",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_dry_run_rejects_unlicensed_legacy_espn_snapshot(tmp_path: Path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        json.dumps(
            {
                "captured_at": "2026-08-16T11:00:00Z",
                "conflict": False,
                "prediction": _prediction(
                    "t_minus_24h",
                    "2026-08-15T17:00:00Z",
                    "2026-08-15T17:00:00Z",
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    live = tmp_path / "current.json"
    payload = _legacy_live([_context()], authorized=False)
    live.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="legacy_espn_serving_rights_missing"):
        publish_archive(
            archive,
            live,
            register_endpoint="https://invalid.example/register",
            forecast_endpoint="https://invalid.example/forecast",
            token="",
            dry_run=True,
        )

    assert json.loads(live.read_text(encoding="utf-8")) == payload


def test_invalid_forecast_group_never_posts_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prediction = _openfootball_prediction()
    prediction["scoreline_matrix"] = {"1-0": 0.8}
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        json.dumps(
            {
                "captured_at": "2026-08-16T11:00:00Z",
                "conflict": False,
                "prediction": prediction,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    calls: list[str] = []

    def fake_publish(_payload, *, endpoint, **_kwargs):
        calls.append(endpoint)
        return {
            "status": "ok",
            "fixtureId": 7,
            "featureSnapshotIds": {"t_minus_24h": 9},
            "created": True,
        }

    monkeypatch.setattr(forecast_publisher, "publish_payload", fake_publish)

    result = publish_archive(
        archive,
        live,
        register_endpoint="https://example.com/register",
        forecast_endpoint="https://example.com/forecast",
        token="secret",
        receipts_path=tmp_path / "forecast-receipts.jsonl",
        publication_epoch="production-v1",
    )

    assert result["published"] == 0
    assert result["requests"] == 0
    assert result["skipped"] == [
        {
            "fixtureId": _openfootball_prediction()["fixture_id"],
            "reason": "scoreline_matrix probabilities must sum to one",
        }
    ]
    assert calls == []


def test_stage_snapshot_drift_blocks_every_remote_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prediction = _openfootball_prediction()
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        json.dumps(
            {
                "captured_at": prediction["prediction_observed_at"],
                "conflict": False,
                "prediction": prediction,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    real_builder = forecast_publisher.build_registration_payload

    def drifted_builder(*args, **kwargs):
        registration = real_builder(*args, **kwargs)
        registration["featureSnapshots"][0]["provenance"][
            "materialSources"
        ].append(
            {
                "name": "OpenFootball",
                "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
            }
        )
        return registration

    calls: list[str] = []
    monkeypatch.setattr(
        forecast_publisher, "build_registration_payload", drifted_builder
    )
    monkeypatch.setattr(
        forecast_publisher,
        "publish_payload",
        lambda _payload, *, endpoint, **_kwargs: calls.append(endpoint),
    )

    result = publish_archive(
        archive,
        live,
        register_endpoint="https://example.com/api/forecast/register",
        forecast_endpoint="https://example.com/api/forecast",
        token="secret",
        receipts_path=tmp_path / "receipts.jsonl",
        publication_epoch="production-v1",
    )

    assert result["requests"] == 0
    assert result["published"] == 0
    assert result["skipped"] == [
        {
            "fixtureId": prediction["fixture_id"],
            "reason": "forecast_stage_material_sources_mismatch",
        }
    ]
    assert calls == []


def test_later_unlicensed_group_blocks_all_forecast_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_prediction = _openfootball_prediction()
    first_context = _openfootball_context()
    second_context = _openfootball_context()
    second_context.update({"home_team": "Second Home", "away_team": "Second Away"})
    second_id = _openfootball_fixture_id(
        second_context["source"]["source_id"],
        "la-liga",
        "2026-27",
        "Second Home",
        "Second Away",
    )
    second_context["id"] = second_id
    second_prediction = _openfootball_prediction()
    second_prediction.update({
        "fixture_id": second_id,
        "provider_fixture_id": second_id,
        "home_team": "Second Home",
        "away_team": "Second Away",
        "live_feature_fields": ["market_1x2"],
        "market_probability": {"home": 0.4, "draw": 0.3, "away": 0.3},
        "live_feature_sources": [{
            "field": "market_1x2",
            "name": "ESPN",
            "url": "https://site.api.espn.com/forged-market",
        }],
    })
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        "\n".join(
            json.dumps({
                "captured_at": "2026-08-16T11:00:00Z",
                "conflict": False,
                "prediction": prediction,
            })
            for prediction in (first_prediction, second_prediction)
        )
        + "\n",
        encoding="utf-8",
    )
    live = tmp_path / "current.json"
    live.write_text(json.dumps({
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [first_context, second_context],
        }
    }), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        forecast_publisher,
        "publish_payload",
        lambda _payload, *, endpoint, **_kwargs: calls.append(endpoint),
    )

    result = publish_archive(
        archive,
        live,
        register_endpoint="https://example.com/register",
        forecast_endpoint="https://example.com/forecast",
        token="secret",
        receipts_path=tmp_path / "receipts.jsonl",
        publication_epoch="production-v1",
    )

    assert result["published"] == 0
    assert result["requests"] == 0
    assert calls == []
    assert result["skipped"][0]["reason"] == (
        "source_rights_blocked:espn_schedule_summary:model_input"
    )


def test_different_stage_material_sources_remain_bound_to_their_own_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _openfootball_prediction("t_minus_24h")
    second = _openfootball_prediction("t_minus_6h")
    second["freeze_cutoff_at"] = "2026-08-16T11:00:00Z"
    second["prediction_observed_at"] = "2026-08-16T11:00:00Z"
    second["live_feature_fields"] = ["weather"]
    second["live_feature_sources"] = [
        {
            "field": "weather",
            "name": "OpenFootball",
                "url": (
                    "https://raw.githubusercontent.com/openfootball/football.json/"
                    "master/2026-27/en.1.json"
            ),
            "observed_at": "2026-08-16T10:59:00Z",
        }
    ]
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        "\n".join(
            json.dumps(
                {
                    "captured_at": prediction["prediction_observed_at"],
                    "conflict": False,
                    "prediction": prediction,
                }
            )
            for prediction in (first, second)
        )
        + "\n",
        encoding="utf-8",
    )
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    calls: list[tuple[str, dict]] = []

    def fake_publish(payload, *, endpoint, **_kwargs):
        calls.append((endpoint, payload))
        if endpoint.endswith("register"):
            return {
                "status": "ok",
                "fixtureId": 7,
                "featureSnapshotIds": {
                    "t_minus_24h": 31,
                    "t_minus_6h": 32,
                },
                "created": True,
            }
        return {
            "status": "ok",
            "predictionId": 11,
            "created": True,
            "stagesCreated": 2,
            "stagesSkipped": 0,
        }

    monkeypatch.setattr(forecast_publisher, "publish_payload", fake_publish)
    result = publish_archive(
        archive,
        live,
        register_endpoint="https://example.com/api/forecast/register",
        forecast_endpoint="https://example.com/api/forecast",
        token="secret",
        receipts_path=tmp_path / "receipts.jsonl",
        publication_epoch="production-v1",
    )

    assert result["requests"] == 2
    assert result["published"] == 1
    assert result["skipped"] == []
    assert [endpoint for endpoint, _payload in calls] == [
        "https://example.com/api/forecast/register",
        "https://example.com/api/forecast",
    ]
    registration = calls[0][1]
    forecast = calls[1][1]
    assert "featureSnapshot" not in registration
    assert registration["observedAt"] == "2026-08-16T11:00:00Z"
    assert [item["freezeStage"] for item in registration["featureSnapshots"]] == [
        "t_minus_24h",
        "t_minus_6h",
    ]
    first_sources = registration["featureSnapshots"][0]["provenance"][
        "materialSources"
    ]
    second_sources = registration["featureSnapshots"][1]["provenance"][
        "materialSources"
    ]
    assert first_sources == [{
        "name": "OpenFootball",
        "url": "https://raw.githubusercontent.com/openfootball/espana/master/2026-27/1-liga.txt",
    }]
    assert second_sources == [
        {
            "name": "OpenFootball",
            "url": "https://raw.githubusercontent.com/openfootball/espana/master/2026-27/1-liga.txt",
        },
        {
            "name": "OpenFootball",
            "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
        },
    ]
    assert [item["featureSnapshotId"] for item in forecast["stages"]] == [31, 32]
    assert forecast["stages"][0]["provenance"]["materialSources"] == first_sources
    assert forecast["stages"][1]["provenance"]["materialSources"] == second_sources


def test_forecast_receipt_is_written_only_after_registration_and_prediction(tmp_path: Path, monkeypatch):
    archive = tmp_path / "archive.jsonl"
    archive.write_text(json.dumps({
        "captured_at": "2026-08-16T11:00:00Z",
        "conflict": False,
        "prediction": _openfootball_prediction(),
    }) + "\n", encoding="utf-8")
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    calls: list[str] = []
    attestation_streams: list[str] = []

    def fake_publish(payload, *, endpoint, producer_attestation, **_kwargs):
        calls.append(endpoint)
        attestation_streams.append(producer_attestation.stream)
        if endpoint.endswith("register"):
            return {
                "status": "ok",
                "fixtureId": 7,
                "featureSnapshotIds": {"t_minus_24h": 9},
                "created": True,
            }
        return {"status": "ok", "predictionId": 11, "created": True, "stagesCreated": 1, "stagesSkipped": 0}

    monkeypatch.setattr(forecast_publisher, "publish_payload", fake_publish)
    receipts = tmp_path / "forecast-receipts.jsonl"
    first = publish_archive(
        archive,
        live,
        register_endpoint="https://example.com/register",
        forecast_endpoint="https://example.com/forecast",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )
    second = publish_archive(
        archive,
        live,
        register_endpoint="https://example.com/register",
        forecast_endpoint="https://example.com/forecast",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )
    assert first["published"] == 1
    assert first["requests"] == 2
    assert second["published"] == 0
    assert second["receiptsSkipped"] == 1
    assert len(calls) == 2
    assert attestation_streams == [
        "forecast_registration",
        "forecast_prediction",
    ]

    receipt = json.loads(receipts.read_text(encoding="utf-8"))
    assert receipt["publicationEpoch"] == "production-v1"
    assert receipt["destinations"] == {
        "forecast": "https://example.com/forecast",
        "register": "https://example.com/register",
    }
    assert receipt["actualOutboundPayloadSha256"]
    assert receipt["responseSha256"]


def test_forecast_receipt_does_not_skip_changed_endpoint_or_epoch(tmp_path: Path, monkeypatch):
    archive = tmp_path / "archive.jsonl"
    archive.write_text(json.dumps({
        "captured_at": "2026-08-16T11:00:00Z",
        "conflict": False,
        "prediction": _openfootball_prediction(),
    }) + "\n", encoding="utf-8")
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    calls: list[str] = []

    def fake_publish(_payload, *, endpoint, **_kwargs):
        calls.append(endpoint)
        if endpoint.endswith("register"):
            return {
                "status": "ok",
                "fixtureId": 7,
                "featureSnapshotIds": {"t_minus_24h": 9},
                "created": True,
            }
        return {"status": "ok", "predictionId": 11, "created": True, "stagesCreated": 1, "stagesSkipped": 0}

    monkeypatch.setattr(forecast_publisher, "publish_payload", fake_publish)
    receipts = tmp_path / "forecast-receipts.jsonl"
    cases = (
        ("https://staging.example/register", "https://staging.example/forecast", "deploy-1"),
        ("https://production.example/register", "https://production.example/forecast", "deploy-1"),
        ("https://production.example/register", "https://production.example/forecast", "deploy-2"),
    )
    for register, forecast, epoch in cases:
        result = publish_archive(
            archive,
            live,
            register_endpoint=register,
            forecast_endpoint=forecast,
            token="secret",
            receipts_path=receipts,
            publication_epoch=epoch,
        )
        assert result["requests"] == 2
    assert len(calls) == 6


def test_missing_publication_epoch_fails_before_forecast_registration(tmp_path: Path, monkeypatch):
    archive = tmp_path / "archive.jsonl"
    archive.write_text(json.dumps({
        "captured_at": "2026-08-16T11:00:00Z",
        "conflict": False,
        "prediction": _openfootball_prediction(),
    }) + "\n", encoding="utf-8")
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    calls: list[dict] = []
    monkeypatch.setattr(forecast_publisher, "publish_payload", lambda payload, **_kwargs: calls.append(payload))

    with pytest.raises(ValueError, match="publication_epoch"):
        publish_archive(
            archive,
            live,
            register_endpoint="https://production.example/register",
            forecast_endpoint="https://production.example/forecast",
            token="secret",
        )
    assert calls == []


def test_missing_maturity_context_fails_before_forecast_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        json.dumps(
            {
                "captured_at": "2026-08-16T11:00:00Z",
                "conflict": False,
                "prediction": _openfootball_prediction(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    calls: list[dict] = []
    monkeypatch.setattr(
        forecast_publisher,
        "require_publication_maturity",
        _REAL_REQUIRE_PUBLICATION_MATURITY,
    )
    monkeypatch.setattr(
        forecast_publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="publication_maturity_context"):
        publish_archive(
            archive,
            live,
            register_endpoint="https://production.example/register",
            forecast_endpoint="https://production.example/forecast",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_missing_cycle_archive_identity_fails_before_forecast_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        json.dumps(
            {
                "captured_at": "2026-08-16T11:00:00Z",
                "conflict": False,
                "prediction": _openfootball_prediction(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    calls: list[dict] = []
    monkeypatch.setattr(
        forecast_publisher,
        "require_prediction_archive_identity",
        _REAL_REQUIRE_PREDICTION_ARCHIVE_IDENTITY,
    )
    monkeypatch.setattr(
        forecast_publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="cycle_evidence_path"):
        publish_archive(
            archive,
            live,
            register_endpoint="https://production.example/register",
            forecast_endpoint="https://production.example/forecast",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_empty_forecast_response_never_writes_completed_receipt(tmp_path: Path, monkeypatch):
    archive = tmp_path / "archive.jsonl"
    archive.write_text(json.dumps({
        "captured_at": "2026-08-16T11:00:00Z",
        "conflict": False,
        "prediction": _openfootball_prediction(),
    }) + "\n", encoding="utf-8")
    live = tmp_path / "current.json"
    live.write_text(json.dumps(_openfootball_live()), encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"

    def fake_publish(_payload, *, endpoint, **_kwargs):
        if endpoint.endswith("register"):
            return {
                "status": "ok",
                "fixtureId": 7,
                "featureSnapshotIds": {"t_minus_24h": 9},
                "created": True,
            }
        return {}

    monkeypatch.setattr(forecast_publisher, "publish_payload", fake_publish)
    result = publish_archive(
        archive,
        live,
        register_endpoint="https://production.example/register",
        forecast_endpoint="https://production.example/forecast",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )

    assert result["published"] == 0
    assert result["skipped"][0]["reason"] == "forecast response acknowledgement mismatch"
    assert not receipts.exists()


def test_cli_model_override_cannot_bypass_a_missing_prospective_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict] = []

    def should_not_publish(*_args, **kwargs):
        calls.append(kwargs)
        return {"published": 1, "skipped": []}

    monkeypatch.setattr(forecast_publisher, "publish_archive", should_not_publish)

    exit_code = forecast_publisher.main(
        [
            str(tmp_path / "archive.jsonl"),
            "--live-path",
            str(tmp_path / "current.json"),
            "--lock",
            str(tmp_path / "missing-lock.json"),
            "--cycle-evidence",
            str(tmp_path / "missing-cycle.json"),
            "--model-version",
            "forged-model",
            "--dry-run",
        ]
    )

    assert exit_code == 1
    assert "prospective_lock_not_found" in capsys.readouterr().err
    assert calls == []
