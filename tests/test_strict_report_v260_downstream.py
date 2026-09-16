from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from league_platform import platform_verification
from league_platform.maturity_validator import validate_maturity


SOURCE_ID = "openfootball:england:2015-16:1-premierleague"
OBSERVED_BEFORE = "2026-08-25T01:02:03+00:00"
LEAGUES = (
    "premier-league",
    "championship",
    "la-liga",
    "bundesliga",
    "serie-a",
    "ligue-1",
)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _admission() -> dict:
    admission = {
        "schema_version": "matchline.openfootball_verified_history_admission.v1",
        "status": "training_admitted",
        "training_admitted": True,
        "training_source": "verified_openfootball_raw_archive_only",
        "admission_scope": "historical_training",
        "loader_contract_version": "1.0.0",
        "adapter_contract_version": "1.0.0",
        "observed_before": OBSERVED_BEFORE,
        "source_ids": [SOURCE_ID],
        "competition_ids": ["premier-league"],
        "selected_records": [
            {
                "source_id": SOURCE_ID,
                "retrieved_at": OBSERVED_BEFORE,
                "record_sha256": "1" * 64,
                "raw_sha256": "2" * 64,
            }
        ],
        "raw_admission_sha256": "3" * 64,
        "raw_manifest_sha256": "4" * 64,
        "source_manifest_sha256": "5" * 64,
        "source_identity_sha256": "6" * 64,
        "training_policy_sha256": "7" * 64,
        "parser_contract_sha256": "8" * 64,
        "raw_rows_sha256": "9" * 64,
        "domain_rows_sha256": "a" * 64,
        "counts": {
            "raw_rows": 1,
            "finished_admitted": 1,
            "upcoming_isolated": 0,
            "exact_kickoff_admitted": 1,
            "date_only_admitted": 0,
            "competition_rows": {"premier-league": 1},
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
    return admission


def _blocked_gates() -> dict:
    return {
        target: {"status": "blocked", "sample_n": 0, "failures": ["insufficient_sample"]}
        for target in (
            "three_way",
            "totals",
            "total_over_under",
            "half_full",
            "handicap",
        )
    }


def _lock(tmp_path: Path) -> dict:
    model = tmp_path / "model.py"
    model.write_text("model\n", encoding="utf-8")
    lock = {
        "schema_version": "1.0.0",
        "status": "pending_prospective_window",
        "model_name": "strict-model",
        "freeze_model_name": "strict-freeze",
        "model_files": [
            {
                "path": str(model),
                "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            }
        ],
        "locked_at": "2026-08-24T20:35:28+00:00",
        "evaluation_window_started_at": "2026-08-24T20:35:29+00:00",
        "evaluation_window": "prospective",
    }
    lock["model_version_sha256"] = _canonical_sha256(
        {
            "model_name": lock["model_name"],
            "freeze_model_name": lock["freeze_model_name"],
            "model_files": lock["model_files"],
        }
    )
    return lock


def _formal_report(lock: dict | None = None) -> dict:
    admission = _admission()
    unavailable = {
        "status": "unavailable",
        "reason": "no_admitted_openfootball_history_source_at_cutoff",
        "sample_n": 0,
        "gates": _blocked_gates(),
    }
    report = {
        "schema_version": "matchline.strict_report.v260",
        "report_lane": "formal_v260",
        "generated_at": "2026-08-25T01:03:00+00:00",
        "training_source_contract": {
            "provider": "OpenFootball",
            "raw_archive_required": True,
            "training_admission_sha256": admission["admission_sha256"],
            "policy_version": "v260",
            "observed_before": OBSERVED_BEFORE,
            "source_ids": [SOURCE_ID],
            "prohibited_formal_inputs": [
                "football_data_csv",
                "legacy_csl_files",
                "snapshot_self_report",
            ],
        },
        "training_admission": admission,
        "leagues": {league: deepcopy(unavailable) for league in LEAGUES},
        "combined": {
            "scoreline_sample_n": 0,
            "scoreline_gate": {
                "status": "blocked",
                "sample_n": 0,
                "failures": ["insufficient_sample"],
            },
        },
        "overall": {
            "chronological_cutoff_gate": "pass",
            "probability_contract_gate": "pass",
            "sample_threshold_gate": "partial_with_explicit_blocks",
            "frequency_baseline_gate": "pass_within_tolerance",
            "market_gate": "pass",
            "freeze_stage_gate": "pass",
            "model_selection_gate": "blocked_selection_debt",
            "production_allowed": False,
            "frequency_baseline_failures": [],
            "availability_blocks": ["csl:no_declared_verified_raw_history_source"],
            "market_failures": [],
            "probability_contract_failures": [],
            "freeze_stage_failures": [],
            "model_selection_failures": ["prospective_lock_missing"],
        },
        "model_selection_audit": {
            "gate_status": "blocked_selection_debt",
            "production_eligible": False,
            "prospective_failures": ["prospective_lock_missing"],
        },
    }
    report["leagues"]["csl"] = {
        "status": "unavailable",
        "reason": "no_declared_verified_raw_history_source",
        "sample_n": 0,
        "gates": _blocked_gates(),
    }
    if lock is not None:
        report["model_selection_audit"]["prospective_lock"] = dict(lock)
    return report


def _contract_failures(report: dict) -> list[str]:
    validator = getattr(platform_verification, "strict_report_v260_failures")
    return validator(report)


def test_v260_contract_accepts_a_complete_content_bound_training_admission() -> None:
    assert _contract_failures(_formal_report()) == []


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda report: report.update(
                {
                    "schema_version": "matchline.strict_report.legacy_research_only.v1",
                    "report_lane": "legacy_research_only",
                }
            ),
            "strict_report.schema_version:invalid",
        ),
        (
            lambda report: report["training_source_contract"].update(
                {"policy_version": "v259"}
            ),
            "strict_report.training_source_contract.policy_version:invalid",
        ),
        (
            lambda report: report["training_admission"].update(
                {"observed_before": "2026-08-24T01:02:03+00:00"}
            ),
            "strict_report.training_admission.observed_before:contract_mismatch",
        ),
        (
            lambda report: report["training_admission"].update(
                {"raw_rows_sha256": "not-a-digest"}
            ),
            "strict_report.training_admission.raw_rows_sha256:invalid",
        ),
        (
            lambda report: report["training_admission"].update(
                {"admission_sha256": "f" * 64}
            ),
            "strict_report.training_admission.admission_sha256:mismatch",
        ),
        (
            lambda report: report["leagues"].update(
                {
                    "csl": {
                        "status": "evaluated",
                        "sample_n": 1200,
                        "gates": {
                            target: {
                                "status": "research_ready",
                                "sample_n": 1200,
                                "failures": [],
                            }
                            for target in _blocked_gates()
                        },
                    }
                }
            ),
            "strict_report.csl:must_be_unavailable_without_verified_raw_source",
        ),
    ],
)
def test_v260_contract_rejects_legacy_policy_cutoff_digest_and_csl_forgery(
    mutation,
    expected_reason: str,
) -> None:
    report = _formal_report()
    mutation(report)

    assert expected_reason in _contract_failures(report)


def test_platform_identity_validation_rejects_csl_forgery(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    report = _formal_report(lock)
    report["leagues"]["csl"] = {
        "status": "evaluated",
        "sample_n": 1200,
        "gates": {
            target: {"status": "research_ready", "sample_n": 1200, "failures": []}
            for target in _blocked_gates()
        },
    }

    result = platform_verification.build_platform_verification(
        strict_report=report,
        prospective_lock=lock,
    )

    assert result["status"] == "blocked"
    assert result["identity_validation"]["status"] == "blocked"
    assert (
        "strict_report.csl:must_be_unavailable_without_verified_raw_source"
        in result["identity_validation"]["reasons"]
    )


def test_maturity_rejects_overall_only_report_as_a_contract_failure() -> None:
    result = validate_maturity(
        {"overall": {"production_allowed": True}},
        require_artifact_bindings=False,
    )

    assert result["status"] == "blocked"
    assert "strict_report.schema_version:invalid" in result["failures"]
    assert "strict_report.training_admission:missing" in result["failures"]
