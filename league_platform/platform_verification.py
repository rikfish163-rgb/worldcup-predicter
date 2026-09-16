"""Build current, artifact-backed platform acceptance evidence.

This module is an evidence aggregator, not a test runner and not a release
override.  Every gate is derived from the supplied strict report, operational
cycle, read model, build/test receipts, or public publication audit.  Missing
evidence stays ``not_verified`` and contradictory evidence stays ``blocked``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCES,
)
from league_platform.prospective_archive import (
    logical_freeze_key,
    stable_prediction_digest,
    validate_prediction,
)
from league_platform.source_rights import POLICY_VERSION
from league_platform.sources.openfootball_verified import (
    ADAPTER_CONTRACT_VERSION,
    ADMISSION_SCHEMA_VERSION,
)


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
REQUIRED_TARGET_GATES = (
    "three_way",
    "totals",
    "total_over_under",
    "half_full",
    "handicap",
)
REQUIRED_API_ROUTES = (
    "/api/v1/catalog",
    "/api/v1/matches",
    "/api/v1/service-status",
)
TEST_EVIDENCE_MAX_AGE = timedelta(hours=24)
OPERATIONAL_EVIDENCE_MAX_AGE = timedelta(minutes=30)
OPERATIONAL_STATUSES = {"pending_prospective_window", "passed"}
TEST_EVIDENCE_PRODUCER = {
    "schema_version": "matchline.platform_test_producer.v1",
    "name": "matchline-platform-test-runner",
    "version": "1",
}
TEST_SUITE_COMMANDS = {
    "python": ("pytest", "-q", "tests"),
    "sites": ("npm", "test", "--", "--run"),
    "typecheck": ("mypy", "--config-file", "pyproject.toml"),
    "lint": ("ruff", "check", "--config", "pyproject.toml"),
}
REQUIRED_CODE_MANIFEST_PATHS = (
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
PREDICTION_ARCHIVE_RECORD_FIELDS = {
    "schema_version",
    "record_key",
    "freeze_key",
    "content_sha256",
    "captured_at",
    "model_version",
    "model_version_sha256",
    "model_lock_window_started_at",
    "conflict",
    "conflict_with",
    "prediction",
}
FORMAL_STRICT_REPORT_SCHEMA = "matchline.strict_report.v260"
FORMAL_STRICT_REPORT_LANE = "formal_v260"
FORMAL_STRICT_REPORT_LEAGUES = {
    "premier-league",
    "championship",
    "la-liga",
    "bundesliga",
    "serie-a",
    "ligue-1",
    "csl",
}
FORMAL_SOURCE_CONTRACT_FIELDS = {
    "provider",
    "raw_archive_required",
    "training_admission_sha256",
    "policy_version",
    "observed_before",
    "source_ids",
    "prohibited_formal_inputs",
}
FORMAL_TRAINING_ADMISSION_FIELDS = {
    "schema_version",
    "status",
    "training_admitted",
    "training_source",
    "admission_scope",
    "loader_contract_version",
    "adapter_contract_version",
    "observed_before",
    "source_ids",
    "competition_ids",
    "selected_records",
    "raw_admission_sha256",
    "raw_manifest_sha256",
    "source_manifest_sha256",
    "source_identity_sha256",
    "training_policy_sha256",
    "parser_contract_sha256",
    "raw_rows_sha256",
    "domain_rows_sha256",
    "counts",
    "statement",
    "admission_sha256",
}
FORMAL_ADMISSION_DIGEST_FIELDS = {
    "raw_admission_sha256",
    "raw_manifest_sha256",
    "source_manifest_sha256",
    "source_identity_sha256",
    "training_policy_sha256",
    "parser_contract_sha256",
    "raw_rows_sha256",
    "domain_rows_sha256",
    "admission_sha256",
}


def canonical_evidence_sha256(value: Mapping[str, Any]) -> str:
    """Return the same whitespace-independent digest used by maturity validation."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _strict_canonical_sha256(value: object) -> str | None:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload).hexdigest()


def strict_report_v260_failures(report: Mapping[str, Any]) -> list[str]:
    """Validate the formal report's immutable raw-training contract.

    This deliberately does not decide statistical maturity.  It establishes
    that the object reaching downstream gates is the formal v260 lane and that
    its complete training-admission receipt is internally content-addressed.
    """

    failures: list[str] = []
    if report.get("schema_version") != FORMAL_STRICT_REPORT_SCHEMA:
        failures.append("strict_report.schema_version:invalid")
    if report.get("report_lane") != FORMAL_STRICT_REPORT_LANE:
        failures.append("strict_report.report_lane:invalid")
    if _utc(report.get("generated_at")) is None:
        failures.append("strict_report.generated_at:invalid")

    raw_contract = report.get("training_source_contract")
    contract = raw_contract if isinstance(raw_contract, Mapping) else None
    if contract is None:
        failures.append("strict_report.training_source_contract:missing")
    else:
        if set(contract) != FORMAL_SOURCE_CONTRACT_FIELDS:
            failures.append("strict_report.training_source_contract.fields:invalid")
        if contract.get("provider") != "OpenFootball":
            failures.append("strict_report.training_source_contract.provider:invalid")
        if contract.get("raw_archive_required") is not True:
            failures.append("strict_report.training_source_contract.raw_archive_required:not_true")
        if contract.get("policy_version") != POLICY_VERSION:
            failures.append("strict_report.training_source_contract.policy_version:invalid")
        if contract.get("prohibited_formal_inputs") != [
            "football_data_csv",
            "legacy_csl_files",
            "snapshot_self_report",
        ]:
            failures.append(
                "strict_report.training_source_contract.prohibited_formal_inputs:invalid"
            )

    raw_admission = report.get("training_admission")
    admission = raw_admission if isinstance(raw_admission, Mapping) else None
    if admission is None:
        failures.append("strict_report.training_admission:missing")
        return list(dict.fromkeys(failures))
    if set(admission) != FORMAL_TRAINING_ADMISSION_FIELDS:
        failures.append("strict_report.training_admission.fields:invalid")
    expected_headers = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "status": "training_admitted",
        "training_admitted": True,
        "training_source": "verified_openfootball_raw_archive_only",
        "admission_scope": "historical_training",
        "loader_contract_version": "1.0.0",
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
    }
    for field, expected in expected_headers.items():
        if admission.get(field) != expected:
            failures.append(f"strict_report.training_admission.{field}:invalid")
    for field in FORMAL_ADMISSION_DIGEST_FIELDS:
        if not _valid_sha256(admission.get(field)):
            failures.append(f"strict_report.training_admission.{field}:invalid")

    observed_before = _utc(admission.get("observed_before"))
    if observed_before is None:
        failures.append("strict_report.training_admission.observed_before:invalid")
    if contract is not None and admission.get("observed_before") != contract.get(
        "observed_before"
    ):
        failures.append("strict_report.training_admission.observed_before:contract_mismatch")

    source_ids = admission.get("source_ids")
    allowed_source_ids = set(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    valid_source_ids = bool(
        isinstance(source_ids, list)
        and source_ids
        and all(isinstance(source_id, str) and source_id for source_id in source_ids)
        and source_ids == sorted(source_ids)
        and len(source_ids) == len(set(source_ids))
        and set(source_ids).issubset(allowed_source_ids)
    )
    if not valid_source_ids:
        failures.append("strict_report.training_admission.source_ids:invalid")
        normalized_source_ids: list[str] = []
    else:
        normalized_source_ids = list(source_ids)
    if contract is not None and contract.get("source_ids") != source_ids:
        failures.append("strict_report.training_admission.source_ids:contract_mismatch")

    expected_competitions = sorted(
        {
            OPENFOOTBALL_HISTORY_SOURCES[source_id]["competition_id"]
            for source_id in normalized_source_ids
        }
    )
    if admission.get("competition_ids") != expected_competitions:
        failures.append("strict_report.training_admission.competition_ids:source_mismatch")

    selected_records = admission.get("selected_records")
    if not isinstance(selected_records, list) or len(selected_records) != len(
        normalized_source_ids
    ):
        failures.append("strict_report.training_admission.selected_records:invalid")
    else:
        selected_record_ids: list[str] = []
        for index, raw_record in enumerate(selected_records):
            if not isinstance(raw_record, Mapping) or set(raw_record) != {
                "source_id",
                "retrieved_at",
                "record_sha256",
                "raw_sha256",
            }:
                failures.append(
                    f"strict_report.training_admission.selected_records[{index}]:invalid"
                )
                continue
            source_id = raw_record.get("source_id")
            if not isinstance(source_id, str):
                failures.append(
                    f"strict_report.training_admission.selected_records[{index}].source_id:invalid"
                )
            else:
                selected_record_ids.append(source_id)
            retrieved_at = _utc(raw_record.get("retrieved_at"))
            if retrieved_at is None or (
                observed_before is not None and retrieved_at > observed_before
            ):
                failures.append(
                    f"strict_report.training_admission.selected_records[{index}].retrieved_at:invalid"
                )
            for field in ("record_sha256", "raw_sha256"):
                if not _valid_sha256(raw_record.get(field)):
                    failures.append(
                        f"strict_report.training_admission.selected_records[{index}].{field}:invalid"
                    )
        if selected_record_ids != normalized_source_ids:
            failures.append(
                "strict_report.training_admission.selected_records:source_ids_mismatch"
            )

    counts = admission.get("counts")
    required_count_fields = {
        "raw_rows",
        "finished_admitted",
        "upcoming_isolated",
        "exact_kickoff_admitted",
        "date_only_admitted",
        "competition_rows",
    }
    if not isinstance(counts, Mapping) or set(counts) != required_count_fields:
        failures.append("strict_report.training_admission.counts:invalid")
    else:
        scalar_counts: dict[str, int] = {}
        for field in required_count_fields - {"competition_rows"}:
            value = counts.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                failures.append(f"strict_report.training_admission.counts.{field}:invalid")
            else:
                scalar_counts[field] = value
        competition_rows = counts.get("competition_rows")
        valid_competition_rows = bool(
            isinstance(competition_rows, Mapping)
            and set(competition_rows) == set(expected_competitions)
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in competition_rows.values()
            )
        )
        if not valid_competition_rows:
            failures.append(
                "strict_report.training_admission.counts.competition_rows:invalid"
            )
        if len(scalar_counts) == 5:
            if scalar_counts["raw_rows"] != (
                scalar_counts["finished_admitted"] + scalar_counts["upcoming_isolated"]
            ):
                failures.append("strict_report.training_admission.counts.raw_rows:mismatch")
            if scalar_counts["finished_admitted"] != (
                scalar_counts["exact_kickoff_admitted"]
                + scalar_counts["date_only_admitted"]
            ):
                failures.append(
                    "strict_report.training_admission.counts.finished_admitted:mismatch"
                )
            if valid_competition_rows and sum(counts["competition_rows"].values()) != scalar_counts[
                "finished_admitted"
            ]:
                failures.append(
                    "strict_report.training_admission.counts.competition_rows:mismatch"
                )

    admission_identity = {
        key: value
        for key, value in admission.items()
        if key not in {"raw_manifest_sha256", "statement", "admission_sha256"}
    }
    computed_admission_sha = _strict_canonical_sha256(admission_identity)
    if computed_admission_sha is None or admission.get(
        "admission_sha256"
    ) != computed_admission_sha:
        failures.append("strict_report.training_admission.admission_sha256:mismatch")
    if contract is not None and contract.get("training_admission_sha256") != admission.get(
        "admission_sha256"
    ):
        failures.append(
            "strict_report.training_source_contract.training_admission_sha256:mismatch"
        )

    leagues = report.get("leagues")
    if not isinstance(leagues, Mapping) or set(leagues) != FORMAL_STRICT_REPORT_LEAGUES:
        failures.append("strict_report.leagues:invalid")
    else:
        for league in FORMAL_STRICT_REPORT_LEAGUES - {"csl"}:
            value = leagues.get(league)
            if league not in expected_competitions:
                gates = value.get("gates") if isinstance(value, Mapping) else None
                valid_unavailable = bool(
                    isinstance(value, Mapping)
                    and value.get("status") == "unavailable"
                    and value.get("reason")
                    == "no_admitted_openfootball_history_source_at_cutoff"
                    and value.get("sample_n") == 0
                    and isinstance(gates, Mapping)
                    and set(gates) == set(REQUIRED_TARGET_GATES)
                    and all(
                        isinstance(gate, Mapping)
                        and gate.get("status") == "blocked"
                        and gate.get("sample_n") == 0
                        and isinstance(gate.get("failures"), list)
                        and bool(gate.get("failures"))
                        for gate in gates.values()
                    )
                )
                if not valid_unavailable:
                    failures.append(
                        f"strict_report.{league}:must_be_unavailable_without_admitted_source"
                    )
        csl = leagues.get("csl")
        if "csl" not in expected_competitions:
            csl_gates = csl.get("gates") if isinstance(csl, Mapping) else None
            valid_csl = bool(
                isinstance(csl, Mapping)
                and csl.get("status") == "unavailable"
                and csl.get("reason") == "no_declared_verified_raw_history_source"
                and csl.get("sample_n") == 0
                and isinstance(csl_gates, Mapping)
                and set(csl_gates) == set(REQUIRED_TARGET_GATES)
                and all(
                    isinstance(gate, Mapping)
                    and gate.get("status") == "blocked"
                    and gate.get("sample_n") == 0
                    and isinstance(gate.get("failures"), list)
                    and bool(gate.get("failures"))
                    for gate in csl_gates.values()
                )
            )
            if not valid_csl:
                failures.append(
                    "strict_report.csl:must_be_unavailable_without_verified_raw_source"
                )
            overall = report.get("overall")
            availability_blocks = (
                overall.get("availability_blocks") if isinstance(overall, Mapping) else None
            )
            if not isinstance(overall, Mapping) or overall.get("production_allowed") is not False:
                failures.append("strict_report.overall.production_allowed:must_be_false_for_csl")
            if not isinstance(availability_blocks, list) or not any(
                isinstance(reason, str)
                and reason.startswith("csl:no_declared_verified_raw_history_source")
                for reason in availability_blocks
            ):
                failures.append("strict_report.overall.availability_blocks:csl_missing")
    return list(dict.fromkeys(failures))


def _valid_primary_probability(prediction: Mapping[str, Any]) -> bool:
    raw = (
        prediction.get("primary_probability_1x2")
        or prediction.get("probability_1x2_from_scoreline")
        or prediction.get("scoreline_probability")
    )
    if not isinstance(raw, Mapping) or set(raw) != {"home", "draw", "away"}:
        return False
    values = list(raw.values())
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
        for value in values
    ):
        return False
    return abs(sum(float(value) for value in values) - 1.0) <= 1e-6


def _archive_record_key(
    *,
    freeze_key: str,
    content_sha256: str,
    model_version_sha256: str,
    model_lock_window_started_at: object,
) -> str:
    return canonical_evidence_sha256(
        {
            "freeze_key": freeze_key,
            "content_sha256": content_sha256,
            "model_version_sha256": model_version_sha256,
            "model_lock_window_started_at": model_lock_window_started_at,
        }
    )


def _validate_prediction_archive_row(
    row: Mapping[str, Any],
    *,
    lock: Mapping[str, Any],
) -> tuple[str, bool]:
    """Return the logical key and whether a structurally valid row is current."""

    if set(row) != PREDICTION_ARCHIVE_RECORD_FIELDS:
        raise ValueError("archive row fields do not match schema 1.0.0")
    if row.get("schema_version") != "1.0.0":
        raise ValueError("archive row schema_version is invalid")
    prediction = row.get("prediction")
    if not isinstance(prediction, Mapping):
        raise ValueError("archive row prediction is invalid")
    model_sha = row.get("model_version_sha256")
    if not isinstance(model_sha, str) or not _valid_sha256(model_sha):
        raise ValueError("archive row model_version_sha256 is invalid")
    if row.get("model_version") != prediction.get("model_version"):
        raise ValueError("archive row model_version does not match prediction")
    if any(str(key).startswith("actual_") or key == "outcome_1x2" for key in prediction):
        raise ValueError("archive prediction contains result fields")
    if not _valid_primary_probability(prediction):
        raise ValueError("archive prediction primary probability is invalid")
    try:
        freeze_key = logical_freeze_key(prediction)
    except (KeyError, TypeError) as exc:
        raise ValueError("archive prediction logical freeze identity is invalid") from exc
    if row.get("freeze_key") != freeze_key:
        raise ValueError("archive row freeze_key does not match prediction")
    content_sha = stable_prediction_digest(prediction)
    if row.get("content_sha256") != content_sha:
        raise ValueError("archive row content_sha256 does not match prediction")
    captured_at = _utc(row.get("captured_at"))
    if captured_at is None:
        raise ValueError("archive row captured_at is invalid")
    conflict_with = row.get("conflict_with")
    if not isinstance(row.get("conflict"), bool) or not isinstance(conflict_with, list):
        raise ValueError("archive row conflict contract is invalid")
    if any(not _valid_sha256(value) for value in conflict_with):
        raise ValueError("archive row conflict_with is invalid")
    if row.get("conflict") is not bool(conflict_with):
        raise ValueError("archive row conflict flag does not match conflict_with")
    expected_record_key = _archive_record_key(
        freeze_key=freeze_key,
        content_sha256=content_sha,
        model_version_sha256=model_sha,
        model_lock_window_started_at=row.get("model_lock_window_started_at"),
    )
    if row.get("record_key") != expected_record_key:
        raise ValueError("archive row record_key is invalid")
    row_lock_window = row.get("model_lock_window_started_at")
    if row_lock_window is not None and _utc(row_lock_window) is None:
        raise ValueError("archive row model lock window is invalid")

    is_current = model_sha == lock.get("model_version_sha256")
    if is_current:
        if row_lock_window != lock.get("evaluation_window_started_at"):
            raise ValueError("archive row lock window does not match current lock")
        validate_prediction(prediction, lock=lock)
    else:
        legacy_validation_lock = dict(lock)
        legacy_validation_lock["freeze_model_name"] = prediction.get("model_version")
        legacy_validation_lock.pop("evaluation_window_started_at", None)
        validate_prediction(prediction, lock=legacy_validation_lock)
    return freeze_key, is_current


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
    require_relative: bool = False,
) -> tuple[Path | None, list[str]]:
    reasons: list[str] = []
    if require_relative and path.is_absolute():
        return None, [f"{prefix}:absolute_path"]
    if ".." in path.parts:
        return None, [f"{prefix}:path_escape"]
    candidate = root / path if require_relative else path
    try:
        resolved_root = root.resolve(strict=True)
        if candidate.is_symlink():
            return None, [f"{prefix}:symlink_not_allowed"]
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except FileNotFoundError:
        reasons.append(f"{prefix}:missing")
        return None, reasons
    except (OSError, RuntimeError, ValueError):
        reasons.append(f"{prefix}:path_escape")
        return None, reasons
    if not resolved.is_file():
        return None, [f"{prefix}:not_file"]
    return resolved, reasons


def _file_input_summary(
    path: Path | None,
    root: Path | None,
    *,
    prefix: str,
) -> tuple[dict[str, Any], list[str], Mapping[str, Any] | None]:
    summary: dict[str, Any] = {
        "present": False,
        "path": str(path) if path is not None else None,
        "raw_file_sha256": None,
        "canonical_content_sha256": None,
        "canonical_sha256": None,
        "schema_version": None,
    }
    if path is None:
        return summary, [f"{prefix}:missing"], None
    if root is None:
        return summary, [f"{prefix}.root:missing"], None
    resolved, reasons = _confined_file(path, root, prefix=prefix)
    if resolved is None:
        return summary, reasons, None
    summary["path"] = str(resolved)
    summary["raw_file_sha256"] = _raw_file_sha256(resolved)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return summary, [*reasons, f"{prefix}:invalid_json"], None
    if not isinstance(value, Mapping):
        return summary, [*reasons, f"{prefix}:invalid_shape"], None
    summary.update(
        {
            "present": True,
            "schema_version": value.get("schema_version"),
            "canonical_content_sha256": canonical_evidence_sha256(value),
            "canonical_sha256": canonical_evidence_sha256(value),
        }
    )
    return summary, reasons, value


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def canonical_model_version_sha256(lock: Mapping[str, Any]) -> str | None:
    """Recompute the aggregate exactly as ``prospective_lock.refresh_lock`` does."""

    model_name = lock.get("model_name")
    freeze_model_name = lock.get("freeze_model_name")
    files = lock.get("model_files")
    if (
        not isinstance(model_name, str)
        or not model_name.strip()
        or not isinstance(freeze_model_name, str)
        or not freeze_model_name.strip()
        or not isinstance(files, list)
        or not files
    ):
        return None
    for entry in files:
        if not isinstance(entry, Mapping):
            return None
        path = entry.get("path")
        digest = entry.get("sha256")
        if (
            not isinstance(path, str)
            or not path.strip()
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            return None
    return canonical_evidence_sha256(
        {
            "model_name": model_name,
            "freeze_model_name": freeze_model_name,
            "model_files": files,
        }
    )


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


def _detail(
    reasons: list[str],
    *,
    missing: bool = False,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    if missing:
        status = "not_verified"
    elif reasons:
        status = "blocked"
    else:
        status = "pass"
    return {
        "status": status,
        "reasons": list(dict.fromkeys(reasons)),
        "evidence": evidence or [],
    }


def _identity(strict_report: Mapping[str, Any], lock: Mapping[str, Any]) -> dict[str, str | None]:
    model_sha = lock.get("model_version_sha256")
    return {
        "strict_report_sha256": canonical_evidence_sha256(strict_report),
        "prospective_lock_sha256": canonical_evidence_sha256(lock),
        "model_version_sha256": model_sha if isinstance(model_sha, str) and model_sha else None,
    }


def _current_identity_failures(
    strict_report: Mapping[str, Any],
    lock: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    model_sha = lock.get("model_version_sha256")
    if not isinstance(model_sha, str) or re.fullmatch(r"[0-9a-f]{64}", model_sha) is None:
        failures.append("prospective_lock.model_version_sha256:invalid")
    computed_model_sha = canonical_model_version_sha256(lock)
    if computed_model_sha is None:
        failures.append("prospective_lock.model_files:invalid")
    elif model_sha != computed_model_sha:
        failures.append("prospective_lock.model_version_sha256:aggregate_mismatch")
    selection = strict_report.get("model_selection_audit")
    nested = selection.get("prospective_lock") if isinstance(selection, Mapping) else None
    if not isinstance(nested, Mapping):
        failures.append("strict_report.prospective_lock:missing")
        return failures
    for field in (
        "model_version_sha256",
        "locked_at",
        "evaluation_window_started_at",
        "evaluation_window",
    ):
        expected = lock.get(field)
        if expected is not None and nested.get(field) != expected:
            failures.append(f"strict_report.prospective_lock.{field}:mismatch")
    return failures


def _force_blocked(detail: Mapping[str, Any], reasons: list[str]) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reasons": list(dict.fromkeys([*reasons, *_list(detail.get("reasons"))])),
        "evidence": _list(detail.get("evidence")),
    }


def _identity_failures(
    evidence: Mapping[str, Any],
    expected: Mapping[str, str | None],
    *,
    prefix: str,
) -> list[str]:
    actual = evidence.get("evidence_identity")
    if not isinstance(actual, Mapping):
        return [f"{prefix}.identity:missing"]
    failures: list[str] = []
    for field, expected_value in expected.items():
        actual_value = actual.get(field)
        if not isinstance(actual_value, str) or not actual_value:
            failures.append(f"{prefix}.identity.{field}:missing")
        elif actual_value != expected_value:
            failures.append(f"{prefix}.identity.{field}:mismatch")
    return failures


def _pretty_json_sha256(value: Mapping[str, Any]) -> str:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _cycle_evaluation_failures(
    cycle: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    lock: Mapping[str, Any],
    reference: datetime,
) -> list[str]:
    reasons: list[str] = []
    if cycle.get("schema_version") != "1.0.0":
        reasons.append("cycle.schema_version:invalid")
    if cycle.get("status") not in OPERATIONAL_STATUSES:
        reasons.append(f"cycle.status:{cycle.get('status', 'missing')}")
    if evaluation.get("schema_version") != "1.0.0":
        reasons.append("evaluation.schema_version:invalid")
    if evaluation.get("status") not in OPERATIONAL_STATUSES:
        reasons.append(f"evaluation.status:{evaluation.get('status', 'missing')}")

    started_at = _utc(cycle.get("started_at"))
    finished_at = _utc(cycle.get("finished_at"))
    generated_at = _utc(evaluation.get("generated_at"))
    capture_as_of = _utc(_mapping(cycle.get("capture")).get("as_of"))
    if started_at is None:
        reasons.append("cycle.started_at:invalid")
    if finished_at is None:
        reasons.append("cycle.finished_at:invalid")
    elif finished_at > reference:
        reasons.append("cycle.finished_at:after_checked_at")
    elif reference - finished_at > OPERATIONAL_EVIDENCE_MAX_AGE:
        reasons.append("cycle.finished_at:expired")
    if started_at is not None and finished_at is not None and finished_at < started_at:
        reasons.append("cycle.finished_at:before_started_at")
    if generated_at is None:
        reasons.append("evaluation.generated_at:invalid")
    elif generated_at > reference:
        reasons.append("evaluation.generated_at:after_checked_at")
    elif reference - generated_at > OPERATIONAL_EVIDENCE_MAX_AGE:
        reasons.append("evaluation.generated_at:expired")
    if started_at is not None and generated_at is not None and generated_at < started_at:
        reasons.append("evaluation.generated_at:before_cycle_started_at")
    if finished_at is not None and generated_at is not None and generated_at > finished_at:
        reasons.append("evaluation.generated_at:after_cycle_finished_at")
    if capture_as_of is None:
        reasons.append("cycle.capture.as_of:invalid")
    elif started_at is not None and capture_as_of < started_at:
        reasons.append("cycle.capture.as_of:before_cycle_started_at")
    elif finished_at is not None and capture_as_of > finished_at:
        reasons.append("cycle.capture.as_of:after_cycle_finished_at")

    embedded_evaluation = cycle.get("evaluation")
    if not isinstance(embedded_evaluation, Mapping):
        reasons.append("cycle.evaluation:missing")
    elif any(evaluation.get(key) != value for key, value in embedded_evaluation.items()):
        reasons.append("cycle.evaluation:external_evaluation_mismatch")
    artifacts = _mapping(cycle.get("artifacts"))
    if artifacts.get("model_version_sha256") != lock.get("model_version_sha256"):
        reasons.append("cycle.artifacts.model_version_sha256:mismatch")
    evaluation_path = artifacts.get("evaluation_evidence")
    if not isinstance(evaluation_path, str) or not evaluation_path.strip():
        reasons.append("cycle.artifacts.evaluation_evidence:missing")
    expected_evaluation_sha = _pretty_json_sha256(evaluation)
    if artifacts.get("evaluation_evidence_sha256") != expected_evaluation_sha:
        reasons.append("cycle.artifacts.evaluation_evidence_sha256:mismatch")
    archive_path = artifacts.get("prediction_archive")
    evaluation_archive = evaluation.get("prediction_archive")
    capture_archive = _mapping(cycle.get("capture")).get("archive")
    if (
        not isinstance(archive_path, str)
        or not archive_path
        or archive_path != evaluation_archive
        or archive_path != capture_archive
    ):
        reasons.append("cycle.artifacts.prediction_archive:path_mismatch")
    return reasons


def _snapshot_cycle_failures(
    snapshot: Mapping[str, Any] | None,
    cycle: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    reference: datetime,
) -> list[str]:
    if snapshot is None:
        return ["offline_snapshot:missing"]
    reasons: list[str] = []
    snapshot_as_of = _utc(snapshot.get("as_of"))
    snapshot_generated_at = _utc(snapshot.get("generated_at"))
    capture_as_of = _utc(_mapping(cycle.get("capture")).get("as_of"))
    cycle_finished_at = _utc(cycle.get("finished_at"))
    evaluation_generated_at = _utc(evaluation.get("generated_at"))
    if snapshot_as_of is None:
        reasons.append("offline_snapshot.as_of:invalid")
    elif capture_as_of is None or snapshot_as_of != capture_as_of:
        reasons.append("offline_snapshot.as_of:cycle_capture_mismatch")
    if snapshot_generated_at is None:
        reasons.append("offline_snapshot.generated_at:invalid")
    elif snapshot_generated_at > reference:
        reasons.append("offline_snapshot.generated_at:after_checked_at")
    elif reference - snapshot_generated_at > OPERATIONAL_EVIDENCE_MAX_AGE:
        reasons.append("offline_snapshot.generated_at:expired")
    if (
        snapshot_generated_at is not None
        and cycle_finished_at is not None
        and snapshot_generated_at < cycle_finished_at
    ):
        reasons.append("offline_snapshot.generated_at:before_cycle_finished_at")
    if (
        snapshot_generated_at is not None
        and evaluation_generated_at is not None
        and snapshot_generated_at < evaluation_generated_at
    ):
        reasons.append("offline_snapshot.generated_at:before_evaluation")
    if (
        snapshot_as_of is not None
        and snapshot_generated_at is not None
        and snapshot_as_of > snapshot_generated_at
    ):
        reasons.append("offline_snapshot.as_of:after_generated_at")
    return reasons


def _runtime_artifact_failures(
    *,
    lock: Mapping[str, Any],
    cycle: Mapping[str, Any] | None,
    evaluation: Mapping[str, Any] | None,
    offline_snapshot: Mapping[str, Any] | None,
    live_snapshot_path: Path | None,
    publication_diagnostic_path: Path | None,
    runtime_root: Path | None,
) -> tuple[
    list[str],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    live_summary, reasons, live_snapshot = _file_input_summary(
        live_snapshot_path,
        runtime_root,
        prefix="live_snapshot",
    )
    archive_summary: dict[str, Any] = {
        "present": False,
        "path": None,
        "raw_file_sha256": None,
        "schema_version": None,
        "canonical_content_sha256": None,
        "canonical_sha256": None,
    }
    diagnostic_summary, diagnostic_reasons, diagnostic = _file_input_summary(
        publication_diagnostic_path,
        runtime_root,
        prefix="publication_diagnostic",
    )
    reasons.extend(diagnostic_reasons)
    if cycle is None or evaluation is None:
        return reasons, live_summary, archive_summary, diagnostic_summary

    sync = _mapping(cycle.get("sync"))
    artifacts = _mapping(cycle.get("artifacts"))
    capture = _mapping(cycle.get("capture"))
    live_raw_sha = live_summary.get("raw_file_sha256")
    declared_live_sha = sync.get("snapshot_sha256")
    if live_summary.get("present") is True:
        if declared_live_sha != live_raw_sha:
            reasons.append("cycle.sync.snapshot_sha256:actual_file_mismatch")
        declared_live_path = artifacts.get("live_snapshot")
        if not isinstance(declared_live_path, str) or not declared_live_path.strip():
            reasons.append("cycle.artifacts.live_snapshot:missing")
        elif live_snapshot_path is not None:
            try:
                declared_resolved = Path(declared_live_path).resolve(strict=True)
                expected_resolved = live_snapshot_path.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                reasons.append("cycle.artifacts.live_snapshot:path_invalid")
            else:
                if declared_resolved != expected_resolved:
                    reasons.append("cycle.artifacts.live_snapshot:path_mismatch")
        live_as_of = _utc(_mapping(live_snapshot).get("as_of"))
        capture_as_of = _utc(capture.get("as_of"))
        sync_as_of = _utc(sync.get("as_of"))
        if live_as_of is None:
            reasons.append("live_snapshot.as_of:invalid")
        if capture_as_of != live_as_of:
            reasons.append("live_snapshot.as_of:cycle_capture_mismatch")
        if sync_as_of != live_as_of:
            reasons.append("live_snapshot.as_of:cycle_sync_mismatch")
        source_snapshot = _mapping(_mapping(offline_snapshot).get("source_snapshot"))
        if not source_snapshot:
            reasons.append("offline_snapshot.source_snapshot:missing")
        else:
            if source_snapshot.get("raw_file_sha256") != live_raw_sha:
                reasons.append("offline_snapshot.source_snapshot.raw_file_sha256:mismatch")
            if _utc(source_snapshot.get("as_of")) != live_as_of:
                reasons.append("offline_snapshot.source_snapshot.as_of:mismatch")

    archive_value = artifacts.get("prediction_archive")
    evaluation_archive = evaluation.get("prediction_archive")
    capture_archive = capture.get("archive")
    if not isinstance(archive_value, str) or not archive_value.strip():
        reasons.append("cycle.artifacts.prediction_archive:missing")
    elif runtime_root is None:
        reasons.append("prediction_archive.root:missing")
    else:
        archive_path, archive_path_reasons = _confined_file(
            Path(archive_value),
            runtime_root,
            prefix="prediction_archive",
        )
        reasons.extend(archive_path_reasons)
        if archive_path is not None:
            archive_sha = _raw_file_sha256(archive_path)
            physical_records = 0
            invalid_records = 0
            legacy_or_superseded_records = 0
            conflict_records = 0
            current_keys: set[str] = set()
            try:
                archive_lines = archive_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                archive_lines = []
                reasons.append("prediction_archive:unreadable")
            for line in archive_lines:
                if not line.strip():
                    continue
                physical_records += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    invalid_records += 1
                    continue
                if not isinstance(row, Mapping):
                    invalid_records += 1
                    continue
                # The archive is append-only across model-lock migrations.
                # A prior lock may use the pre-window schema (without
                # model_lock_window_started_at), so it cannot be validated as
                # a current row.  A valid, non-current model digest is an
                # explicit supersession boundary, not a current corruption;
                # malformed/current-digest rows remain fail-closed below.
                row_model_sha = row.get("model_version_sha256")
                lock_model_sha = lock.get("model_version_sha256")
                if (
                    isinstance(row_model_sha, str)
                    and len(row_model_sha) == 64
                    and all(char in "0123456789abcdefABCDEF" for char in row_model_sha)
                    and row_model_sha != lock_model_sha
                ):
                    legacy_or_superseded_records += 1
                    continue
                try:
                    freeze_key, is_current = _validate_prediction_archive_row(
                        row,
                        lock=lock,
                    )
                except (OSError, ValueError):
                    invalid_records += 1
                    continue
                if row.get("conflict") is True:
                    conflict_records += 1
                    continue
                if not is_current:
                    continue
                current_keys.add(freeze_key)
            current_records = len(current_keys)
            archive_summary.update(
                {
                    "present": True,
                    "path": str(archive_path),
                    "raw_file_sha256": archive_sha,
                    "physical_records": physical_records,
                    "current_lock_records": current_records,
                    "invalid_record_count": invalid_records,
                    "legacy_or_superseded_records": legacy_or_superseded_records,
                    "conflict_record_count": conflict_records,
                }
            )
            if invalid_records:
                reasons.append("prediction_archive.invalid_records:nonzero")
            if conflict_records:
                reasons.append("prediction_archive.conflict_records:nonzero")
            if current_records == 0:
                reasons.append("prediction_archive.current_lock_records:empty")
            scored_n = evaluation.get("scored_n")
            pending_n = evaluation.get("pending_n")
            counts_are_valid = (
                isinstance(scored_n, int)
                and not isinstance(scored_n, bool)
                and scored_n >= 0
                and isinstance(pending_n, int)
                and not isinstance(pending_n, bool)
                and pending_n >= 0
            )
            if not counts_are_valid:
                reasons.append("evaluation.current_lock_count:invalid")
            elif (
                isinstance(scored_n, int)
                and isinstance(pending_n, int)
                and current_records != scored_n + pending_n
            ):
                reasons.append("prediction_archive.current_lock_records:evaluation_count_mismatch")
            cycle_predictions = capture.get("predictions")
            if (
                isinstance(cycle_predictions, int)
                and not isinstance(cycle_predictions, bool)
                and cycle_predictions > current_records
            ):
                reasons.append("prediction_archive.current_lock_records:cycle_prediction_mismatch")
            if evaluation_archive != archive_value or capture_archive != archive_value:
                reasons.append("prediction_archive:path_mismatch")
            if artifacts.get("prediction_archive_sha256") != archive_sha:
                reasons.append("cycle.artifacts.prediction_archive_sha256:actual_file_mismatch")
            if evaluation.get("prediction_archive_sha256") != archive_sha:
                reasons.append("evaluation.prediction_archive_sha256:actual_file_mismatch")

    if diagnostic is not None and live_snapshot is not None:
        if diagnostic.get("status") != "published":
            reasons.append("publication_diagnostic.status:not_published")
        published = _mapping(diagnostic.get("published"))
        declared_output = published.get("output") or diagnostic.get("output")
        if not isinstance(declared_output, str) or live_snapshot_path is None:
            reasons.append("publication_diagnostic.output:missing")
        else:
            try:
                output_resolved = Path(declared_output).resolve(strict=True)
                live_resolved = live_snapshot_path.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                reasons.append("publication_diagnostic.output:invalid")
            else:
                if output_resolved != live_resolved:
                    reasons.append("publication_diagnostic.output:mismatch")
        if published.get("as_of") != live_snapshot.get("as_of"):
            reasons.append("publication_diagnostic.published.as_of:mismatch")
        if published.get("content_sha256") != canonical_evidence_sha256(live_snapshot):
            reasons.append("publication_diagnostic.published.content_sha256:canonical_mismatch")
    return reasons, live_summary, archive_summary, diagnostic_summary


def _traceability_detail(
    strict_report: Mapping[str, Any],
    lock: Mapping[str, Any],
    cycle: Mapping[str, Any] | None,
    evaluation: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    reference: datetime,
) -> dict[str, Any]:
    if cycle is None or evaluation is None:
        missing = []
        if cycle is None:
            missing.append("cycle:missing")
        if evaluation is None:
            missing.append("evaluation:missing")
        return _detail(missing, missing=True)

    reasons: list[str] = []
    reasons.extend(_cycle_evaluation_failures(cycle, evaluation, lock, reference))
    reasons.extend(_snapshot_cycle_failures(snapshot, cycle, evaluation, reference))
    overall = _mapping(strict_report.get("overall"))
    capture = _mapping(cycle.get("capture"))
    live_results = _mapping(capture.get("live_results"))
    feature_archive = _mapping(capture.get("feature_archive"))
    sync = _mapping(cycle.get("sync"))
    if overall.get("chronological_cutoff_gate") != "pass":
        reasons.append(
            "strict_report.overall.chronological_cutoff_gate:"
            f"{overall.get('chronological_cutoff_gate', 'missing')}"
        )
    snapshot_sha = sync.get("snapshot_sha256")
    if not isinstance(snapshot_sha, str) or re.fullmatch(r"[0-9a-f]{64}", snapshot_sha) is None:
        reasons.append("cycle.sync.snapshot_sha256:missing_or_invalid")
    predictions = capture.get("predictions")
    scored_n = evaluation.get("scored_n")
    pending_n = evaluation.get("pending_n")
    current_records = sum(
        value
        for value in (scored_n, pending_n)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    )
    latest_cycle_has_prediction = (
        isinstance(predictions, int) and not isinstance(predictions, bool) and predictions > 0
    )
    if not latest_cycle_has_prediction and current_records < 1:
        return _detail(
            [*reasons, "cycle.capture.predictions:no_current_prediction"],
            missing=True,
            evidence=[
                "strict_report",
                "cycle",
                "evaluation",
                "offline_snapshot",
                "prospective_lock",
                "live_snapshot",
                "prediction_archive",
                "publication_diagnostic",
            ],
        )
    for field, value in (
        ("cycle.capture.conflicts", capture.get("conflicts")),
        ("cycle.capture.live_results.conflict_count", live_results.get("conflict_count")),
        ("evaluation.archive_conflicts", evaluation.get("archive_conflicts")),
        ("evaluation.result_conflicts", evaluation.get("result_conflicts")),
    ):
        if value != 0:
            reasons.append(f"{field}:{value if value is not None else 'missing'}")
    if _list(feature_archive.get("errors")):
        reasons.append("cycle.capture.feature_archive.errors:nonempty")
    if _list(evaluation.get("invalid_records")):
        reasons.append("evaluation.invalid_records:nonempty")
    if evaluation.get("model_version_sha256") != lock.get("model_version_sha256"):
        reasons.append("evaluation.model_version_sha256:mismatch")
    return _detail(
        reasons,
        evidence=[
            "strict_report",
            "cycle",
            "evaluation",
            "offline_snapshot",
            "prospective_lock",
            "live_snapshot",
            "prediction_archive",
            "publication_diagnostic",
        ],
    )


def _source_isolation_detail(
    cycle: Mapping[str, Any] | None,
    evaluation: Mapping[str, Any] | None,
    lock: Mapping[str, Any],
    reference: datetime,
) -> dict[str, Any]:
    if cycle is None or evaluation is None:
        missing = []
        if cycle is None:
            missing.append("cycle:missing")
        if evaluation is None:
            missing.append("evaluation:missing")
        return _detail(missing, missing=True)

    reasons: list[str] = []
    reasons.extend(_cycle_evaluation_failures(cycle, evaluation, lock, reference))
    capture = _mapping(cycle.get("capture"))
    live_results = _mapping(capture.get("live_results"))
    storage = _mapping(evaluation.get("storage"))
    if cycle.get("runtime_only") is not True:
        reasons.append("cycle.runtime_only:not_true")
    if evaluation.get("results_not_used_for_selection") is not True:
        reasons.append("evaluation.results_not_used_for_selection:not_true")
    for field, value in (
        ("cycle.capture.conflicts", capture.get("conflicts")),
        ("cycle.capture.live_results.conflict_count", live_results.get("conflict_count")),
        ("evaluation.archive_conflicts", evaluation.get("archive_conflicts")),
        ("evaluation.result_conflicts", evaluation.get("result_conflicts")),
    ):
        if value != 0:
            reasons.append(f"{field}:{value if value is not None else 'missing'}")
    if _list(evaluation.get("invalid_records")):
        reasons.append("evaluation.invalid_records:nonempty")
    if storage.get("runtime_only") is not True:
        reasons.append("evaluation.storage.runtime_only:not_true")
    if storage.get("repository_check_skipped") is not True:
        reasons.append("evaluation.storage.repository_check_skipped:not_true")
    read_only_paths = [str(value) for value in _list(storage.get("read_only_paths"))]
    if not any(path.endswith("prospective-model-lock-current.json") for path in read_only_paths):
        reasons.append("evaluation.storage.read_only_model_lock:missing")
    if _list(storage.get("blocked_paths")):
        reasons.append("evaluation.storage.blocked_paths:nonempty")
    return _detail(
        reasons,
        evidence=["cycle", "evaluation", "prospective_lock", "prediction_archive"],
    )


def _calibration_detail(strict_report: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    overall = _mapping(strict_report.get("overall"))
    if overall.get("probability_contract_gate") != "pass":
        reasons.append(
            "strict_report.overall.probability_contract_gate:"
            f"{overall.get('probability_contract_gate', 'missing')}"
        )
    if overall.get("frequency_baseline_gate") != "pass_within_tolerance":
        reasons.append(
            "strict_report.overall.frequency_baseline_gate:"
            f"{overall.get('frequency_baseline_gate', 'missing')}"
        )
    if _list(overall.get("probability_contract_failures")):
        reasons.append("strict_report.overall.probability_contract_failures:nonempty")
    if _list(overall.get("frequency_baseline_failures")):
        reasons.append("strict_report.overall.frequency_baseline_failures:nonempty")
    return _detail(reasons, evidence=["strict_report"])


def _low_quality_detail(
    strict_report: Mapping[str, Any],
    lock: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if snapshot is None:
        return _detail(["offline_snapshot:missing"], missing=True)
    reasons: list[str] = []
    if snapshot.get("schema_version") != "matchline.offline_snapshot.v1":
        reasons.append("offline_snapshot.schema_version:invalid")
    snapshot_strict = _mapping(snapshot.get("strict_backtest"))
    if snapshot_strict.get("generated_at") != strict_report.get("generated_at"):
        reasons.append("offline_snapshot.strict_backtest.generated_at:mismatch")
    snapshot_evaluation = _mapping(snapshot.get("prospective_evaluation"))
    if snapshot_evaluation.get("model_version_sha256") != lock.get("model_version_sha256"):
        reasons.append("offline_snapshot.prospective_evaluation.model_version_sha256:mismatch")

    predictions = _list(snapshot.get("research_predictions"))
    low_rows = [
        row for row in predictions if _mapping(_mapping(row).get("coverage")).get("level") == "low"
    ]
    if not low_rows:
        return _detail(
            [*reasons, "offline_snapshot.research_predictions.low:not_exercised"],
            missing=True,
            evidence=["offline_snapshot", "strict_report", "prospective_lock"],
        )
    formal_fixture_ids = {
        row.get("fixture_id")
        for raw_row in _list(snapshot.get("predictions"))
        if (row := _mapping(raw_row)).get("fixture_id") is not None
    }
    for index, raw_row in enumerate(low_rows):
        row = _mapping(raw_row)
        coverage = _mapping(row.get("coverage"))
        if row.get("status") != "research_only":
            reasons.append(f"offline_snapshot.low[{index}].status:not_research_only")
        quality_gate = row.get("quality_gate")
        if not isinstance(quality_gate, str) or not quality_gate.startswith(
            "blocked_for_production"
        ):
            reasons.append(f"offline_snapshot.low[{index}].quality_gate:not_blocked")
        if not _list(coverage.get("missing")):
            reasons.append(f"offline_snapshot.low[{index}].coverage.missing:empty")
        if coverage.get("critical_conflict") is not False:
            reasons.append(f"offline_snapshot.low[{index}].coverage.critical_conflict:not_false")
        if row.get("fixture_id") in formal_fixture_ids:
            reasons.append(f"offline_snapshot.low[{index}].formal_prediction:mixed")
    return _detail(
        reasons,
        evidence=["offline_snapshot", "strict_report", "prospective_lock"],
    )


def _independent_scoring_detail(strict_report: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    leagues = strict_report.get("leagues")
    if not isinstance(leagues, Mapping) or not leagues:
        return _detail(["strict_report.leagues:missing"])
    for league, raw_value in leagues.items():
        league_value = _mapping(raw_value)
        if (
            league_value.get("status") == "unavailable"
            and league_value.get("reason")
            in {
                "no_admitted_openfootball_history_source_at_cutoff",
                "no_declared_verified_raw_history_source",
            }
        ):
            continue
        gates = _mapping(league_value.get("gates"))
        for target in REQUIRED_TARGET_GATES:
            gate = gates.get(target)
            if not isinstance(gate, Mapping):
                reasons.append(f"strict_report.{league}.{target}:missing")
                continue
            sample_n = gate.get("sample_n")
            if not isinstance(sample_n, int) or isinstance(sample_n, bool) or sample_n < 1:
                reasons.append(f"strict_report.{league}.{target}.sample_n:insufficient")
            status = gate.get("status")
            if status in {None, "blocked", "unavailable", "not_evaluated"}:
                reasons.append(f"strict_report.{league}.{target}.status:{status or 'missing'}")
            if _list(gate.get("failures")):
                reasons.append(f"strict_report.{league}.{target}.failures:nonempty")
    combined = _mapping(strict_report.get("combined"))
    scoreline = _mapping(combined.get("scoreline_gate"))
    scoreline_n = combined.get("scoreline_sample_n")
    if not isinstance(scoreline_n, int) or isinstance(scoreline_n, bool) or scoreline_n < 1:
        reasons.append("strict_report.combined.scoreline_sample_n:insufficient")
    if scoreline.get("status") in {None, "blocked", "unavailable", "not_evaluated"}:
        reasons.append(
            f"strict_report.combined.scoreline_gate.status:{scoreline.get('status', 'missing')}"
        )
    if _list(scoreline.get("failures")):
        reasons.append("strict_report.combined.scoreline_gate.failures:nonempty")
    return _detail(reasons, evidence=["strict_report"])


def _publication_detail(
    publication: Mapping[str, Any] | None,
    lock: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    missing: list[str] = []
    if publication is None:
        missing.append("publication_audit:missing")
    if snapshot is None:
        missing.append("offline_snapshot:missing")
    if missing:
        return _detail(missing, missing=True)
    reasons: list[str] = []
    assert publication is not None
    assert snapshot is not None
    status = publication.get("status")
    if status != "pass":
        reasons.append(f"publication_audit.status:{status or 'missing'}")
    if _list(publication.get("errors")):
        reasons.append("publication_audit.errors:nonempty")
    checks = _mapping(publication.get("checks"))
    for field in (
        "public_read_model_reachable",
        "model_lock_match",
        "snapshot_time_comparable",
        "snapshot_within_lag_budget",
        "public_contract_routes_ok",
    ):
        if checks.get(field) is not True:
            reasons.append(f"publication_audit.checks.{field}:not_true")
    model_sha = lock.get("model_version_sha256")
    local = _mapping(publication.get("local"))
    if local.get("model_version_sha256") != model_sha:
        reasons.append("publication_audit.local.model_version_sha256:mismatch")
    local_as_of = _utc(local.get("as_of"))
    snapshot_as_of = _utc(snapshot.get("as_of"))
    if local_as_of is None or snapshot_as_of is None or local_as_of != snapshot_as_of:
        reasons.append("publication_audit.local.as_of:offline_snapshot_mismatch")
    checked_at = _utc(publication.get("checked_at"))
    snapshot_generated_at = _utc(snapshot.get("generated_at"))
    if checked_at is None:
        reasons.append("publication_audit.checked_at:invalid")
    if snapshot_generated_at is None:
        reasons.append("offline_snapshot.generated_at:invalid")
    elif checked_at is not None and checked_at < snapshot_generated_at:
        reasons.append("publication_audit.checked_at:before_offline_snapshot")
    public = _mapping(publication.get("public"))
    if public.get("model_version_sha256") != model_sha:
        reasons.append("publication_audit.public.model_version_sha256:mismatch")
    public_as_of = _utc(public.get("as_of"))
    if public_as_of is None or snapshot_as_of is None or public_as_of != snapshot_as_of:
        reasons.append("publication_audit.public.as_of:offline_snapshot_mismatch")
    if public.get("mode") != "d1":
        reasons.append(f"publication_audit.public.mode:{public.get('mode', 'missing')}")
    return _detail(
        reasons,
        evidence=[
            "publication_audit",
            "prospective_lock",
            "offline_snapshot",
            "live_snapshot",
            "publication_diagnostic",
        ],
    )


def _code_manifest_failures(
    test_evidence: Mapping[str, Any],
    code_root: Path | None,
) -> list[str]:
    reasons: list[str] = []
    manifest = test_evidence.get("code_manifest")
    if not isinstance(manifest, Mapping):
        return ["test_evidence.code_manifest:missing"]
    if manifest.get("schema_version") != "matchline.code_manifest.v1":
        reasons.append("test_evidence.code_manifest.schema_version:invalid")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        return [*reasons, "test_evidence.code_manifest.files:invalid"]
    entries: dict[str, Mapping[str, Any]] = {}
    for raw_entry in raw_files:
        if not isinstance(raw_entry, Mapping):
            reasons.append("test_evidence.code_manifest.files:invalid")
            continue
        path = raw_entry.get("path")
        if not isinstance(path, str) or path in entries:
            reasons.append("test_evidence.code_manifest.files:invalid")
            continue
        entries[path] = raw_entry
    if set(entries) != set(REQUIRED_CODE_MANIFEST_PATHS):
        reasons.append("test_evidence.code_manifest.files:not_allowlisted")
    if code_root is None:
        reasons.append("test_evidence.code_manifest.root:missing")
    else:
        for relative in REQUIRED_CODE_MANIFEST_PATHS:
            entry = entries.get(relative)
            if entry is None:
                continue
            path, path_reasons = _confined_file(
                Path(relative),
                code_root,
                prefix=f"test_evidence.code_manifest.{relative}",
                require_relative=True,
            )
            reasons.extend(path_reasons)
            if path is None:
                continue
            declared = entry.get("sha256")
            if not isinstance(declared, str) or re.fullmatch(r"[0-9a-f]{64}", declared) is None:
                reasons.append(f"test_evidence.code_manifest.{relative}.sha256:invalid")
            elif declared != _raw_file_sha256(path):
                reasons.append(f"test_evidence.code_manifest.{relative}:file_mismatch")
    expected_revision = canonical_evidence_sha256(manifest)
    if test_evidence.get("code_revision") != expected_revision:
        reasons.append("test_evidence.code_revision:manifest_mismatch")
    return reasons


def _suite_log_failures(
    name: str,
    suite: Mapping[str, Any],
    test_evidence_root: Path | None,
) -> list[str]:
    reasons: list[str] = []
    command = suite.get("command")
    expected_command = list(TEST_SUITE_COMMANDS[name])
    if command != expected_command:
        reasons.append(f"test_evidence.suites.{name}.command:not_allowlisted")
    if suite.get("status") != "passed":
        reasons.append(f"test_evidence.suites.{name}.status:{suite.get('status', 'missing')}")
    raw_log_path = suite.get("log_path")
    if not isinstance(raw_log_path, str) or not raw_log_path.strip():
        return [*reasons, f"test_evidence.suites.{name}.log_path:missing"]
    declared_sha = suite.get("log_sha256")
    if not isinstance(declared_sha, str) or re.fullmatch(r"[0-9a-f]{64}", declared_sha) is None:
        return [*reasons, f"test_evidence.suites.{name}.log_sha256:invalid"]
    expected_relative = Path("logs") / f"{name}-{declared_sha}.json"
    if Path(raw_log_path) != expected_relative:
        reasons.append(f"test_evidence.suites.{name}.log_path:not_content_addressed")
    if test_evidence_root is None:
        return [*reasons, "test_evidence.root:missing"]
    log_path, path_reasons = _confined_file(
        Path(raw_log_path),
        test_evidence_root,
        prefix=f"test_evidence.suites.{name}.log_path",
        require_relative=True,
    )
    reasons.extend(path_reasons)
    if log_path is None:
        return reasons
    if _raw_file_sha256(log_path) != declared_sha:
        reasons.append(f"test_evidence.suites.{name}.log_sha256:file_mismatch")
        return reasons
    try:
        log = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [*reasons, f"test_evidence.suites.{name}.log:invalid_json"]
    if not isinstance(log, Mapping):
        return [*reasons, f"test_evidence.suites.{name}.log:invalid_shape"]
    expected_log: dict[str, Any] = {
        "schema_version": "matchline.test_suite_log.v1",
        "suite": name,
        "command": expected_command,
        "status": suite.get("status"),
        "exit_status": suite.get("exit_status"),
    }
    if name in {"python", "sites"}:
        expected_log["counts"] = {
            field: suite.get(field) for field in ("passed", "failed", "skipped", "total")
        }
    if dict(log) != expected_log:
        reasons.append(f"test_evidence.suites.{name}.log:receipt_mismatch")
    return reasons


def _test_identity_detail(
    test_evidence: Mapping[str, Any] | None,
    expected_identity: Mapping[str, str | None],
    snapshot: Mapping[str, Any] | None,
    build_evidence: Mapping[str, Any] | None,
    reference: datetime,
    code_root: Path | None,
) -> tuple[list[str], bool]:
    if test_evidence is None:
        return ["test_evidence:missing"], True
    reasons: list[str] = []
    if test_evidence.get("schema_version") != "matchline.platform_test_evidence.v1":
        reasons.append("test_evidence.schema_version:invalid")
    reasons.extend(_identity_failures(test_evidence, expected_identity, prefix="test_evidence"))
    producer = test_evidence.get("producer")
    if not isinstance(producer, Mapping):
        reasons.append("test_evidence.producer:missing")
    elif dict(producer) != TEST_EVIDENCE_PRODUCER:
        reasons.append("test_evidence.producer:not_allowlisted")
    reasons.extend(_code_manifest_failures(test_evidence, code_root))
    generated_at = _utc(test_evidence.get("generated_at"))
    if generated_at is None:
        reasons.append("test_evidence.generated_at:invalid")
    elif generated_at > reference:
        reasons.append("test_evidence.generated_at:after_checked_at")
    elif reference - generated_at > TEST_EVIDENCE_MAX_AGE:
        reasons.append("test_evidence.generated_at:expired")

    missing = False
    if snapshot is None:
        reasons.append("offline_snapshot:missing")
        missing = True
    if build_evidence is None:
        reasons.append("sites_build_evidence:missing")
        missing = True
    if missing:
        return reasons, True

    assert snapshot is not None
    assert build_evidence is not None
    artifacts = test_evidence.get("artifact_identity")
    if not isinstance(artifacts, Mapping):
        reasons.append("test_evidence.artifact_identity:missing")
    else:
        expected_artifacts = {
            "offline_snapshot_sha256": canonical_evidence_sha256(snapshot),
            "sites_build_evidence_sha256": canonical_evidence_sha256(build_evidence),
        }
        for field, expected in expected_artifacts.items():
            actual = artifacts.get(field)
            if not isinstance(actual, str) or not actual:
                reasons.append(f"test_evidence.artifact_identity.{field}:missing")
            elif actual != expected:
                reasons.append(f"test_evidence.artifact_identity.{field}:mismatch")

    build_finished_at = _utc(build_evidence.get("finished_at"))
    if build_finished_at is None:
        reasons.append("sites_build_evidence.finished_at:invalid")
    elif generated_at is not None and generated_at < build_finished_at:
        reasons.append("test_evidence.generated_at:before_sites_build")
    return reasons, False


def _test_suite_detail(
    test_evidence: Mapping[str, Any] | None,
    expected_identity: Mapping[str, str | None],
    snapshot: Mapping[str, Any] | None,
    build_evidence: Mapping[str, Any] | None,
    reference: datetime,
    test_evidence_root: Path | None,
    code_root: Path | None,
) -> dict[str, Any]:
    reasons, missing = _test_identity_detail(
        test_evidence,
        expected_identity,
        snapshot,
        build_evidence,
        reference,
        code_root,
    )
    if missing:
        return _detail(reasons, missing=True)
    assert test_evidence is not None
    suites = _mapping(test_evidence.get("suites"))
    for name in ("python", "sites", "typecheck", "lint"):
        suite = suites.get(name)
        if not isinstance(suite, Mapping):
            reasons.append(f"test_evidence.suites.{name}:missing")
            continue
        exit_status = suite.get("exit_status")
        if not isinstance(exit_status, int) or isinstance(exit_status, bool) or exit_status != 0:
            reasons.append(
                f"test_evidence.suites.{name}.exit_status:"
                f"{exit_status if exit_status is not None else 'missing'}"
            )
        if name in {"python", "sites"}:
            counts: dict[str, int] = {}
            for field in ("passed", "failed", "skipped", "total"):
                value = suite.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    reasons.append(f"test_evidence.suites.{name}.{field}:invalid")
                else:
                    counts[field] = value
            if len(counts) == 4:
                if counts["total"] != (counts["passed"] + counts["failed"] + counts["skipped"]):
                    reasons.append(f"test_evidence.suites.{name}.total:mismatch")
                if counts["total"] < 2:
                    reasons.append(f"test_evidence.suites.{name}.total:too_small")
                if counts["passed"] < 2:
                    reasons.append(f"test_evidence.suites.{name}.passed:too_small")
            if counts.get("failed") != 0:
                reasons.append(
                    f"test_evidence.suites.{name}.failed:{counts.get('failed', 'missing')}"
                )
        reasons.extend(_suite_log_failures(name, suite, test_evidence_root))
    return _detail(
        reasons,
        evidence=[
            "test_evidence",
            "sites_build_evidence",
            "offline_snapshot",
            "strict_report",
            "prospective_lock",
        ],
    )


def _build_failures(
    build: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    if build.get("schema_version") != "matchline.sites_build_resource.v1":
        reasons.append("sites_build_evidence.schema_version:invalid")
    if build.get("exit_status") != 0:
        reasons.append(f"sites_build_evidence.exit_status:{build.get('exit_status', 'missing')}")
    started = _utc(build.get("started_at"))
    finished = _utc(build.get("finished_at"))
    generated = _utc(_mapping(snapshot).get("generated_at"))
    if started is None:
        reasons.append("sites_build_evidence.started_at:invalid")
    if finished is None:
        reasons.append("sites_build_evidence.finished_at:invalid")
    if started is not None and finished is not None and finished < started:
        reasons.append("sites_build_evidence.finished_at:before_started_at")
    if generated is None:
        reasons.append("offline_snapshot.generated_at:invalid")
    elif started is not None and started < generated:
        reasons.append("sites_build_evidence.started_at:before_offline_snapshot")
    return reasons


def _sites_api_mobile_detail(
    test_evidence: Mapping[str, Any] | None,
    build_evidence: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    expected_identity: Mapping[str, str | None],
    reference: datetime,
    code_root: Path | None,
) -> dict[str, Any]:
    missing_reasons: list[str] = []
    if test_evidence is None:
        missing_reasons.append("test_evidence:missing")
    if build_evidence is None:
        missing_reasons.append("sites_build_evidence:missing")
    if snapshot is None:
        missing_reasons.append("offline_snapshot:missing")
    if missing_reasons:
        return _detail(missing_reasons, missing=True)

    assert test_evidence is not None
    assert build_evidence is not None
    assert snapshot is not None
    reasons, _ = _test_identity_detail(
        test_evidence,
        expected_identity,
        snapshot,
        build_evidence,
        reference,
        code_root,
    )
    reasons.extend(_build_failures(build_evidence, snapshot))
    api = _mapping(test_evidence.get("api"))
    routes = _list(api.get("routes"))
    observed: dict[str, object] = {}
    for raw_route in routes:
        route = _mapping(raw_route)
        path = route.get("path")
        if isinstance(path, str):
            observed[path.split("?", 1)[0]] = route.get("http_status")
    for required in REQUIRED_API_ROUTES:
        if observed.get(required) != 200:
            reasons.append(f"test_evidence.api.routes.{required}:not_200")
    mobile = _mapping(_mapping(test_evidence.get("browser")).get("mobile"))
    if mobile.get("http_status") != 200:
        reasons.append("test_evidence.browser.mobile.http_status:not_200")
    width = mobile.get("viewport_width")
    height = mobile.get("viewport_height")
    if not isinstance(width, int) or isinstance(width, bool) or not 320 <= width <= 480:
        reasons.append("test_evidence.browser.mobile.viewport_width:invalid")
    if not isinstance(height, int) or isinstance(height, bool) or height < 568:
        reasons.append("test_evidence.browser.mobile.viewport_height:invalid")
    if mobile.get("console_errors") != 0:
        reasons.append("test_evidence.browser.mobile.console_errors:nonzero")
    if mobile.get("console_warnings") != 0:
        reasons.append("test_evidence.browser.mobile.console_warnings:nonzero")
    return _detail(
        reasons,
        evidence=[
            "test_evidence",
            "sites_build_evidence",
            "offline_snapshot",
            "strict_report",
            "prospective_lock",
        ],
    )


def _input_summary(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {"present": False, "schema_version": None, "canonical_sha256": None}
    return {
        "present": True,
        "schema_version": value.get("schema_version"),
        "canonical_sha256": canonical_evidence_sha256(value),
    }


def build_platform_verification(
    *,
    strict_report: Mapping[str, Any],
    prospective_lock: Mapping[str, Any],
    cycle: Mapping[str, Any] | None = None,
    evaluation: Mapping[str, Any] | None = None,
    offline_snapshot: Mapping[str, Any] | None = None,
    sites_build_evidence: Mapping[str, Any] | None = None,
    test_evidence: Mapping[str, Any] | None = None,
    publication_audit: Mapping[str, Any] | None = None,
    live_snapshot_path: Path | None = None,
    publication_diagnostic_path: Path | None = None,
    runtime_root: Path | None = None,
    test_evidence_root: Path | None = None,
    code_root: Path | None = None,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Derive all platform gates from current, separately supplied artifacts."""

    reference = checked_at or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    identity = _identity(strict_report, prospective_lock)
    strict_contract_failures = strict_report_v260_failures(strict_report)
    identity_failures = [
        *strict_contract_failures,
        *_current_identity_failures(strict_report, prospective_lock),
    ]
    (
        runtime_artifact_failures,
        live_snapshot_summary,
        prediction_archive_summary,
        publication_diagnostic_summary,
    ) = _runtime_artifact_failures(
        lock=prospective_lock,
        cycle=cycle,
        evaluation=evaluation,
        offline_snapshot=offline_snapshot,
        live_snapshot_path=live_snapshot_path,
        publication_diagnostic_path=publication_diagnostic_path,
        runtime_root=runtime_root,
    )
    details = {
        "prediction_traceability_gate": _traceability_detail(
            strict_report,
            prospective_lock,
            cycle,
            evaluation,
            offline_snapshot,
            reference,
        ),
        "append_only_source_isolation_gate": _source_isolation_detail(
            cycle,
            evaluation,
            prospective_lock,
            reference,
        ),
        "calibration_gate": _calibration_detail(strict_report),
        "low_quality_degradation_gate": _low_quality_detail(
            strict_report,
            prospective_lock,
            offline_snapshot,
        ),
        "independent_target_scoring_gate": _independent_scoring_detail(strict_report),
        "sites_d1_consistency_gate": _publication_detail(
            publication_audit,
            prospective_lock,
            offline_snapshot,
        ),
        "sites_api_mobile_gate": _sites_api_mobile_detail(
            test_evidence,
            sites_build_evidence,
            offline_snapshot,
            identity,
            reference,
            code_root,
        ),
        "test_suite_gate": _test_suite_detail(
            test_evidence,
            identity,
            offline_snapshot,
            sites_build_evidence,
            reference,
            test_evidence_root,
            code_root,
        ),
    }
    if runtime_artifact_failures:
        for gate in (
            "prediction_traceability_gate",
            "append_only_source_isolation_gate",
            "sites_d1_consistency_gate",
        ):
            details[gate] = _force_blocked(details[gate], runtime_artifact_failures)
    if identity_failures:
        details["prediction_traceability_gate"] = _force_blocked(
            details["prediction_traceability_gate"],
            identity_failures,
        )
        details["test_suite_gate"] = _force_blocked(
            details["test_suite_gate"],
            identity_failures,
        )
    if strict_contract_failures:
        for gate in (
            "calibration_gate",
            "low_quality_degradation_gate",
            "independent_target_scoring_gate",
            "sites_api_mobile_gate",
        ):
            details[gate] = _force_blocked(details[gate], strict_contract_failures)
    maturity = {gate: details[gate]["status"] for gate in PLATFORM_GATES}
    passed = all(status == "pass" for status in maturity.values())
    strict_input = _input_summary(strict_report)
    strict_admission = _mapping(strict_report.get("training_admission"))
    strict_contract = _mapping(strict_report.get("training_source_contract"))
    strict_input.update(
        {
            "report_lane": strict_report.get("report_lane"),
            "policy_version": strict_contract.get("policy_version"),
            "training_admission_sha256": strict_admission.get("admission_sha256"),
            "observed_before": strict_admission.get("observed_before"),
            "source_ids": strict_admission.get("source_ids"),
        }
    )
    inputs = {
        "strict_report": strict_input,
        "prospective_lock": _input_summary(prospective_lock),
        "cycle": _input_summary(cycle),
        "evaluation": _input_summary(evaluation),
        "offline_snapshot": _input_summary(offline_snapshot),
        "sites_build_evidence": _input_summary(sites_build_evidence),
        "test_evidence": _input_summary(test_evidence),
        "publication_audit": _input_summary(publication_audit),
        "live_snapshot": live_snapshot_summary,
        "prediction_archive": prediction_archive_summary,
        "publication_diagnostic": publication_diagnostic_summary,
    }
    return {
        "schema_version": "matchline.platform_verification.v2",
        "checked_at": reference.astimezone(timezone.utc).isoformat(),
        "status": "pass" if passed else "blocked",
        "passed": passed,
        "evidence_identity": identity,
        "identity_validation": {
            "status": "blocked" if identity_failures else "pass",
            "reasons": identity_failures,
        },
        "strict_report_validation": {
            "status": "blocked" if strict_contract_failures else "pass",
            "reasons": strict_contract_failures,
        },
        "maturity": maturity,
        "gate_evidence": details,
        "inputs": inputs,
        "decision": (
            "All platform evidence gates are backed by current artifacts."
            if passed
            else "One or more platform gates are blocked or not verified; do not promote release."
        ),
    }


def _load_required(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} root must be an object")
    return value


def _load_optional(path: Path | None) -> Mapping[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return _load_required(path)


def _write_atomic(path: Path, value: Mapping[str, Any]) -> bytes:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.platform.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return payload


def _archive_content_addressed(
    archive_dir: Path, value: Mapping[str, Any], payload: bytes
) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    digest = canonical_evidence_sha256(value)
    archive = archive_dir / f"platform-verification-{digest}.json"
    if archive.exists():
        existing = _load_required(archive)
        if canonical_evidence_sha256(existing) != digest:
            raise ValueError(f"content-addressed platform evidence collision: {archive}")
        return archive
    temporary = archive.with_name(f".{archive.name}.platform.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(archive)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-report", type=Path, required=True)
    parser.add_argument("--prospective-lock", type=Path, required=True)
    parser.add_argument("--cycle", type=Path)
    parser.add_argument("--evaluation", type=Path)
    parser.add_argument("--offline-snapshot", type=Path)
    parser.add_argument("--sites-build-evidence", type=Path)
    parser.add_argument("--test-evidence", type=Path)
    parser.add_argument("--publication-audit", type=Path)
    parser.add_argument("--live-snapshot", type=Path)
    parser.add_argument("--publication-diagnostic", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--test-evidence-root", type=Path)
    parser.add_argument("--code-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive-dir", type=Path)
    args = parser.parse_args()

    paths = {
        "cycle": args.cycle,
        "evaluation": args.evaluation,
        "offline_snapshot": args.offline_snapshot,
        "sites_build_evidence": args.sites_build_evidence,
        "test_evidence": args.test_evidence,
        "publication_audit": args.publication_audit,
    }
    result = build_platform_verification(
        strict_report=_load_required(args.strict_report),
        prospective_lock=_load_required(args.prospective_lock),
        cycle=_load_optional(args.cycle),
        evaluation=_load_optional(args.evaluation),
        offline_snapshot=_load_optional(args.offline_snapshot),
        sites_build_evidence=_load_optional(args.sites_build_evidence),
        test_evidence=_load_optional(args.test_evidence),
        publication_audit=_load_optional(args.publication_audit),
        live_snapshot_path=args.live_snapshot,
        publication_diagnostic_path=args.publication_diagnostic,
        runtime_root=args.runtime_root,
        test_evidence_root=args.test_evidence_root,
        code_root=args.code_root,
    )
    result["inputs"]["strict_report"]["path"] = str(args.strict_report)
    result["inputs"]["prospective_lock"]["path"] = str(args.prospective_lock)
    for name, path in paths.items():
        result["inputs"][name]["path"] = str(path) if path is not None else None
    payload = _write_atomic(args.output, result)
    archive = (
        _archive_content_addressed(args.archive_dir, result, payload)
        if args.archive_dir is not None
        else None
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "passed": result["passed"],
                "maturity": result["maturity"],
                "output": str(args.output),
                "archive": str(archive) if archive else None,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()


__all__ = [
    "build_platform_verification",
    "canonical_evidence_sha256",
    "canonical_model_version_sha256",
    "strict_report_v260_failures",
]
