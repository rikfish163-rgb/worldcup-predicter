"""Build the public prediction projection from the causal freeze archive.

The platform still has a useful ``build_future_predictions`` path for local
research and diagnostics.  It is intentionally not the public forecast
source: that path is computed from one live snapshot and therefore does not
prove that a prediction was frozen at the advertised stage.  Sites/API use
this module instead and only expose immutable, non-conflict rows from the
active prospective model lock.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping

from league_platform.fixture_feed import (
    blocked_fixture_record,
    prediction_coverage,
    upcoming_fixture_rows,
)


SUPPORTED_STAGES = (
    "t_minus_24h",
    "t_minus_6h",
    "t_minus_90m",
    "lineup_confirmation",
)
_STAGE_RANK = {stage: index for index, stage in enumerate(SUPPORTED_STAGES)}
_EXPECTED_FEATURES = ("recent_xg", "market", "weather", "injuries", "lineups")
PUBLIC_PREDICTION_HORIZON = timedelta(days=7)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _finite_payload(value: object) -> bool:
    """Reject NaN/Infinity anywhere in an archived prediction payload."""

    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(_finite_payload(key) and _finite_payload(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_finite_payload(item) for item in value)
    return True


def _validated_horizon(value: timedelta | None) -> timedelta | None:
    if value is None:
        return None
    if not isinstance(value, timedelta):
        raise ValueError("prediction horizon must be a timedelta or None")
    if value < timedelta(0):
        raise ValueError("prediction horizon must be non-negative")
    return value


def _inside_public_prediction_horizon(
    value: Any,
    *,
    as_of: datetime,
    horizon: timedelta = PUBLIC_PREDICTION_HORIZON,
) -> bool:
    kickoff = _timestamp(value)
    return kickoff is not None and as_of < kickoff <= as_of + horizon


def _probability(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, float] = {}
    for key in ("home", "draw", "away"):
        try:
            result[key] = float(value[key])
        except (KeyError, TypeError, ValueError):
            return None
    if any(not math.isfinite(number) or number < 0 or number > 1 for number in result.values()):
        return None
    if abs(sum(result.values()) - 1.0) > 0.02:
        return None
    return result


def _available_features(prediction: Mapping[str, Any]) -> set[str]:
    values = prediction.get("live_feature_fields")
    fields = {str(item) for item in values} if isinstance(values, list) else set()
    available: set[str] = set()
    if "recent_xg" in fields:
        available.add("recent_xg")
    if fields.intersection({"market_1x2", "sports_lottery_hhad", "odds", "market"}):
        available.add("market")
    if "weather" in fields:
        available.add("weather")
    if fields.intersection({"injuries", "player_status"}):
        available.add("injuries")
    if fields.intersection({"lineups", "official_lineup"}):
        available.add("lineups")
    if prediction.get("freeze_stage") == "lineup_confirmation" and prediction.get(
        "lineup_confirmation_fingerprint"
    ):
        available.add("lineups")
    return available


def _coverage(prediction: Mapping[str, Any]) -> dict[str, Any]:
    available = _available_features(prediction)
    missing = [name for name in _EXPECTED_FEATURES if name not in available]
    ratio = round(len(available) / len(_EXPECTED_FEATURES), 6)
    level = "high" if ratio >= 0.8 and not missing else "medium" if ratio >= 0.5 else "low"
    return {
        "level": level,
        "ratio": ratio,
        "missing": missing,
        "critical_conflict": False,
        "source": "prospective_archive",
    }


def _upcoming_candidates(
    snapshot: Mapping[str, Any],
    *,
    as_of: datetime | None,
    horizon: timedelta | None = PUBLIC_PREDICTION_HORIZON,
) -> list[dict[str, Any]]:
    """Return every auditable upcoming row in the requested window.

    The compact ``matches`` projection is preferred by
    :func:`fixture_feed.snapshot_fixture_rows`; a provider-neutral feed is
    used only when that projection is absent/empty.  Rows with an invalid
    kickoff remain in the result so callers can explain why they are blocked.
    """

    return upcoming_fixture_rows(
        snapshot,
        as_of=as_of,
        horizon=horizon,
        include_unparseable_kickoff=True,
    )


def _snapshot_clock(snapshot: Mapping[str, Any]) -> datetime | None:
    """Choose the first valid causal clock from current data or generation metadata."""

    current = snapshot.get("current_data")
    values: list[Any] = []
    if isinstance(current, Mapping):
        values.append(current.get("as_of"))
    values.append(snapshot.get("generated_at"))
    for value in values:
        parsed = _timestamp(value)
        if parsed is not None:
            return parsed
    return None


def _blocked_candidates(
    candidates: list[dict[str, Any]],
    *,
    reason: str,
    message: str,
) -> list[dict[str, Any]]:
    return [
        blocked_fixture_record(row, reason=reason, message=message)
        for row in candidates
    ]


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_archive_rows(
    archive_path: Path,
    *,
    lock: Mapping[str, Any],
    as_of: datetime,
    fixture_ids: set[str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return latest valid row per fixture/stage for the active lock.

    Every time check is deliberately fail-closed.  A row observed after the
    static snapshot, a future freeze cutoff, a conflicting row, or a row from
    an older model lock cannot leak into the public projection.
    """

    expected_hash = lock.get("model_version_sha256")
    if not isinstance(expected_hash, str) or not expected_hash:
        return []
    candidates: dict[tuple[str, str], tuple[datetime, dict[str, Any], dict[str, Any]]] = {}
    quarantined: set[tuple[str, str]] = set()
    try:
        lines = archive_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("conflict") is True:
            continue
        if record.get("model_version_sha256") != expected_hash:
            continue
        prediction = record.get("prediction")
        if not isinstance(prediction, dict):
            continue
        # ``json.loads`` accepts NaN/Infinity by default.  Do not let such a
        # value survive in a secondary matrix or metadata field simply because
        # the primary 1X2 map happened to be finite.
        if not _finite_payload(prediction):
            continue
        fixture_id = prediction.get("fixture_id")
        stage = prediction.get("freeze_stage")
        if not isinstance(fixture_id, str) or fixture_id not in fixture_ids:
            continue
        if stage not in _STAGE_RANK:
            continue
        kickoff = _timestamp(prediction.get("kickoff_at"))
        cutoff = _timestamp(prediction.get("freeze_cutoff_at") or prediction.get("cutoff_at"))
        observed = _timestamp(
            prediction.get("prediction_observed_at")
            or prediction.get("as_of")
            or record.get("captured_at")
        )
        captured = _timestamp(record.get("captured_at")) or observed
        if kickoff is None or cutoff is None or observed is None or captured is None:
            continue
        if kickoff <= as_of or cutoff > as_of or observed > as_of or captured > as_of:
            continue
        if not cutoff < kickoff or observed < cutoff or observed >= kickoff:
            continue
        if not _probability(
            prediction.get("scoreline_probability")
            or prediction.get("primary_probability_1x2")
            or prediction.get("probability_1x2_from_scoreline")
        ):
            continue
        key = (fixture_id, stage)
        if key in quarantined:
            continue
        previous = candidates.get(key)
        if previous is None or captured > previous[0]:
            candidates[key] = (captured, record, prediction)
        elif captured == previous[0] and prediction != previous[2]:
            # Equal capture clocks with different content are an unresolved
            # same-stage conflict.  Keeping either row would make the public
            # projection order-dependent, so quarantine the stage entirely.
            candidates.pop(key, None)
            quarantined.add(key)
    return [(record, prediction) for _, record, prediction in candidates.values()]


def _freeze_versions(
    fixture_id: str,
    kickoff: datetime,
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    by_stage = {prediction.get("freeze_stage"): (record, prediction) for record, prediction in rows}
    versions: list[dict[str, Any]] = []
    for stage in SUPPORTED_STAGES:
        value = by_stage.get(stage)
        if value is None:
            expected_cutoff = (
                kickoff - timedelta(hours=24)
                if stage == "t_minus_24h"
                else kickoff - timedelta(hours=6)
                if stage == "t_minus_6h"
                else kickoff - timedelta(minutes=90)
                if stage == "t_minus_90m"
                else None
            )
            versions.append(
                {
                    "stage": stage,
                    "status": "pending",
                    "cutoff_at": expected_cutoff.isoformat().replace("+00:00", "Z")
                    if expected_cutoff
                    else None,
                }
            )
            continue
        record, prediction = value
        versions.append(
            {
                "stage": stage,
                "status": "available",
                "cutoff_at": _iso(prediction.get("freeze_cutoff_at") or prediction.get("cutoff_at")),
                "observed_at": _iso(
                    prediction.get("prediction_observed_at")
                    or prediction.get("as_of")
                    or record.get("captured_at")
                ),
                "record_key": record.get("record_key"),
                "content_sha256": record.get("content_sha256"),
                "feature_coverage": _coverage(prediction).get("ratio"),
                "missing_features": _coverage(prediction).get("missing"),
                "primary_probability_1x2": prediction.get("scoreline_probability")
                or prediction.get("primary_probability_1x2")
                or prediction.get("probability_1x2_from_scoreline"),
                "scoreline_matrix": prediction.get("scoreline_matrix"),
                "total_goals_probability": prediction.get("total_goals_probability"),
                "total_over_under_line": prediction.get("total_over_under_line"),
                "total_over_under_probability": prediction.get("total_over_under_probability"),
                "half_full_probability": prediction.get("half_full_probability"),
                "handicap_probability": prediction.get("handicap_probability"),
            }
        )
    return versions


def _project_prediction(
    record: Mapping[str, Any],
    prediction: Mapping[str, Any],
    *,
    fixture_rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    probability = _probability(
        prediction.get("scoreline_probability")
        or prediction.get("primary_probability_1x2")
        or prediction.get("probability_1x2_from_scoreline")
    ) or {}
    kickoff = _timestamp(prediction.get("kickoff_at"))
    feature_fields = list(prediction.get("live_feature_fields") or [])
    result = dict(prediction)
    result.update(
        {
            "prediction_source": "prospective_archive",
            "archive_record_key": record.get("record_key"),
            "archive_content_sha256": record.get("content_sha256"),
            "model_version_sha256": record.get("model_version_sha256"),
            "primary_model": prediction.get("model_version"),
            "primary_probability_1x2": probability,
            "probability_1x2_from_scoreline": probability,
            "dixon_coles_probability": probability,
            "as_of": prediction.get("prediction_observed_at") or prediction.get("as_of"),
            "cutoff_at": prediction.get("freeze_cutoff_at") or prediction.get("cutoff_at"),
            "status": "research_only",
            "quality_gate": "blocked_for_production_until_prospective_and_source_gates_pass",
            "coverage": _coverage(prediction),
            "feature_coverage": {
                "recent_xg": "recent_xg" in _available_features(prediction),
                "current_market": "market" in _available_features(prediction),
                "weather": "weather" in _available_features(prediction),
                "injuries": "injuries" in _available_features(prediction),
                "lineups": "lineups" in _available_features(prediction),
                "lineups_confirmed": prediction.get("freeze_stage") == "lineup_confirmation",
                "live_feature_fields": feature_fields,
            },
            "freeze_versions": _freeze_versions(
                str(prediction.get("fixture_id")),
                kickoff or datetime.min.replace(tzinfo=timezone.utc),
                fixture_rows,
            ),
        }
    )
    return result


def build_public_predictions(
    snapshot: Mapping[str, Any],
    *,
    archive_path: Path,
    lock_path: Path,
    prediction_horizon: timedelta | None = PUBLIC_PREDICTION_HORIZON,
) -> dict[str, Any]:
    """Return the only prediction projection allowed into Sites/API.

    The optional draft count is intentionally metadata only.  Draft rows are
    not copied into ``predictions`` and therefore cannot be mistaken for
    stage-frozen forecasts by the browser or a downstream consumer.
    """

    current = snapshot.get("current_data") if isinstance(snapshot.get("current_data"), Mapping) else {}
    as_of = _snapshot_clock(snapshot)
    try:
        horizon = _validated_horizon(prediction_horizon)
    except ValueError:
        # Preserve the per-fixture coverage invariant even when a caller
        # supplies an invalid horizon.  No model/archive row is admitted.
        candidates = _upcoming_candidates(snapshot, as_of=as_of, horizon=None)
        blocked = _blocked_candidates(
            candidates,
            reason="public_prediction_invalid_horizon",
            message="预测窗口无效，该场次没有可审计的公开预测。",
        )
        return {
            "status": "unavailable",
            "as_of": current.get("as_of") or snapshot.get("generated_at"),
            "predictions": [],
            "blocked": blocked,
            "coverage": prediction_coverage(candidates, [], blocked),
            "prediction_source": "prospective_archive",
            "message": "预测窗口无效，公开预测已阻断。",
        }
    lock = _read_json_object(lock_path)
    if as_of is None or lock.get("status") not in {"pending_prospective_window", "passed"}:
        candidates = _upcoming_candidates(snapshot, as_of=as_of, horizon=horizon)
        blocked = _blocked_candidates(
            candidates,
            reason="public_prediction_lock_or_clock_unavailable",
            message="当前前瞻模型锁或时间截点不可验证，该场次没有可审计的公开预测。",
        )
        return {
            "status": "unavailable",
            "as_of": current.get("as_of") or snapshot.get("generated_at"),
            "predictions": [],
            "blocked": blocked,
            "coverage": prediction_coverage(candidates, [], blocked),
            "prediction_source": "prospective_archive",
            "message": "当前前瞻模型锁或时间截点不可验证，公开预测已阻断。",
        }
    # A stale source snapshot cannot establish that a formerly upcoming
    # fixture is still upcoming, so never expose archived rows as the active
    # frozen/public lane.  ``build_stale_research_predictions`` may reuse
    # causal rows for research, but it annotates them as stale and filters
    # kickoffs against the current reference clock.
    if current.get("status") == "stale":
        candidates = _upcoming_candidates(snapshot, as_of=as_of, horizon=horizon)
        blocked = _blocked_candidates(
            candidates,
            reason="current_source_snapshot_expired",
            message="当前公开源快照已过期，冻结预测不作为实时状态展示。",
        )
        return {
            "status": "stale_snapshot",
            "as_of": as_of.isoformat().replace("+00:00", "Z"),
            "prediction_source": "prospective_archive",
            "active_model_version_sha256": lock.get("model_version_sha256"),
            "predictions": [],
            "blocked": blocked,
            "coverage": prediction_coverage(candidates, [], blocked),
            "archive": {
                "path": str(archive_path),
                "lock_path": str(lock_path),
                "rows_considered": 0,
                "fixtures_with_current_freeze": 0,
                "fixtures_waiting_for_freeze": len(blocked),
            },
            "message": (
                "当前公开源快照已过期；冻结预测不作为实时状态展示。"
                "如仍有因果归档，仅保留在研究工作台并明确标注过期。"
            ),
        }
    candidates = _upcoming_candidates(snapshot, as_of=as_of, horizon=horizon)
    upcoming = {
        str(item.get("id")): item
        for item in candidates
        if isinstance(item.get("id"), str)
    }
    rows = _load_archive_rows(
        archive_path,
        lock=lock,
        as_of=as_of,
        fixture_ids=set(upcoming),
    )
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for record, prediction in rows:
        grouped[str(prediction["fixture_id"])].append((record, prediction))
    projected: list[dict[str, Any]] = []
    projection_blocked: set[str] = set()
    for fixture_id, fixture_rows in grouped.items():
        fixture_rows.sort(key=lambda item: _STAGE_RANK.get(str(item[1].get("freeze_stage")), -1))
        record, prediction = fixture_rows[-1]
        try:
            projected.append(_project_prediction(record, prediction, fixture_rows=fixture_rows))
        except Exception:
            # A malformed archive row must be visible as a fixture-level block,
            # never as a partially populated prediction or a silent omission.
            projection_blocked.add(fixture_id)
    projected.sort(key=lambda item: str(item.get("kickoff_at") or ""))
    blocked = [
        blocked_fixture_record(
            upcoming[fixture_id],
            reason=(
                "invalid_causal_freeze_record"
                if fixture_id in projection_blocked
                else "no_causal_freeze_record"
            ),
            message=(
                "冻结归档记录无效，已阻断该场次；不生成即时草稿替代。"
                if fixture_id in projection_blocked
                else "尚未观察到满足冻结截点的当前模型归档；不生成即时草稿替代。"
            ),
        )
        for fixture_id in sorted((set(upcoming) - set(grouped)) | projection_blocked)
    ]
    return {
        "status": "research_only",
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "prediction_source": "prospective_archive",
        "active_model_version_sha256": lock.get("model_version_sha256"),
        "predictions": projected,
        "blocked": blocked,
        "coverage": prediction_coverage(candidates, projected, blocked),
        "archive": {
            "path": str(archive_path),
            "lock_path": str(lock_path),
            "rows_considered": len(rows),
            "fixtures_with_current_freeze": len(projected),
            "fixtures_waiting_for_freeze": len(blocked),
        },
        "message": (
            "只展示当前模型锁下已经按冻结截点写入、且未发生冲突的最新阶段；"
            "其余比赛等待合法冻结，不用即时快照冒充前瞻预测。"
        ),
    }


def build_stale_research_predictions(
    snapshot: Mapping[str, Any],
    *,
    archive_path: Path,
    lock_path: Path,
    now: datetime | None = None,
    prediction_horizon: timedelta | None = PUBLIC_PREDICTION_HORIZON,
) -> dict[str, Any]:
    """Keep the last causal research rows visible when the live source is stale.

    This is deliberately a research-only fallback. It never populates the
    public frozen ``predictions`` lane, never treats an old fixture status as
    current, and removes rows whose kickoff has already passed the reference
    clock. A source outage should not erase useful pre-match analysis from the
    workbench, but the stale boundary must be explicit for every consumer.
    """

    current = snapshot.get("current_data") if isinstance(snapshot.get("current_data"), Mapping) else {}
    try:
        horizon = _validated_horizon(prediction_horizon)
    except ValueError:
        candidates = _upcoming_candidates(snapshot, as_of=now, horizon=None)
        blocked = _blocked_candidates(
            candidates,
            reason="stale_research_invalid_horizon",
            message="研究预测窗口无效，该场次没有可审计的研究记录。",
        )
        return {
            "status": "unavailable",
            "predictions": [],
            "blocked": blocked,
            "coverage": prediction_coverage(candidates, [], blocked),
            "message": "研究预测窗口无效，研究预测保持阻断。",
        }
    if current.get("status") != "stale":
        return {
            "status": "unavailable",
            "predictions": [],
            "blocked": [],
            "coverage": prediction_coverage([], [], []),
            "message": "仅在当前源快照过期时启用最后有效研究快照。",
        }
    snapshot_as_of = _timestamp(current.get("as_of") or snapshot.get("generated_at"))
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)
    lock = _read_json_object(lock_path)
    if snapshot_as_of is None or lock.get("status") not in {"pending_prospective_window", "passed"}:
        candidates = _upcoming_candidates(snapshot, as_of=reference, horizon=horizon)
        blocked = _blocked_candidates(
            candidates,
            reason="stale_research_snapshot_or_lock_unavailable",
            message="过期快照或当前模型锁不可验证，该场次没有可审计的研究记录。",
        )
        return {
            "status": "unavailable",
            "predictions": [],
            "blocked": blocked,
            "coverage": prediction_coverage(candidates, [], blocked),
            "message": "过期快照或当前模型锁不可验证，研究预测保持阻断。",
        }

    fixture_rows = _upcoming_candidates(snapshot, as_of=reference, horizon=horizon)
    fixture_ids = {str(item["id"]) for item in fixture_rows}
    rows = _load_archive_rows(
        archive_path,
        lock=lock,
        # Unlike the normal public projection, this fallback is explicitly
        # allowed to use archive rows observed after the stale snapshot.  The
        # rows are still bounded by ``reference`` below, so no future
        # observation can enter the research lane.
        as_of=reference,
        fixture_ids=fixture_ids,
    )
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for record, prediction in rows:
        grouped[str(prediction["fixture_id"])].append((record, prediction))

    projected: list[dict[str, Any]] = []
    projection_blocked: set[str] = set()
    for _, fixture_rows_for_prediction in grouped.items():
        fixture_rows_for_prediction.sort(
            key=lambda item: _STAGE_RANK.get(str(item[1].get("freeze_stage")), -1)
        )
        record, prediction = fixture_rows_for_prediction[-1]
        kickoff = _timestamp(prediction.get("kickoff_at"))
        if kickoff is None or kickoff <= reference:
            projection_blocked.add(str(prediction.get("fixture_id")))
            continue
        try:
            item = _project_prediction(
                record,
                prediction,
                fixture_rows=fixture_rows_for_prediction,
            )
        except Exception:
            projection_blocked.add(str(prediction.get("fixture_id")))
            continue
        item.update(
            {
                "research_snapshot_status": "stale_snapshot",
                "stale_snapshot": True,
                "stale_snapshot_as_of": snapshot_as_of.isoformat().replace("+00:00", "Z"),
                "stale_snapshot_reference_at": reference.isoformat().replace("+00:00", "Z"),
                "stale_snapshot_reason": "current_source_snapshot_expired",
            }
        )
        projected.append(item)
    projected.sort(key=lambda item: str(item.get("kickoff_at") or ""))

    # ``fixture_rows`` is already the bounded future window.  Keep rows with a
    # date-only or otherwise unresolved kickoff in the denominator so they get
    # an explicit block rather than disappearing from stale research coverage.
    future_fixture_ids = {
        str(item["id"])
        for item in fixture_rows
        if isinstance(item.get("id"), str) and item.get("id").strip()
    }
    projected_ids = {str(item["fixture_id"]) for item in projected}
    blocked = [
        blocked_fixture_record(
            next(item for item in fixture_rows if str(item["id"]) == fixture_id),
            reason="stale_snapshot_no_causal_archive",
            message="当前源已过期，且最后有效快照没有可复用的冻结研究记录。",
        )
        for fixture_id in sorted((future_fixture_ids - projected_ids) | projection_blocked)
    ]
    return {
        "status": "stale_snapshot",
        "as_of": snapshot_as_of.isoformat().replace("+00:00", "Z"),
        "reference_at": reference.isoformat().replace("+00:00", "Z"),
        "prediction_source": "prospective_archive_stale_snapshot",
        "active_model_version_sha256": lock.get("model_version_sha256"),
        "predictions": projected,
        "blocked": blocked,
        "coverage": prediction_coverage(fixture_rows, projected, blocked),
        "archive": {
            "path": str(archive_path),
            "lock_path": str(lock_path),
            "rows_considered": len(rows),
            "fixtures_with_stale_research": len(projected),
            "fixtures_waiting_for_current_source": len(blocked),
        },
        "message": (
            "当前公开源快照已过期；保留最后一次合法冻结记录作为研究工作台参考，"
            "不进入生产预测、不代表实时状态或投注优势。"
        ),
    }


__all__ = ["build_public_predictions", "build_stale_research_predictions"]
