"""Read-only query service over a validated platform snapshot."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

from league_platform.snapshot import build_platform_snapshot


class PlatformStore:
    """Keep one immutable source snapshot and expose bounded UI queries."""

    def __init__(self, data_dir: Path, *, now: datetime | None = None):
        self._snapshot = build_platform_snapshot(data_dir, now=now)

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
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        matches = self._snapshot["matches"]
        if competition_id:
            matches = [item for item in matches if item["competition_id"] == competition_id]
        if season:
            matches = [item for item in matches if item["season"] == season]
        if status:
            matches = [item for item in matches if item["status"] == status]
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
        evaluated = summary["evaluated_models"]
        return {
            "status": "ok" if unavailable == 0 and evaluated > 0 else "degraded",
            "generated_at": self._snapshot["generated_at"],
            "sources": {
                "available": summary["available_competitions"],
                "unavailable": unavailable,
            },
            "models": {
                "evaluated": evaluated,
                "gate": (
                    "passed"
                    if evaluated == len(self._snapshot["competitions"])
                    else "blocked_until_walk_forward_validation"
                ),
            },
        }
