"""Read-only query service over a validated platform snapshot."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
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
        self._snapshot = build_platform_snapshot(data_dir, now=now)
        if live_path is not None:
            self._snapshot = attach_current_data(self._snapshot, live_path, now=now)
        self._predictions = build_future_predictions(self._snapshot)

    def snapshot(self) -> dict:
        return deepcopy(self._snapshot)

    def competitions(self) -> list[dict]:
        return deepcopy(self._snapshot["competitions"])

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
        matches = self._snapshot["matches"]
        competition_ids = {item["id"] for item in self._snapshot["competitions"]}
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
        summary = self._snapshot["summary"]
        unavailable = summary["unavailable_competitions"]
        fresh = summary["fresh_competitions"]
        stale = summary["stale_competitions"]
        evaluated = summary["evaluated_models"]
        research_predictions = len(self._predictions.get("predictions", []))
        return {
            "status": (
                "ok"
                if unavailable == 0
                and stale == 0
                and fresh == len(self._snapshot["competitions"])
                and evaluated == len(self._snapshot["competitions"])
                else "degraded"
            ),
            "generated_at": self._snapshot["generated_at"],
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
                    if evaluated == len(self._snapshot["competitions"])
                    else "blocked_until_walk_forward_validation"
                ),
            },
            "current_data": deepcopy(
                self._snapshot.get("current_data", {"status": "unavailable", "as_of": None})
            ),
        }

    def model_evaluations(self) -> list[dict]:
        return [
            {"competition_id": item["id"], **deepcopy(item["model_health"])}
            for item in self._snapshot["competitions"]
        ]

    def predictions(self) -> dict:
        return deepcopy(self._predictions)
