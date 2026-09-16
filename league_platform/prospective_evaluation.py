"""Score append-only prospective freezes against the first observed results."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from league_platform.catalog import LEAGUES
from league_platform.metrics import (
    assert_no_future_leakage,
    bootstrap_mean_ci,
    evaluate_three_way,
    scoreline_target_consistency_error,
)
from league_platform.openfootball_raw_archive import (
    MANIFEST_NAME,
    OpenFootballRawArchiveError,
    load_verified_openfootball_archive,
)
from league_platform.prospective_archive import logical_freeze_key, stable_prediction_digest
from league_platform.sources.live_results import load_verified_result_rows


REQUIRED_FREEZE_STAGES = frozenset(
    {"t_minus_24h", "t_minus_6h", "t_minus_90m", "lineup_confirmation"}
)
TARGETS = ("three_way", "total_goals", "scoreline", "half_full")
# The minimums are part of the evidence contract, not tuning knobs.  Keep the
# values explicit in the output so a report can be reproduced without relying
# on an external gate implementation.
PROSPECTIVE_SAMPLE_THRESHOLDS = {
    "three_way": 1000,
    "total_goals": 1000,
    "half_full": 1000,
    "scoreline": 5000,
}
REQUIRED_LEAGUES = frozenset(league.id for league in LEAGUES)
OPENFOOTBALL_RAW_ARCHIVE_ENV = "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"
DEFAULT_OPENFOOTBALL_RAW_ARCHIVE_DIR = Path("data/live/openfootball-raw")
_MAX_RAW_ARCHIVE_MANIFEST_BYTES = 16 * 1024 * 1024


def resolve_openfootball_raw_archive_dir(
    configured: Path | str | None,
    *,
    runtime_default: Path,
) -> Path:
    """Resolve only operator/runtime configuration, never snapshot payload data."""

    if configured is not None:
        return Path(configured)
    environment_value = os.environ.get(OPENFOOTBALL_RAW_ARCHIVE_ENV, "").strip()
    return Path(environment_value) if environment_value else Path(runtime_default)


def _manifest_source_ids(manifest_path: Path) -> list[str]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(manifest_path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("OpenFootball raw archive manifest must be a regular file")
        if before.st_size <= 0 or before.st_size > _MAX_RAW_ARCHIVE_MANIFEST_BYTES:
            raise ValueError("OpenFootball raw archive manifest size is invalid")
        payload = bytearray()
        while len(payload) <= _MAX_RAW_ARCHIVE_MANIFEST_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_RAW_ARCHIVE_MANIFEST_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if len(payload) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("OpenFootball raw archive manifest changed while being read")
    finally:
        os.close(descriptor)
    source_ids: set[str] = set()
    for line_number, line in enumerate(bytes(payload).decode("utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(
                f"OpenFootball raw archive manifest has a blank line at {line_number}"
            )
        record = json.loads(line)
        source_id = record.get("source_id") if isinstance(record, dict) else None
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("OpenFootball raw archive manifest source_id is invalid")
        source_ids.add(source_id)
    if not source_ids:
        raise ValueError("OpenFootball raw archive manifest is empty")
    return sorted(source_ids)


def require_durable_openfootball_raw_archive(path: Path | str) -> Path:
    """Require a durable, locally replayable OpenFootball evidence archive."""

    archive_root = Path(path)
    try:
        resolved = archive_root.resolve(strict=False)
        volatile_root = Path("/dev/shm").resolve(strict=True)
        resolved.relative_to(volatile_root)
    except ValueError:
        pass
    except (OSError, RuntimeError) as exc:
        raise ValueError("OpenFootball raw archive must be a readable durable directory") from exc
    else:
        raise ValueError("OpenFootball raw archive must be on durable storage")
    try:
        source_ids = _manifest_source_ids(archive_root / MANIFEST_NAME)
        load_verified_openfootball_archive(
            archive_root,
            source_ids=source_ids,
            observed_before=datetime.max.replace(tzinfo=timezone.utc),
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        OpenFootballRawArchiveError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            "OpenFootball raw archive must be a readable durable verified archive"
        ) from exc
    return archive_root


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _score_label(score: dict[str, Any]) -> str:
    home = int(score["home"])
    away = int(score["away"])
    return "home" if home > away else "draw" if home == away else "away"


def _half_full_label(score: dict[str, Any], halftime: dict[str, Any] | None) -> str | None:
    if not isinstance(halftime, dict) or "home" not in halftime or "away" not in halftime:
        return None
    half_home, half_away = int(halftime["home"]), int(halftime["away"])
    half = "H" if half_home > half_away else "D" if half_home == half_away else "A"
    return f"{half}/{_score_label(score)[0].upper()}"


def _valid_score(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    if any(
        isinstance(value.get(key), bool)
        or not isinstance(value.get(key), int)
        or int(value[key]) < 0
        for key in ("home", "away")
    ):
        return None
    return {"home": int(value["home"]), "away": int(value["away"])}


def _valid_halftime_score(value: object, final_score: dict[str, int]) -> dict[str, int] | None:
    half = _valid_score(value)
    if half is None or any(half[key] > final_score[key] for key in ("home", "away")):
        return None
    return half


def _merge_result_candidate(
    results: dict[str, dict[str, Any]],
    conflicts: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> None:
    """Merge one causally observed result while retaining every conflict."""

    fixture_id = candidate["fixture_id"]
    candidate["halftime_score"] = _valid_halftime_score(
        candidate.get("halftime_score"), candidate["score"]
    )
    existing = results.get(fixture_id)
    if existing is None:
        results[fixture_id] = candidate
        return
    if existing["score"] != candidate["score"]:
        conflicts.append({"fixture_id": fixture_id, "first": existing, "conflict": candidate})
        return
    # A missing halftime score is not a contradiction: the independent
    # provider may enrich the same final result on a later snapshot.
    existing_half = existing.get("halftime_score")
    candidate_half = candidate.get("halftime_score")
    if (
        isinstance(existing_half, dict)
        and isinstance(candidate_half, dict)
        and existing_half != candidate_half
    ):
        conflicts.append({"fixture_id": fixture_id, "first": existing, "conflict": candidate})
        return
    if existing_half is None and isinstance(candidate_half, dict):
        existing["halftime_score"] = candidate_half
    if _utc(candidate["observed_at"], field="result.observed_at") < _utc(
        existing["observed_at"], field="result.observed_at"
    ):
        if existing.get("halftime_score") is not None and candidate_half is None:
            candidate["halftime_score"] = existing["halftime_score"]
        results[fixture_id] = candidate


def load_first_results_admission(
    snapshot_archive_dir: Path,
    *,
    openfootball_raw_archive_dir: Path,
) -> dict[str, Any]:
    """Load earliest formal results plus fail-closed admission diagnostics."""

    replay = load_verified_result_rows(
        snapshot_paths=sorted((snapshot_archive_dir / "raw").glob("*.json")),
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        # Each replay is independently bounded by its snapshot ``as_of``.
        # This outer ceiling exists only to satisfy the shared loader contract.
        reference=datetime.max.replace(tzinfo=timezone.utc),
    )
    results: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for row in replay["rows"]:
        score = _valid_score(row.get("score"))
        if score is None:
            continue
        candidate = {
            "fixture_id": str(row["fixture_id"]),
            "competition_id": str(row["competition_id"]),
            "kickoff_at": row["kickoff_at"].isoformat(),
            "observed_at": row["observed_at"].isoformat(),
            "score": score,
            "halftime_score": row.get("halftime_score"),
            "source_path": str(row["source_file"]),
            "source_name": str(row["source_name"]),
        }
        _merge_result_candidate(results, conflicts, candidate)
    return {
        "results": results,
        "conflicts": conflicts,
        "admission": replay["admission"],
        "invalid_snapshots": replay["invalid_snapshots"],
    }


def load_first_results(
    snapshot_archive_dir: Path,
    *,
    openfootball_raw_archive_dir: Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Compatibility view over structured formal-result admission."""

    loaded = load_first_results_admission(
        snapshot_archive_dir,
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
    )
    return loaded["results"], loaded["conflicts"]


def _valid_probability(value: object, labels: tuple[str, ...]) -> dict[str, float] | None:
    if not isinstance(value, dict) or set(value) != set(labels):
        return None
    try:
        values = {label: float(value[label]) for label in labels}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(number) and number >= 0 for number in values.values()):
        return None
    total = sum(values.values())
    if total <= 0 or abs(total - 1.0) > 1e-5:
        return None
    return values


def _prediction_target_contract_errors(prediction: dict[str, Any]) -> dict[str, str]:
    """Return target-specific matrix marginal errors without hiding valid 1X2.

    Older prediction archives may carry a scoreline matrix alongside an
    independently rendered 1X2/total distribution.  A mismatch invalidates
    only the affected derived target; a valid primary 1X2 probability can
    still be scored and remains auditable.  This keeps the public evaluator
    compatible while ensuring malformed target rows never contribute a metric
    for the target they cannot support.
    """

    errors: dict[str, str] = {}
    one_x_two_fields = {
        "primary_probability_1x2",
        "probability_1x2_from_scoreline",
        "scoreline_probability",
        "independent_scoreline_probability",
    }
    total_fields = {
        "total_goals_probability",
        "independent_total_goals_probability",
    }
    if not isinstance(prediction.get("scoreline_matrix"), dict) and not isinstance(
        prediction.get("independent_scoreline_matrix"), dict
    ):
        return errors
    for target, fields_to_remove in (
        ("scoreline", total_fields),
        ("total_goals", one_x_two_fields),
    ):
        candidate = dict(prediction)
        for field in fields_to_remove:
            candidate.pop(field, None)
        error = scoreline_target_consistency_error(candidate)
        if error is not None:
            errors[target] = error
    return errors


def _prediction_causal_contract_error(prediction: dict[str, Any]) -> str | None:
    """Validate the prediction's freeze/observation chronology before scoring."""

    # The prospective schema calls the boundary ``freeze_cutoff_at`` while
    # strict backtest rows call it ``cutoff_at``.  The metrics audit accepts
    # both; normalize only for this in-memory check and never mutate the
    # archived payload.
    candidate = dict(prediction)
    if candidate.get("cutoff_at") in (None, ""):
        candidate["cutoff_at"] = candidate.get("freeze_cutoff_at")
    try:
        assert_no_future_leakage([candidate])
    except (KeyError, TypeError, ValueError) as exc:
        return str(exc)
    return None


def _result_fingerprint(result: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "fixture_id": result.get("fixture_id"),
            "observed_at": result.get("observed_at"),
            "score": result.get("score"),
            "halftime_score": result.get("halftime_score"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _score_prediction(
    prediction: dict[str, Any],
    result: dict[str, Any],
    *,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = result["score"]
    actual_1x2 = _score_label(score)
    evaluated: dict[str, Any] = {
        "fixture_id": prediction["fixture_id"],
        "competition_id": prediction.get("competition_id"),
        "freeze_stage": prediction.get("freeze_stage"),
        "kickoff_at": prediction["kickoff_at"],
        "result_observed_at": result["observed_at"],
        "actual_1x2": actual_1x2,
        "actual_score": f"{score['home']}-{score['away']}",
        "model_version": prediction.get("model_version"),
        "model_version_sha256": record.get("model_version_sha256")
        if isinstance(record, dict)
        else None,
        "provider_fixture_id": prediction.get("provider_fixture_id")
        or prediction.get("fixture_id"),
        "stage_locked_at": prediction.get("prediction_observed_at") or prediction.get("as_of"),
        "freeze_cutoff_at": prediction.get("freeze_cutoff_at"),
        "result_fingerprint": _result_fingerprint(result),
        "targets": {},
    }
    target_contract_errors = _prediction_target_contract_errors(prediction)
    one_x_two = _valid_probability(
        prediction.get("primary_probability_1x2")
        or prediction.get("probability_1x2_from_scoreline")
        or prediction.get("scoreline_probability"),
        ("home", "draw", "away"),
    )
    if one_x_two is not None:
        evaluated["targets"]["three_way"] = {
            "probability": one_x_two,
            "log_loss": -math.log(max(1e-12, one_x_two[actual_1x2])),
            "brier": sum(
                (one_x_two[label] - float(label == actual_1x2)) ** 2 for label in one_x_two
            ),
        }
        market_probability = _valid_probability(
            prediction.get("market_probability"), ("home", "draw", "away")
        )
        if market_probability is not None:
            evaluated["targets"]["three_way"]["market_log_loss"] = -math.log(
                max(1e-12, market_probability[actual_1x2])
            )
            evaluated["targets"]["three_way"]["market_brier"] = sum(
                (market_probability[label] - float(label == actual_1x2)) ** 2
                for label in market_probability
            )
    total_probability = _valid_probability(
        prediction.get("total_goals_probability"), ("0", "1", "2", "3", "4", "5+")
    )
    if total_probability is not None and "total_goals" not in target_contract_errors:
        total = int(score["home"]) + int(score["away"])
        total_label = str(total) if total < 5 else "5+"
        evaluated["targets"]["total_goals"] = {
            "probability": total_probability,
            "actual": total_label,
            "log_loss": -math.log(max(1e-12, total_probability[total_label])),
        }
    matrix = prediction.get("scoreline_matrix")
    actual_score = f"{score['home']}-{score['away']}"
    if isinstance(matrix, dict) and "scoreline" not in target_contract_errors:
        try:
            values = {str(key): float(value) for key, value in matrix.items()}
        except (TypeError, ValueError):
            values = {}
        if (
            values
            and all(math.isfinite(value) and value >= 0 for value in values.values())
            and sum(values.values()) > 0
            and all(
                isinstance(label, str)
                and label.count("-") == 1
                and all(part.isdigit() for part in label.split("-"))
                for label in values
            )
        ):
            values = {key: value / sum(values.values()) for key, value in values.items()}
            top5 = sorted(values, key=lambda key: (-values[key], key))[:5]
            evaluated["targets"]["scoreline"] = {
                "log_loss": -math.log(max(1e-12, values.get(actual_score, 0.0))),
                "top5_hit": actual_score in top5,
            }
    half_full = _valid_probability(
        prediction.get("half_full_probability"),
        tuple(f"{half}/{full}" for half in "HDA" for full in "HDA"),
    )
    actual_half_full = _half_full_label(score, result.get("halftime_score"))
    if half_full is not None and actual_half_full is not None:
        evaluated["targets"]["half_full"] = {
            "log_loss": -math.log(max(1e-12, half_full[actual_half_full])),
            "actual": actual_half_full,
        }
    return evaluated


def evaluate_archive(
    prediction_archive: Path,
    snapshot_archive_dir: Path,
    *,
    openfootball_raw_archive_dir: Path,
    lock: dict[str, Any] | None = None,
    include_scored_rows: bool = False,
) -> dict[str, Any]:
    """Score current-lock predictions against immutable result snapshots.

    The archive is append-only and may contain rows from a superseded lock.
    Those rows remain auditable, but must not contaminate the current
    prospective independence gate after a model migration.
    """

    openfootball_raw_archive_dir = require_durable_openfootball_raw_archive(
        openfootball_raw_archive_dir
    )
    result_load = load_first_results_admission(
        snapshot_archive_dir,
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
    )
    results = result_load["results"]
    result_conflicts = result_load["conflicts"]
    result_conflict_ids = {
        str(item.get("fixture_id"))
        for item in result_conflicts
        if isinstance(item, dict) and item.get("fixture_id")
    }
    window_start: datetime | None = None
    if isinstance(lock, dict) and lock.get("evaluation_window_started_at") is not None:
        window_start = _utc(
            lock["evaluation_window_started_at"], field="evaluation_window_started_at"
        )
    scored: list[dict[str, Any]] = []
    pending = 0
    archive_conflicts = 0
    legacy_model_records = 0
    superseded_model_records = 0
    stale_lock_records = 0
    result_conflict_predictions = 0
    stable_by_freeze: dict[str, str] = {}
    invalid_records: list[dict[str, Any]] = []
    if prediction_archive.exists():
        for line_number, line in enumerate(
            prediction_archive.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_records.append({"line": line_number, "reason": "invalid_json"})
                continue
            if not isinstance(record, dict) or not isinstance(record.get("prediction"), dict):
                invalid_records.append({"line": line_number, "reason": "invalid_record"})
                continue
            prediction = record["prediction"]
            expected_model = lock.get("freeze_model_name") if isinstance(lock, dict) else None
            expected_sha = lock.get("model_version_sha256") if isinstance(lock, dict) else None
            if (
                isinstance(expected_model, str)
                and prediction.get("model_version") != expected_model
            ):
                legacy_model_records += 1
                superseded_model_records += 1
                continue
            if (
                isinstance(expected_sha, str)
                and record.get("model_version_sha256") != expected_sha
            ):
                legacy_model_records += 1
                stale_lock_records += 1
                continue
            try:
                kickoff = _utc(prediction["kickoff_at"], field="prediction.kickoff_at")
                if window_start is not None and kickoff < window_start:
                    invalid_records.append(
                        {
                            "line": line_number,
                            "reason": "prediction_kickoff_predates_locked_evaluation_window",
                        }
                    )
                    continue
                if window_start is not None:
                    prediction_observed = _utc(
                        prediction.get("prediction_observed_at"),
                        field="prediction.prediction_observed_at",
                    )
                    if prediction_observed < window_start:
                        invalid_records.append(
                            {
                                "line": line_number,
                                "reason": "prediction_observed_before_locked_evaluation_window",
                            }
                        )
                        continue
            except (KeyError, ValueError) as exc:
                invalid_records.append({"line": line_number, "reason": str(exc)})
                continue
            causal_error = _prediction_causal_contract_error(prediction)
            if causal_error is not None:
                # A row that was observed after its freeze (or after kickoff)
                # is quarantined before it can occupy a freeze identity,
                # become pending, or influence any target metric.
                invalid_records.append(
                    {
                        "line": line_number,
                        "reason": f"prediction_causal_contract:{causal_error}",
                    }
                )
                continue
            try:
                freeze_key = logical_freeze_key(prediction)
            except (KeyError, TypeError):
                freeze_key = str(record.get("freeze_key") or "")
            stable_digest = stable_prediction_digest(prediction)
            previous_digest = stable_by_freeze.get(freeze_key)
            if previous_digest is not None and previous_digest == stable_digest:
                # A later poll of the same immutable freeze carries newer
                # audit timestamps but must not inflate pending/scored
                # samples or stage coverage.
                continue
            if previous_digest is None:
                stable_by_freeze[freeze_key] = stable_digest
            if record.get("conflict"):
                # Older cycles included kickoff_time_observed_at in the
                # immutable digest.  Reclassify those bookkeeping-only rows
                # after the canonical digest fix; retain real probability
                # conflicts as hard blocks.
                if previous_digest == stable_digest:
                    continue
                archive_conflicts += 1
                continue
            if previous_digest is not None:
                # A non-conflict record with a different payload under the
                # same logical stage is malformed evidence; fail closed.
                archive_conflicts += 1
                continue
            fixture_id = prediction.get("fixture_id")
            if fixture_id in result_conflict_ids:
                # Preserve every source snapshot, but never score an outcome
                # that cannot be resolved causally.
                result_conflict_predictions += 1
                continue
            result = results.get(fixture_id)
            if result is None:
                pending += 1
                continue
            try:
                result_observed = _utc(result["observed_at"], field="result.observed_at")
                if result_observed < kickoff:
                    invalid_records.append(
                        {"line": line_number, "reason": "result_observed_before_kickoff"}
                    )
                    continue
            except ValueError as exc:
                invalid_records.append({"line": line_number, "reason": str(exc)})
                continue
            scored_row = _score_prediction(prediction, result, record=record)
            target_contract_errors = _prediction_target_contract_errors(prediction)
            if target_contract_errors:
                # Keep valid independent targets (for example a primary 1X2
                # distribution) while explicitly quarantining the affected
                # scoreline/total-goals lanes.  A row with no valid targets is
                # not a scored sample at all.
                invalid_records.append(
                    {
                        "line": line_number,
                        "reason": "prediction_target_contract",
                        "targets": sorted(target_contract_errors),
                        "details": target_contract_errors,
                    }
                )
            if scored_row["targets"]:
                scored.append(scored_row)
            else:
                invalid_records.append(
                    {
                        "line": line_number,
                        "reason": "prediction_has_no_valid_targets",
                    }
                )

    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_league: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in scored:
        league = str(row.get("competition_id") or "unknown")
        for target in row["targets"]:
            by_target[target].append(row)
            by_league[league][target] += 1
    target_metrics: dict[str, Any] = {}
    for target, rows in by_target.items():
        if target == "three_way":
            probabilities = [row["targets"][target]["probability"] for row in rows]
            outcomes = [row["actual_1x2"] for row in rows]
            metrics = evaluate_three_way(probabilities, outcomes)
            losses = [row["targets"][target]["log_loss"] for row in rows]
            target_metrics[target] = {**metrics, "log_loss_ci": bootstrap_mean_ci(losses)}
            market_rows = [
                row["targets"][target]
                for row in rows
                if "market_brier" in row["targets"][target]
                and "market_log_loss" in row["targets"][target]
            ]
            if market_rows:
                market_losses = [float(row["market_log_loss"]) for row in market_rows]
                target_metrics[target]["market_sample_n"] = len(market_rows)
                target_metrics[target]["market_brier"] = sum(
                    float(row["market_brier"]) for row in market_rows
                ) / len(market_rows)
                target_metrics[target]["market_log_loss"] = sum(market_losses) / len(market_losses)
                target_metrics[target]["market_log_loss_ci"] = bootstrap_mean_ci(market_losses)
        else:
            losses = [float(row["targets"][target]["log_loss"]) for row in rows]
            target_metrics[target] = {
                "sample_n": len(rows),
                "log_loss": sum(losses) / len(losses),
                "log_loss_ci": bootstrap_mean_ci(losses),
            }
            if target == "scoreline":
                target_metrics[target]["top5_hit"] = sum(
                    bool(row["targets"][target]["top5_hit"]) for row in rows
                ) / len(rows)
    league_counts = {league: dict(counts) for league, counts in by_league.items()}
    target_sample_requirements = {
        target: {
            "sample_n": len(by_target.get(target, [])),
            "minimum_n": minimum,
            "shortfall_n": max(0, minimum - len(by_target.get(target, []))),
            "met": len(by_target.get(target, [])) >= minimum,
        }
        for target, minimum in PROSPECTIVE_SAMPLE_THRESHOLDS.items()
    }
    sample_requirements_met = bool(
        all(
            league_counts.get(league, {}).get(target, 0)
            >= PROSPECTIVE_SAMPLE_THRESHOLDS[target]
            for league in REQUIRED_LEAGUES
            for target in ("three_way", "total_goals", "half_full")
        )
        and target_sample_requirements["scoreline"]["met"]
    )
    required_targets_scored = all(target in by_target and by_target[target] for target in TARGETS)
    freezes_by_fixture: dict[str, set[str]] = defaultdict(set)
    for row in scored:
        freezes_by_fixture[row["fixture_id"]].add(str(row["freeze_stage"]))
    prediction_freezes_verified = bool(freezes_by_fixture) and all(
        REQUIRED_FREEZE_STAGES <= stages for stages in freezes_by_fixture.values()
    )
    results_not_used_for_selection = bool(
        (lock or {}).get("results_not_used_for_selection") is True
    )
    result_admission = result_load["admission"]
    result_admission_clean = bool(
        result_admission["quarantined_rows"] == 0 and result_load["invalid_snapshots"] == 0
    )
    market_sample_n = sum(
        1
        for row in by_target.get("three_way", [])
        if "market_log_loss" in row["targets"].get("three_way", {})
        and "market_brier" in row["targets"].get("three_way", {})
    )
    market_baseline_available = market_sample_n > 0
    market_baseline_status = (
        "available" if market_baseline_available else "unavailable_no_independent_market_baseline"
    )
    promotion_eligible = bool(
        sample_requirements_met
        and required_targets_scored
        and prediction_freezes_verified
        and results_not_used_for_selection
        and not result_conflicts
        and result_admission_clean
        and market_baseline_available
    )
    output = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prediction_archive": str(prediction_archive),
        "snapshot_archive": str(snapshot_archive_dir),
        "status": "passed" if promotion_eligible else "pending_prospective_window",
        "scored_n": len(scored),
        "pending_n": pending,
        "archive_conflicts": archive_conflicts,
        "legacy_model_records": legacy_model_records,
        "superseded_model_records": superseded_model_records,
        "stale_lock_records": stale_lock_records,
        "result_conflicts": len(result_conflicts),
        "result_conflict_predictions": result_conflict_predictions,
        "result_admission": result_admission,
        "result_admission_clean": result_admission_clean,
        "result_invalid_snapshots": result_load["invalid_snapshots"],
        "invalid_records": invalid_records,
        "target_metrics": target_metrics,
        "league_target_counts": league_counts,
        "target_sample_requirements": target_sample_requirements,
        "sample_thresholds": dict(PROSPECTIVE_SAMPLE_THRESHOLDS),
        "market_baseline_available": market_baseline_available,
        "market_baseline_status": market_baseline_status,
        "market_baseline_sample_n": market_sample_n,
        "results_not_used_for_selection": results_not_used_for_selection,
        "sample_requirements_met": sample_requirements_met,
        "all_required_targets_scored": required_targets_scored,
        "prediction_freezes_verified": prediction_freezes_verified,
        # The prospective evaluator is an evidence gate, never a deployment
        # switch.  Even a future fully passing window must be explicitly
        # promoted by the parent release gate; today this mirrors the strict
        # eligibility conjunction and remains false when the market baseline
        # is unavailable.
        "production_allowed": promotion_eligible,
        "promotion_eligible": promotion_eligible,
        "freeze_stage_counts": {
            fixture_id: sorted(stages) for fixture_id, stages in freezes_by_fixture.items()
        },
    }
    if include_scored_rows:
        # This is an explicit opt-in export for downstream D1 publishing.  It
        # is intentionally absent from the default evidence JSON so the
        # compact gate artifact remains stable and contains no mutable result
        # payload beyond the aggregate metrics.
        output["scored_rows"] = scored
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prediction-archive", type=Path, default=Path("data/live/prospective_predictions.jsonl")
    )
    parser.add_argument("--snapshot-archive", type=Path, default=Path("data/live/archive"))
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        "--openfootball-raw-archive",
        dest="openfootball_raw_archive_dir",
        type=Path,
    )
    parser.add_argument(
        "--lock", type=Path, default=Path("docs/evidence/prospective-model-lock-current.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    openfootball_raw_archive_dir = resolve_openfootball_raw_archive_dir(
        args.openfootball_raw_archive_dir,
        runtime_default=args.snapshot_archive.parent / "openfootball-raw",
    )
    result = evaluate_archive(
        args.prediction_archive,
        args.snapshot_archive,
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        lock=lock,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "status",
                    "scored_n",
                    "pending_n",
                    "sample_requirements_met",
                    "all_required_targets_scored",
                    "prediction_freezes_verified",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_OPENFOOTBALL_RAW_ARCHIVE_DIR",
    "OPENFOOTBALL_RAW_ARCHIVE_ENV",
    "PROSPECTIVE_SAMPLE_THRESHOLDS",
    "TARGETS",
    "evaluate_archive",
    "load_first_results",
    "load_first_results_admission",
    "require_durable_openfootball_raw_archive",
    "resolve_openfootball_raw_archive_dir",
]
