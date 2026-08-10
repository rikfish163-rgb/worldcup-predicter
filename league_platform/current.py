"""Validate and attach as-of current-source snapshots to historical state."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from league_platform.identity import team_id


def attach_current_data(
    snapshot: dict,
    live_path: Path,
    *,
    now: datetime | None = None,
    freshness_limit: timedelta = timedelta(hours=6),
) -> dict:
    payload = deepcopy(snapshot)
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if not live_path.exists():
        payload["current_data"] = {
            "status": "unavailable",
            "message": "尚未运行多源当前数据同步。",
            "as_of": None,
            "roles": {},
        }
        return payload

    live = json.loads(live_path.read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(live["as_of"])
    if as_of.tzinfo is None:
        raise ValueError("live snapshot as_of must be timezone-aware")
    if as_of > checked_at + timedelta(minutes=5):
        raise ValueError("live snapshot as_of is in the future")
    age = checked_at.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)
    status = "fresh" if age <= freshness_limit else "stale"

    features = live.get("understat", {}).get("team_features", [])
    feature_team_ids = {team_id(item["competition_id"], item["team"]): item for item in features}
    fixtures = []
    for fixture in live.get("espn", {}).get("fixtures", []):
        competition_id = fixture["competition_id"]
        home_id = team_id(competition_id, fixture["home_team"])
        away_id = team_id(competition_id, fixture["away_team"])
        fixtures.append(
            {
                "id": fixture["id"],
                "competition_id": competition_id,
                "season": fixture["season"],
                "kickoff_at": fixture["kickoff_at"],
                "home_team": fixture["home_team"],
                "away_team": fixture["away_team"],
                "home_team_id": home_id,
                "away_team_id": away_id,
                "status": fixture["status"],
                "score": fixture["score"],
                "market_probability": None,
                "current_features": {
                    "home": feature_team_ids.get(home_id),
                    "away": feature_team_ids.get(away_id),
                },
                "source": fixture["source"],
            }
        )
    ids = [fixture["id"] for fixture in fixtures]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate current fixture IDs")

    for competition in payload["competitions"]:
        competition_id = competition["id"]
        competition_fixtures = [
            fixture for fixture in fixtures if fixture["competition_id"] == competition_id
        ]
        competition["current_source_status"] = status if competition_fixtures else "unavailable"
        competition["current_fixture_count"] = len(competition_fixtures)
        competition["current_xg_team_count"] = sum(
            item["competition_id"] == competition_id for item in features
        )

    payload["matches"].extend(fixtures)
    payload["matches"].sort(key=lambda item: item["kickoff_at"], reverse=True)
    payload["summary"]["current_fixture_count"] = len(fixtures)
    payload["summary"]["current_xg_team_count"] = len(features)
    payload["current_data"] = {
        "status": status,
        "message": (
            "当前赛程快照在新鲜度窗口内。"
            if status == "fresh"
            else "当前赛程快照已过期，未来预测被阻断。"
        ),
        "as_of": as_of.isoformat(),
        "age_seconds": max(0, int(age.total_seconds())),
        "roles": live.get("roles", {}),
        "provider_errors": {
            "espn": live.get("espn", {}).get("errors", []),
            "understat": live.get("understat", {}).get("errors", []),
        },
        "fixture_count": len(fixtures),
        "xg_team_count": len(features),
    }
    return payload
