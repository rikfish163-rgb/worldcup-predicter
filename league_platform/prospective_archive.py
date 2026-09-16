"""Append-only archive contract for causally frozen future predictions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_OUTPUT = Path("data/live/prospective_predictions.jsonl")
_HEX = frozenset("0123456789abcdefABCDEF")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    return parsed.astimezone(timezone.utc)


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX for char in value)


def validate_lock_integrity(lock: Mapping[str, Any]) -> None:
    """Fail closed if the precommitted model-file inventory changed."""

    if lock.get("status") not in {"pending_prospective_window", "passed"}:
        raise ValueError("prospective lock status is not scoreable")
    if not _valid_digest(lock.get("model_version_sha256")):
        raise ValueError("prospective lock model_version_sha256 is invalid")
    files = lock.get("model_files")
    if not isinstance(files, list) or not files:
        raise ValueError("prospective lock model_files is missing")
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping):
            raise ValueError(f"prospective lock model file entry {index} is invalid")
        path_value = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(path_value, str) or not path_value.strip() or not _valid_digest(expected):
            raise ValueError(f"prospective lock model file entry {index} is invalid")
        path = Path(path_value)
        if not path.is_file():
            raise ValueError(f"prospective lock model file is missing: {path_value}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"prospective lock model file hash mismatch: {path_value}")


def validate_prediction(prediction: Mapping[str, Any], *, lock: Mapping[str, Any]) -> None:
    """Validate one prediction before it can enter the immutable archive."""

    validate_lock_integrity(lock)
    required = ("fixture_id", "competition_id", "kickoff_at", "freeze_stage", "freeze_cutoff_at", "model_version")
    missing = [field for field in required if not prediction.get(field)]
    if missing:
        raise ValueError(f"prediction is missing required fields: {', '.join(missing)}")
    kickoff = _timestamp(prediction["kickoff_at"], field="kickoff_at")
    window_start_value = lock.get("evaluation_window_started_at")
    window_start: datetime | None = None
    if window_start_value is not None:
        window_start = _timestamp(
            window_start_value,
            field="evaluation_window_started_at",
        )
        if kickoff < window_start:
            raise ValueError("kickoff_at predates the locked prospective evaluation window")
    cutoff = _timestamp(prediction["freeze_cutoff_at"], field="freeze_cutoff_at")
    if window_start is not None and cutoff < window_start:
        raise ValueError("freeze_cutoff_at predates the locked prospective evaluation window")
    observed = _timestamp(
        prediction.get("prediction_observed_at") or prediction.get("as_of"),
        field="prediction_observed_at",
    )
    if not cutoff < kickoff:
        raise ValueError("freeze_cutoff_at must be before kickoff_at")
    if observed < cutoff or observed >= kickoff:
        raise ValueError("prediction_observed_at must be within the freeze window")
    stage = prediction["freeze_stage"]
    if stage not in {"t_minus_24h", "t_minus_6h", "t_minus_90m", "lineup_confirmation"}:
        raise ValueError("unsupported freeze_stage")
    if stage == "lineup_confirmation":
        lineup_observed = prediction.get("lineup_observed_at")
        if lineup_observed is None:
            raise ValueError("lineup_confirmation requires lineup_observed_at")
        lineup_time = _timestamp(lineup_observed, field="lineup_observed_at")
        if lineup_time != cutoff:
            raise ValueError("lineup_observed_at must equal freeze_cutoff_at")
        if not _valid_digest(prediction.get("lineup_confirmation_fingerprint")):
            raise ValueError(
                "lineup_confirmation requires a complete XI fingerprint"
            )
    for value in prediction.get("feature_times", []):
        feature_time = _timestamp(value, field="feature_time")
        if feature_time > cutoff:
            raise ValueError("feature timestamp is after the freeze cutoff")
    fixture_observed = prediction.get("fixture_freeze_observed_at")
    if fixture_observed is not None:
        fixture_observed_at = _timestamp(
            fixture_observed,
            field="fixture_freeze_observed_at",
        )
        if fixture_observed_at > cutoff:
            raise ValueError("fixture observation is after the freeze cutoff")
        if prediction.get("fixture_observation_basis") not in {
            "current_fixture_source",
            "archived_fixture_snapshot",
        }:
            raise ValueError("fixture observation basis is invalid")
    if any(str(key).startswith("actual_") or key == "outcome_1x2" for key in prediction):
        raise ValueError("prediction archive cannot contain result fields")
    model_name = lock.get("freeze_model_name") or lock.get("model_name")
    if model_name and prediction["model_version"] != model_name:
        raise ValueError("prediction model_version does not match the prospective lock")


def _freeze_key(prediction: Mapping[str, Any]) -> str:
    # ``lineup_confirmation`` is a semantic stage, not a clock tick.  The
    # first confirmed XI establishes that stage; polling the same XI again
    # must not create a new logical freeze merely because the provider
    # returned a newer observation timestamp.  A changed payload still maps
    # to the same key and is therefore preserved as a hard conflict.
    freeze_cutoff = (
        None
        if prediction.get("freeze_stage") == "lineup_confirmation"
        else prediction["freeze_cutoff_at"]
    )
    return _sha256(
        {
            "fixture_id": prediction["fixture_id"],
            "freeze_stage": prediction["freeze_stage"],
            "freeze_cutoff_at": freeze_cutoff,
            "model_version": prediction["model_version"],
        }
    )


def logical_freeze_key(prediction: Mapping[str, Any]) -> str:
    """Return the stable logical identity for one fixture/stage/model."""

    return _freeze_key(prediction)


def _stable_prediction_content(prediction: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude observation bookkeeping from the immutable prediction body.

    Polling the same freeze window later can legitimately change observation
    timestamps (``prediction_observed_at``, ``as_of`` and the observed kickoff
    timestamp) while the frozen probability payload remains identical. Those
    timestamps stay in the archived payload, but do not manufacture a false
    model conflict.
    """

    # These fields are retained in every archived row for traceability, but
    # they are not frozen model content.  Providers can return a new response
    # hash or observation timestamp on the next poll while the cutoff-bound
    # probabilities remain identical.  Treating those bookkeeping changes as
    # a model conflict would poison an otherwise valid prospective window.
    audit_only = {
        "as_of",
        "prediction_observed_at",
        "kickoff_time_observed_at",
        "fixture_freeze_observed_at",
        "fixture_observation_basis",
        "feature_times",
        "live_feature_sources",
        "market_retrieved_at",
        "market_time_bound_at",
        "source_file",
        "source_name",
        "source_license_status",
        "source_sha256",
        "provider_fixture_id",
    }
    if prediction.get("freeze_stage") == "lineup_confirmation":
        # The lineup stage is frozen when the XI is observed.  These fields
        # describe that observation boundary, not model content; retaining
        # them in the row keeps auditability while excluding them from the
        # idempotency digest.
        audit_only.update({"cutoff_at", "freeze_cutoff_at", "lineup_observed_at"})
    return {key: value for key, value in prediction.items() if key not in audit_only}


def stable_prediction_digest(prediction: Mapping[str, Any]) -> str:
    """Return the immutable-content digest used for idempotent freeze polling."""

    return _sha256(_stable_prediction_content(prediction))


def _lineup_confirmation_fingerprint(prediction: Mapping[str, Any]) -> str | None:
    value = prediction.get("lineup_confirmation_fingerprint")
    return value if _valid_digest(value) else None


def _belongs_to_current_lock_identity(
    row: Mapping[str, Any],
    *,
    model_sha256: str,
    window_started_at: datetime | None,
) -> bool:
    """Exclude same-model rows created under an older prospective window."""

    if str(row.get("model_version_sha256") or "") != model_sha256:
        return True
    if window_started_at is None:
        return True
    prediction = row.get("prediction")
    if not isinstance(prediction, Mapping):
        return False
    try:
        cutoff = _timestamp(prediction.get("freeze_cutoff_at"), field="freeze_cutoff_at")
        observed = _timestamp(
            prediction.get("prediction_observed_at") or prediction.get("as_of"),
            field="prediction_observed_at",
        )
    except ValueError:
        return False
    return cutoff >= window_started_at and observed >= window_started_at


def append_predictions(
    output: Path,
    predictions: list[Mapping[str, Any]],
    *,
    lock: Mapping[str, Any],
    captured_at: datetime | None = None,
) -> dict[str, int]:
    """Append validated predictions idempotently and preserve conflicts."""

    validate_lock_integrity(lock)
    capture_time = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if capture_time.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    output.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if output.exists():
        for line_number, line in enumerate(output.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid prospective archive JSON at line {line_number}") from exc
            if not isinstance(row, dict) or not row.get("record_key") or not row.get("freeze_key"):
                raise ValueError(f"invalid prospective archive record at line {line_number}")
            existing.append(row)
    lock_model_sha = str(lock["model_version_sha256"])
    lock_window_value = lock.get("evaluation_window_started_at")
    lock_window_started_at = (
        _timestamp(lock_window_value, field="evaluation_window_started_at")
        if lock_window_value is not None
        else None
    )
    current_identity_rows = [
        row
        for row in existing
        if _belongs_to_current_lock_identity(
            row,
            model_sha256=lock_model_sha,
            window_started_at=lock_window_started_at,
        )
    ]
    # The same immutable prediction may be re-emitted after a lock-manifest
    # migration.  Deduplicate within a model-lock identity, while preserving
    # the old append-only row for audit and replay.
    existing_content = {
        (
            stable_prediction_digest(row["prediction"])
            if isinstance(row.get("prediction"), Mapping)
            else row.get("content_sha256"),
            str(row.get("model_version_sha256") or ""),
        )
        for row in current_identity_rows
        # A prior process may have marked a row as a conflict solely because
        # the old digest included audit-only fields.  Keep that row for audit,
        # but allow a clean canonical row to repair the current lock view.
        if row.get("conflict") is not True
    }
    # A logical freeze can be re-emitted under a new pre-result model lock.
    # Keep every prior digest for audit, but only compare payloads within the
    # current lock identity; a changed payload across a validated migration is
    # a new model record, not an unresolved same-model conflict.
    freeze_records: dict[str, list[tuple[str, str]]] = {}
    lineup_fingerprints: dict[tuple[str, str], set[str]] = {}
    for row in current_identity_rows:
        prediction = row.get("prediction")
        try:
            freeze_key = _freeze_key(prediction) if isinstance(prediction, Mapping) else str(row["freeze_key"])
        except (KeyError, TypeError):
            freeze_key = str(row["freeze_key"])
        freeze_records.setdefault(freeze_key, []).append(
            (
                stable_prediction_digest(row["prediction"])
                if isinstance(row.get("prediction"), Mapping)
                else str(row.get("content_sha256")),
                str(row.get("model_version_sha256") or ""),
            )
        )
        if (
            row.get("conflict") is not True
            and isinstance(prediction, Mapping)
            and prediction.get("freeze_stage") == "lineup_confirmation"
        ):
            fingerprint = _lineup_confirmation_fingerprint(prediction)
            if fingerprint is not None:
                lineup_fingerprints.setdefault(
                    (freeze_key, str(row.get("model_version_sha256") or "")),
                    set(),
                ).add(fingerprint)

    fresh = 0
    skipped_duplicate = 0
    conflicts = 0
    pending: list[dict[str, Any]] = []
    for raw_prediction in predictions:
        prediction = dict(raw_prediction)
        validate_prediction(prediction, lock=lock)
        freeze_key = _freeze_key(prediction)
        lineup_fingerprint = _lineup_confirmation_fingerprint(prediction)
        if (
            prediction.get("freeze_stage") == "lineup_confirmation"
            and lineup_fingerprint is not None
            and lineup_fingerprint
            in lineup_fingerprints.get((freeze_key, lock_model_sha), set())
        ):
            skipped_duplicate += 1
            continue
        content_hash = stable_prediction_digest(prediction)
        content_key = (content_hash, lock_model_sha)
        if content_key in existing_content:
            skipped_duplicate += 1
            continue
        conflict_with = [
            value
            for value, row_lock_sha in freeze_records.get(freeze_key, [])
            if row_lock_sha == lock_model_sha and value != content_hash
        ]
        record = {
            "schema_version": "1.0.0",
            "record_key": _sha256(
                {
                    "freeze_key": freeze_key,
                    "content_sha256": content_hash,
                    "model_version_sha256": lock_model_sha,
                    "model_lock_window_started_at": lock_window_value,
                }
            ),
            "freeze_key": freeze_key,
            "content_sha256": content_hash,
            "captured_at": capture_time.isoformat(),
            "model_version": prediction["model_version"],
            "model_version_sha256": lock["model_version_sha256"],
            "model_lock_window_started_at": lock_window_value,
            "conflict": bool(conflict_with),
            "conflict_with": conflict_with,
            "prediction": prediction,
        }
        pending.append(record)
        existing_content.add(content_key)
        freeze_records.setdefault(freeze_key, []).append((content_hash, lock_model_sha))
        if lineup_fingerprint is not None:
            lineup_fingerprints.setdefault((freeze_key, lock_model_sha), set()).add(
                lineup_fingerprint
            )
        fresh += 1
        conflicts += int(bool(conflict_with))

    if pending:
        with output.open("a", encoding="utf-8") as stream:
            for record in pending:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"appended": fresh, "skipped_duplicate": skipped_duplicate, "conflicts": conflicts}


__all__ = [
    "DEFAULT_OUTPUT",
    "append_predictions",
    "logical_freeze_key",
    "stable_prediction_digest",
    "validate_lock_integrity",
    "validate_prediction",
]
