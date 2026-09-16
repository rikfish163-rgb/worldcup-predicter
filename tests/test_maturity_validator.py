from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from league_platform.live_sources.openfootball_live import OPENFOOTBALL_HISTORY_SOURCES
from league_platform.maturity_validator import validate_maturity


LEAGUES = (
    "premier-league",
    "championship",
    "la-liga",
    "bundesliga",
    "serie-a",
    "ligue-1",
)


def _strict_report() -> dict:
    target_samples = {
        "three_way": 1200,
        "totals": 1200,
        "half_full": 1200,
        "handicap": 1200,
    }
    stages = {
        name: {"status": "evaluated", "time_audit": {"status": "pass"}}
        for name in ("t_minus_24h", "t_minus_6h", "t_minus_90m", "lineup_confirmation")
    }
    source_ids = sorted(
        next(
            source_id
            for source_id, config in sorted(OPENFOOTBALL_HISTORY_SOURCES.items())
            if config["competition_id"] == competition_id
        )
        for competition_id in LEAGUES
    )
    admission = {
        "schema_version": "matchline.openfootball_verified_history_admission.v1",
        "status": "training_admitted",
        "training_admitted": True,
        "training_source": "verified_openfootball_raw_archive_only",
        "admission_scope": "historical_training",
        "loader_contract_version": "1.0.0",
        "adapter_contract_version": "1.0.0",
        "observed_before": "2026-08-24T21:02:00+00:00",
        "source_ids": source_ids,
        "competition_ids": sorted(LEAGUES),
        "selected_records": [
            {
                "source_id": source_id,
                "retrieved_at": "2026-08-24T21:02:00+00:00",
                "record_sha256": hashlib.sha256(f"record:{source_id}".encode()).hexdigest(),
                "raw_sha256": hashlib.sha256(f"raw:{source_id}".encode()).hexdigest(),
            }
            for source_id in source_ids
        ],
        "raw_admission_sha256": "1" * 64,
        "raw_manifest_sha256": "2" * 64,
        "source_manifest_sha256": "3" * 64,
        "source_identity_sha256": "4" * 64,
        "training_policy_sha256": "5" * 64,
        "parser_contract_sha256": "6" * 64,
        "raw_rows_sha256": "7" * 64,
        "domain_rows_sha256": "8" * 64,
        "counts": {
            "raw_rows": 6,
            "finished_admitted": 6,
            "upcoming_isolated": 0,
            "exact_kickoff_admitted": 6,
            "date_only_admitted": 0,
            "competition_rows": {league: 1 for league in LEAGUES},
        },
        "statement": (
            "Only rows bound by this manifest are admitted as OpenFootball "
            "historical training data."
        ),
    }
    admission["admission_sha256"] = _canonical_sha256(
        {
            key: value
            for key, value in admission.items()
            if key not in {"raw_manifest_sha256", "statement"}
        }
    )
    report = {
        "schema_version": "matchline.strict_report.v260",
        "report_lane": "formal_v260",
        "generated_at": "2026-08-24T21:02:08+00:00",
        "training_source_contract": {
            "provider": "OpenFootball",
            "raw_archive_required": True,
            "training_admission_sha256": admission["admission_sha256"],
            "policy_version": "v260",
            "observed_before": admission["observed_before"],
            "source_ids": source_ids,
            "prohibited_formal_inputs": [
                "football_data_csv",
                "legacy_csl_files",
                "snapshot_self_report",
            ],
        },
        "training_admission": admission,
        "overall": {
            "chronological_cutoff_gate": "pass",
            "probability_contract_gate": "pass",
            "sample_threshold_gate": "partial_with_explicit_blocks",
            "frequency_baseline_gate": "pass_within_tolerance",
            "market_gate": "pass",
            "freeze_stage_gate": "pass",
            "model_selection_gate": "pass_prospective_independent_test",
            "production_allowed": False,
            "frequency_baseline_failures": [],
            "availability_blocks": ["csl:no_declared_verified_raw_history_source"],
            "market_failures": [],
            "probability_contract_failures": [],
            "freeze_stage_failures": [],
            "model_selection_failures": [],
        },
        "combined": {
            "scoreline_sample_n": 6000,
            "scoreline_gate": {"status": "research_ready", "sample_n": 6000},
        },
        "leagues": {
            league: {
                "gates": {
                    target: {
                        "status": "research_ready",
                        "sample_n": sample,
                        "sample_threshold": 1000,
                        "failures": [],
                    }
                    for target, sample in target_samples.items()
                },
                "time_audit": {
                    "status": "pass",
                    "future_leakage_violations": 0,
                    "market_bound_violations": 0,
                    "feature_time_violations": 0,
                },
                "freeze_stages": stages,
            }
            for league in LEAGUES
        },
        "model_selection_audit": {
            "gate_status": "pass_prospective_independent_test",
            "production_eligible": True,
            "prospective_failures": [],
        },
    }
    report["leagues"]["csl"] = {
        "status": "unavailable",
        "reason": "no_declared_verified_raw_history_source",
        "sample_n": 0,
        "gates": {
            target: {"status": "blocked", "sample_n": 0, "failures": ["insufficient_sample"]}
            for target in (
                "three_way",
                "totals",
                "total_over_under",
                "half_full",
                "handicap",
            )
        },
        "time_audit": {"status": "unavailable"},
        "freeze_stages": {},
    }
    return report


def _canonical_sha256(value: dict) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_lock() -> dict:
    lock = {
        "status": "pending_prospective_window",
        "model_name": "strict-model",
        "freeze_model_name": "strict-freeze",
        "model_files": [{"path": "league_platform/model.py", "sha256": "c" * 64}],
        "locked_at": "2026-08-24T20:35:28+00:00",
        "evaluation_window_started_at": "2026-08-24T20:35:29+00:00",
    }
    lock["model_version_sha256"] = _canonical_sha256(
        {
            "model_name": lock["model_name"],
            "freeze_model_name": lock["freeze_model_name"],
            "model_files": lock["model_files"],
        }
    )
    return lock


def _platform_verification(report: dict, lock: dict | None = None) -> dict:
    identity = {"strict_report_sha256": _canonical_sha256(report)}
    if lock is not None:
        identity.update(
            {
                "prospective_lock_sha256": _canonical_sha256(lock),
                "model_version_sha256": lock["model_version_sha256"],
            }
        )
    maturity = {
        key: "pass"
        for key in (
            "prediction_traceability_gate",
            "append_only_source_isolation_gate",
            "calibration_gate",
            "low_quality_degradation_gate",
            "independent_target_scoring_gate",
            "sites_d1_consistency_gate",
            "sites_api_mobile_gate",
            "test_suite_gate",
        )
    }
    gate_inputs = {
        "prediction_traceability_gate": {
            "strict_report",
            "prospective_lock",
            "cycle",
            "evaluation",
            "offline_snapshot",
            "live_snapshot",
            "prediction_archive",
            "publication_diagnostic",
        },
        "append_only_source_isolation_gate": {
            "prospective_lock",
            "cycle",
            "evaluation",
            "prediction_archive",
        },
        "calibration_gate": {"strict_report"},
        "low_quality_degradation_gate": {
            "strict_report",
            "prospective_lock",
            "offline_snapshot",
        },
        "independent_target_scoring_gate": {"strict_report"},
        "sites_d1_consistency_gate": {
            "prospective_lock",
            "offline_snapshot",
            "publication_audit",
            "live_snapshot",
            "publication_diagnostic",
        },
        "sites_api_mobile_gate": {
            "strict_report",
            "prospective_lock",
            "offline_snapshot",
            "sites_build_evidence",
            "test_evidence",
        },
        "test_suite_gate": {
            "strict_report",
            "prospective_lock",
            "offline_snapshot",
            "sites_build_evidence",
            "test_evidence",
        },
    }
    if lock is None:
        for names in gate_inputs.values():
            names.discard("prospective_lock")
    input_names = (
        "strict_report",
        "prospective_lock",
        "cycle",
        "evaluation",
        "offline_snapshot",
        "sites_build_evidence",
        "test_evidence",
        "publication_audit",
        "live_snapshot",
        "prediction_archive",
        "publication_diagnostic",
    )
    inputs = {
        name: {
            "present": name != "prospective_lock" or lock is not None,
            "schema_version": None,
            "canonical_sha256": (
                _canonical_sha256(report)
                if name == "strict_report"
                else _canonical_sha256(lock)
                if name == "prospective_lock" and lock is not None
                else hashlib.sha256(name.encode("utf-8")).hexdigest()
                if name != "prospective_lock"
                else None
            ),
        }
        for name in input_names
    }
    inputs["live_snapshot"].update({"path": "/runtime/current.json", "raw_file_sha256": "1" * 64})
    inputs["prediction_archive"].update(
        {
            "path": "/runtime/prospective_predictions.jsonl",
            "raw_file_sha256": "2" * 64,
            "canonical_sha256": None,
        }
    )
    inputs["publication_diagnostic"].update(
        {"path": "/runtime/current.publication.json", "raw_file_sha256": "3" * 64}
    )
    admission = report["training_admission"]
    contract = report["training_source_contract"]
    inputs["strict_report"].update(
        {
            "schema_version": report["schema_version"],
            "report_lane": report["report_lane"],
            "policy_version": contract["policy_version"],
            "training_admission_sha256": admission["admission_sha256"],
            "observed_before": admission["observed_before"],
            "source_ids": admission["source_ids"],
        }
    )
    return {
        "schema_version": "matchline.platform_verification.v2",
        "checked_at": "2026-08-24T22:00:00+00:00",
        "status": "pass",
        "passed": True,
        "evidence_identity": identity,
        "identity_validation": {"status": "pass", "reasons": []},
        "strict_report_validation": {"status": "pass", "reasons": []},
        "maturity": maturity,
        "gate_evidence": {
            key: {
                "status": "pass",
                "reasons": [],
                "evidence": sorted(gate_inputs[key]),
            }
            for key in maturity
        },
        "inputs": inputs,
    }


def test_validator_keeps_release_blocked_without_a_declared_csl_raw_source():
    report = _strict_report()
    result = validate_maturity(
        report,
        platform_verification=_platform_verification(report),
        require_artifact_bindings=False,
    )
    assert result["status"] == "blocked"
    assert result["passed"] is False
    assert "overall.production_allowed:false" in result["failures"]
    assert "csl.three_way:insufficient_sample" in result["failures"]


def test_validator_defaults_fail_closed_for_label_only_platform_evidence():
    report = _strict_report()

    result = validate_maturity(
        report,
        platform_verification=_platform_verification(report),
    )

    assert result["status"] == "blocked"
    assert "artifact_bindings:missing" in result["failures"]


def test_validator_exposes_market_freeze_selection_and_platform_blocks():
    report = _strict_report()
    report["overall"].update(
        {
            "market_gate": "research_only_underperforms_market",
            "freeze_stage_gate": "partial_with_explicit_blocks",
            "model_selection_gate": "blocked_selection_debt",
            "production_allowed": False,
            "market_failures": ["premier-league:three_way"],
            "freeze_stage_failures": ["csl:lineup_confirmation"],
            "model_selection_failures": ["prospective_lock_missing"],
        }
    )
    report["leagues"]["csl"]["freeze_stages"]["lineup_confirmation"] = {"status": "unavailable"}
    verification = _platform_verification(report)
    verification["maturity"]["sites_d1_consistency_gate"] = "blocked"

    result = validate_maturity(report, platform_verification=verification)

    assert result["status"] == "blocked"
    assert result["passed"] is False
    assert "overall.market_gate:research_only_underperforms_market" in result["failures"]
    assert "overall.freeze_stage_gate:partial_with_explicit_blocks" in result["failures"]
    assert "overall.model_selection_gate:blocked_selection_debt" in result["failures"]
    assert "csl.lineup_confirmation:not_evaluated" in result["failures"]
    assert "platform.sites_d1_consistency_gate:blocked" in result["failures"]


def test_validator_requires_every_league_target_and_scoreline_threshold():
    report = _strict_report()
    del report["leagues"]["csl"]["gates"]["handicap"]
    report["combined"]["scoreline_sample_n"] = 4999

    result = validate_maturity(
        report,
        platform_verification=_platform_verification(report),
    )

    assert "csl.handicap:missing" in result["failures"]
    assert "combined.scoreline:insufficient_sample" in result["failures"]


def test_validator_blocks_stale_nested_prospective_lock_identity():
    report = _strict_report()
    lock = _valid_lock()
    lock["evaluation_window"] = "2026/27 after v39"
    report["model_selection_audit"]["prospective_lock"] = dict(lock)

    result = validate_maturity(
        report,
        platform_verification=_platform_verification(report, lock),
        prospective_lock=lock,
        require_artifact_bindings=False,
    )
    assert result["status"] == "blocked"
    assert "overall.production_allowed:false" in result["failures"]
    assert not any("strict_report.prospective_lock" in reason for reason in result["failures"])

    report["model_selection_audit"]["prospective_lock"]["evaluation_window"] = "2026/27 after v38"
    stale = validate_maturity(
        report,
        platform_verification=_platform_verification(report, lock),
        prospective_lock=lock,
        require_artifact_bindings=False,
    )
    assert stale["status"] == "blocked"
    assert "strict_report.prospective_lock.evaluation_window:mismatch" in stale["failures"]


def test_validator_blocks_stale_platform_evidence_even_when_all_gates_pass():
    stale_report = _strict_report()
    stale_report["generated_at"] = "2026-08-14T00:00:00+00:00"
    platform = _platform_verification(stale_report)
    current_report = _strict_report()
    current_report["generated_at"] = "2026-08-25T00:00:00+00:00"

    result = validate_maturity(
        current_report,
        platform_verification=platform,
    )

    assert result["status"] == "blocked"
    assert "platform.evidence_identity.strict_report_sha256:mismatch" in result["failures"]


def test_validator_requires_platform_evidence_identity():
    report = _strict_report()
    platform = _platform_verification(report)
    del platform["evidence_identity"]

    result = validate_maturity(report, platform_verification=platform)

    assert result["status"] == "blocked"
    assert "platform.evidence_identity:missing" in result["failures"]


def test_validator_binds_platform_evidence_to_current_prospective_lock():
    report = _strict_report()
    lock = _valid_lock()
    lock["evaluation_window"] = "current-window"
    report["model_selection_audit"]["prospective_lock"] = dict(lock)
    platform = _platform_verification(report)

    result = validate_maturity(
        report,
        platform_verification=platform,
        prospective_lock=lock,
    )

    assert result["status"] == "blocked"
    assert "platform.evidence_identity.prospective_lock_sha256:missing" in result["failures"]
    assert "platform.evidence_identity.model_version_sha256:missing" in result["failures"]


def test_validator_recomputes_lock_aggregate_before_accepting_platform_labels():
    report = _strict_report()
    lock = _valid_lock()
    lock["model_version_sha256"] = "f" * 64
    report["model_selection_audit"]["prospective_lock"] = dict(lock)

    result = validate_maturity(
        report,
        platform_verification=_platform_verification(report, lock),
        prospective_lock=lock,
    )

    assert result["status"] == "blocked"
    assert "prospective_lock.model_version_sha256:aggregate_mismatch" in result["failures"]


def test_validator_rejects_all_pass_labels_without_platform_evidence_contract():
    report = _strict_report()
    weak_platform = _platform_verification(report)
    for key in (
        "schema_version",
        "checked_at",
        "status",
        "passed",
        "identity_validation",
        "gate_evidence",
        "inputs",
    ):
        del weak_platform[key]

    result = validate_maturity(report, platform_verification=weak_platform)

    assert result["status"] == "blocked"
    assert "platform.schema_version:missing" in result["failures"]
    assert "platform.status:missing" in result["failures"]
    assert "platform.passed:missing" in result["failures"]
    assert "platform.gate_evidence:missing" in result["failures"]
    assert "platform.inputs:missing" in result["failures"]


def test_validator_rejects_gate_details_without_required_artifact_references():
    report = _strict_report()
    platform = _platform_verification(report)
    platform["gate_evidence"]["prediction_traceability_gate"]["evidence"] = ["strict_report"]

    result = validate_maturity(report, platform_verification=platform)

    assert result["status"] == "blocked"
    assert (
        "platform.gate_evidence.prediction_traceability_gate.evidence:incomplete"
        in result["failures"]
    )


def test_cli_audit_writes_blocked_receipt_but_enforcement_returns_nonzero(tmp_path: Path):
    report = _strict_report()
    report["overall"]["production_allowed"] = False
    strict_path = tmp_path / "strict.json"
    platform_path = tmp_path / "platform.json"
    audit_output = tmp_path / "audit.json"
    enforcement_output = tmp_path / "enforcement.json"
    strict_path.write_text(json.dumps(report), encoding="utf-8")
    platform_path.write_text(
        json.dumps(_platform_verification(report)),
        encoding="utf-8",
    )
    common = [
        sys.executable,
        "-m",
        "league_platform.maturity_validator",
        "--strict-report",
        str(strict_path),
        "--platform-verification",
        str(platform_path),
        "--prospective-lock",
        str(tmp_path / "absent-lock.json"),
    ]

    audit = subprocess.run(
        [*common, "--output", str(audit_output)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    enforce = subprocess.run(
        [*common, "--output", str(enforcement_output), "--require-passed"],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert audit.returncode == 0
    assert json.loads(audit_output.read_text(encoding="utf-8"))["status"] == "blocked"
    assert enforce.returncode != 0
    assert json.loads(enforcement_output.read_text(encoding="utf-8"))["status"] == "blocked"


def test_cli_enforcement_rejects_label_only_passing_platform_evidence(tmp_path: Path):
    report = _strict_report()
    lock = _valid_lock()
    report["model_selection_audit"]["prospective_lock"] = dict(lock)
    strict_path = tmp_path / "strict.json"
    platform_path = tmp_path / "platform.json"
    lock_path = tmp_path / "lock.json"
    output = tmp_path / "enforcement.json"
    platform = _platform_verification(report, lock)
    platform["checked_at"] = datetime.now(timezone.utc).isoformat()
    strict_path.write_text(json.dumps(report), encoding="utf-8")
    platform_path.write_text(json.dumps(platform), encoding="utf-8")
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "league_platform.maturity_validator",
            "--strict-report",
            str(strict_path),
            "--platform-verification",
            str(platform_path),
            "--prospective-lock",
            str(lock_path),
            "--output",
            str(output),
            "--max-platform-age-seconds",
            "1800",
            "--require-passed",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked"
    assert "artifact_bindings.cycle.path:missing" in receipt["failures"]


def test_cli_enforcement_fails_closed_when_prospective_lock_is_missing(tmp_path: Path):
    report = _strict_report()
    strict_path = tmp_path / "strict.json"
    platform_path = tmp_path / "platform.json"
    output = tmp_path / "enforcement.json"
    platform = _platform_verification(report)
    platform["checked_at"] = datetime.now(timezone.utc).isoformat()
    strict_path.write_text(json.dumps(report), encoding="utf-8")
    platform_path.write_text(json.dumps(platform), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "league_platform.maturity_validator",
            "--strict-report",
            str(strict_path),
            "--platform-verification",
            str(platform_path),
            "--prospective-lock",
            str(tmp_path / "absent-lock.json"),
            "--output",
            str(output),
            "--require-passed",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "prospective_lock:missing" in json.loads(output.read_text(encoding="utf-8"))["failures"]


def test_cli_enforcement_rejects_expired_platform_evidence(tmp_path: Path):
    report = _strict_report()
    strict_path = tmp_path / "strict.json"
    platform_path = tmp_path / "platform.json"
    output = tmp_path / "enforcement.json"
    platform = _platform_verification(report)
    platform["checked_at"] = "2000-01-01T00:00:00+00:00"
    strict_path.write_text(json.dumps(report), encoding="utf-8")
    platform_path.write_text(json.dumps(platform), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "league_platform.maturity_validator",
            "--strict-report",
            str(strict_path),
            "--platform-verification",
            str(platform_path),
            "--prospective-lock",
            str(tmp_path / "absent-lock.json"),
            "--output",
            str(output),
            "--max-platform-age-seconds",
            "1800",
            "--require-passed",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert (
        "platform.checked_at:expired" in json.loads(output.read_text(encoding="utf-8"))["failures"]
    )


def test_cli_enforcement_blocks_when_raw_platform_inputs_are_unbound(
    tmp_path: Path,
):
    from league_platform.maturity_validator import validate_maturity_receipt

    runtime_root = tmp_path / "runtime"
    repository_root = tmp_path / "repository"
    runtime_root.mkdir()
    (repository_root / "docs/evidence").mkdir(parents=True)
    report = _strict_report()
    lock = _valid_lock()
    report["model_selection_audit"]["prospective_lock"] = dict(lock)
    platform = _platform_verification(report, lock)
    platform["checked_at"] = datetime.now(timezone.utc).isoformat()

    strict_path = runtime_root / "strict.json"
    platform_path = runtime_root / "platform.json"
    cycle_path = runtime_root / "cycle.json"
    live_path = runtime_root / "current.json"
    offline_path = runtime_root / "offline_snapshot.json"
    diagnostic_path = runtime_root / "current.publication.json"
    lock_path = repository_root / "docs/evidence/lock.json"
    output = runtime_root / "maturity.json"
    live = {
        "schema_version": "1.0.0",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "fixtures": [],
    }
    live_path.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    live_raw_sha = hashlib.sha256(live_path.read_bytes()).hexdigest()
    cycle = {
        "schema_version": "1.0.0",
        "sync": {"snapshot_sha256": live_raw_sha},
    }
    offline = {"schema_version": "matchline.offline_snapshot.v1"}
    diagnostic = {"schema_version": "1.0.0", "status": "published"}
    platform["inputs"]["prospective_lock"]["path"] = str(lock_path)
    platform["inputs"]["cycle"].update(
        {"path": str(cycle_path), "canonical_sha256": _canonical_sha256(cycle)}
    )
    platform["inputs"]["offline_snapshot"].update(
        {
            "path": str(offline_path),
            "canonical_sha256": _canonical_sha256(offline),
        }
    )
    platform["inputs"]["live_snapshot"] = {
        "present": True,
        "path": str(live_path),
        "schema_version": live["schema_version"],
        "raw_file_sha256": live_raw_sha,
        "canonical_content_sha256": _canonical_sha256(live),
        "canonical_sha256": _canonical_sha256(live),
    }
    platform["inputs"]["publication_diagnostic"] = {
        "present": True,
        "path": str(diagnostic_path),
        "schema_version": diagnostic["schema_version"],
        "raw_file_sha256": hashlib.sha256(json.dumps(diagnostic).encode("utf-8")).hexdigest(),
        "canonical_content_sha256": _canonical_sha256(diagnostic),
        "canonical_sha256": _canonical_sha256(diagnostic),
    }
    strict_path.write_text(json.dumps(report), encoding="utf-8")
    cycle_path.write_text(json.dumps(cycle), encoding="utf-8")
    offline_path.write_text(json.dumps(offline), encoding="utf-8")
    diagnostic_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    platform["inputs"]["publication_diagnostic"]["raw_file_sha256"] = hashlib.sha256(
        diagnostic_path.read_bytes()
    ).hexdigest()
    platform_path.write_text(json.dumps(platform), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "league_platform.maturity_validator",
            "--strict-report",
            str(strict_path),
            "--platform-verification",
            str(platform_path),
            "--prospective-lock",
            str(lock_path),
            "--cycle",
            str(cycle_path),
            "--live-snapshot",
            str(live_path),
            "--offline-snapshot",
            str(offline_path),
            "--publication-diagnostic",
            str(diagnostic_path),
            "--runtime-root",
            str(runtime_root),
            "--repository-root",
            str(repository_root),
            "--output",
            str(output),
            "--max-platform-age-seconds",
            "1800",
            "--require-passed",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "matchline.maturity_gate.v2"
    assert set(receipt["artifact_bindings"]) == {
        "platform_verification",
        "strict_report",
        "prospective_lock",
        "cycle",
        "evaluation",
        "offline_snapshot",
        "sites_build_evidence",
        "test_evidence",
        "publication_audit",
        "live_snapshot",
        "prediction_archive",
        "publication_diagnostic",
    }
    missing_paths = {
        "strict_report": runtime_root / "missing-strict.json",
        "evaluation": runtime_root / "missing-evaluation.json",
        "prediction_archive": runtime_root / "missing-archive.jsonl",
        "sites_build_evidence": repository_root / "missing-build.json",
        "test_evidence": repository_root / "missing-tests.json",
        "publication_audit": repository_root / "missing-publication.json",
    }
    forged_receipt = json.loads(json.dumps(receipt))
    forged_receipt.update({"status": "passed", "passed": True, "failures": []})

    unbound = validate_maturity_receipt(
        forged_receipt,
        artifact_paths={
            "platform_verification": platform_path,
            "strict_report": strict_path,
            "prospective_lock": lock_path,
            "cycle": cycle_path,
            "live_snapshot": live_path,
            "offline_snapshot": offline_path,
            "publication_diagnostic": diagnostic_path,
            **missing_paths,
        },
        runtime_root=runtime_root,
        repository_root=repository_root,
    )

    assert unbound["status"] == "blocked"
    assert "artifact_bindings.evaluation.path:missing" in unbound["failures"]
    assert "artifact_bindings.prediction_archive.path:missing" in unbound["failures"]
    assert "platform.rebuild:unavailable" in unbound["failures"]
