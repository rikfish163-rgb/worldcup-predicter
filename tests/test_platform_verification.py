from __future__ import annotations

import hashlib
import importlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform.live_sources.openfootball_live import OPENFOOTBALL_HISTORY_SOURCES
from league_platform.prospective_archive import append_predictions


SUITE_COMMANDS = {
    "python": ["pytest", "-q", "tests"],
    "sites": ["npm", "test", "--", "--run"],
    "typecheck": ["mypy", "--config-file", "pyproject.toml"],
    "lint": ["ruff", "check", "--config", "pyproject.toml"],
}
REQUIRED_CODE_PATHS = (
    "league_platform/build_offline_bundle.py",
    "league_platform/fixture_serving.py",
    "league_platform/maturity_validator.py",
    "league_platform/platform_verification.py",
    "league_platform/prospective_archive.py",
    "league_platform/prospective_capture.py",
    "league_platform/prospective_cycle.py",
    "league_platform/prospective_evaluation.py",
    "league_platform/publication_audit.py",
    "league_platform/publish_cycle.py",
    "league_platform/publish_forecast.py",
    "league_platform/publish_intelligence.py",
    "league_platform/publish_snapshot.py",
    "league_platform/publish_stage_evaluations.py",
    "league_platform/runtime_evidence.py",
    "league_platform/source_rights.py",
    "league_platform/openfootball_history_sync.py",
    "league_platform/openfootball_raw_archive.py",
    "league_platform/live_sources/openfootball_live.py",
    "league_platform/sources/openfootball_verified.py",
    "league_platform/strict_backtest.py",
    "league_platform/strict_report.py",
    "deploy/systemd/run-matchline-strict-report.sh",
    "deploy/systemd/matchline-prospective-cycle.service",
    "deploy/systemd/matchline-sites-publish.service",
    "matchline_sites/app/api/d1-source-rights-policy.ts",
    "matchline_sites/app/api/ingest/route.ts",
    "matchline_sites/app/api/fixtures/register/route.ts",
    "matchline_sites/app/api/forecast/register/route.ts",
    "matchline_sites/app/api/forecast/route.ts",
    "matchline_sites/app/api/intelligence/route.ts",
    "matchline_sites/app/api/source-runs/route.ts",
    "matchline_sites/app/api/stage-evaluations/route.ts",
    "matchline_sites/app/api/evaluations/route.ts",
    "matchline_sites/app/api/release-status/route.ts",
)
COMMERCIAL_TRUST_BOUNDARY_PATHS = (
    "league_platform/fixture_serving.py",
    "league_platform/publish_snapshot.py",
    "league_platform/publish_forecast.py",
    "league_platform/publish_intelligence.py",
    "league_platform/build_offline_bundle.py",
    "league_platform/source_rights.py",
    "league_platform/openfootball_history_sync.py",
    "league_platform/openfootball_raw_archive.py",
    "league_platform/live_sources/openfootball_live.py",
    "league_platform/sources/openfootball_verified.py",
    "matchline_sites/app/api/d1-source-rights-policy.ts",
    "matchline_sites/app/api/ingest/route.ts",
    "matchline_sites/app/api/forecast/route.ts",
    "matchline_sites/app/api/intelligence/route.ts",
)


def _module():
    try:
        return importlib.import_module("league_platform.platform_verification")
    except ModuleNotFoundError:
        pytest.fail("platform verification generator is not implemented")


def _canonical_sha256(value: dict) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _model_version_sha256(lock: dict) -> str:
    return _canonical_sha256(
        {
            "model_name": lock["model_name"],
            "freeze_model_name": lock["freeze_model_name"],
            "model_files": lock["model_files"],
        }
    )


def _pretty_json_sha256(value: dict) -> str:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _lock() -> dict:
    model_path = Path(__file__).resolve().parents[1] / "league_platform/model.py"
    lock = {
        "schema_version": "1.0.0",
        "status": "pending_prospective_window",
        "locked_at": "2026-08-24T20:35:28+00:00",
        "evaluation_window_started_at": "2026-08-24T20:35:29+00:00",
        "model_name": "strict-model",
        "freeze_model_name": "strict-freeze",
        "model_files": [{"path": str(model_path), "sha256": _raw_sha256(model_path)}],
    }
    lock["model_version_sha256"] = _model_version_sha256(lock)
    return lock


def _prediction(lock: dict) -> dict:
    return {
        "fixture_id": "fixture-1",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-25T15:00:00+00:00",
        "freeze_stage": "t_minus_24h",
        "freeze_cutoff_at": "2026-08-24T21:57:00+00:00",
        "prediction_observed_at": "2026-08-24T21:58:30+00:00",
        "model_version": lock["freeze_model_name"],
        "feature_times": [],
        "primary_probability_1x2": {"home": 0.4, "draw": 0.3, "away": 0.3},
    }


def _strict_report(lock: dict) -> dict:
    target_gate = {"status": "research_ready", "sample_n": 1001, "failures": []}
    source_ids = sorted(
        next(
            source_id
            for source_id, config in sorted(OPENFOOTBALL_HISTORY_SOURCES.items())
            if config["competition_id"] == competition_id
        )
        for competition_id in (
            "premier-league",
            "championship",
            "la-liga",
            "bundesliga",
            "serie-a",
            "ligue-1",
        )
    )
    competition_ids = sorted(
        {OPENFOOTBALL_HISTORY_SOURCES[source_id]["competition_id"] for source_id in source_ids}
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
        "competition_ids": competition_ids,
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
            "competition_rows": {competition_id: 1 for competition_id in competition_ids},
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
            "probability_contract_failures": [],
            "frequency_baseline_gate": "pass_within_tolerance",
            "frequency_baseline_failures": [],
            "production_allowed": False,
            "availability_blocks": ["csl:no_declared_verified_raw_history_source"],
        },
        "leagues": {
            league: {
                "status": "evaluated",
                "gates": {
                    name: dict(target_gate)
                    for name in (
                        "three_way",
                        "totals",
                        "total_over_under",
                        "scoreline",
                        "half_full",
                        "handicap",
                    )
                }
            }
            for league in competition_ids
        },
        "combined": {
            "scoreline_sample_n": 5001,
            "scoreline_gate": {"status": "research_ready", "failures": []},
        },
        "model_selection_audit": {
            "prospective_lock": {
                "model_version_sha256": lock["model_version_sha256"],
                "locked_at": lock["locked_at"],
                "evaluation_window_started_at": lock["evaluation_window_started_at"],
            }
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
    }
    return report


def _maturity_strict_report(lock: dict) -> dict:
    strict = _strict_report(lock)
    strict["overall"].update(
        {
            "sample_threshold_gate": "partial_with_explicit_blocks",
            "market_gate": "pass",
            "freeze_stage_gate": "pass",
            "model_selection_gate": "pass_prospective_independent_test",
            "production_allowed": False,
            "availability_blocks": ["csl:no_declared_verified_raw_history_source"],
            "market_failures": [],
            "freeze_stage_failures": [],
            "model_selection_failures": [],
        }
    )
    target_gate = {"status": "research_ready", "sample_n": 1200, "failures": []}
    stages = {
        stage: {"status": "evaluated", "time_audit": {"status": "pass"}}
        for stage in ("t_minus_24h", "t_minus_6h", "t_minus_90m", "lineup_confirmation")
    }
    strict["leagues"].update({
        league: {
            "status": "evaluated",
            "gates": {
                target: dict(target_gate)
                for target in (
                    "three_way",
                    "totals",
                    "total_over_under",
                    "half_full",
                    "handicap",
                )
            },
            "time_audit": {
                "status": "pass",
                "future_leakage_violations": 0,
                "market_bound_violations": 0,
                "feature_time_violations": 0,
            },
            "freeze_stages": stages,
        }
        for league in (
            "premier-league",
            "championship",
            "la-liga",
            "bundesliga",
            "serie-a",
            "ligue-1",
        )
    })
    strict["combined"] = {
        "scoreline_sample_n": 6000,
        "scoreline_gate": {"status": "research_ready", "failures": []},
    }
    strict["model_selection_audit"] = {
        "gate_status": "pass_prospective_independent_test",
        "production_eligible": True,
        "prospective_failures": [],
        "prospective_lock": dict(lock),
    }
    return strict


def _cycle(
    lock: dict | None = None,
    evaluation: dict | None = None,
) -> dict:
    bound_lock = lock if lock is not None else _lock()
    bound_evaluation = evaluation if evaluation is not None else _evaluation(bound_lock)
    archive = "/runtime/prospective_predictions.jsonl"
    return {
        "schema_version": "1.0.0",
        "status": "pending_prospective_window",
        "started_at": "2026-08-24T21:58:00+00:00",
        "finished_at": "2026-08-24T21:59:30+00:00",
        "runtime_only": True,
        "sync": {
            "as_of": "2026-08-24T21:59:00+00:00",
            "snapshot_sha256": "b" * 64,
        },
        "capture": {
            "as_of": "2026-08-24T21:59:00+00:00",
            "predictions": 1,
            "appended": 1,
            "skipped_duplicate": 0,
            "conflicts": 0,
            "feature_archive": {"errors": []},
            "live_results": {"conflict_count": 0},
            "archive": archive,
        },
        "evaluation": dict(bound_evaluation),
        "artifacts": {
            "model_version_sha256": bound_lock["model_version_sha256"],
            "prediction_archive": archive,
            "evaluation_evidence": "/runtime/prospective-evaluation-current.json",
            "evaluation_evidence_sha256": _pretty_json_sha256(bound_evaluation),
        },
    }


def _evaluation(lock: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "generated_at": "2026-08-24T21:59:00+00:00",
        "status": "pending_prospective_window",
        "model_version_sha256": lock["model_version_sha256"],
        "prediction_archive": "/runtime/prospective_predictions.jsonl",
        "results_not_used_for_selection": True,
        "archive_conflicts": 0,
        "result_conflicts": 0,
        "invalid_records": [],
        "storage": {
            "runtime_only": True,
            "repository_check_skipped": True,
            "read_only_paths": ["docs/evidence/prospective-model-lock-current.json"],
            "blocked_paths": [],
        },
    }


def _snapshot(strict: dict, lock: dict) -> dict:
    return {
        "schema_version": "matchline.offline_snapshot.v1",
        "generated_at": "2026-08-24T22:00:00+00:00",
        "as_of": "2026-08-24T21:59:00+00:00",
        "production_ready": False,
        "strict_backtest": {"generated_at": strict["generated_at"]},
        "prospective_evaluation": {
            "model_version_sha256": lock["model_version_sha256"],
        },
        "research_prediction_summary": {
            "status": "research_only",
            "production_allowed": False,
        },
        "research_predictions": [
            {
                "fixture_id": "fixture-low",
                "status": "research_only",
                "quality_gate": "blocked_for_production_without_current_evidence",
                "coverage": {
                    "level": "low",
                    "missing": ["injuries", "lineups"],
                    "critical_conflict": False,
                },
            }
        ],
        "predictions": [],
    }


def _build_evidence() -> dict:
    return {
        "schema_version": "matchline.sites_build_resource.v1",
        "started_at": "2026-08-24T22:00:01+00:00",
        "finished_at": "2026-08-24T22:00:20+00:00",
        "exit_status": 0,
    }


def _identity(strict: dict, lock: dict) -> dict:
    return {
        "strict_report_sha256": _canonical_sha256(strict),
        "prospective_lock_sha256": _canonical_sha256(lock),
        "model_version_sha256": lock["model_version_sha256"],
    }


def _test_evidence(
    strict: dict,
    lock: dict,
    *,
    snapshot: dict | None = None,
    build_evidence: dict | None = None,
) -> dict:
    bound_snapshot = snapshot if snapshot is not None else _snapshot(strict, lock)
    bound_build = build_evidence if build_evidence is not None else _build_evidence()
    return {
        "schema_version": "matchline.platform_test_evidence.v1",
        "generated_at": "2026-08-24T22:01:00+00:00",
        "producer": {
            "schema_version": "matchline.platform_test_producer.v1",
            "name": "matchline-platform-test-runner",
            "version": "1",
        },
        "evidence_identity": _identity(strict, lock),
        "artifact_identity": {
            "offline_snapshot_sha256": _canonical_sha256(bound_snapshot),
            "sites_build_evidence_sha256": _canonical_sha256(bound_build),
        },
        "suites": {
            "python": {
                "exit_status": 0,
                "passed": 868,
                "failed": 0,
                "skipped": 0,
                "total": 868,
                "command": ["pytest", "-q", "tests"],
            },
            "sites": {
                "exit_status": 0,
                "passed": 318,
                "failed": 0,
                "skipped": 0,
                "total": 318,
                "command": ["npm", "test", "--", "--run"],
            },
            "typecheck": {
                "exit_status": 0,
                "command": ["mypy", "--config-file", "pyproject.toml"],
            },
            "lint": {
                "exit_status": 0,
                "command": ["ruff", "check", "--config", "pyproject.toml"],
            },
        },
        "api": {
            "routes": [
                {"path": "/api/v1/catalog", "http_status": 200},
                {"path": "/api/v1/matches", "http_status": 200},
                {"path": "/api/v1/service-status", "http_status": 200},
            ]
        },
        "browser": {
            "mobile": {
                "http_status": 200,
                "viewport_width": 390,
                "viewport_height": 844,
                "console_errors": 0,
                "console_warnings": 0,
            }
        },
    }


def _write_attested_test_evidence(
    root: Path,
    code_root: Path,
    strict: dict,
    lock: dict,
    *,
    snapshot: dict | None = None,
    build_evidence: dict | None = None,
) -> dict:
    evidence = _test_evidence(
        strict,
        lock,
        snapshot=snapshot,
        build_evidence=build_evidence,
    )
    evidence["producer"] = {
        "schema_version": "matchline.platform_test_producer.v1",
        "name": "matchline-platform-test-runner",
        "version": "1",
    }
    manifest_files = []
    for index, relative in enumerate(REQUIRED_CODE_PATHS):
        path = code_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"verified code {index}\n".encode())
        manifest_files.append({"path": relative, "sha256": _raw_sha256(path)})
    manifest = {
        "schema_version": "matchline.code_manifest.v1",
        "files": manifest_files,
    }
    evidence["code_manifest"] = manifest
    evidence["code_revision"] = _canonical_sha256(manifest)

    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name, command in SUITE_COMMANDS.items():
        suite = evidence["suites"][name]
        suite["status"] = "passed"
        suite["command"] = list(command)
        log_record = {
            "schema_version": "matchline.test_suite_log.v1",
            "suite": name,
            "command": list(command),
            "status": "passed",
            "exit_status": 0,
        }
        if name in {"python", "sites"}:
            log_record["counts"] = {
                key: suite[key] for key in ("passed", "failed", "skipped", "total")
            }
        log_bytes = (json.dumps(log_record, ensure_ascii=False, sort_keys=True) + "\n").encode()
        digest = hashlib.sha256(log_bytes).hexdigest()
        relative_log = Path("logs") / f"{name}-{digest}.json"
        (root / relative_log).write_bytes(log_bytes)
        suite["log_path"] = relative_log.as_posix()
        suite["log_sha256"] = digest
    return evidence


def _write_runtime_artifacts(
    root: Path,
    strict: dict,
    lock: dict,
) -> tuple[dict, dict, dict, Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    live = {
        "schema_version": "1.0.0",
        "as_of": "2026-08-24T21:59:00+00:00",
        "fixtures": [],
    }
    live_path = root / "current.json"
    live_path.write_text(
        json.dumps(live, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    live_raw_sha = _raw_sha256(live_path)

    archive_path = root / "prospective_predictions.jsonl"
    append_predictions(
        archive_path,
        [_prediction(lock)],
        lock=lock,
        captured_at=datetime(2026, 8, 24, 21, 59, tzinfo=timezone.utc),
    )
    archive_sha = _raw_sha256(archive_path)
    evaluation = _evaluation(lock)
    evaluation["scored_n"] = 0
    evaluation["pending_n"] = 1
    evaluation["prediction_archive"] = str(archive_path)
    evaluation["prediction_archive_sha256"] = archive_sha
    cycle = _cycle(lock, evaluation)
    cycle["sync"]["snapshot_sha256"] = live_raw_sha
    cycle["artifacts"]["live_snapshot"] = str(live_path)
    cycle["artifacts"]["prediction_archive"] = str(archive_path)
    cycle["artifacts"]["prediction_archive_sha256"] = archive_sha
    cycle["capture"]["archive"] = str(archive_path)

    snapshot = _snapshot(strict, lock)
    snapshot["source_snapshot"] = {
        "as_of": live["as_of"],
        "raw_file_sha256": live_raw_sha,
    }
    diagnostic = {
        "schema_version": "1.0.0",
        "status": "published",
        "output": str(live_path),
        "published": {
            "output": str(live_path),
            "as_of": live["as_of"],
            "content_sha256": _canonical_sha256(live),
        },
    }
    diagnostic_path = root / "current.publication.json"
    diagnostic_path.write_text(
        json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return cycle, evaluation, snapshot, live_path, archive_path, diagnostic_path


def _publication_audit(lock: dict, *, status: str = "pass") -> dict:
    errors = [] if status == "pass" else ["public_snapshot_behind_local"]
    checks_pass = status == "pass"
    return {
        "schema_version": "matchline.publication_audit.v1",
        "checked_at": "2026-08-24T22:00:30+00:00",
        "status": status,
        "errors": errors,
        "checks": {
            "public_read_model_reachable": True,
            "model_lock_match": checks_pass,
            "snapshot_time_comparable": True,
            "snapshot_within_lag_budget": checks_pass,
            "public_contract_routes_ok": True,
        },
        "local": {
            "as_of": "2026-08-24T21:59:00+00:00",
            "model_version_sha256": lock["model_version_sha256"],
        },
        "public": {
            "mode": "d1",
            "as_of": "2026-08-24T21:59:00+00:00",
            "model_version_sha256": lock["model_version_sha256"] if checks_pass else None,
        },
    }


def _complete_inputs(tmp_path: Path) -> tuple[dict, dict, dict]:
    lock = _lock()
    strict = _strict_report(lock)
    module = _module()
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, _, diagnostic_path = _write_runtime_artifacts(
        runtime_root, strict, lock
    )
    build = _build_evidence()
    test_root = tmp_path / "test-evidence"
    code_root = tmp_path / "code"
    result = module.build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=build,
        test_evidence=_write_attested_test_evidence(
            test_root,
            code_root,
            strict,
            lock,
            snapshot=snapshot,
            build_evidence=build,
        ),
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        test_evidence_root=test_root,
        code_root=code_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )
    return strict, lock, result


def test_generator_derives_all_gates_and_binds_current_report_and_lock(
    tmp_path: Path,
):
    strict, lock, result = _complete_inputs(tmp_path)

    assert result["status"] == "pass"
    assert set(result["maturity"].values()) == {"pass"}
    assert result["evidence_identity"] == _identity(strict, lock)
    assert result["checked_at"] == "2026-08-24T22:02:00+00:00"


def test_maturity_receipt_rebuilds_platform_from_all_bound_raw_inputs(
    tmp_path: Path,
):
    from league_platform.maturity_validator import (
        _build_artifact_bindings,
        validate_maturity,
        validate_maturity_receipt,
    )

    runtime_root = tmp_path / "runtime"
    repository_root = tmp_path / "repository"
    runtime_root.mkdir()
    repository_root.mkdir()
    lock = _lock()
    strict = _maturity_strict_report(lock)
    cycle, evaluation, snapshot, live_path, archive_path, diagnostic_path = (
        _write_runtime_artifacts(runtime_root, strict, lock)
    )
    build = _build_evidence()
    publication = _publication_audit(lock)
    test_root = repository_root / "evidence"
    tests = _write_attested_test_evidence(
        test_root,
        repository_root,
        strict,
        lock,
        snapshot=snapshot,
        build_evidence=build,
    )
    paths = {
        "platform_verification": runtime_root / "platform-verification-current.json",
        "strict_report": runtime_root / "strict-backtest-current.json",
        "prospective_lock": repository_root / "docs/evidence/prospective-model-lock-current.json",
        "cycle": runtime_root / "prospective-cycle-latest.json",
        "evaluation": runtime_root / "prospective-evaluation-current.json",
        "offline_snapshot": runtime_root / "offline_snapshot.json",
        "sites_build_evidence": repository_root
        / "matchline_sites/.runtime/sites-build-resource.json",
        "test_evidence": test_root / "platform-test-evidence-current.json",
        "publication_audit": repository_root / "docs/evidence/publication-audit-current.json",
        "live_snapshot": live_path,
        "prediction_archive": archive_path,
        "publication_diagnostic": diagnostic_path,
    }
    values = {
        "strict_report": strict,
        "prospective_lock": lock,
        "cycle": cycle,
        "evaluation": evaluation,
        "offline_snapshot": snapshot,
        "sites_build_evidence": build,
        "test_evidence": tests,
        "publication_audit": publication,
    }
    for name, value in values.items():
        _write_json(paths[name], value)

    checked_at = datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc)
    platform = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=build,
        test_evidence=tests,
        publication_audit=publication,
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        test_evidence_root=test_root,
        code_root=repository_root,
        checked_at=checked_at,
    )
    assert platform["status"] == "pass"
    for name, path in paths.items():
        if name != "platform_verification":
            platform["inputs"][name]["path"] = str(path.resolve())
    _write_json(paths["platform_verification"], platform)

    bindings, binding_failures, _ = _build_artifact_bindings(
        paths,
        runtime_root=runtime_root,
        repository_root=repository_root,
    )
    assert binding_failures == []
    generated_at = datetime(2026, 8, 24, 22, 3, tzinfo=timezone.utc)
    receipt = validate_maturity(
        strict,
        platform_verification=platform,
        prospective_lock=lock,
        generated_at=generated_at,
        platform_max_age_seconds=1800,
        require_prospective_lock=True,
        artifact_bindings=bindings,
        artifact_binding_failures=binding_failures,
    )
    assert receipt["status"] == "blocked"
    assert receipt["passed"] is False
    assert "overall.production_allowed:false" in receipt["failures"]
    assert "csl.three_way:insufficient_sample" in receipt["failures"]
    verified = validate_maturity_receipt(
        receipt,
        artifact_paths=paths,
        runtime_root=runtime_root,
        repository_root=repository_root,
        checked_at=generated_at,
    )
    assert verified["status"] == "blocked"
    assert "maturity.status:blocked" in verified["failures"]
    assert "maturity.failures:nonempty" in verified["failures"]

    trust_boundary_path = repository_root / "league_platform/publish_forecast.py"
    original_code = trust_boundary_path.read_bytes()
    trust_boundary_path.write_text("changed publication code\n", encoding="utf-8")
    changed_code = validate_maturity_receipt(
        receipt,
        artifact_paths=paths,
        runtime_root=runtime_root,
        repository_root=repository_root,
        checked_at=generated_at,
    )
    assert "platform.rebuild:mismatch" in changed_code["failures"]
    trust_boundary_path.write_bytes(original_code)

    blocked_platform = deepcopy(platform)
    blocked_platform["status"] = "blocked"
    blocked_platform["passed"] = False
    blocked_platform["maturity"]["sites_d1_consistency_gate"] = "blocked"
    blocked_platform["gate_evidence"]["sites_d1_consistency_gate"].update(
        {"status": "blocked", "reasons": ["publication_audit.status:blocked"]}
    )
    _write_json(paths["platform_verification"], blocked_platform)
    forged_receipt = deepcopy(receipt)
    forged_binding = forged_receipt["artifact_bindings"]["platform_verification"]
    forged_binding["raw_file_sha256"] = _raw_sha256(paths["platform_verification"])
    forged_binding["canonical_content_sha256"] = _canonical_sha256(blocked_platform)
    forged = validate_maturity_receipt(
        forged_receipt,
        artifact_paths=paths,
        runtime_root=runtime_root,
        repository_root=repository_root,
        checked_at=generated_at,
    )
    assert "platform.status:blocked" in forged["failures"]
    assert "platform.sites_d1_consistency_gate:blocked" in forged["failures"]

    _write_json(paths["platform_verification"], platform)
    expired_receipt = deepcopy(receipt)
    expired_receipt["expires_at"] = "2000-01-01T00:00:00+00:00"
    expired = validate_maturity_receipt(
        expired_receipt,
        artifact_paths=paths,
        runtime_root=runtime_root,
        repository_root=repository_root,
        checked_at=generated_at,
    )
    assert "maturity.expires_at:expired" in expired["failures"]

    live_path.write_text(json.dumps({"as_of": "tampered"}), encoding="utf-8")
    tampered = validate_maturity_receipt(
        receipt,
        artifact_paths=paths,
        runtime_root=runtime_root,
        repository_root=repository_root,
        checked_at=generated_at,
    )
    assert "artifact_bindings.live_snapshot.raw_file_sha256:mismatch" in tampered["failures"]


def test_generator_recomputes_live_snapshot_and_prediction_archive_bytes(
    tmp_path: Path,
):
    lock = _lock()
    strict = _strict_report(lock)
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, _, diagnostic_path = _write_runtime_artifacts(
        runtime_root, strict, lock
    )
    build = _build_evidence()
    test_root = tmp_path / "test-evidence"
    code_root = tmp_path / "code"
    tests = _write_attested_test_evidence(
        test_root,
        code_root,
        strict,
        lock,
        snapshot=snapshot,
        build_evidence=build,
    )

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=build,
        test_evidence=tests,
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        test_evidence_root=test_root,
        code_root=code_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["prediction_traceability_gate"] == "pass"
    assert result["maturity"]["test_suite_gate"] == "pass"
    assert result["inputs"]["live_snapshot"]["raw_file_sha256"] == _raw_sha256(live_path)
    assert (
        result["inputs"]["prediction_archive"]["raw_file_sha256"]
        == cycle["artifacts"]["prediction_archive_sha256"]
    )


def test_generator_blocks_tampered_live_snapshot_and_archive_bytes(tmp_path: Path):
    lock = _lock()
    strict = _strict_report(lock)
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, archive_path, diagnostic_path = (
        _write_runtime_artifacts(runtime_root, strict, lock)
    )
    archive_path.write_text('{"fixture_id":"tampered"}\n', encoding="utf-8")
    live_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "as_of": "2026-08-24T21:59:00+00:00",
                "fixtures": [{"id": "tampered"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    reasons = result["gate_evidence"]["prediction_traceability_gate"]["reasons"]
    assert "cycle.sync.snapshot_sha256:actual_file_mismatch" in reasons
    assert "cycle.artifacts.prediction_archive_sha256:actual_file_mismatch" in reasons


def test_generator_rejects_empty_archive_even_when_cycle_counts_claim_predictions(
    tmp_path: Path,
):
    lock = _lock()
    strict = _strict_report(lock)
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, archive_path, diagnostic_path = (
        _write_runtime_artifacts(runtime_root, strict, lock)
    )
    archive_path.write_text("")
    empty_sha = _raw_sha256(archive_path)
    cycle["artifacts"]["prediction_archive_sha256"] = empty_sha
    evaluation["prediction_archive_sha256"] = empty_sha
    cycle["evaluation"] = dict(evaluation)
    cycle["artifacts"]["evaluation_evidence_sha256"] = _pretty_json_sha256(evaluation)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["prediction_traceability_gate"] == "blocked"
    assert (
        "prediction_archive.current_lock_records:empty"
        in result["gate_evidence"]["prediction_traceability_gate"]["reasons"]
    )


def test_generator_rejects_archive_row_outside_locked_prediction_contract(
    tmp_path: Path,
):
    lock = _lock()
    strict = _strict_report(lock)
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, archive_path, diagnostic_path = (
        _write_runtime_artifacts(runtime_root, strict, lock)
    )
    archive_path.write_text(
        json.dumps(
            {
                "model_version_sha256": lock["model_version_sha256"],
                "freeze_key": "forged-freeze-key",
                "prediction": {
                    "fixture_id": "forged-fixture",
                    "freeze_stage": "t_minus_24h",
                    "freeze_cutoff_at": "not-a-time",
                    "model_version": lock["freeze_model_name"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    forged_sha = _raw_sha256(archive_path)
    cycle["artifacts"]["prediction_archive_sha256"] = forged_sha
    evaluation["prediction_archive_sha256"] = forged_sha
    cycle["evaluation"] = dict(evaluation)
    cycle["artifacts"]["evaluation_evidence_sha256"] = _pretty_json_sha256(evaluation)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    reasons = result["gate_evidence"]["prediction_traceability_gate"]["reasons"]
    assert "prediction_archive.invalid_records:nonzero" in reasons


def test_test_receipt_rejects_forged_log_hash_command_and_changed_code(
    tmp_path: Path,
):
    lock = _lock()
    strict = _strict_report(lock)
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, _, diagnostic_path = _write_runtime_artifacts(
        runtime_root, strict, lock
    )
    build = _build_evidence()
    test_root = tmp_path / "test-evidence"
    code_root = tmp_path / "code"
    tests = _write_attested_test_evidence(
        test_root,
        code_root,
        strict,
        lock,
        snapshot=snapshot,
        build_evidence=build,
    )
    tests["suites"]["python"]["log_sha256"] = "f" * 64
    tests["suites"]["sites"]["command"] = ["npm", "test"]
    (code_root / "league_platform/platform_verification.py").write_text(
        "changed\n", encoding="utf-8"
    )

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=build,
        test_evidence=tests,
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        test_evidence_root=test_root,
        code_root=code_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    reasons = result["gate_evidence"]["test_suite_gate"]["reasons"]
    assert "test_evidence.suites.python.log_sha256:file_mismatch" in reasons
    assert "test_evidence.suites.sites.command:not_allowlisted" in reasons
    assert (
        "test_evidence.code_manifest.league_platform/platform_verification.py:file_mismatch"
        in reasons
    )


@pytest.mark.parametrize("relative_path", COMMERCIAL_TRUST_BOUNDARY_PATHS)
def test_test_receipt_binds_each_commercial_trust_boundary_file(
    tmp_path: Path,
    relative_path: str,
):
    lock = _lock()
    strict = _strict_report(lock)
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, _, diagnostic_path = _write_runtime_artifacts(
        runtime_root, strict, lock
    )
    build = _build_evidence()
    test_root = tmp_path / "test-evidence"
    code_root = tmp_path / "code"
    tests = _write_attested_test_evidence(
        test_root,
        code_root,
        strict,
        lock,
        snapshot=snapshot,
        build_evidence=build,
    )
    (code_root / relative_path).write_text("changed\n", encoding="utf-8")

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=build,
        test_evidence=tests,
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        test_evidence_root=test_root,
        code_root=code_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    reasons = result["gate_evidence"]["test_suite_gate"]["reasons"]
    assert f"test_evidence.code_manifest.{relative_path}:file_mismatch" in reasons


@pytest.mark.parametrize(
    ("log_path", "expected_reason"),
    [
        ("logs/missing.json", "test_evidence.suites.python.log_path:missing"),
        ("../outside.json", "test_evidence.suites.python.log_path:path_escape"),
    ],
)
def test_test_receipt_rejects_missing_or_escaping_log_paths(
    tmp_path: Path,
    log_path: str,
    expected_reason: str,
):
    lock = _lock()
    strict = _strict_report(lock)
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, _, diagnostic_path = _write_runtime_artifacts(
        runtime_root, strict, lock
    )
    build = _build_evidence()
    test_root = tmp_path / "test-evidence"
    code_root = tmp_path / "code"
    tests = _write_attested_test_evidence(
        test_root,
        code_root,
        strict,
        lock,
        snapshot=snapshot,
        build_evidence=build,
    )
    tests = deepcopy(tests)
    tests["suites"]["python"]["log_path"] = log_path

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=build,
        test_evidence=tests,
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        test_evidence_root=test_root,
        code_root=code_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert expected_reason in result["gate_evidence"]["test_suite_gate"]["reasons"]


def test_traceability_rejects_non_hex_snapshot_digest():
    lock = _lock()
    strict = _strict_report(lock)
    cycle = _cycle()
    cycle["sync"]["snapshot_sha256"] = "z" * 64

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["prediction_traceability_gate"] == "blocked"
    assert (
        "cycle.sync.snapshot_sha256:missing_or_invalid"
        in result["gate_evidence"]["prediction_traceability_gate"]["reasons"]
    )


def test_expired_test_receipt_cannot_clear_test_or_browser_api_gates():
    lock = _lock()
    strict = _strict_report(lock)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
        checked_at=datetime(2026, 8, 26, 0, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["test_suite_gate"] == "blocked"
    assert result["maturity"]["sites_api_mobile_gate"] == "blocked"
    assert (
        "test_evidence.generated_at:expired"
        in result["gate_evidence"]["test_suite_gate"]["reasons"]
    )


def test_stale_cycle_and_evaluation_cannot_be_rewrapped_as_fresh_platform_evidence():
    lock = _lock()
    strict = _strict_report(lock)
    evaluation = _evaluation(lock)
    evaluation["generated_at"] = "2026-08-23T21:59:00+00:00"
    cycle = _cycle()
    cycle["started_at"] = "2026-08-23T21:58:00+00:00"
    cycle["finished_at"] = "2026-08-23T21:59:30+00:00"
    cycle["capture"]["as_of"] = "2026-08-23T21:59:00+00:00"
    cycle["evaluation"] = dict(evaluation)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["prediction_traceability_gate"] == "blocked"
    assert result["maturity"]["append_only_source_isolation_gate"] == "blocked"
    assert (
        "cycle.finished_at:expired"
        in result["gate_evidence"]["prediction_traceability_gate"]["reasons"]
    )
    assert (
        "evaluation.generated_at:expired"
        in result["gate_evidence"]["prediction_traceability_gate"]["reasons"]
    )


def test_failed_cycle_status_and_mismatched_snapshot_time_are_blocked():
    lock = _lock()
    strict = _strict_report(lock)
    evaluation = _evaluation(lock)
    evaluation["status"] = "failed"
    cycle = _cycle(lock, evaluation)
    cycle["status"] = "failed"
    snapshot = _snapshot(strict, lock)
    snapshot["as_of"] = "2026-08-24T21:50:00+00:00"

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock, snapshot=snapshot),
        publication_audit=_publication_audit(lock),
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["prediction_traceability_gate"] == "blocked"
    assert result["maturity"]["append_only_source_isolation_gate"] == "blocked"
    reasons = result["gate_evidence"]["prediction_traceability_gate"]["reasons"]
    assert "cycle.status:failed" in reasons
    assert "evaluation.status:failed" in reasons
    assert "offline_snapshot.as_of:cycle_capture_mismatch" in reasons
    assert result["status"] == "blocked"


def test_test_receipt_requires_producer_commands_logs_and_code_revision():
    lock = _lock()
    strict = _strict_report(lock)
    tests = _test_evidence(strict, lock)
    tests.pop("producer", None)
    tests.pop("code_revision", None)
    tests["suites"]["python"].pop("command", None)
    tests["suites"]["python"].pop("log_sha256", None)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=tests,
        publication_audit=_publication_audit(lock),
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["test_suite_gate"] == "blocked"
    reasons = result["gate_evidence"]["test_suite_gate"]["reasons"]
    assert "test_evidence.producer:missing" in reasons
    assert "test_evidence.code_manifest:missing" in reasons
    assert "test_evidence.suites.python.command:not_allowlisted" in reasons
    assert "test_evidence.suites.python.log_path:missing" in reasons


def test_single_pass_test_receipt_is_too_weak_to_clear_suite_gate():
    lock = _lock()
    strict = _strict_report(lock)
    tests = _test_evidence(strict, lock)
    tests["suites"]["python"] = {
        "exit_status": 0,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "total": 1,
    }

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=tests,
        publication_audit=_publication_audit(lock),
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["test_suite_gate"] == "blocked"
    assert (
        "test_evidence.suites.python.total:too_small"
        in result["gate_evidence"]["test_suite_gate"]["reasons"]
    )


def test_browser_api_receipt_must_bind_current_snapshot_and_build():
    lock = _lock()
    strict = _strict_report(lock)
    tests = _test_evidence(strict, lock)
    tests["artifact_identity"] = {
        "offline_snapshot_sha256": "0" * 64,
        "sites_build_evidence_sha256": "1" * 64,
    }

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=tests,
        publication_audit=_publication_audit(lock),
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["test_suite_gate"] == "blocked"
    assert result["maturity"]["sites_api_mobile_gate"] == "blocked"
    assert (
        "test_evidence.artifact_identity.offline_snapshot_sha256:mismatch"
        in result["gate_evidence"]["sites_api_mobile_gate"]["reasons"]
    )


def test_traceability_can_use_existing_current_lock_records_when_latest_cycle_adds_none(
    tmp_path: Path,
):
    lock = _lock()
    strict = _strict_report(lock)
    runtime_root = tmp_path / "runtime"
    cycle, evaluation, snapshot, live_path, _, diagnostic_path = _write_runtime_artifacts(
        runtime_root, strict, lock
    )
    evaluation["scored_n"] = 1
    evaluation["pending_n"] = 0
    cycle["evaluation"] = dict(evaluation)
    cycle["artifacts"]["evaluation_evidence_sha256"] = _pretty_json_sha256(evaluation)
    cycle["capture"]["predictions"] = 0
    cycle["capture"]["appended"] = 0

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=snapshot,
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
        live_snapshot_path=live_path,
        publication_diagnostic_path=diagnostic_path,
        runtime_root=runtime_root,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["prediction_traceability_gate"] == "pass"


def test_generator_blocks_lock_without_a_sha256_model_identity():
    lock = _lock()
    lock["model_version_sha256"] = "not-a-sha256"
    strict = _strict_report(lock)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
    )

    assert result["status"] == "blocked"
    assert result["maturity"]["prediction_traceability_gate"] == "blocked"
    assert result["maturity"]["test_suite_gate"] == "blocked"
    assert (
        "prospective_lock.model_version_sha256:invalid" in result["identity_validation"]["reasons"]
    )


def test_generator_recomputes_and_rejects_tampered_aggregate_model_hash():
    lock = _lock()
    lock.update(
        {
            "model_name": "strict-model",
            "freeze_model_name": "strict-freeze",
            "model_files": [{"path": "league_platform/model.py", "sha256": "c" * 64}],
        }
    )
    assert _model_version_sha256(lock) != "f" * 64
    lock["model_version_sha256"] = "f" * 64
    strict = _strict_report(lock)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked"
    assert (
        "prospective_lock.model_version_sha256:aggregate_mismatch"
        in result["identity_validation"]["reasons"]
    )


def test_generator_blocks_strict_report_nested_lock_window_mismatch():
    lock = _lock()
    strict = _strict_report(lock)
    strict["model_selection_audit"]["prospective_lock"]["evaluation_window_started_at"] = (
        "2026-08-23T00:00:00+00:00"
    )

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
    )

    assert result["status"] == "blocked"
    assert result["maturity"]["prediction_traceability_gate"] == "blocked"
    assert (
        "strict_report.prospective_lock.evaluation_window_started_at:mismatch"
        in result["identity_validation"]["reasons"]
    )


def test_blocked_publication_audit_can_never_clear_sites_d1_consistency():
    lock = _lock()
    strict = _strict_report(lock)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock, status="blocked"),
    )

    assert result["maturity"]["sites_d1_consistency_gate"] == "blocked"
    reasons = result["gate_evidence"]["sites_d1_consistency_gate"]["reasons"]
    assert "publication_audit.status:blocked" in reasons
    assert result["status"] == "blocked"


def test_same_model_but_stale_publication_audit_cannot_clear_current_snapshot():
    lock = _lock()
    strict = _strict_report(lock)
    publication = _publication_audit(lock)
    publication["local"]["as_of"] = "2026-08-23T21:59:00+00:00"

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=publication,
    )

    assert result["maturity"]["sites_d1_consistency_gate"] == "blocked"
    assert (
        "publication_audit.local.as_of:offline_snapshot_mismatch"
        in result["gate_evidence"]["sites_d1_consistency_gate"]["reasons"]
    )


def test_stale_public_as_of_cannot_clear_sites_d1_consistency():
    lock = _lock()
    strict = _strict_report(lock)
    publication = _publication_audit(lock)
    publication["public"]["as_of"] = "2000-01-01T00:00:00+00:00"

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=publication,
        checked_at=datetime(2026, 8, 24, 22, 2, tzinfo=timezone.utc),
    )

    assert result["maturity"]["sites_d1_consistency_gate"] == "blocked"
    assert (
        "publication_audit.public.as_of:offline_snapshot_mismatch"
        in result["gate_evidence"]["sites_d1_consistency_gate"]["reasons"]
    )


def test_missing_test_and_build_evidence_fail_closed_instead_of_reusing_old_passes():
    lock = _lock()
    strict = _strict_report(lock)

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        publication_audit=_publication_audit(lock),
    )

    assert result["maturity"]["test_suite_gate"] == "not_verified"
    assert result["maturity"]["sites_api_mobile_gate"] == "not_verified"
    assert result["status"] == "blocked"


def test_stale_test_identity_and_failed_build_are_blocked():
    lock = _lock()
    strict = _strict_report(lock)
    tests = _test_evidence(strict, lock)
    tests["evidence_identity"]["strict_report_sha256"] = "old"
    build = _build_evidence()
    build["exit_status"] = 1

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=_snapshot(strict, lock),
        sites_build_evidence=build,
        test_evidence=tests,
        publication_audit=_publication_audit(lock),
    )

    assert result["maturity"]["test_suite_gate"] == "blocked"
    assert result["maturity"]["sites_api_mobile_gate"] == "blocked"
    assert (
        "test_evidence.identity.strict_report_sha256:mismatch"
        in result["gate_evidence"]["test_suite_gate"]["reasons"]
    )
    assert (
        "sites_build_evidence.exit_status:1"
        in result["gate_evidence"]["sites_api_mobile_gate"]["reasons"]
    )


def test_low_quality_and_independent_scoring_are_derived_from_artifacts():
    lock = _lock()
    strict = _strict_report(lock)
    strict["leagues"]["premier-league"]["gates"]["handicap"] = {
        "status": "blocked",
        "sample_n": 0,
        "failures": ["no_samples"],
    }
    snapshot = _snapshot(strict, lock)
    snapshot["research_predictions"][0]["quality_gate"] = "research_ready"

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=snapshot,
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
    )

    assert result["maturity"]["low_quality_degradation_gate"] == "blocked"
    assert result["maturity"]["independent_target_scoring_gate"] == "blocked"


def test_low_quality_gate_can_pass_when_global_production_is_enabled():
    lock = _lock()
    strict = _strict_report(lock)
    snapshot = _snapshot(strict, lock)
    snapshot["production_ready"] = True
    snapshot["research_prediction_summary"]["production_allowed"] = True

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=snapshot,
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
    )

    assert result["maturity"]["low_quality_degradation_gate"] == "pass"


def test_low_quality_gate_blocks_low_row_mixed_into_formal_predictions():
    lock = _lock()
    strict = _strict_report(lock)
    snapshot = _snapshot(strict, lock)
    snapshot["predictions"] = [{"fixture_id": "fixture-low"}]

    result = _module().build_platform_verification(
        strict_report=strict,
        prospective_lock=lock,
        cycle=_cycle(),
        evaluation=_evaluation(lock),
        offline_snapshot=snapshot,
        sites_build_evidence=_build_evidence(),
        test_evidence=_test_evidence(strict, lock),
        publication_audit=_publication_audit(lock),
    )

    assert result["maturity"]["low_quality_degradation_gate"] == "blocked"
    assert (
        "offline_snapshot.low[0].formal_prediction:mixed"
        in result["gate_evidence"]["low_quality_degradation_gate"]["reasons"]
    )


def test_service_generates_current_platform_evidence_before_validation():
    service = Path("deploy/systemd/matchline-prospective-cycle.service").read_text(
        encoding="utf-8"
    )
    generator = "-m league_platform.platform_verification"
    validator = "-m league_platform.maturity_validator"

    assert generator in service
    assert service.index(generator) < service.index(validator)
    assert "${MATCHLINE_RUNTIME_DIR}/platform-verification-current.json" in service
    assert (
        "--platform-verification ${MATCHLINE_RUNTIME_DIR}/platform-verification-current.json"
        in service
    )
    maturity_block = service[
        service.index("ExecStartPost=/usr/bin/python3 -m league_platform.maturity_validator") :
    ]
    assert "--require-passed" not in maturity_block
    assert service.count(
        "league_platform.runtime_evidence --runtime-dir=${MATCHLINE_RUNTIME_DIR}"
    ) == 2
    assert "--require-passed" not in service
    assert "platform-maturity-verification-2026-08-14-v16-latest.json" not in service
