from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from league_platform.intelligence_repair import quarantine_ledger
from league_platform.prospective_archive import append_predictions
from league_platform.runtime_evidence import (
    _archive_summary,
    _intelligence_ledger_summary,
    mirror_runtime_pointers,
    refresh_runtime_evidence,
)


def _valid_lock(
    tmp_path: Path,
    *,
    freeze_model_name: str = "runtime-freeze-model",
) -> dict:
    model_file = tmp_path / "model.py"
    model_file.write_text("locked model\n", encoding="utf-8")
    lock = {
        "status": "pending_prospective_window",
        "model_name": "runtime-model",
        "freeze_model_name": freeze_model_name,
        "model_files": [
            {
                "path": str(model_file),
                "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
            }
        ],
    }
    lock["model_version_sha256"] = hashlib.sha256(
        json.dumps(
            {
                "model_name": lock["model_name"],
                "freeze_model_name": lock["freeze_model_name"],
                "model_files": lock["model_files"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return lock


def _prediction(
    lock: dict,
    *,
    fixture_id: str = "fixture-1",
    freeze_stage: str = "t_minus_24h",
) -> dict:
    cutoff = {
        "t_minus_24h": "2026-08-29T15:00:00+00:00",
        "t_minus_6h": "2026-08-30T09:00:00+00:00",
        "t_minus_90m": "2026-08-30T13:30:00+00:00",
    }[freeze_stage]
    observed = {
        "t_minus_24h": "2026-08-29T16:00:00+00:00",
        "t_minus_6h": "2026-08-30T09:30:00+00:00",
        "t_minus_90m": "2026-08-30T14:00:00+00:00",
    }[freeze_stage]
    return {
        "fixture_id": fixture_id,
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-30T15:00:00+00:00",
        "freeze_stage": freeze_stage,
        "freeze_cutoff_at": cutoff,
        "prediction_observed_at": observed,
        "model_version": lock["freeze_model_name"],
        "feature_times": [],
        "primary_probability_1x2": {"home": 0.4, "draw": 0.3, "away": 0.3},
    }


def test_archive_summary_reports_invalid_json_and_non_object_records(tmp_path: Path):
    lock = _valid_lock(tmp_path)
    archive = tmp_path / "predictions.jsonl"
    append_predictions(
        archive,
        [_prediction(lock)],
        lock=lock,
    )
    with archive.open("a", encoding="utf-8") as stream:
        stream.write("{bad-json\n[]\n")

    summary, stages = _archive_summary(
        archive,
        lock,
        {"scored_n": 0},
    )

    assert summary["status"] == "invalid_records"
    assert summary["physical_records"] == 3
    assert summary["valid_json_records"] == 1
    assert summary["invalid_record_count"] == 2
    assert summary["invalid_json_records"] == 1
    assert summary["non_object_records"] == 1
    assert stages == {"t_minus_24h": 1}


def test_archive_summary_blocks_missing_archive_instead_of_reporting_empty_pass(
    tmp_path: Path,
):
    summary, stages = _archive_summary(
        tmp_path / "missing.jsonl",
        {"model_version_sha256": "a" * 64},
        {"scored_n": 0},
    )

    assert summary["status"] == "missing"
    assert summary["invalid_record_count"] == 0
    assert stages == {}


def test_archive_summary_blocks_empty_current_lock_archive(tmp_path: Path):
    archive = tmp_path / "empty.jsonl"
    archive.write_text("")
    lock = _valid_lock(tmp_path)

    summary, _ = _archive_summary(
        archive,
        lock,
        {"scored_n": 0},
    )

    assert summary["status"] == "empty_current_lock"


def test_archive_summary_rejects_prediction_that_fails_locked_contract(tmp_path: Path):
    lock = _valid_lock(tmp_path)
    archive = tmp_path / "forged.jsonl"
    archive.write_text(
        json.dumps(
            {
                "model_version_sha256": lock["model_version_sha256"],
                "freeze_key": "forged",
                "prediction": {
                    "fixture_id": "forged",
                    "freeze_stage": "t_minus_24h",
                    "freeze_cutoff_at": "not-a-time",
                    "model_version": lock["freeze_model_name"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary, stages = _archive_summary(archive, lock, {"scored_n": 0, "pending_n": 1})

    assert summary["status"] == "invalid_records"
    assert summary["invalid_shape_records"] == 1
    assert summary["current_lock_records"] == 0
    assert stages == {}


def test_archive_summary_classifies_pre_window_schema_as_superseded(tmp_path: Path):
    lock = _valid_lock(tmp_path)
    archive = tmp_path / "legacy.jsonl"
    legacy = {
        "schema_version": "1.0.0",
        "record_key": "a" * 64,
        "freeze_key": "b" * 64,
        "content_sha256": "c" * 64,
        "captured_at": "2026-08-13T18:22:14.777518+00:00",
        "model_version": "strict_dynamic_elo_scoreline_stage_replay_v3_causal_rho_live",
        "model_version_sha256": "d" * 64,
        "conflict": False,
        "conflict_with": [],
        "prediction": {"fixture_id": "legacy:1"},
    }
    archive.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    summary, stages = _archive_summary(archive, lock, {"scored_n": 0})

    assert summary["status"] == "empty_current_lock"
    assert summary["invalid_record_count"] == 0
    assert summary["legacy_or_superseded_records"] == 1
    assert summary["current_lock_records"] == 0
    assert stages == {}


def test_archive_summary_rejects_tampered_lock_aggregate(tmp_path: Path):
    lock = _valid_lock(tmp_path)
    lock["model_version_sha256"] = "f" * 64
    archive = tmp_path / "tampered-lock.jsonl"
    append_predictions(archive, [_prediction(lock)], lock=lock)

    summary, stages = _archive_summary(
        archive,
        lock,
        {"scored_n": 0, "pending_n": 1},
    )

    assert summary["status"] == "invalid_lock"
    assert summary["current_lock_records"] == 0
    assert stages == {}


def test_runtime_refresh_blocks_missing_archive_and_sites_bundle(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    snapshot.write_text(json.dumps({"as_of": "2026-08-25T00:00:00+00:00"}))
    snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    cycle = tmp_path / "cycle.json"
    evaluation = tmp_path / "evaluation.json"
    lock = tmp_path / "lock.json"
    cycle.write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "sync": {"snapshot_sha256": snapshot_sha},
                "capture": {},
            }
        ),
        encoding="utf-8",
    )
    evaluation.write_text(json.dumps({"scored_n": 0, "pending_n": 0}))
    lock.write_text(json.dumps({"model_version_sha256": "m" * 64}))

    result = refresh_runtime_evidence(
        cycle_path=cycle,
        evaluation_path=evaluation,
        lock_path=lock,
        snapshot_path=snapshot,
        bundle_path=tmp_path / "missing-offline.js",
        archive_path=tmp_path / "missing-predictions.jsonl",
    )

    assert result["status"] == "blocked"
    assert "prediction_archive:missing" in result["failures"]
    assert "sites_bundle:missing_or_unreadable" in result["failures"]


def test_runtime_refresh_reconciles_archive_count_with_evaluation(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    bundle = tmp_path / "bundle.js"
    archive = tmp_path / "predictions.jsonl"
    cycle = tmp_path / "cycle.json"
    evaluation = tmp_path / "evaluation.json"
    lock_path = tmp_path / "lock.json"
    lock = _valid_lock(tmp_path)
    snapshot.write_text(json.dumps({"as_of": "2026-08-25T00:00:00+00:00"}))
    bundle.write_text("bundle\n")
    append_predictions(archive, [_prediction(lock)], lock=lock)
    cycle.write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "sync": {"snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()},
                "capture": {"predictions": 2},
            }
        )
    )
    evaluation.write_text(json.dumps({"scored_n": 0, "pending_n": 2}))
    lock_path.write_text(json.dumps(lock))

    result = refresh_runtime_evidence(
        cycle_path=cycle,
        evaluation_path=evaluation,
        lock_path=lock_path,
        snapshot_path=snapshot,
        bundle_path=bundle,
        archive_path=archive,
    )

    assert result["status"] == "blocked"
    assert (
        "prediction_archive.current_lock_records:evaluation_count_mismatch" in result["failures"]
    )
    assert (
        "prediction_archive.current_lock_records:cycle_prediction_mismatch" in result["failures"]
    )


def test_runtime_cli_audits_blocked_state_but_enforcement_exits_nonzero(
    tmp_path: Path,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    snapshot = runtime / "current.json"
    snapshot.write_text(json.dumps({"as_of": "2026-08-25T00:00:00+00:00"}))
    cycle = runtime / "prospective-cycle-latest.json"
    evaluation = runtime / "prospective-evaluation-current.json"
    cycle.write_text(
        json.dumps(
            {
                "status": "unknown",
                "sync": {"snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()},
                "capture": {},
            }
        )
    )
    evaluation.write_text(json.dumps({"scored_n": 0, "pending_n": 0}))
    lock = tmp_path / "lock.json"
    bundle = tmp_path / "bundle.js"
    archive = runtime / "prospective_predictions.jsonl"
    lock.write_text(json.dumps({"model_version_sha256": "m" * 64}))
    bundle.write_text("bundle\n")
    archive.write_text("")
    common = [
        sys.executable,
        "-m",
        "league_platform.runtime_evidence",
        "--runtime-dir",
        str(runtime),
        "--lock",
        str(lock),
        "--snapshot",
        str(snapshot),
        "--bundle",
        str(bundle),
        "--archive",
        str(archive),
        "--audit-output-dir",
        str(tmp_path / "audit"),
        "--mirror-target-dir",
        str(tmp_path / "mirror"),
    ]

    audit = subprocess.run(common, check=False, capture_output=True, text=True)
    enforce = subprocess.run(
        [*common, "--require-passed"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert audit.returncode == 0
    assert json.loads(audit.stdout)["status"] == "blocked"
    assert enforce.returncode != 0
    assert json.loads(enforce.stdout)["status"] == "blocked"


@pytest.mark.parametrize(
    ("cycle_status", "declared_hash"),
    (("pending_prospective_window", "f" * 64), ("blocked_storage", None)),
)
def test_runtime_refresh_blocks_snapshot_hash_mismatch_and_non_success_cycle_status(
    tmp_path: Path,
    cycle_status: str,
    declared_hash: str | None,
):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    snapshot = tmp_path / "current.json"
    snapshot.write_text(json.dumps({"as_of": "2026-08-25T00:00:00+00:00"}), encoding="utf-8")
    actual_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    cycle_hash = declared_hash if declared_hash is not None else actual_hash
    cycle = {
        "status": cycle_status,
        "sync": {"snapshot_sha256": cycle_hash},
        "capture": {},
    }
    evaluation = {"status": "pending_prospective_window", "scored_n": 0, "pending_n": 0}
    cycle_path = evidence / "cycle.json"
    evaluation_path = evidence / "evaluation.json"
    lock_path = evidence / "lock.json"
    pointer = evidence / "lock-integrity.json"
    cycle_path.write_text(json.dumps(cycle), encoding="utf-8")
    evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
    lock_path.write_text(json.dumps({"model_version_sha256": "m" * 64}), encoding="utf-8")
    pointer.write_text("{}", encoding="utf-8")

    result = refresh_runtime_evidence(
        cycle_path=cycle_path,
        evaluation_path=evaluation_path,
        lock_path=lock_path,
        snapshot_path=snapshot,
        bundle_path=tmp_path / "absent-bundle.js",
        archive_path=tmp_path / "absent-archive.jsonl",
        lock_integrity_path=pointer,
    )

    latest = json.loads(pointer.read_text(encoding="utf-8"))["latest_cycle"]
    assert result["status"] == "blocked"
    assert result["snapshot_sha256"] == actual_hash
    assert latest["exit_code"] == 1
    if declared_hash is not None:
        assert result["snapshot_integrity"]["status"] == "blocked"
        assert result["snapshot_integrity"]["declared_sha256"] == declared_hash
        assert result["snapshot_integrity"]["actual_sha256"] == actual_hash


def test_refresh_runtime_evidence_tracks_cycle_hashes_archive_and_oddstorm(tmp_path: Path):
    evidence = tmp_path / "evidence"
    data = tmp_path / "data"
    snapshot_path = data / "current.json"
    bundle_path = tmp_path / "offline_data.js"
    archive_path = data / "predictions.jsonl"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": "2026-08-15T10:14:52+00:00",
                "oddstorm": {
                    "lines": [
                        {
                            "effective_at_source": "observed_at_fallback_no_line_timestamp",
                            "enters_model": False,
                            "model_exclusion_reason": "line_timestamp_unavailable",
                        },
                        {
                            "effective_at_source": "provider_timestamp",
                            "enters_model": False,
                            "model_exclusion_reason": "policy",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    bundle_path.write_text("bundle\n", encoding="utf-8")
    lock = _valid_lock(data)
    cycle = {
        "status": "pending_prospective_window",
        "started_at": "2026-08-15T10:11:22+00:00",
        "finished_at": "2026-08-15T10:15:23+00:00",
        "sync": {
            "as_of": "2026-08-15T10:14:52+00:00",
            "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        },
        "capture": {
            "appended": 2,
            "skipped_duplicate": 13,
            "blocked": 4,
            "live_results": {"conflict_count": 0},
        },
        "market_audit": {"conflicts": 0},
    }
    evaluation = {
        "status": "pending_prospective_window",
        "scored_n": 0,
        "pending_n": 2,
        "result_conflicts": 0,
        "invalid_records": [],
    }
    (evidence / "prospective-cycle-latest.json").parent.mkdir(parents=True)
    (evidence / "prospective-cycle-latest.json").write_text(json.dumps(cycle), encoding="utf-8")
    (evidence / "prospective-evaluation-current.json").write_text(
        json.dumps(evaluation), encoding="utf-8"
    )
    (evidence / "prospective-model-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    append_predictions(
        archive_path,
        [
            _prediction(lock, fixture_id="fixture-24h", freeze_stage="t_minus_24h"),
            _prediction(lock, fixture_id="fixture-6h", freeze_stage="t_minus_6h"),
        ],
        lock=lock,
    )
    legacy_lock = _valid_lock(
        data,
        freeze_model_name="legacy-freeze-model",
    )
    append_predictions(
        archive_path,
        [
            _prediction(
                legacy_lock,
                fixture_id="fixture-legacy",
                freeze_stage="t_minus_90m",
            )
        ],
        lock=legacy_lock,
    )
    lock_integrity = evidence / "lock-integrity.json"
    platform = evidence / "platform.json"
    oddstorm = evidence / "oddstorm.json"
    freeze = evidence / "freeze.json"
    for path in (lock_integrity, platform, oddstorm, freeze):
        path.write_text(json.dumps({}), encoding="utf-8")

    result = refresh_runtime_evidence(
        cycle_path=evidence / "prospective-cycle-latest.json",
        evaluation_path=evidence / "prospective-evaluation-current.json",
        lock_path=evidence / "prospective-model-lock.json",
        snapshot_path=snapshot_path,
        bundle_path=bundle_path,
        archive_path=archive_path,
        lock_integrity_path=lock_integrity,
        platform_path=platform,
        oddstorm_path=oddstorm,
        freeze_path=freeze,
    )

    assert len(result["updated"]) == 4
    assert result["archive"]["physical_records"] == 3
    assert result["archive"]["current_lock_records"] == 2
    assert result["freeze_stage_counts"] == {"t_minus_24h": 1, "t_minus_6h": 1}
    lock_report = json.loads(lock_integrity.read_text(encoding="utf-8"))
    assert (
        lock_report["sites_bundle"]["identity_status"]
        == "file_present_source_identity_not_verified"
    )
    assert "snapshot-to-Sites synchronization pass" not in lock_report["decision"]
    assert json.loads(oddstorm.read_text(encoding="utf-8"))["runtime_snapshot"]["line_count"] == 2
    assert json.loads(freeze.read_text(encoding="utf-8"))["current_lock_rows"] == 2


def test_runtime_pointers_mirror_atomically_without_moving_ledgers(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cycle = runtime / "prospective-cycle-latest.json"
    evaluation = runtime / "prospective-evaluation-current.json"
    cycle.write_text(json.dumps({"status": "pending"}), encoding="utf-8")
    evaluation.write_text(json.dumps({"scored_n": 0}), encoding="utf-8")
    targets = tmp_path / "docs" / "evidence"

    result = mirror_runtime_pointers(
        runtime,
        cycle_target=targets / cycle.name,
        evaluation_target=targets / evaluation.name,
    )

    assert result["status"] == "mirrored"
    assert len(result["pointers"]) == 2
    assert (targets / cycle.name).read_text(encoding="utf-8") == cycle.read_text(encoding="utf-8")
    assert (targets / evaluation.name).read_text(encoding="utf-8") == evaluation.read_text(
        encoding="utf-8"
    )
    assert not list(targets.glob("*.mirror.*.tmp"))


def test_runtime_evidence_can_isolate_mutable_audit_outputs_from_model_lock(tmp_path: Path):
    model_evidence = tmp_path / "model-evidence"
    runtime = tmp_path / "runtime"
    model_evidence.mkdir()
    runtime.mkdir()
    lock = model_evidence / "prospective-model-lock.json"
    cycle = runtime / "runtime-only-cycle-latest.json"
    evaluation = runtime / "runtime-only-evaluation-current.json"
    snapshot = runtime / "current.json"
    bundle = runtime / "offline.js"
    archive = runtime / "prospective_predictions.jsonl"
    model_pointer = model_evidence / "prospective-lock-integrity-2026-08-14-v16-latest.json"
    runtime_pointer = runtime / model_pointer.name

    lock.write_text(json.dumps({"model_version_sha256": "m" * 64}), encoding="utf-8")
    snapshot.write_text(json.dumps({"as_of": "2026-08-24T16:02:10+00:00"}), encoding="utf-8")
    cycle.write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "sync": {"snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()},
                "capture": {},
            }
        ),
        encoding="utf-8",
    )
    evaluation.write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "scored_n": 0,
                "pending_n": 3,
                "result_conflicts": 0,
                "invalid_records": [],
            }
        ),
        encoding="utf-8",
    )
    bundle.write_text("bundle\n", encoding="utf-8")
    archive.write_text("", encoding="utf-8")
    model_pointer.write_text(json.dumps({"owner": "model"}), encoding="utf-8")
    runtime_pointer.write_text(json.dumps({"owner": "runtime"}), encoding="utf-8")

    result = refresh_runtime_evidence(
        cycle_path=cycle,
        evaluation_path=evaluation,
        lock_path=lock,
        snapshot_path=snapshot,
        bundle_path=bundle,
        archive_path=archive,
        audit_output_dir=runtime,
    )

    assert json.loads(model_pointer.read_text(encoding="utf-8")) == {"owner": "model"}
    runtime_report = json.loads(runtime_pointer.read_text(encoding="utf-8"))
    assert runtime_report["owner"] == "runtime"
    assert runtime_report["model_version_sha256"] == "m" * 64
    assert str(runtime_pointer) in result["updated"]


def test_runtime_evidence_reports_intelligence_ledger_read_diagnostics(tmp_path: Path):
    evidence = tmp_path / "evidence"
    data = tmp_path / "data"
    evidence.mkdir(parents=True)
    data.mkdir(parents=True)
    snapshot_path = data / "current.json"
    bundle_path = tmp_path / "offline_data.js"
    archive_path = data / "predictions.jsonl"
    ledger_path = data / "observations.jsonl"
    snapshot_path.write_text(json.dumps({"as_of": "2026-08-15T10:14:52+00:00"}), encoding="utf-8")
    bundle_path.write_text("bundle\n", encoding="utf-8")
    ledger_path.write_text('{"ok": true}\n{bad-json\n', encoding="utf-8")
    lock_sha = "m" * 64
    (evidence / "prospective-cycle-latest.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "sync": {
                    "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
                },
                "capture": {},
            }
        ),
        encoding="utf-8",
    )
    (evidence / "prospective-evaluation-current.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "scored_n": 0,
                "pending_n": 0,
                "result_conflicts": 0,
                "invalid_records": [],
            }
        ),
        encoding="utf-8",
    )
    (evidence / "prospective-model-lock.json").write_text(
        json.dumps({"model_version_sha256": lock_sha}), encoding="utf-8"
    )
    lock_integrity = evidence / "lock-integrity.json"
    platform = evidence / "platform.json"
    lock_integrity.write_text(json.dumps({}), encoding="utf-8")
    platform.write_text(json.dumps({}), encoding="utf-8")

    result = refresh_runtime_evidence(
        cycle_path=evidence / "prospective-cycle-latest.json",
        evaluation_path=evidence / "prospective-evaluation-current.json",
        lock_path=evidence / "prospective-model-lock.json",
        snapshot_path=snapshot_path,
        bundle_path=bundle_path,
        archive_path=archive_path,
        intelligence_ledger_path=ledger_path,
        lock_integrity_path=lock_integrity,
        platform_path=platform,
    )

    summary = result["intelligence_ledger"]
    assert summary["status"] == "diagnostic_read_errors"
    assert summary["physical_records"] == 2
    assert summary["valid_json_records"] == 1
    assert summary["read_error_count"] == 1
    assert summary["read_errors"][0]["line"] == 2
    assert (
        json.loads(lock_integrity.read_text(encoding="utf-8"))["intelligence_ledger"][
            "read_error_count"
        ]
        == 1
    )
    assert (
        "diagnostic_read_errors"
        in json.loads(platform.read_text(encoding="utf-8"))["verification"]["intelligence_ledger"]
    )


def test_runtime_evidence_distinguishes_quarantined_recoverable_line(tmp_path: Path):
    ledger = tmp_path / "observations.jsonl"
    quarantine = tmp_path / "observations.quarantine.jsonl"
    first = json.dumps({"broken": True})[:-2]
    second = json.dumps({"complete": True})
    ledger.write_text(first + second + "\n", encoding="utf-8")

    quarantine_ledger(ledger, quarantine, write=True)
    summary = _intelligence_ledger_summary(ledger, quarantine_path=quarantine)

    assert summary["status"] == "quarantined_read_errors"
    assert summary["read_error_count"] == 1
    assert summary["quarantined_read_error_count"] == 1
    assert summary["unquarantined_read_error_count"] == 0
    assert summary["recovered_json_records"] == 0


def test_runtime_evidence_uses_default_quarantine_sidecar_for_default_ledger(tmp_path: Path):
    ledger = tmp_path / "observations.jsonl"
    quarantine = tmp_path / "observations.quarantine.jsonl"
    ledger.write_text('{"broken": true\n', encoding="utf-8")

    quarantine_ledger(ledger, quarantine, write=True)
    summary = _intelligence_ledger_summary(ledger)

    assert summary["quarantine_path"] == str(quarantine)
    assert summary["status"] == "quarantined_read_errors"
    assert summary["quarantined_read_error_count"] == 1


def test_refresh_runtime_evidence_collapses_repeated_lineup_poll(tmp_path: Path):
    evidence = tmp_path / "evidence"
    data = tmp_path / "data"
    evidence.mkdir(parents=True)
    data.mkdir(parents=True)
    snapshot_path = data / "current.json"
    bundle_path = tmp_path / "offline_data.js"
    archive_path = data / "predictions.jsonl"
    snapshot_path.write_text(json.dumps({"as_of": "2026-08-15T10:14:52+00:00"}), encoding="utf-8")
    bundle_path.write_text("bundle\n", encoding="utf-8")
    lock = _valid_lock(data)
    cycle = {
        "status": "pending_prospective_window",
        "sync": {"snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest()},
        "capture": {},
    }
    evaluation = {
        "status": "pending_prospective_window",
        "scored_n": 0,
        "pending_n": 1,
        "result_conflicts": 0,
        "invalid_records": [],
    }
    (evidence / "prospective-cycle-latest.json").write_text(json.dumps(cycle), encoding="utf-8")
    (evidence / "prospective-evaluation-current.json").write_text(
        json.dumps(evaluation), encoding="utf-8"
    )
    (evidence / "prospective-model-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    base = {
        "fixture_id": "espn:1",
        "competition_id": "csl",
        "kickoff_at": "2026-08-30T18:00:00+00:00",
        "freeze_stage": "lineup_confirmation",
        "freeze_cutoff_at": "2026-08-30T16:00:00+00:00",
        "lineup_observed_at": "2026-08-30T16:00:00+00:00",
        "prediction_observed_at": "2026-08-30T16:05:00+00:00",
        "model_version": lock["freeze_model_name"],
        "primary_probability_1x2": {"home": 0.4, "draw": 0.3, "away": 0.3},
        "lineup_confirmation_fingerprint": "c" * 64,
    }
    append_predictions(archive_path, [base], lock=lock)
    archived_line = archive_path.read_text(encoding="utf-8").strip()
    archive_path.write_text(f"{archived_line}\n{archived_line}\n", encoding="utf-8")
    result = refresh_runtime_evidence(
        cycle_path=evidence / "prospective-cycle-latest.json",
        evaluation_path=evidence / "prospective-evaluation-current.json",
        lock_path=evidence / "prospective-model-lock.json",
        snapshot_path=snapshot_path,
        bundle_path=bundle_path,
        archive_path=archive_path,
    )
    assert result["archive"]["physical_records"] == 2
    assert result["archive"]["current_lock_records"] == 1
    assert result["freeze_stage_counts"] == {"lineup_confirmation": 1}


def test_runtime_platform_pointer_reports_current_lock_identity(tmp_path: Path):
    evidence = tmp_path / "evidence"
    data = tmp_path / "data"
    evidence.mkdir(parents=True)
    data.mkdir(parents=True)
    snapshot_path = data / "current.json"
    bundle_path = tmp_path / "offline_data.js"
    archive_path = data / "predictions.jsonl"
    snapshot_path.write_text(json.dumps({"as_of": "2026-08-15T10:14:52+00:00"}), encoding="utf-8")
    bundle_path.write_text("bundle\n", encoding="utf-8")
    lock_sha = "m" * 64
    (evidence / "prospective-cycle-latest.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "sync": {
                    "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
                },
                "capture": {},
            }
        ),
        encoding="utf-8",
    )
    (evidence / "prospective-evaluation-current.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "scored_n": 0,
                "pending_n": 0,
                "result_conflicts": 0,
                "invalid_records": [],
            }
        ),
        encoding="utf-8",
    )
    (evidence / "prospective-model-lock.json").write_text(
        json.dumps({"model_version_sha256": lock_sha}), encoding="utf-8"
    )
    platform = evidence / "platform.json"
    verified_at = "2026-08-01T00:00:00+00:00"
    stale_identity = {"strict_report_sha256": "stale-report"}
    platform.write_text(
        json.dumps(
            {
                "checked_at": verified_at,
                "evidence_identity": stale_identity,
            }
        ),
        encoding="utf-8",
    )
    refresh_runtime_evidence(
        cycle_path=evidence / "prospective-cycle-latest.json",
        evaluation_path=evidence / "prospective-evaluation-current.json",
        lock_path=evidence / "prospective-model-lock.json",
        snapshot_path=snapshot_path,
        bundle_path=bundle_path,
        archive_path=archive_path,
        platform_path=platform,
    )
    report = json.loads(platform.read_text(encoding="utf-8"))
    assert f"lock_model_sha256={lock_sha}" in report["verification"]["latest_cycle"]
    assert "v19 lock accepted" not in report["verification"]["latest_cycle"]
    assert report["checked_at"] == verified_at
    assert report["evidence_identity"] == stale_identity
    assert report["runtime_pointer_refreshed_at"] > verified_at


def test_runtime_refreshes_current_model_selection_audit(tmp_path: Path):
    evidence = tmp_path / "evidence"
    data = tmp_path / "data"
    evidence.mkdir(parents=True)
    data.mkdir(parents=True)
    snapshot_path = data / "current.json"
    bundle_path = tmp_path / "offline_data.js"
    archive_path = data / "predictions.jsonl"
    snapshot_path.write_text(json.dumps({"as_of": "2026-08-15T10:14:52+00:00"}), encoding="utf-8")
    bundle_path.write_text("bundle\n", encoding="utf-8")
    lock_sha = "m" * 64
    (evidence / "prospective-cycle-latest.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "sync": {
                    "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
                },
                "capture": {},
            }
        ),
        encoding="utf-8",
    )
    (evidence / "prospective-evaluation-current.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "scored_n": 0,
                "pending_n": 0,
                "result_conflicts": 0,
                "invalid_records": [],
            }
        ),
        encoding="utf-8",
    )
    (evidence / "prospective-model-lock.json").write_text(
        json.dumps({"model_version_sha256": lock_sha}), encoding="utf-8"
    )
    selection = evidence / "model-selection-audit-current.json"
    selection.write_text(json.dumps({}), encoding="utf-8")
    result = refresh_runtime_evidence(
        cycle_path=evidence / "prospective-cycle-latest.json",
        evaluation_path=evidence / "prospective-evaluation-current.json",
        lock_path=evidence / "prospective-model-lock.json",
        snapshot_path=snapshot_path,
        bundle_path=bundle_path,
        archive_path=archive_path,
        selection_path=selection,
    )
    assert str(selection) in result["updated"]
    report = json.loads(selection.read_text(encoding="utf-8"))
    assert report["prospective_lock"]["model_version_sha256"] == lock_sha
    assert report["gate_status"] == "blocked_selection_debt"
