"""Strict, artifact-backed completion gate for Matchline research maturity.

The validator is intentionally conservative.  A green unit-test suite alone
cannot clear release: the strict report must show causal timing, legal
probabilities, target-specific sample sizes, baseline and market gates, all
four freeze stages, and a genuinely prospective model-selection lock.  Platform
evidence must separately confirm traceability, append-only isolation, scoring,
and Sites/D1 consistency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from league_platform.platform_verification import (
    build_platform_verification,
    canonical_model_version_sha256,
    strict_report_v260_failures,
)


CORE_TARGETS = {
    "three_way": 1000,
    "totals": 1000,
    "half_full": 1000,
    "handicap": 1000,
}
FREEZE_STAGES = ("t_minus_24h", "t_minus_6h", "t_minus_90m", "lineup_confirmation")
PLATFORM_GATES = (
    "prediction_traceability_gate",
    "append_only_source_isolation_gate",
    "calibration_gate",
    "low_quality_degradation_gate",
    "independent_target_scoring_gate",
    "sites_d1_consistency_gate",
    "sites_api_mobile_gate",
    "test_suite_gate",
)
PLATFORM_VERIFICATION_SCHEMA = "matchline.platform_verification.v2"
MATURITY_GATE_SCHEMA = "matchline.maturity_gate.v2"
MATURITY_RECEIPT_TTL = timedelta(minutes=30)
MATURITY_RECEIPT_FIELDS = {
    "schema_version",
    "generated_at",
    "expires_at",
    "status",
    "passed",
    "failures",
    "artifact_bindings",
    "requirements",
}
PLATFORM_INPUTS = (
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
ARTIFACT_BINDING_NAMES = ("platform_verification", *PLATFORM_INPUTS)
REPOSITORY_BOUND_ARTIFACTS = {
    "prospective_lock",
    "sites_build_evidence",
    "test_evidence",
    "publication_audit",
}
PLATFORM_GATE_INPUTS = {
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


def _as_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _failure_once(failures: list[str], value: str) -> None:
    if value not in failures:
        failures.append(value)


def _strict_failures(report: Mapping[str, Any]) -> list[str]:
    failures = strict_report_v260_failures(report)
    raw_overall = report.get("overall")
    if isinstance(raw_overall, Mapping):
        overall = raw_overall
    else:
        _failure_once(failures, "overall:missing")
        overall = {}
    expected_overall = {
        "chronological_cutoff_gate": "pass",
        "probability_contract_gate": "pass",
        "sample_threshold_gate": "pass",
        "frequency_baseline_gate": "pass_within_tolerance",
        "market_gate": "pass",
        "freeze_stage_gate": "pass",
        "model_selection_gate": "pass_prospective_independent_test",
    }
    for key, expected in expected_overall.items():
        if overall.get(key) != expected:
            _failure_once(failures, f"overall.{key}:{overall.get(key, 'missing')}")
    if overall.get("production_allowed") is not True:
        _failure_once(failures, "overall.production_allowed:false")
    for key in (
        "frequency_baseline_failures",
        "availability_blocks",
        "market_failures",
        "probability_contract_failures",
        "freeze_stage_failures",
        "model_selection_failures",
    ):
        values = overall.get(key)
        if isinstance(values, list):
            for item in values:
                _failure_once(failures, f"overall.{key}:{item}")
        elif values is not None:
            _failure_once(failures, f"overall.{key}:invalid")

    leagues = report.get("leagues")
    if not isinstance(leagues, Mapping) or not leagues:
        _failure_once(failures, "leagues:missing")
        return failures
    acceptable_target_statuses = {"research_ready", "beats_market", "pass", "production_ready"}
    for league, raw_value in leagues.items():
        if not isinstance(raw_value, Mapping):
            _failure_once(failures, f"leagues.{league}:invalid")
            continue
        value = raw_value
        gates = value.get("gates")
        if not isinstance(gates, Mapping):
            _failure_once(failures, f"{league}.gates:missing")
            continue
        for target, threshold in CORE_TARGETS.items():
            gate = gates.get(target)
            if not isinstance(gate, Mapping):
                _failure_once(failures, f"{league}.{target}:missing")
                continue
            sample = gate.get("sample_n")
            if not isinstance(sample, int) or isinstance(sample, bool) or sample < threshold:
                _failure_once(failures, f"{league}.{target}:insufficient_sample")
            if gate.get("status") not in acceptable_target_statuses:
                _failure_once(failures, f"{league}.{target}:{gate.get('status', 'missing')}")
            for item in gate.get("failures", []) if isinstance(gate.get("failures"), list) else []:
                _failure_once(failures, f"{league}.{target}:failure:{item}")
        # If a totals market gate is present, it is a required supported target
        # and must be independently clear.  CSL may legitimately have no such
        # market section yet; absence is handled by the official `totals` gate.
        market_total_gate = gates.get("total_over_under")
        if market_total_gate is not None:
            if not isinstance(market_total_gate, Mapping):
                _failure_once(failures, f"{league}.total_over_under:invalid")
            elif market_total_gate.get("status") not in acceptable_target_statuses:
                _failure_once(
                    failures,
                    f"{league}.total_over_under:{market_total_gate.get('status', 'missing')}",
                )
        time_audit = value.get("time_audit")
        if isinstance(time_audit, Mapping):
            if time_audit.get("status") != "pass":
                _failure_once(
                    failures, f"{league}.time_audit:{time_audit.get('status', 'missing')}"
                )
            for key in (
                "future_leakage_violations",
                "market_bound_violations",
                "feature_time_violations",
            ):
                if time_audit.get(key, 0) != 0:
                    _failure_once(failures, f"{league}.time_audit:{key}")
        else:
            _failure_once(failures, f"{league}.time_audit:missing")
        stages = value.get("freeze_stages")
        if not isinstance(stages, Mapping):
            _failure_once(failures, f"{league}.freeze_stages:missing")
        else:
            for stage in FREEZE_STAGES:
                entry = stages.get(stage)
                if not isinstance(entry, Mapping) or entry.get("status") != "evaluated":
                    _failure_once(failures, f"{league}.{stage}:not_evaluated")
                    continue
                stage_audit = entry.get("time_audit")
                if not isinstance(stage_audit, Mapping) or stage_audit.get("status") != "pass":
                    _failure_once(failures, f"{league}.{stage}:time_audit_blocked")

    combined = report.get("combined")
    if not isinstance(combined, Mapping):
        _failure_once(failures, "combined:missing")
    else:
        scoreline_sample = combined.get("scoreline_sample_n")
        if (
            not isinstance(scoreline_sample, int)
            or isinstance(scoreline_sample, bool)
            or scoreline_sample < 5000
        ):
            _failure_once(failures, "combined.scoreline:insufficient_sample")
        scoreline_gate = combined.get("scoreline_gate")
        if not isinstance(scoreline_gate, Mapping) or scoreline_gate.get("status") not in {
            "research_ready",
            "beats_market",
            "pass",
            "production_ready",
        }:
            _failure_once(failures, "combined.scoreline:gate_blocked")
    selection = report.get("model_selection_audit")
    if not isinstance(selection, Mapping) or selection.get("production_eligible") is not True:
        _failure_once(failures, "model_selection_audit:production_ineligible")
    return failures


def _canonical_evidence_sha256(value: Mapping[str, Any]) -> str:
    """Return a whitespace-independent digest for one JSON evidence object."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _raw_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _confined_file(
    path: Path,
    root: Path,
    *,
    prefix: str,
) -> tuple[Path | None, list[str]]:
    try:
        resolved_root = root.resolve(strict=True)
        if path.is_symlink():
            return None, [f"{prefix}:symlink_not_allowed"]
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except FileNotFoundError:
        return None, [f"{prefix}:missing"]
    except (OSError, RuntimeError, ValueError):
        return None, [f"{prefix}:path_escape"]
    if not resolved.is_file():
        return None, [f"{prefix}:not_file"]
    return resolved, []


def _artifact_binding(
    name: str,
    path: Path | None,
    *,
    runtime_root: Path | None,
    repository_root: Path | None,
) -> tuple[dict[str, Any], list[str], Mapping[str, Any] | None]:
    binding: dict[str, Any] = {
        "path": str(path) if path is not None else None,
        "raw_file_sha256": None,
        "canonical_content_sha256": None,
        "schema_version": None,
    }
    if path is None:
        return binding, [f"artifact_bindings.{name}.path:missing"], None
    root = repository_root if name in REPOSITORY_BOUND_ARTIFACTS else runtime_root
    if root is None:
        return binding, [f"artifact_bindings.{name}.root:missing"], None
    resolved, failures = _confined_file(
        path,
        root,
        prefix=f"artifact_bindings.{name}.path",
    )
    if resolved is None:
        return binding, failures, None
    binding["path"] = str(resolved)
    binding["raw_file_sha256"] = _raw_file_sha256(resolved)
    if name == "prediction_archive":
        binding["schema_version"] = "matchline.prospective_prediction_archive.jsonl.v1"
        return binding, failures, None
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return binding, [*failures, f"artifact_bindings.{name}:invalid_json"], None
    if not isinstance(value, Mapping):
        return binding, [*failures, f"artifact_bindings.{name}:invalid_shape"], None
    binding["schema_version"] = value.get("schema_version")
    binding["canonical_content_sha256"] = _canonical_evidence_sha256(value)
    if name == "prospective_lock":
        computed_model_sha = canonical_model_version_sha256(value)
        binding["model_version_sha256"] = value.get("model_version_sha256")
        binding["computed_model_version_sha256"] = computed_model_sha
        if computed_model_sha is None:
            failures.append("artifact_bindings.prospective_lock.model_files:invalid")
        elif value.get("model_version_sha256") != computed_model_sha:
            failures.append(
                "artifact_bindings.prospective_lock.model_version_sha256:aggregate_mismatch"
            )
    return binding, failures, value


def _build_artifact_bindings(
    artifact_paths: Mapping[str, Path | None],
    *,
    runtime_root: Path | None,
    repository_root: Path | None,
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Mapping[str, Any]]]:
    bindings: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    values: dict[str, Mapping[str, Any]] = {}
    for name in ARTIFACT_BINDING_NAMES:
        binding, item_failures, value = _artifact_binding(
            name,
            artifact_paths.get(name),
            runtime_root=runtime_root,
            repository_root=repository_root,
        )
        bindings[name] = binding
        failures.extend(item_failures)
        if value is not None:
            values[name] = value
    return bindings, failures, values


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _maturity_requirements() -> dict[str, Any]:
    return {
        "no_future_leakage": "strict_report.overall.chronological_cutoff_gate=pass and all time audits have zero violations",
        "probability_contract": "strict_report.overall.probability_contract_gate=pass",
        "sample_thresholds": "all leagues core targets >=1000; combined scoreline >=5000",
        "prospective_independence": "strict_report.model_selection_audit.production_eligible=true",
        "freeze_stages": list(FREEZE_STAGES),
        "platform_evidence": list(PLATFORM_GATES),
        "platform_evidence_identity": (
            "platform evidence must match the canonical strict-report digest and, "
            "when supplied, the current prospective-lock digest and model hash"
        ),
    }


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _receipt_platform_failures(
    platform: Mapping[str, Any],
    *,
    prospective_lock: Mapping[str, Any] | None,
    reference: datetime,
) -> list[str]:
    """Validate the live platform artifact, not the receipt's claim about it."""

    failures: list[str] = []
    if platform.get("schema_version") != PLATFORM_VERIFICATION_SCHEMA:
        failures.append(f"platform.schema_version:{platform.get('schema_version') or 'missing'}")
    if platform.get("status") != "pass":
        failures.append(f"platform.status:{platform.get('status') or 'missing'}")
    if platform.get("passed") is not True:
        rendered = (
            "missing" if platform.get("passed") is None else str(platform.get("passed")).lower()
        )
        failures.append(f"platform.passed:{rendered}")
    checked_at = _utc(platform.get("checked_at"))
    if checked_at is None:
        failures.append("platform.checked_at:invalid")
    elif checked_at > reference:
        failures.append("platform.checked_at:future")
    elif reference - checked_at > MATURITY_RECEIPT_TTL:
        failures.append("platform.checked_at:expired")

    identity_validation = platform.get("identity_validation")
    if not isinstance(identity_validation, Mapping):
        failures.append("platform.identity_validation:missing")
    else:
        if identity_validation.get("status") != "pass":
            failures.append(
                "platform.identity_validation.status:"
                f"{identity_validation.get('status', 'missing')}"
            )
        reasons = identity_validation.get("reasons")
        if reasons != []:
            failures.append("platform.identity_validation.reasons:not_empty_list")

    strict_validation = platform.get("strict_report_validation")
    if not isinstance(strict_validation, Mapping):
        failures.append("platform.strict_report_validation:missing")
    else:
        if strict_validation.get("status") != "pass":
            failures.append(
                "platform.strict_report_validation.status:"
                f"{strict_validation.get('status', 'missing')}"
            )
        if strict_validation.get("reasons") != []:
            failures.append("platform.strict_report_validation.reasons:not_empty_list")

    maturity = platform.get("maturity")
    if not isinstance(maturity, Mapping):
        failures.append("platform.maturity:missing")
    else:
        if set(maturity) != set(PLATFORM_GATES):
            failures.append("platform.maturity:gates_mismatch")
        for gate in PLATFORM_GATES:
            if maturity.get(gate) != "pass":
                failures.append(f"platform.{gate}:{maturity.get(gate, 'missing')}")

    gate_evidence = platform.get("gate_evidence")
    if not isinstance(gate_evidence, Mapping):
        failures.append("platform.gate_evidence:missing")
    else:
        if set(gate_evidence) != set(PLATFORM_GATES):
            failures.append("platform.gate_evidence:gates_mismatch")
        for gate in PLATFORM_GATES:
            detail = gate_evidence.get(gate)
            if not isinstance(detail, Mapping):
                failures.append(f"platform.gate_evidence.{gate}:missing")
                continue
            if detail.get("status") != "pass":
                failures.append(
                    f"platform.gate_evidence.{gate}.status:{detail.get('status', 'missing')}"
                )
            if detail.get("reasons") != []:
                failures.append(f"platform.gate_evidence.{gate}.reasons:not_empty_list")
            evidence = detail.get("evidence")
            if (
                not isinstance(evidence, list)
                or any(not isinstance(item, str) for item in evidence)
                or not set(PLATFORM_GATE_INPUTS[gate]).issubset(evidence)
            ):
                failures.append(f"platform.gate_evidence.{gate}.evidence:incomplete")

    inputs = platform.get("inputs")
    if not isinstance(inputs, Mapping):
        failures.append("platform.inputs:missing")
        inputs = {}
    elif set(inputs) != set(PLATFORM_INPUTS):
        failures.append("platform.inputs:names_mismatch")
    for name in PLATFORM_INPUTS:
        summary = inputs.get(name)
        if not isinstance(summary, Mapping):
            failures.append(f"platform.inputs.{name}:missing")
            continue
        if summary.get("present") is not True:
            failures.append(f"platform.inputs.{name}.present:not_true")
        if name == "prediction_archive":
            if not _valid_digest(summary.get("raw_file_sha256")):
                failures.append("platform.inputs.prediction_archive.raw_file_sha256:invalid")
        elif not _valid_digest(summary.get("canonical_sha256")):
            failures.append(f"platform.inputs.{name}.canonical_sha256:invalid")
        if name == "strict_report":
            if summary.get("schema_version") != "matchline.strict_report.v260":
                failures.append("platform.inputs.strict_report.schema_version:invalid")
            if summary.get("report_lane") != "formal_v260":
                failures.append("platform.inputs.strict_report.report_lane:invalid")
            if summary.get("policy_version") != "v260":
                failures.append("platform.inputs.strict_report.policy_version:invalid")
            if not _valid_digest(summary.get("training_admission_sha256")):
                failures.append(
                    "platform.inputs.strict_report.training_admission_sha256:invalid"
                )
            if _utc(summary.get("observed_before")) is None:
                failures.append("platform.inputs.strict_report.observed_before:invalid")
            source_ids = summary.get("source_ids")
            if not isinstance(source_ids, list) or not source_ids:
                failures.append("platform.inputs.strict_report.source_ids:invalid")

    identity = platform.get("evidence_identity")
    if not isinstance(identity, Mapping):
        failures.append("platform.evidence_identity:missing")
    else:
        strict_summary = inputs.get("strict_report")
        strict_digest = (
            strict_summary.get("canonical_sha256") if isinstance(strict_summary, Mapping) else None
        )
        if not _valid_digest(strict_digest):
            failures.append("platform.inputs.strict_report.canonical_sha256:invalid")
        elif identity.get("strict_report_sha256") != strict_digest:
            failures.append("platform.evidence_identity.strict_report_sha256:mismatch")
        if prospective_lock is None:
            failures.append("platform.evidence_identity.prospective_lock:unverifiable")
        else:
            expected_lock_digest = _canonical_evidence_sha256(prospective_lock)
            if identity.get("prospective_lock_sha256") != expected_lock_digest:
                failures.append("platform.evidence_identity.prospective_lock_sha256:mismatch")
            if identity.get("model_version_sha256") != prospective_lock.get(
                "model_version_sha256"
            ):
                failures.append("platform.evidence_identity.model_version_sha256:mismatch")
    return failures


def _rebuild_platform_failures(
    platform: Mapping[str, Any],
    *,
    values: Mapping[str, Mapping[str, Any]],
    bindings: Mapping[str, Mapping[str, Any]],
    runtime_root: Path,
    repository_root: Path,
) -> list[str]:
    required_json = set(PLATFORM_INPUTS) - {"prediction_archive"}
    if not required_json.issubset(values):
        return ["platform.rebuild:unavailable"]
    checked_at = _utc(platform.get("checked_at"))
    if checked_at is None:
        return ["platform.rebuild:unavailable"]
    try:
        live_path = Path(str(bindings["live_snapshot"]["path"]))
        diagnostic_path = Path(str(bindings["publication_diagnostic"]["path"]))
        test_evidence_path = Path(str(bindings["test_evidence"]["path"]))
        rebuilt = build_platform_verification(
            strict_report=values["strict_report"],
            prospective_lock=values["prospective_lock"],
            cycle=values["cycle"],
            evaluation=values["evaluation"],
            offline_snapshot=values["offline_snapshot"],
            sites_build_evidence=values["sites_build_evidence"],
            test_evidence=values["test_evidence"],
            publication_audit=values["publication_audit"],
            live_snapshot_path=live_path,
            publication_diagnostic_path=diagnostic_path,
            runtime_root=runtime_root,
            test_evidence_root=test_evidence_path.parent,
            code_root=repository_root,
            checked_at=checked_at,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return ["platform.rebuild:failed"]
    platform_inputs = platform.get("inputs")
    rebuilt_inputs = rebuilt.get("inputs")
    if not isinstance(platform_inputs, Mapping) or not isinstance(rebuilt_inputs, dict):
        return ["platform.rebuild:failed"]
    for name in PLATFORM_INPUTS:
        original_summary = platform_inputs.get(name)
        rebuilt_summary = rebuilt_inputs.get(name)
        if isinstance(original_summary, Mapping) and isinstance(rebuilt_summary, dict):
            rebuilt_summary["path"] = original_summary.get("path")
    if _canonical_evidence_sha256(rebuilt) != _canonical_evidence_sha256(platform):
        return ["platform.rebuild:mismatch"]
    return []


def validate_maturity_receipt(
    receipt: Mapping[str, Any],
    *,
    artifact_paths: Mapping[str, Path],
    runtime_root: Path,
    repository_root: Path,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Re-read every bound file before a publication process performs writes."""

    reference = checked_at or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    failures: list[str] = []
    if set(receipt) != MATURITY_RECEIPT_FIELDS:
        failures.append("maturity.fields:mismatch")
    if receipt.get("schema_version") != MATURITY_GATE_SCHEMA:
        failures.append("maturity.schema_version:invalid")
    if receipt.get("status") != "passed":
        failures.append(f"maturity.status:{receipt.get('status', 'missing')}")
    if receipt.get("passed") is not True:
        failures.append("maturity.passed:not_true")
    receipt_failures = receipt.get("failures")
    if not isinstance(receipt_failures, list):
        failures.append("maturity.failures:invalid")
    elif receipt_failures:
        failures.append("maturity.failures:nonempty")
    if receipt.get("requirements") != _maturity_requirements():
        failures.append("maturity.requirements:mismatch")
    generated_at = _utc(receipt.get("generated_at"))
    expires_at = _utc(receipt.get("expires_at"))
    if generated_at is None:
        failures.append("maturity.generated_at:invalid")
    elif generated_at > reference.astimezone(timezone.utc):
        failures.append("maturity.generated_at:future")
    if expires_at is None:
        failures.append("maturity.expires_at:invalid")
    elif expires_at < reference.astimezone(timezone.utc):
        failures.append("maturity.expires_at:expired")
    if generated_at is not None and expires_at is not None:
        if expires_at <= generated_at:
            failures.append("maturity.expires_at:not_after_generated_at")
        elif expires_at - generated_at > MATURITY_RECEIPT_TTL:
            failures.append("maturity.expires_at:ttl_too_long")

    stored_bindings = receipt.get("artifact_bindings")
    if not isinstance(stored_bindings, Mapping):
        return {
            "status": "blocked",
            "failures": [*failures, "maturity.artifact_bindings:missing"],
        }
    if set(artifact_paths) != set(ARTIFACT_BINDING_NAMES):
        failures.append("maturity.artifact_paths:names_mismatch")
    if set(stored_bindings) != set(ARTIFACT_BINDING_NAMES):
        failures.append("maturity.artifact_bindings:names_mismatch")
    current_bindings, binding_failures, values = _build_artifact_bindings(
        artifact_paths,
        runtime_root=runtime_root,
        repository_root=repository_root,
    )
    failures.extend(binding_failures)
    for name in ARTIFACT_BINDING_NAMES:
        stored = stored_bindings.get(name)
        if not isinstance(stored, Mapping):
            failures.append(f"artifact_bindings.{name}:missing")
            continue
        current = current_bindings[name]
        if set(stored) != set(current):
            failures.append(f"artifact_bindings.{name}.fields:mismatch")
        for field in (
            "path",
            "raw_file_sha256",
            "canonical_content_sha256",
            "schema_version",
        ):
            if stored.get(field) != current.get(field):
                failures.append(f"artifact_bindings.{name}.{field}:mismatch")
        if name == "prospective_lock":
            for field in (
                "model_version_sha256",
                "computed_model_version_sha256",
            ):
                if stored.get(field) != current.get(field):
                    failures.append(f"artifact_bindings.{name}.{field}:mismatch")

    cycle = values.get("cycle")
    live_binding = current_bindings["live_snapshot"]
    if cycle is not None:
        sync = cycle.get("sync")
        declared_snapshot_sha = sync.get("snapshot_sha256") if isinstance(sync, Mapping) else None
        if declared_snapshot_sha != live_binding.get("raw_file_sha256"):
            failures.append("cycle.sync.snapshot_sha256:actual_file_mismatch")

    platform = values.get("platform_verification")
    if platform is not None:
        failures.extend(
            _receipt_platform_failures(
                platform,
                prospective_lock=values.get("prospective_lock"),
                reference=reference.astimezone(timezone.utc),
            )
        )
        inputs = platform.get("inputs")
        if not isinstance(inputs, Mapping):
            failures.append("platform.inputs:missing")
        else:
            for input_name in PLATFORM_INPUTS:
                summary = inputs.get(input_name)
                if not isinstance(summary, Mapping):
                    failures.append(f"platform.inputs.{input_name}:missing")
                    continue
                binding = current_bindings[input_name]
                if input_name != "prediction_archive":
                    actual_canonical = summary.get("canonical_content_sha256")
                    if actual_canonical is None:
                        actual_canonical = summary.get("canonical_sha256")
                    if actual_canonical != binding.get("canonical_content_sha256"):
                        failures.append(
                            f"platform.inputs.{input_name}.canonical_content_sha256:mismatch"
                        )
                summary_raw = summary.get("raw_file_sha256")
                if input_name in {
                    "live_snapshot",
                    "prediction_archive",
                    "publication_diagnostic",
                } and (summary_raw != binding.get("raw_file_sha256")):
                    failures.append(f"platform.inputs.{input_name}.raw_file_sha256:mismatch")
                try:
                    summary_path = Path(str(summary.get("path"))).resolve(strict=True)
                    binding_path = Path(str(binding.get("path"))).resolve(strict=True)
                except (OSError, RuntimeError, ValueError):
                    failures.append(f"platform.inputs.{input_name}.path:invalid")
                else:
                    if summary_path != binding_path:
                        failures.append(f"platform.inputs.{input_name}.path:mismatch")
        failures.extend(
            _rebuild_platform_failures(
                platform,
                values=values,
                bindings=current_bindings,
                runtime_root=runtime_root,
                repository_root=repository_root,
            )
        )

    failures = list(dict.fromkeys(failures))
    return {"status": "pass" if not failures else "blocked", "failures": failures}


def _platform_failures(
    platform_verification: Mapping[str, Any] | None,
    *,
    strict_report: Mapping[str, Any],
    prospective_lock: Mapping[str, Any] | None,
    reference: datetime,
    max_age_seconds: int | None,
) -> list[str]:
    failures: list[str] = []
    if platform_verification is None:
        return [f"platform.{key}:missing" for key in PLATFORM_GATES]

    failures.extend(strict_report_v260_failures(strict_report))

    selection = strict_report.get("model_selection_audit")
    nested_lock = selection.get("prospective_lock") if isinstance(selection, Mapping) else None
    expects_lock = prospective_lock is not None or isinstance(nested_lock, Mapping)

    schema_version = platform_verification.get("schema_version")
    if schema_version != PLATFORM_VERIFICATION_SCHEMA:
        failures.append(f"platform.schema_version:{schema_version or 'missing'}")
    status = platform_verification.get("status")
    if status != "pass":
        failures.append(f"platform.status:{status or 'missing'}")
    passed = platform_verification.get("passed")
    if passed is not True:
        rendered = "missing" if passed is None else str(passed).lower()
        failures.append(f"platform.passed:{rendered}")

    checked_at = _utc(platform_verification.get("checked_at"))
    if checked_at is None:
        failures.append("platform.checked_at:invalid")
    else:
        age_seconds = (reference - checked_at).total_seconds()
        if age_seconds < 0:
            failures.append("platform.checked_at:future")
        elif max_age_seconds is not None and age_seconds > max_age_seconds:
            failures.append("platform.checked_at:expired")

    identity_validation = platform_verification.get("identity_validation")
    if not isinstance(identity_validation, Mapping):
        failures.append("platform.identity_validation:missing")
    else:
        if identity_validation.get("status") != "pass":
            failures.append(
                "platform.identity_validation.status:"
                f"{identity_validation.get('status', 'missing')}"
            )
        validation_reasons = identity_validation.get("reasons")
        if not isinstance(validation_reasons, list):
            failures.append("platform.identity_validation.reasons:invalid")
        elif validation_reasons:
            failures.append("platform.identity_validation.reasons:nonempty")

    strict_validation = platform_verification.get("strict_report_validation")
    if not isinstance(strict_validation, Mapping):
        failures.append("platform.strict_report_validation:missing")
    else:
        if strict_validation.get("status") != "pass":
            failures.append(
                "platform.strict_report_validation.status:"
                f"{strict_validation.get('status', 'missing')}"
            )
        validation_reasons = strict_validation.get("reasons")
        if not isinstance(validation_reasons, list):
            failures.append("platform.strict_report_validation.reasons:invalid")
        elif validation_reasons:
            failures.append("platform.strict_report_validation.reasons:nonempty")

    maturity = platform_verification.get("maturity")
    if not isinstance(maturity, Mapping):
        failures.extend(f"platform.{key}:missing" for key in PLATFORM_GATES)
    else:
        for key in PLATFORM_GATES:
            if maturity.get(key) != "pass":
                failures.append(f"platform.{key}:{maturity.get(key, 'missing')}")

    gate_evidence = platform_verification.get("gate_evidence")
    if not isinstance(gate_evidence, Mapping):
        failures.append("platform.gate_evidence:missing")
    else:
        for key in PLATFORM_GATES:
            detail = gate_evidence.get(key)
            if not isinstance(detail, Mapping):
                failures.append(f"platform.gate_evidence.{key}:missing")
                continue
            detail_status = detail.get("status")
            maturity_status = maturity.get(key) if isinstance(maturity, Mapping) else None
            if detail_status != "pass":
                failures.append(
                    f"platform.gate_evidence.{key}.status:{detail_status or 'missing'}"
                )
            if detail_status != maturity_status:
                failures.append(f"platform.gate_evidence.{key}.status:maturity_mismatch")
            reasons = detail.get("reasons")
            if not isinstance(reasons, list):
                failures.append(f"platform.gate_evidence.{key}.reasons:invalid")
            elif reasons:
                failures.append(f"platform.gate_evidence.{key}.reasons:nonempty")
            evidence = detail.get("evidence")
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(item, str) or not item for item in evidence)
            ):
                failures.append(f"platform.gate_evidence.{key}.evidence:invalid")
            else:
                required_evidence = set(PLATFORM_GATE_INPUTS[key])
                if not expects_lock:
                    required_evidence.discard("prospective_lock")
                if not required_evidence.issubset(evidence):
                    failures.append(f"platform.gate_evidence.{key}.evidence:incomplete")

    inputs = platform_verification.get("inputs")
    if not isinstance(inputs, Mapping):
        failures.append("platform.inputs:missing")
    else:
        required_inputs = set(PLATFORM_INPUTS)
        if not expects_lock:
            required_inputs.discard("prospective_lock")
        for name in PLATFORM_INPUTS:
            summary = inputs.get(name)
            if not isinstance(summary, Mapping):
                failures.append(f"platform.inputs.{name}:missing")
                continue
            present = summary.get("present")
            digest = summary.get("canonical_sha256")
            if name in required_inputs and present is not True:
                failures.append(f"platform.inputs.{name}.present:not_true")
            if present is True:
                if name == "prediction_archive":
                    raw_digest = summary.get("raw_file_sha256")
                    if (
                        not isinstance(raw_digest, str)
                        or re.fullmatch(r"[0-9a-f]{64}", raw_digest) is None
                    ):
                        failures.append(
                            "platform.inputs.prediction_archive.raw_file_sha256:invalid"
                        )
                elif not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    failures.append(f"platform.inputs.{name}.canonical_sha256:invalid")
                if name in {
                    "live_snapshot",
                    "prediction_archive",
                    "publication_diagnostic",
                }:
                    path = summary.get("path")
                    if not isinstance(path, str) or not path.strip():
                        failures.append(f"platform.inputs.{name}.path:invalid")
        strict_summary = inputs.get("strict_report")
        if isinstance(strict_summary, Mapping):
            if strict_summary.get("canonical_sha256") != _canonical_evidence_sha256(strict_report):
                failures.append("platform.inputs.strict_report.canonical_sha256:mismatch")
            strict_admission = strict_report.get("training_admission")
            strict_contract = strict_report.get("training_source_contract")
            expected_strict_identity = {
                "schema_version": strict_report.get("schema_version"),
                "report_lane": strict_report.get("report_lane"),
                "policy_version": (
                    strict_contract.get("policy_version")
                    if isinstance(strict_contract, Mapping)
                    else None
                ),
                "training_admission_sha256": (
                    strict_admission.get("admission_sha256")
                    if isinstance(strict_admission, Mapping)
                    else None
                ),
                "observed_before": (
                    strict_admission.get("observed_before")
                    if isinstance(strict_admission, Mapping)
                    else None
                ),
                "source_ids": (
                    strict_admission.get("source_ids")
                    if isinstance(strict_admission, Mapping)
                    else None
                ),
            }
            for field, expected in expected_strict_identity.items():
                if strict_summary.get(field) != expected:
                    failures.append(f"platform.inputs.strict_report.{field}:mismatch")
        lock_summary = inputs.get("prospective_lock")
        if prospective_lock is not None and isinstance(lock_summary, Mapping):
            if lock_summary.get("canonical_sha256") != _canonical_evidence_sha256(
                prospective_lock
            ):
                failures.append("platform.inputs.prospective_lock.canonical_sha256:mismatch")

    # Gate labels are not portable evidence by themselves.  Bind every
    # platform verification artifact to the exact strict report and, when a
    # repository lock is supplied, to that exact lock.  Otherwise an older
    # all-pass pointer can accidentally clear a newer model/research report.
    identity = platform_verification.get("evidence_identity")
    if not isinstance(identity, Mapping):
        failures.append("platform.evidence_identity:missing")
        return failures

    expected_report_sha = _canonical_evidence_sha256(strict_report)
    report_sha = identity.get("strict_report_sha256")
    if not isinstance(report_sha, str) or not report_sha:
        failures.append("platform.evidence_identity.strict_report_sha256:missing")
    elif report_sha != expected_report_sha:
        failures.append("platform.evidence_identity.strict_report_sha256:mismatch")

    expected_model_sha: Any = None
    if prospective_lock is not None:
        expected_lock_sha = _canonical_evidence_sha256(prospective_lock)
        lock_sha = identity.get("prospective_lock_sha256")
        if not isinstance(lock_sha, str) or not lock_sha:
            failures.append("platform.evidence_identity.prospective_lock_sha256:missing")
        elif lock_sha != expected_lock_sha:
            failures.append("platform.evidence_identity.prospective_lock_sha256:mismatch")
        expected_model_sha = prospective_lock.get("model_version_sha256")
    elif isinstance(nested_lock, Mapping):
        expected_model_sha = nested_lock.get("model_version_sha256")

    if isinstance(expected_model_sha, str) and expected_model_sha:
        model_sha = identity.get("model_version_sha256")
        if not isinstance(model_sha, str) or not model_sha:
            failures.append("platform.evidence_identity.model_version_sha256:missing")
        elif model_sha != expected_model_sha:
            failures.append("platform.evidence_identity.model_version_sha256:mismatch")
    return failures


def _prospective_lock_failures(
    strict_report: Mapping[str, Any],
    prospective_lock: Mapping[str, Any] | None,
) -> list[str]:
    """Reject a strict report that embeds stale prospective-lock metadata.

    The strict report is a derived artifact and can outlive a pre-result lock
    migration.  Comparing the identity fields here keeps maturity validation
    fail-closed instead of silently evaluating a report against a superseded
    lock.  The check is optional for library callers that only validate an
    isolated synthetic report; the CLI supplies the repository lock by
    default.
    """

    if prospective_lock is None:
        return []
    computed_model_sha = canonical_model_version_sha256(prospective_lock)
    aggregate_failures: list[str] = []
    if computed_model_sha is None:
        aggregate_failures.append("prospective_lock.model_files:invalid")
    elif prospective_lock.get("model_version_sha256") != computed_model_sha:
        aggregate_failures.append("prospective_lock.model_version_sha256:aggregate_mismatch")
    selection = strict_report.get("model_selection_audit")
    nested = selection.get("prospective_lock") if isinstance(selection, Mapping) else None
    if not isinstance(nested, Mapping):
        return [*aggregate_failures, "strict_report.prospective_lock:missing"]
    failures: list[str] = list(aggregate_failures)
    for field in (
        "status",
        "model_version_sha256",
        "freeze_model_name",
        "locked_at",
        "evaluation_window_started_at",
        "evaluation_window",
    ):
        expected = prospective_lock.get(field)
        if expected is not None and nested.get(field) != expected:
            failures.append(f"strict_report.prospective_lock.{field}:mismatch")
    return failures


def validate_maturity(
    strict_report: Mapping[str, Any],
    *,
    platform_verification: Mapping[str, Any] | None = None,
    prospective_lock: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
    platform_max_age_seconds: int | None = None,
    require_prospective_lock: bool = False,
    artifact_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    artifact_binding_failures: list[str] | None = None,
    expires_at: datetime | None = None,
    require_artifact_bindings: bool = True,
) -> dict[str, Any]:
    """Validate all release evidence and return a machine-readable artifact."""

    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    if platform_max_age_seconds is not None and (
        not isinstance(platform_max_age_seconds, int)
        or isinstance(platform_max_age_seconds, bool)
        or platform_max_age_seconds < 1
    ):
        raise ValueError("platform_max_age_seconds must be a positive integer")
    reference = generated_at.astimezone(timezone.utc)
    if expires_at is None:
        expires_at = reference + MATURITY_RECEIPT_TTL
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expires_at must be timezone-aware")
    failures = _strict_failures(strict_report)
    if require_prospective_lock and prospective_lock is None:
        failures.append("prospective_lock:missing")
    failures.extend(_prospective_lock_failures(strict_report, prospective_lock))
    failures.extend(
        _platform_failures(
            platform_verification,
            strict_report=strict_report,
            prospective_lock=prospective_lock,
            reference=reference,
            max_age_seconds=platform_max_age_seconds,
        )
    )
    if require_artifact_bindings and not artifact_bindings:
        failures.append("artifact_bindings:missing")
    failures.extend(artifact_binding_failures or [])
    # Preserve ordering while deduplicating cross-checks.
    failures = list(dict.fromkeys(failures))
    return {
        "schema_version": MATURITY_GATE_SCHEMA,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "status": "passed" if not failures else "blocked",
        "passed": not failures,
        "failures": failures,
        "artifact_bindings": dict(artifact_bindings or {}),
        "requirements": _maturity_requirements(),
    }


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} root must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-report", type=Path, required=True)
    parser.add_argument("--platform-verification", type=Path)
    parser.add_argument(
        "--prospective-lock",
        type=Path,
        default=Path("docs/evidence/prospective-model-lock-current.json"),
        help="current lock used to verify the strict report's nested lock identity",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-platform-age-seconds",
        type=int,
        help="optionally reject a platform receipt older than this many seconds",
    )
    parser.add_argument(
        "--require-passed",
        action="store_true",
        help="return nonzero after writing the receipt when maturity is blocked",
    )
    parser.add_argument("--cycle", type=Path)
    parser.add_argument("--evaluation", type=Path)
    parser.add_argument("--live-snapshot", type=Path)
    parser.add_argument("--prediction-archive", type=Path)
    parser.add_argument("--offline-snapshot", type=Path)
    parser.add_argument("--sites-build-evidence", type=Path)
    parser.add_argument("--test-evidence", type=Path)
    parser.add_argument("--publication-audit", type=Path)
    parser.add_argument("--publication-diagnostic", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--receipt-ttl-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    if args.receipt_ttl_seconds < 1 or args.receipt_ttl_seconds > int(
        MATURITY_RECEIPT_TTL.total_seconds()
    ):
        parser.error("--receipt-ttl-seconds must be between 1 and 1800")
    strict = _load_json(args.strict_report)
    platform = _load_json(args.platform_verification) if args.platform_verification else None
    lock = _load_json(args.prospective_lock) if args.prospective_lock.exists() else None
    artifact_paths: dict[str, Path | None] = {
        "platform_verification": args.platform_verification,
        "strict_report": args.strict_report,
        "prospective_lock": args.prospective_lock,
        "cycle": args.cycle,
        "evaluation": args.evaluation,
        "live_snapshot": args.live_snapshot,
        "prediction_archive": args.prediction_archive,
        "offline_snapshot": args.offline_snapshot,
        "sites_build_evidence": args.sites_build_evidence,
        "test_evidence": args.test_evidence,
        "publication_audit": args.publication_audit,
        "publication_diagnostic": args.publication_diagnostic,
    }
    bindings, binding_failures, _ = _build_artifact_bindings(
        artifact_paths,
        runtime_root=args.runtime_root,
        repository_root=args.repository_root,
    )
    generated_at = datetime.now(timezone.utc)
    result = validate_maturity(
        strict,
        platform_verification=platform,
        prospective_lock=lock,
        generated_at=generated_at,
        platform_max_age_seconds=args.max_platform_age_seconds,
        require_prospective_lock=args.require_passed,
        artifact_bindings=bindings,
        artifact_binding_failures=binding_failures,
        expires_at=generated_at + timedelta(seconds=args.receipt_ttl_seconds),
    )
    if args.require_passed and result["passed"] is True:
        expected_paths = {name: path for name, path in artifact_paths.items() if path is not None}
        verification = validate_maturity_receipt(
            result,
            artifact_paths=expected_paths,
            runtime_root=args.runtime_root or Path("/nonexistent-runtime-root"),
            repository_root=args.repository_root,
            checked_at=generated_at,
        )
        if verification["status"] != "pass":
            result["failures"] = list(
                dict.fromkeys([*result["failures"], *verification["failures"]])
            )
            result["status"] = "blocked"
            result["passed"] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "passed": result["passed"],
                "failure_count": len(result["failures"]),
            },
            ensure_ascii=False,
        )
    )
    return 1 if args.require_passed and result["passed"] is not True else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["validate_maturity", "validate_maturity_receipt"]
