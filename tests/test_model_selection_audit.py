from __future__ import annotations

import json
import hashlib

from league_platform.model_selection_audit import build_model_selection_audit


def test_reused_holdout_is_development_only_without_prospective_lock(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "model-candidate-a.json").write_text(
        json.dumps({"protocol": {"holdout": ["2122", "2223"]}}), encoding="utf-8"
    )
    (evidence / "model-candidate-b.json").write_text(
        json.dumps({"protocol": {"final_holdout": "2122 onward"}}), encoding="utf-8"
    )
    (evidence / "model-candidate-c.json").write_text(
        json.dumps({"protocol": {"validation": "2020"}}), encoding="utf-8"
    )

    audit = build_model_selection_audit(evidence)

    assert audit["candidate_report_count"] == 3
    assert audit["reports_referencing_reused_holdout_count"] == 2
    assert audit["current_evaluation_role"] == "development_backtest_after_repeated_inspection"
    assert audit["gate_status"] == "blocked_selection_debt"
    assert audit["production_eligible"] is False


def test_prospective_lock_pass_requires_all_independence_evidence(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "model-candidate-a.json").write_text(
        json.dumps({"holdout": "2122 onward"}), encoding="utf-8"
    )
    prospective = {
        "status": "passed",
        "model_version_sha256": "a" * 64,
        "locked_at": "2026-08-13T03:00:00+00:00",
        "evaluation_window_started_at": "2026-08-14T00:00:00+00:00",
        "results_not_used_for_selection": True,
        "sample_requirements_met": True,
        "all_required_targets_scored": True,
        "prediction_freezes_verified": True,
    }

    audit = build_model_selection_audit(evidence, prospective_lock=prospective)

    assert audit["gate_status"] == "pass_prospective_independent_test"
    assert audit["production_eligible"] is True


def test_prospective_lock_cannot_pass_when_window_predates_lock(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    prospective = {
        "status": "passed",
        "model_version_sha256": "a" * 64,
        "locked_at": "2026-08-14T00:00:00+00:00",
        "evaluation_window_started_at": "2026-08-13T00:00:00+00:00",
        "results_not_used_for_selection": True,
        "sample_requirements_met": True,
        "all_required_targets_scored": True,
        "prediction_freezes_verified": True,
    }

    audit = build_model_selection_audit(evidence, prospective_lock=prospective)

    assert audit["gate_status"] == "blocked_selection_debt"
    assert "evaluation_window_not_after_model_lock" in audit["prospective_failures"]


def test_prospective_lock_rejects_model_file_hash_mismatch(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    model_file = tmp_path / "model.py"
    model_file.write_text("locked implementation\n", encoding="utf-8")
    prospective = {
        "status": "passed",
        "model_version_sha256": "a" * 64,
        "locked_at": "2026-08-13T03:00:00+00:00",
        "evaluation_window_started_at": "2026-08-14T00:00:00+00:00",
        "results_not_used_for_selection": True,
        "sample_requirements_met": True,
        "all_required_targets_scored": True,
        "prediction_freezes_verified": True,
        "model_files": [
            {"path": str(model_file), "sha256": "b" * 64},
            {"path": str(model_file), "sha256": "b" * 63},
        ],
    }

    audit = build_model_selection_audit(evidence, prospective_lock=prospective)

    assert audit["gate_status"] == "blocked_selection_debt"
    assert f"model_file_hash_mismatch:{model_file}" in audit["prospective_failures"]
    assert f"model_file_hash_invalid:{model_file}" in audit["prospective_failures"]


def test_prospective_lock_accepts_matching_model_file_hash(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    model_file = tmp_path / "model.py"
    model_file.write_text("locked implementation\n", encoding="utf-8")
    prospective = {
        "status": "passed",
        "model_version_sha256": "a" * 64,
        "locked_at": "2026-08-13T03:00:00+00:00",
        "evaluation_window_started_at": "2026-08-14T00:00:00+00:00",
        "results_not_used_for_selection": True,
        "sample_requirements_met": True,
        "all_required_targets_scored": True,
        "prediction_freezes_verified": True,
        "model_files": [
            {"path": str(model_file), "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest()}
        ],
    }

    audit = build_model_selection_audit(evidence, prospective_lock=prospective)

    assert audit["gate_status"] == "pass_prospective_independent_test"
