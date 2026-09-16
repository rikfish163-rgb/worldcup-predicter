"""Audit repeated holdout inspection and require a prospective final test.

Walk-forward ordering protects rows from future feature leakage, but it does
not make a test set permanently untouched.  Once candidate decisions have
been informed by its results, that period becomes development evidence.  This
module records that model-selection debt and only clears it with a model lock
created before a genuinely prospective evaluation window.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


REUSED_HOLDOUT_PATTERNS = (
    re.compile(r"(?<!\d)2122(?!\d)", re.IGNORECASE),
    re.compile(r"2021\s*[/_-]\s*22", re.IGNORECASE),
    re.compile(r"2021\s*[/_-]\s*2022", re.IGNORECASE),
)
REQUIRED_PROSPECTIVE_FLAGS = (
    "results_not_used_for_selection",
    "sample_requirements_met",
    "all_required_targets_scored",
    "prediction_freezes_verified",
)


def _utc_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    return parsed.astimezone(timezone.utc)


def _valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _model_file_failures(prospective_lock: Mapping[str, Any]) -> list[str]:
    """Validate the optional source-file inventory carried by a model lock.

    Older synthetic locks used by callers do not include ``model_files`` and
    remain valid under the legacy contract.  Once an inventory is present,
    however, every entry must be a real file whose bytes match the committed
    SHA-256.  This prevents a lock from silently describing a different
    implementation than the one used for prospective scoring.
    """

    inventory = prospective_lock.get("model_files")
    if inventory is None:
        return []
    failures: list[str] = []
    if not isinstance(inventory, list) or not inventory:
        return ["model_files_invalid"]
    for index, entry in enumerate(inventory):
        if not isinstance(entry, Mapping):
            failures.append(f"model_file_entry_invalid:{index}")
            continue
        path_value = entry.get("path")
        digest = entry.get("sha256")
        label = str(path_value) if isinstance(path_value, str) and path_value else str(index)
        if not isinstance(path_value, str) or not path_value.strip():
            failures.append(f"model_file_path_invalid:{label}")
            continue
        if not _valid_sha256(digest):
            failures.append(f"model_file_hash_invalid:{label}")
            continue
        path = Path(path_value)
        if not path.exists() or not path.is_file():
            failures.append(f"model_file_missing:{label}")
            continue
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            failures.append(f"model_file_unreadable:{label}")
            continue
        if actual != digest:
            failures.append(f"model_file_hash_mismatch:{label}")
    return failures


def _mentions_reused_holdout(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_mentions_reused_holdout(item) for item in value.values())
    if isinstance(value, list):
        return any(_mentions_reused_holdout(item) for item in value)
    text = str(value)
    return any(pattern.search(text) for pattern in REUSED_HOLDOUT_PATTERNS)


def _prospective_failures(prospective_lock: Mapping[str, Any] | None) -> list[str]:
    if prospective_lock is None:
        return ["prospective_lock_missing"]
    failures: list[str] = []
    if prospective_lock.get("status") != "passed":
        failures.append("prospective_status_not_passed")
    if not _valid_sha256(prospective_lock.get("model_version_sha256")):
        failures.append("model_version_sha256_invalid")
    failures.extend(_model_file_failures(prospective_lock))
    for flag in REQUIRED_PROSPECTIVE_FLAGS:
        if prospective_lock.get(flag) is not True:
            failures.append(f"{flag}_not_true")
    try:
        locked_at = _utc_timestamp(prospective_lock.get("locked_at"), field="locked_at")
    except ValueError:
        locked_at = None
        failures.append("locked_at_invalid")
    try:
        window_start = _utc_timestamp(
            prospective_lock.get("evaluation_window_started_at"),
            field="evaluation_window_started_at",
        )
    except ValueError:
        window_start = None
        failures.append("evaluation_window_started_at_invalid")
    if locked_at is not None and window_start is not None and window_start <= locked_at:
        failures.append("evaluation_window_not_after_model_lock")
    return failures


def build_model_selection_audit(
    evidence_dir: Path,
    *,
    prospective_lock: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return an evidence inventory and a conservative independence gate."""

    reference = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reports: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    for path in sorted(evidence_dir.glob("model-candidate-*.json")):
        try:
            raw = path.read_bytes()
            decoded = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            parse_errors.append({"path": str(path), "error": str(exc)})
            continue
        reports.append(
            {
                "path": str(path),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "references_reused_2021_22_onward_period": _mentions_reused_holdout(decoded),
            }
        )
    reused = [row for row in reports if row["references_reused_2021_22_onward_period"]]
    failures = _prospective_failures(prospective_lock)
    passes = not failures
    return {
        "schema_version": "1.0.0",
        "generated_at": reference.isoformat(),
        "candidate_evidence_dir": str(evidence_dir),
        "candidate_report_count": len(reports),
        "candidate_parse_errors": parse_errors,
        "reports_referencing_reused_holdout_count": len(reused),
        "reports_referencing_reused_holdout": reused,
        "reused_period": "2021/22 through 2025/26",
        "current_evaluation_role": "development_backtest_after_repeated_inspection",
        "prospective_lock": dict(prospective_lock) if prospective_lock is not None else None,
        "prospective_failures": failures,
        "gate_status": (
            "pass_prospective_independent_test" if passes else "blocked_selection_debt"
        ),
        "production_eligible": passes,
        "policy": (
            "Chronological walk-forward evidence remains useful for development and leakage auditing, "
            "but a repeatedly inspected period is not an untouched final test. Production promotion "
            "requires a precommitted model hash and a later prospective window whose results were not "
            "used for model selection."
        ),
    }


def _load_optional_lock(path: Path | None) -> Mapping[str, Any] | None:
    if path is None or not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("prospective lock root must be an object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=Path("docs/evidence"))
    parser.add_argument("--prospective-lock", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit = build_model_selection_audit(
        args.evidence_dir,
        prospective_lock=_load_optional_lock(args.prospective_lock),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: audit[key] for key in ("gate_status", "candidate_report_count", "reports_referencing_reused_holdout_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = ["build_model_selection_audit"]
