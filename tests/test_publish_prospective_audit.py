from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from league_platform import publish_prospective_audit as publisher


MODEL_SHA = "a" * 64


def _write_json(path: Path, value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _fixture(tmp_path: Path) -> dict[str, Path]:
    model_file = tmp_path / "model.py"
    model_file.write_text("MODEL = 'fixed'\n", encoding="utf-8")
    lock = {
        "status": "pending_prospective_window",
        "locked_at": "2026-08-27T11:20:00+00:00",
        "evaluation_window_started_at": "2026-08-27T11:20:01+00:00",
        "model_name": "strict-model",
        "freeze_model_name": "freeze-model",
        "model_version_sha256": MODEL_SHA,
        "model_files": [
            {
                "path": str(model_file),
                "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
            }
        ],
    }
    lock_path = tmp_path / "lock.json"
    _write_json(lock_path, lock)

    live = {
        "schema_version": "1.0.0",
        "as_of": "2026-08-27T15:37:52.404644+00:00",
        "openfootball_current": {
            "status": "ok",
            "raw_archive_admission": {
                "schema_version": "matchline.openfootball_raw_admission.v1",
                "status": "verified_current_raw",
                "policy_version": "v260",
                "admission_sha256": "b" * 64,
                "rows_sha256": "c" * 64,
                "source_manifest_sha256": "d" * 64,
                "source_ids": ["openfootball:football.json:2026-27:en.1"],
            },
        },
    }
    live_path = tmp_path / "current.json"
    live_sha = _write_json(live_path, live)

    prediction = {
        "fixture_id": "openfootball:premier-league:fixture-1",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-28T19:00:00+00:00",
        "freeze_stage": "t_minus_24h",
        "freeze_cutoff_at": "2026-08-27T19:00:00+00:00",
        "prediction_observed_at": "2026-08-27T19:00:01+00:00",
        "model_version": "freeze-model",
        "probabilities": {"home": 0.4, "draw": 0.3, "away": 0.3},
    }
    archive_row = {
        "schema_version": "1.0.0",
        "model_version_sha256": MODEL_SHA,
        "model_lock_window_started_at": lock["evaluation_window_started_at"],
        "prediction": prediction,
    }
    archive_path = tmp_path / "predictions.jsonl"
    archive_path.write_text(json.dumps(archive_row) + "\n", encoding="utf-8")

    evaluation = {
        "schema_version": "1.0.0",
        "generated_at": "2026-08-27T15:42:25.473609+00:00",
        "status": "pending_prospective_window",
        "scored_n": 0,
        "pending_n": 1,
        "result_conflicts": 0,
        "sample_requirements_met": False,
        "all_required_targets_scored": False,
        "prediction_freezes_verified": False,
        "promotion_eligible": False,
        "production_allowed": False,
        "target_sample_requirements": {
            target: {
                "sample_n": 0,
                "minimum_n": minimum,
                "shortfall_n": minimum,
                "met": False,
            }
            for target, minimum in {
                "three_way": 1000,
                "total_goals": 1000,
                "half_full": 1000,
                "scoreline": 5000,
            }.items()
        },
    }
    evaluation_path = tmp_path / "evaluation.json"
    _write_json(evaluation_path, evaluation)

    cycle = {
        "status": "pending_prospective_window",
        "started_at": "2026-08-27T15:37:50.353388+00:00",
        "finished_at": "2026-08-27T15:42:53.736611+00:00",
        "sync": {
            "as_of": live["as_of"],
            "fixtures": 844,
            "markets": 0,
            "snapshot_sha256": live_sha,
        },
        "capture": {
            "upcoming_fixtures": 74,
            "lineup_observed": 0,
            "predictions": 1,
            "appended": 1,
            "skipped_duplicate": 0,
            "blocked": 1,
            "blocked_diagnostics": {
                "count": 1,
                "by_reason": {"no_freeze_cutoff_observed_before_as_of": 1},
                "items": [
                    {
                        "reason": "no_freeze_cutoff_observed_before_as_of",
                        "competition_id": "premier-league",
                        "next_fixture_id": "openfootball:premier-league:fixture-2",
                        "next_kickoff_at": "2026-08-29T19:00:00+00:00",
                        "next_freeze_stage": "t_minus_24h",
                        "next_freeze_cutoff_at": "2026-08-28T19:00:00+00:00",
                    }
                ],
            },
        },
        "evaluation": evaluation,
    }
    cycle_path = tmp_path / "cycle.json"
    _write_json(cycle_path, cycle)
    return {
        "lock": lock_path,
        "cycle": cycle_path,
        "evaluation": evaluation_path,
        "live": live_path,
        "archive": archive_path,
    }


def _build(paths: dict[str, Path]) -> dict[str, object]:
    return publisher.build_prospective_audit_snapshot(
        lock_path=paths["lock"],
        cycle_path=paths["cycle"],
        evaluation_path=paths["evaluation"],
        live_snapshot_path=paths["live"],
        prediction_archive_path=paths["archive"],
        generated_at=datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc),
    )


def test_builds_probability_free_lock_bound_audit_snapshot(tmp_path: Path) -> None:
    payload = _build(_fixture(tmp_path))

    assert payload["schemaVersion"] == "matchline.prospective_audit_snapshot.v1"
    assert payload["sourcePolicyIds"] == ["openfootball_current"]
    assert payload["model"]["versionSha256"] == MODEL_SHA
    assert payload["cycle"]["fixtureCount"] == 844
    assert payload["capture"]["currentFreezeCount"] == 1
    assert payload["capture"]["stageCounts"] == {
        "t_minus_24h": 1,
        "t_minus_6h": 0,
        "t_minus_90m": 0,
        "lineup_confirmation": 0,
    }
    assert payload["freezeRecords"][0]["observedAt"] == "2026-08-27T19:00:01.000Z"
    assert payload["evaluation"]["productionAllowed"] is False
    assert payload["nextFreezes"][0]["competitionId"] == "premier-league"
    serialized = json.dumps(payload, sort_keys=True)
    assert "probabilities" not in serialized
    assert "scoreline" in serialized  # target name only
    assert "fixture-1" in serialized  # freeze identity metadata only
    assert "/dev/shm" not in serialized


def test_default_snapshot_identity_is_retry_stable_for_the_same_cycle(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    kwargs = {
        "lock_path": paths["lock"],
        "cycle_path": paths["cycle"],
        "evaluation_path": paths["evaluation"],
        "live_snapshot_path": paths["live"],
        "prediction_archive_path": paths["archive"],
    }
    first = publisher.build_prospective_audit_snapshot(**kwargs)
    second = publisher.build_prospective_audit_snapshot(**kwargs)
    assert first == second
    assert first["generatedAt"] == "2026-08-27T15:42:53.736Z"


def test_accepts_runtime_lock_identity_when_evaluation_mirrors_cycle(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    cycle = json.loads(paths["cycle"].read_text(encoding="utf-8"))
    evaluation = json.loads(paths["evaluation"].read_text(encoding="utf-8"))
    lock_identity = {
        "path": "/durable/candidate-lock.json",
        "sha256": "e" * 64,
        "bytes_sha256": "e" * 64,
        "lock_bytes_sha256": "e" * 64,
        "lock_identity_sha256": "f" * 64,
        "fingerprint": "f" * 64,
        "model_version_sha256": MODEL_SHA,
        "locked_at": "2026-08-27T11:20:00+00:00",
        "evaluation_window_started_at": "2026-08-27T11:20:01+00:00",
    }
    cycle["lock_identity"] = lock_identity
    evaluation["lock_identity"] = lock_identity
    _write_json(paths["cycle"], cycle)
    _write_json(paths["evaluation"], evaluation)

    payload = _build(paths)

    assert payload["model"]["versionSha256"] == MODEL_SHA


def test_rejects_evaluation_lock_identity_that_differs_from_cycle(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    cycle = json.loads(paths["cycle"].read_text(encoding="utf-8"))
    evaluation = json.loads(paths["evaluation"].read_text(encoding="utf-8"))
    cycle["lock_identity"] = {"fingerprint": "a" * 64}
    evaluation["lock_identity"] = {"fingerprint": "b" * 64}
    _write_json(paths["cycle"], cycle)
    _write_json(paths["evaluation"], evaluation)

    with pytest.raises(ValueError, match="lock identity"):
        _build(paths)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda paths: paths["live"].write_text("{}", encoding="utf-8"), "snapshot"),
        (lambda paths: paths["archive"].write_text("", encoding="utf-8"), "archive"),
    ],
)
def test_fails_closed_on_unbound_runtime_artifacts(
    tmp_path: Path,
    mutator: object,
    message: str,
) -> None:
    paths = _fixture(tmp_path)
    mutator(paths)  # type: ignore[operator]
    with pytest.raises(ValueError, match=message):
        _build(paths)


def test_attestation_and_ack_are_bound_to_exact_audit_body(tmp_path: Path) -> None:
    payload = _build(_fixture(tmp_path))
    body = publisher.canonical_json_bytes(payload)
    headers = publisher.prospective_audit_attestation_headers(
        body,
        endpoint="https://predict.example/api/prospective-audit",
        key_id="publisher-key-1",
        signing_secret="local-test-hmac-material-" * 2,
        payload=payload,
        issued_at=datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc),
    )
    assert headers["X-Matchline-Producer-Signature"].startswith("v1=")

    body_sha = hashlib.sha256(body).hexdigest()
    response = {
        "status": "ok",
        "created": 1,
        "auditId": 7,
        "snapshotSha256": body_sha,
        "modelVersionSha256": MODEL_SHA,
        "currentFreezeCount": 1,
        "scoredN": 0,
        "pendingN": 1,
    }
    assert publisher.validate_prospective_audit_ack(payload, response)["auditId"] == 7
    with pytest.raises(ValueError, match="response contract"):
        publisher.validate_prospective_audit_ack(
            payload,
            {**response, "pendingN": 2},
        )
