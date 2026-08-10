"""Read-only query service over a validated platform snapshot."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

from league_platform.snapshot import build_platform_snapshot
from league_platform.current import attach_current_data
from league_platform.future import build_future_predictions


MATCH_STATUSES = {"upcoming", "live", "finished", "postponed", "cancelled"}


class PlatformStore:
    """Keep one immutable source snapshot and expose bounded UI queries."""

    def __init__(
        self,
        data_dir: Path,
        *,
        now: datetime | None = None,
        live_path: Path | None = None,
    ):
        self._base_snapshot = build_platform_snapshot(data_dir, now=now)
        self._live_path = live_path
        self._clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))

    def _current_view(self) -> tuple[dict, dict]:
        snapshot = deepcopy(self._base_snapshot)
        if self._live_path is not None:
            snapshot = attach_current_data(snapshot, self._live_path, now=self._clock())
        return snapshot, build_future_predictions(snapshot)

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
        return [
            {"competition_id": item["id"], **deepcopy(item["model_health"])}
            for item in self._base_snapshot["competitions"]
        ]

    def predictions(self) -> dict:
        _, predictions = self._current_view()
        return predictions
