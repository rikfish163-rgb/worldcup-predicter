"""Read-only query service over a validated platform snapshot."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any

from league_platform.catalog import LEAGUES
from league_platform.fixture_feed import fixture_rows
from league_platform.snapshot import build_platform_snapshot
from league_platform.current import attach_current_data
from league_platform.future import DEFAULT_PREDICTION_HORIZON, build_future_predictions
from league_platform.public_predictions import (
    build_public_predictions,
    build_stale_research_predictions,
)


MATCH_STATUSES = {"upcoming", "live", "finished", "postponed", "cancelled"}

_PROSPECTIVE_REQUIRED_LEAGUES = tuple(league.id for league in LEAGUES)
_PROSPECTIVE_TARGET_THRESHOLDS = {
    "three_way": 1000,
    "total_goals": 1000,
    "half_full": 1000,
    "scoreline": 5000,
}


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse an aware timestamp for diagnostics without failing the store."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _validated_ci(value: object) -> dict[str, float] | None:
    """Expose only finite, ordered bootstrap intervals in the D1/API view."""

    if not isinstance(value, dict):
        return None
    try:
        mean = float(value["mean"])
        lower = float(value["lower"])
        upper = float(value["upper"])
        level = float(value["level"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (mean, lower, upper, level)):
        return None
    if not 0.0 < level <= 1.0 or lower > mean or mean > upper:
        return None
    return {
        "mean": round(mean, 8),
        "lower": round(lower, 8),
        "upper": round(upper, 8),
        "level": round(level, 6),
    }


class PlatformStore:
    """Keep one immutable source snapshot and expose bounded UI queries."""

    def __init__(
        self,
        data_dir: Path,
        *,
        now: datetime | None = None,
        live_path: Path | None = None,
        strict_report_path: Path | None = None,
        prospective_lock_path: Path | None = None,
        evidence_dir: Path | None = None,
        runtime_evidence_dir: Path | None = None,
        openfootball_raw_archive_dir: Path | None = None,
    ):
        self._data_dir = data_dir
        self._base_snapshot = build_platform_snapshot(data_dir, now=now)
        self._live_path = live_path
        self._clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
        self._openfootball_raw_archive_dir = openfootball_raw_archive_dir
        self._evidence_dir = evidence_dir or data_dir.parent.parent / "docs" / "evidence"
        self._runtime_evidence_dir = runtime_evidence_dir or self._evidence_dir
        self._prospective_lock_path = prospective_lock_path or (
            self._evidence_dir / "prospective-model-lock-current.json"
        )
        self._prospective_lock = self._load_json_object(
            self._prospective_lock_path
        )
        # Use the latest strict-report artifact that carries prospective-lock
        # identity.  Older expanded reports predate lock binding and must not
        # silently appear as current research evidence.
        default_strict_report = self._runtime_evidence_dir / "strict-backtest-current.json"
        self._strict_report_path = strict_report_path or default_strict_report
        self._strict_report = self._load_strict_report(self._strict_report_path)

    def _prospective_status(self) -> dict:
        """Expose compact, read-only prospective gate evidence to Sites/API."""

        def read_path(path: Path) -> dict[str, Any]:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return value if isinstance(value, dict) else {}

        def read_pointer(
            root: Path, name: str, runtime_only_name: str
        ) -> tuple[dict[str, Any], Path]:
            """Read runtime evidence without hiding a present corrupt pointer.

            Runtime-only cycles deliberately use a separate filename so they
            cannot be mistaken for the ordinary publication lane. The
            read-only workbench may consume that evidence when the standard
            pointer is absent. When both pointers are valid objects, the
            newer timestamp wins so a failed runtime-only attempt cannot be
            hidden behind an older pending pointer. A malformed standard
            pointer still wins, preserving fail-closed corruption handling.
            """

            primary = root / name
            secondary = root / runtime_only_name
            if not primary.exists():
                return read_path(secondary), secondary
            primary_value = read_path(primary)
            if not secondary.exists():
                return primary_value, primary
            secondary_value = read_path(secondary)
            if not primary_value or not secondary_value:
                return primary_value, primary

            def pointer_time(value: dict[str, Any]) -> datetime | None:
                for key in (
                    "finished_at",
                    "generated_at",
                    "checked_at",
                    "as_of",
                    "updated_at",
                ):
                    timestamp = _parse_utc_timestamp(value.get(key))
                    if timestamp is not None:
                        return timestamp
                return None

            primary_time = pointer_time(primary_value)
            secondary_time = pointer_time(secondary_value)
            if (
                primary_time is not None
                and secondary_time is not None
                and secondary_time > primary_time
            ):
                return secondary_value, secondary
            return primary_value, primary

        evaluation, evaluation_path = read_pointer(
            self._runtime_evidence_dir,
            "prospective-evaluation-current.json",
            "runtime-only-evaluation-current.json",
        )
        try:
            lock = json.loads(self._prospective_lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lock = {}
        if not isinstance(lock, dict):
            lock = {}
        cycle, cycle_path = read_pointer(
            self._runtime_evidence_dir,
            "prospective-cycle-latest.json",
            "runtime-only-cycle-latest.json",
        )
        selection = read_path(self._evidence_dir / "model-selection-audit-current.json")
        evaluation_evidence = str(evaluation_path)
        cycle_evidence = str(cycle_path)
        raw_capture = cycle.get("capture")
        capture: dict[str, Any] = raw_capture if isinstance(raw_capture, dict) else {}
        raw_diagnostics = capture.get("blocked_diagnostics")
        diagnostics: dict[str, Any] = (
            raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
        )
        next_freezes: list[dict[str, Any]] = []
        for item in diagnostics.get("items", []):
            if not isinstance(item, dict):
                continue
            required = (
                "competition_id",
                "next_fixture_id",
                "next_kickoff_at",
                "next_freeze_stage",
                "next_freeze_cutoff_at",
            )
            if not all(item.get(key) for key in required):
                continue
            next_freezes.append({key: item[key] for key in required})

        # The polling service is intentionally not a training loop.  Keep a
        # small, explicit progress ledger beside the publication gate so the
        # UI can explain whether it is waiting for real results, waiting for a
        # causal freeze cutoff, or merely skipping an already archived key.
        def capture_count(key: str, default: int = 0) -> int:
            value = _finite_int(capture.get(key))
            return value if value is not None else default
        scored_n = _finite_int(evaluation.get("scored_n")) or 0
        pending_n = _finite_int(evaluation.get("pending_n")) or 0
        result_conflicts = _finite_int(evaluation.get("result_conflicts")) or 0
        predictions_generated = capture_count("predictions")
        predictions_appended = capture_count("appended")
        duplicates_skipped = capture_count("skipped_duplicate")
        blocked_n = capture_count("blocked")

        raw_cycle_sync = cycle.get("sync")
        cycle_sync: dict[str, Any] = (
            raw_cycle_sync if isinstance(raw_cycle_sync, dict) else {}
        )
        cycle_status = cycle.get("status", "unavailable")
        cycle_failed = cycle_status in {
            "failed",
            "interrupted",
            "blocked",
            "blocked_storage",
        }
        raw_cycle_failure = cycle.get("failure")
        cycle_failure: dict[str, str] = {}
        if isinstance(raw_cycle_failure, dict):
            for key in ("stage", "type", "message"):
                value = raw_cycle_failure.get(key)
                if isinstance(value, str) and value.strip():
                    cycle_failure[key] = value.strip()[:500]
        cycle_started_at = cycle.get("started_at")
        cycle_finished_at = cycle.get("finished_at")
        cycle_started = _parse_utc_timestamp(cycle_started_at)
        cycle_finished = _parse_utc_timestamp(cycle_finished_at)
        reference = self._clock() or datetime.now(timezone.utc)
        if reference.tzinfo is None or reference.utcoffset() is None:
            reference = reference.replace(tzinfo=timezone.utc)
        else:
            reference = reference.astimezone(timezone.utc)
        cycle_age_seconds = (
            max(0, int((reference - cycle_finished).total_seconds()))
            if cycle_finished is not None
            else None
        )
        cycle_duration_seconds = (
            max(0, int((cycle_finished - cycle_started).total_seconds()))
            if cycle_started is not None and cycle_finished is not None
            else None
        )
        window_started_at = lock.get("evaluation_window_started_at")
        window_started = _parse_utc_timestamp(window_started_at)
        window_elapsed_seconds = (
            max(0, int((reference - window_started).total_seconds()))
            if window_started is not None
            else None
        )

        target_metrics = evaluation.get("target_metrics")
        target_metrics = target_metrics if isinstance(target_metrics, dict) else {}
        league_target_counts = evaluation.get("league_target_counts")
        league_target_counts = league_target_counts if isinstance(league_target_counts, dict) else {}

        def target_count(target: str) -> int:
            metric = target_metrics.get(target)
            if isinstance(metric, dict) and _finite_int(metric.get("sample_n")) is not None:
                return max(0, _finite_int(metric.get("sample_n")) or 0)
            return sum(
                max(
                    0,
                    _finite_int((counts or {}).get(target)) or 0,
                )
                for counts in league_target_counts.values()
                if isinstance(counts, dict)
            )

        requirements = {
            "per_league": {
                league: {
                    target: {
                        "current_n": max(
                            0,
                            _finite_int(
                                (league_target_counts.get(league) or {}).get(target)
                                if isinstance(league_target_counts.get(league), dict)
                                else None
                            )
                            or 0,
                        ),
                        "required_n": _PROSPECTIVE_TARGET_THRESHOLDS[target],
                    }
                    for target in ("three_way", "total_goals", "half_full")
                }
                for league in _PROSPECTIVE_REQUIRED_LEAGUES
            },
            "scoreline": {
                "current_n": target_count("scoreline"),
                "required_n": _PROSPECTIVE_TARGET_THRESHOLDS["scoreline"],
            },
        }

        next_freezes.sort(
            key=lambda item: (
                _parse_utc_timestamp(item.get("next_freeze_cutoff_at"))
                or datetime.max.replace(tzinfo=timezone.utc),
                str(item.get("next_fixture_id")),
            )
        )
        diagnostic_source = "cycle_capture"
        if not next_freezes and self._live_path is not None:
            # A transient empty source response can replace the cycle receipt
            # while the atomic live pointer correctly remains on the last
            # valid snapshot. Recompute the next clock opportunity from that
            # preserved pointer so the workbench does not lose its operator
            # guidance merely because the latest poll was blocked.
            try:
                preserved = json.loads(self._live_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                preserved = {}
            fixtures = fixture_rows(preserved) if isinstance(preserved, dict) else []
            fallback_by_competition: dict[str, dict[str, str]] = {}
            freeze_offsets = (
                ("t_minus_24h", 24 * 60 * 60),
                ("t_minus_6h", 6 * 60 * 60),
                ("t_minus_90m", 90 * 60),
            )
            if isinstance(fixtures, list):
                for fixture in fixtures:
                    if not isinstance(fixture, dict) or fixture.get("status") != "upcoming":
                        continue
                    competition_id = fixture.get("competition_id")
                    fixture_id = fixture.get("id")
                    kickoff = _parse_utc_timestamp(fixture.get("kickoff_at"))
                    if (
                        not isinstance(competition_id, str)
                        or not isinstance(fixture_id, str)
                        or kickoff is None
                        or kickoff <= reference
                    ):
                        continue
                    candidates = []
                    for stage, seconds in freeze_offsets:
                        cutoff = kickoff - timedelta(seconds=seconds)
                        if reference <= cutoff < kickoff:
                            candidates.append((cutoff, stage))
                    if not candidates:
                        continue
                    cutoff, stage = min(candidates, key=lambda item: item[0])
                    candidate = {
                        "competition_id": competition_id,
                        "next_fixture_id": fixture_id,
                        "next_kickoff_at": kickoff.isoformat(),
                        "next_freeze_stage": stage,
                        "next_freeze_cutoff_at": cutoff.isoformat(),
                    }
                    previous = fallback_by_competition.get(competition_id)
                    previous_cutoff = (
                        _parse_utc_timestamp(previous["next_freeze_cutoff_at"])
                        if previous is not None
                        else None
                    )
                    if previous_cutoff is None or cutoff < previous_cutoff:
                        fallback_by_competition[competition_id] = candidate
            if fallback_by_competition:
                next_freezes = list(fallback_by_competition.values())
                diagnostic_source = "last_valid_live_snapshot"
        next_freeze = next_freezes[0] if next_freezes else None

        strict_current = self._strict_report_lock_status().get("status") == "current"
        strict_overall = (
            self._strict_report.get("overall")
            if strict_current and isinstance(self._strict_report, dict)
            else {}
        )
        strict_overall = strict_overall if isinstance(strict_overall, dict) else {}
        selection_gate_status = selection.get("gate_status", "unavailable")

        reasons: list[dict[str, object]] = []
        if cycle_failed:
            reasons.append({
                "code": "prospective_cycle_failed",
                "count": None,
                "blocks_release": True,
                "evidence": f"{cycle_evidence}:failure",
            })
        if pending_n > 0:
            reasons.append({
                "code": "awaiting_verified_results",
                "count": pending_n,
                "blocks_release": True,
                "evidence": evaluation_evidence,
            })
        raw_publication = capture.get("publication")
        publication: dict[str, Any] = (
            raw_publication if isinstance(raw_publication, dict) else {}
        )
        if publication.get("status") == "blocked":
            reasons.append({
                "code": "live_snapshot_publication_blocked",
                "count": None,
                "blocks_release": True,
                "evidence": f"{cycle_evidence}:capture.publication",
            })
        if blocked_n > 0:
            reasons.append({
                "code": "awaiting_causal_freeze_cutoff",
                "count": blocked_n,
                "blocks_release": True,
                "evidence": f"{cycle_evidence}:capture.blocked_diagnostics",
            })
        # A cycle may append a newly eligible freeze and skip older lock keys
        # in the same pass.  The skipped keys are still an observable
        # idempotency diagnostic; hiding them whenever ``appended`` is non-zero
        # makes the progress contract depend on batch ordering.
        if duplicates_skipped > 0:
            reasons.append({
                "code": "idempotent_duplicate_lock_keys",
                "count": duplicates_skipped,
                "blocks_release": False,
                "evidence": f"{cycle_evidence}:capture.skipped_duplicate",
            })
        if evaluation.get("sample_requirements_met") is not True:
            reasons.append({
                "code": "independent_sample_below_gate",
                "count": scored_n,
                "blocks_release": True,
                "evidence": f"{evaluation_evidence}:sample_requirements_met",
            })
        legacy_model_records = _finite_int(evaluation.get("legacy_model_records")) or 0
        if legacy_model_records > 0:
            reasons.append({
                "code": "prospective_window_restarted_by_lock_migration",
                "count": legacy_model_records,
                "blocks_release": True,
                "evidence": f"{evaluation_evidence}:legacy_model_records",
            })
        if strict_overall.get("market_gate") not in (None, "pass"):
            reasons.append({
                "code": "market_gate_not_passed",
                "count": None,
                "blocks_release": True,
                "evidence": f"{self._strict_report_path}:overall.market_gate",
            })
        if selection_gate_status not in (None, "passed", "pass"):
            reasons.append({
                "code": "model_selection_gate_blocked",
                "count": None,
                "blocks_release": True,
                "evidence": (
                    f"{self._evidence_dir / 'model-selection-audit-current.json'}:gate_status"
                ),
            })
        if result_conflicts > 0:
            reasons.append({
                "code": "result_conflicts_quarantined",
                "count": result_conflicts,
                "blocks_release": True,
                "evidence": f"{evaluation_evidence}:result_conflicts",
            })

        if cycle_failed:
            progress_state = "cycle_failed"
        elif evaluation.get("status") == "passed":
            progress_state = "passed"
        elif result_conflicts > 0:
            progress_state = "blocked_by_result_conflict"
        elif scored_n == 0 and pending_n > 0:
            progress_state = "awaiting_first_verified_result"
        elif pending_n > 0:
            progress_state = "awaiting_verified_results"
        elif blocked_n > 0:
            progress_state = "awaiting_causal_freeze_cutoff"
        elif predictions_appended > 0:
            progress_state = "capturing"
        else:
            progress_state = "no_growth_observed"

        progress = {
            "schema_version": "matchline.prospective_progress.v1",
            "state": progress_state,
            "phase": "prospective_collection",
            "explanation": (
                "模型参数在独立前瞻窗口内保持冻结；轮询只负责捕获满足时间边界的预测并等待真实赛果，"
                "不会把四天的重复轮询当成四天的训练收敛。"
            ),
            "model": {
                "state": "frozen",
                "parameters_mutable": False,
                "model_version_sha256": lock.get("model_version_sha256"),
                "reason": "results_not_used_for_selection",
            },
            "window": {
                "started_at": window_started_at,
                "elapsed_seconds": window_elapsed_seconds,
                "legacy_model_records": legacy_model_records,
                "status": lock.get("status", "unavailable"),
            },
            "last_cycle": {
                "status": cycle_status,
                "started_at": cycle_started_at,
                "finished_at": cycle_finished_at,
                "as_of": cycle_sync.get("as_of"),
                "age_seconds": cycle_age_seconds,
                "duration_seconds": cycle_duration_seconds,
                "snapshot_sha256": cycle_sync.get("snapshot_sha256"),
                "failure": cycle_failure or None,
            },
            "capture": {
                "upcoming_fixtures": capture_count("upcoming_fixtures"),
                "predictions_generated": predictions_generated,
                "predictions_appended": predictions_appended,
                "duplicates_skipped": duplicates_skipped,
                "blocked": blocked_n,
                "lineup_observed": capture_count("lineup_observed"),
            },
            "evaluation": {
                "scored_n": scored_n,
                "pending_n": pending_n,
                "result_conflicts": result_conflicts,
                "sample_requirements_met": evaluation.get("sample_requirements_met") is True,
                "all_required_targets_scored": evaluation.get("all_required_targets_scored") is True,
            },
            "requirements": requirements,
            "next_freeze": next_freeze,
            "reasons": reasons,
        }
        return {
            "status": evaluation.get("status", "unavailable"),
            "scored_n": evaluation.get("scored_n", 0),
            "pending_n": evaluation.get("pending_n", 0),
            "result_conflicts": evaluation.get("result_conflicts", 0),
            "sample_requirements_met": evaluation.get("sample_requirements_met", False),
            "all_required_targets_scored": evaluation.get("all_required_targets_scored", False),
            "prediction_freezes_verified": evaluation.get("prediction_freezes_verified", False),
            "model_version_sha256": lock.get("model_version_sha256"),
            "locked_at": lock.get("locked_at"),
            "evaluation_window_started_at": lock.get("evaluation_window_started_at"),
            "cycle_status": cycle_status,
            "cycle_as_of": (cycle.get("sync") or {}).get("as_of"),
            "blocked_diagnostics": {
                "count": diagnostics.get("count", len(next_freezes)),
                "by_reason": diagnostics.get("by_reason", {}),
                "next_freezes": next_freezes,
                "source": diagnostic_source,
            },
            "selection_gate_status": selection.get("gate_status", "unavailable"),
            "selection_production_eligible": selection.get("production_eligible") is True,
            "evidence_sources": {
                "model_root": str(self._evidence_dir),
                "runtime_root": str(self._runtime_evidence_dir),
            },
            "progress": progress,
        }

    def _current_view(self) -> tuple[dict, dict]:
        observed_before = self._clock()
        snapshot = deepcopy(self._base_snapshot)
        self._attach_strict_report(snapshot)
        if self._live_path is not None:
            snapshot = attach_current_data(snapshot, self._live_path, now=observed_before)
        snapshot["prospective_evaluation"] = self._prospective_status()
        return snapshot, build_future_predictions(
            snapshot,
            openfootball_raw_archive_dir=self._openfootball_raw_archive_dir,
            observed_before=observed_before,
            prediction_horizon=DEFAULT_PREDICTION_HORIZON,
        )

    @staticmethod
    def _load_strict_report(path: Path) -> dict | None:
        """Load an optional generated strict report without making it required."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and isinstance(payload.get("leagues"), dict) else None

    @staticmethod
    def _load_json_object(path: Path) -> dict | None:
        """Read an optional evidence object without making the store fail open."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _strict_report_lock_status(self) -> dict[str, object]:
        """Return whether the strict report belongs to the current lock.

        A strict report is a derived artifact.  If a pre-result lock migration
        changes its identity, showing the old report beside the new lock would
        make the UI look current while maturity validation correctly rejects
        it.  Fail closed and expose the mismatch as audit metadata instead.
        """

        expected = self._prospective_lock
        if expected is None:
            return {"status": "unavailable", "reason": "current_prospective_lock_missing"}
        if self._strict_report is None:
            return {"status": "unavailable", "reason": "strict_report_missing"}
        selection = self._strict_report.get("model_selection_audit")
        observed = selection.get("prospective_lock") if isinstance(selection, dict) else None
        if not isinstance(observed, dict):
            return {"status": "stale", "reason": "strict_report_prospective_lock_missing"}
        fields = (
            "status",
            "model_version_sha256",
            "freeze_model_name",
            "locked_at",
            "evaluation_window_started_at",
            "evaluation_window",
        )
        mismatches = [field for field in fields if observed.get(field) != expected.get(field)]
        if mismatches:
            return {
                "status": "stale",
                "reason": "strict_report_prospective_lock_mismatch",
                "mismatches": mismatches,
                "expected_lock": {
                    field: expected.get(field)
                    for field in fields
                    if expected.get(field) is not None
                },
                "observed_lock": {
                    field: observed.get(field)
                    for field in fields
                    if observed.get(field) is not None
                },
            }
        return {"status": "current"}

    @staticmethod
    def _strict_metric(value: dict) -> dict:
        return {
            "sample_n": value.get("sample_n", 0),
            "brier_score": value.get("brier"),
            "log_loss": value.get("log_loss"),
            "rps": value.get("rps"),
            "ece": value.get("ece"),
        }

    def _attach_strict_report(self, snapshot: dict) -> None:
        """Attach only a cutoff-matching strict report to API/UI payloads."""

        if self._strict_report is None:
            return
        lock_status = self._strict_report_lock_status()
        if lock_status.get("status") != "current":
            # Keep the current simple model health visible, but never expose a
            # stale strict report as if it were evaluated under the active
            # prospective lock.  This marker is safe for both Sites and the
            # offline bundle and gives operators an actionable refresh reason.
            snapshot["strict_backtest"] = {
                "status": lock_status.get("status", "unavailable"),
                "report_generated_at": self._strict_report.get("generated_at"),
                "audit": lock_status,
            }
            return
        report_leagues = self._strict_report["leagues"]
        by_id = {item["id"]: item for item in snapshot.get("competitions", [])}
        unknown_leagues = set(report_leagues) - set(by_id)
        if unknown_leagues:
            return
        missing_leagues = sorted(set(by_id) - set(report_leagues))
        unavailable_leagues: list[str] = []
        cutoff_mismatches: list[str] = []
        for league_id, report in report_leagues.items():
            report_cutoff = _parse_utc_timestamp(report.get("data_cutoff"))
            snapshot_cutoff = _parse_utc_timestamp(
                by_id[league_id]["model_health"].get("data_cutoff")
            )
            if report_cutoff is None:
                unavailable_leagues.append(league_id)
                continue
            # The strict artifact carries its own causal data cutoff.  The
            # catalog's lightweight model-health row can legitimately come
            # from a different snapshot (and may use a local timezone), so a
            # mismatch is an audit note rather than a reason to discard an
            # otherwise lock-bound report.  Never infer the report cutoff
            # from the catalog row.
            if snapshot_cutoff is None or report_cutoff != snapshot_cutoff:
                cutoff_mismatches.append(league_id)
        for league_id, report in report_leagues.items():
            if league_id in unavailable_leagues:
                continue
            three_way = report["three_way"]
            model = three_way.get("model_on_market_sample") or three_way["model"]
            full_model = three_way["model"]
            historical = three_way.get("historical_frequency_on_market_sample") or three_way["historical_frequency"]
            full_historical = three_way["historical_frequency"]
            market = three_way.get("market")
            gate = report["gates"]["three_way"]
            by_id[league_id]["strict_model_health"] = {
                "status": "evaluated",
                "model": report["model"],
                "sample_n": model["sample_n"],
                "full_sample_n": report["sample_n"],
                "evaluation_season": "扩展逐季",
                "held_out_seasons": report["held_out_seasons"],
                "warmup_seasons": report["warmup_seasons"],
                "data_cutoff": report["data_cutoff"],
                "quality_gate": gate["status"],
                "selected_candidate": report["model"],
                "selected_metrics": self._strict_metric(model),
                "full_metrics": self._strict_metric(full_model),
                "baselines": {
                    "historical_frequency": self._strict_metric(historical),
                    "full_historical_frequency": self._strict_metric(full_historical),
                    "market": self._strict_metric(market) if market else None,
                },
                "market_comparison": {
                    "brier_delta_ci": _validated_ci(
                        three_way.get("model_minus_market_brier_ci")
                    ),
                    "model_brier_ci": _validated_ci(three_way.get("model_brier_ci")),
                    "market_brier_ci": _validated_ci(three_way.get("market_brier_ci")),
                },
                "walk_forward_folds": [
                    {
                        "fold": fold["fold"],
                        "start_at": fold["start_at"],
                        "end_at": fold["end_at"],
                        "sample_n": fold["sample_n"],
                        "brier_score": fold["three_way_brier"],
                        "log_loss": None,
                        "rps": None,
                        "ece": None,
                    }
                    for fold in report.get("walk_forward_folds", [])
                ],
                "strict_targets": report["targets"],
                "gates": report["gates"],
                "time_audit": report.get("time_audit"),
                "freeze_stages": report.get("freeze_stages", {}),
            }
        covered_leagues = sorted(
            league_id
            for league_id in report_leagues
            if league_id not in unavailable_leagues
        )
        missing_leagues = sorted(set(missing_leagues) | set(unavailable_leagues))
        snapshot["strict_backtest"] = {
            "status": "partial" if missing_leagues else "current",
            "generated_at": self._strict_report.get("generated_at"),
            "combined": self._strict_report.get("combined"),
            "overall": self._strict_report.get("overall"),
            "covered_competitions": covered_leagues,
            "missing_competitions": missing_leagues,
            "catalog_cutoff_mismatches": sorted(cutoff_mismatches),
            "time_audit": {
                league_id: report.get("time_audit")
                for league_id, report in report_leagues.items()
            },
            "freeze_stages": {
                league_id: report.get("freeze_stages", {})
                for league_id, report in report_leagues.items()
            },
        }

    def snapshot(self) -> dict:
        snapshot, _ = self._current_view()
        return snapshot

    def competitions(self) -> list[dict]:
        snapshot, _ = self._current_view()
        return deepcopy(snapshot["competitions"])

    def matches(
        self,
        *,
        competition_id: str | None = None,
        season: str | None = None,
        status: str | None = None,
        match_date: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        snapshot, _ = self._current_view()
        matches = snapshot["matches"]
        competition_ids = {item["id"] for item in snapshot["competitions"]}
        if competition_id and competition_id not in competition_ids:
            raise ValueError(f"unknown competition: {competition_id}")
        if status and status not in MATCH_STATUSES:
            raise ValueError(f"unknown status: {status}")
        if match_date:
            try:
                date.fromisoformat(match_date)
            except ValueError as exc:
                raise ValueError("date must use YYYY-MM-DD") from exc
        if competition_id:
            matches = [item for item in matches if item["competition_id"] == competition_id]
        if season:
            matches = [item for item in matches if item["season"] == season]
        if status:
            matches = [item for item in matches if item["status"] == status]
        if match_date:
            matches = [item for item in matches if item["kickoff_at"][:10] == match_date]
        if query:
            normalized = query.strip().casefold()
            matches = [
                item
                for item in matches
                if normalized in item["home_team"].casefold()
                or normalized in item["away_team"].casefold()
            ]
        total = len(matches)
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        return {
            "total": total,
            "count": min(safe_limit, max(0, total - safe_offset)),
            "offset": safe_offset,
            "limit": safe_limit,
            "matches": deepcopy(matches[safe_offset : safe_offset + safe_limit]),
        }

    def health(self) -> dict:
        snapshot, predictions = self._current_view()
        summary = snapshot["summary"]
        unavailable = summary["unavailable_competitions"]
        fresh = summary["fresh_competitions"]
        stale = summary["stale_competitions"]
        evaluated = summary["evaluated_models"]
        research_predictions = len(predictions.get("predictions", []))
        current_status = snapshot.get("current_data", {}).get("status", "unavailable")
        return {
            "status": (
                "ok"
                if unavailable == 0
                and stale == 0
                and fresh == len(snapshot["competitions"])
                and evaluated == len(snapshot["competitions"])
                and current_status == "fresh"
                else "degraded"
            ),
            "generated_at": snapshot["generated_at"],
            "sources": {
                "available": summary["available_competitions"],
                "fresh": fresh,
                "stale": stale,
                "unavailable": unavailable,
            },
            "models": {
                "evaluated": evaluated,
                "research_predictions": research_predictions,
                "gate": (
                    "research_predictions_available_production_blocked"
                    if research_predictions
                    else "historical_baselines_evaluated_current_predictions_blocked"
                    if evaluated == len(snapshot["competitions"])
                    else "blocked_until_walk_forward_validation"
                ),
            },
            "current_data": deepcopy(
                snapshot.get("current_data", {"status": "unavailable", "as_of": None})
            ),
        }

    def model_evaluations(self) -> list[dict]:
        snapshot = self.snapshot()
        return [
            {
                "competition_id": item["id"],
                **deepcopy(item.get("strict_model_health") or item["model_health"]),
            }
            for item in snapshot["competitions"]
        ]

    def predictions(self) -> dict:
        _, predictions = self._current_view()
        return predictions

    def public_predictions(self, *, include_research_drafts: bool = False) -> dict:
        """Return only causally frozen predictions suitable for Sites/API.

        ``predictions()`` remains the broader local research view for
        backwards-compatible diagnostics.  The public surface must use the
        append-only prospective archive so a current snapshot can never be
        mistaken for a stage-frozen forecast.
        """

        snapshot, drafts = self._current_view()
        archive_path = (
            self._live_path.parent / "prospective_predictions.jsonl"
            if self._live_path is not None
            else self._data_dir.parent / "live" / "prospective_predictions.jsonl"
        )
        result = build_public_predictions(
            snapshot,
            archive_path=archive_path,
            lock_path=self._prospective_lock_path,
        )
        result["research_draft_count"] = len(drafts.get("predictions", []))
        result["research_draft_status"] = drafts.get("status")
        # The static Sites bundle may show a separate research lane so that a
        # researcher can inspect causal as-of projections while the strict
        # prospective archive remains the only production/public forecast
        # lane.  Keep the default API response unchanged: callers such as the
        # legacy JSON endpoint must not receive drafts by accident.
        if include_research_drafts:
            result["research_predictions"] = deepcopy(drafts)
            raw_current = snapshot.get("current_data")
            current: dict[str, Any] = (
                raw_current if isinstance(raw_current, dict) else {}
            )
            if current.get("status") == "stale" and not result["research_predictions"].get("predictions"):
                stale_research = build_stale_research_predictions(
                    snapshot,
                    archive_path=archive_path,
                    lock_path=self._prospective_lock_path,
                    now=self._clock(),
                )
                if stale_research.get("predictions"):
                    result["research_predictions"] = stale_research
        return result
