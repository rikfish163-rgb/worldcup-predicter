"""Build the compact source-backed payload consumed by the Matchline UI."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from league_platform.catalog import LEAGUES
from league_platform.model import evaluate_league
from league_platform.sources.match_history import MatchHistorySource


def build_platform_snapshot(data_dir: Path, *, now: datetime | None = None) -> dict:
    """Return a deterministic platform snapshot from reviewed local data."""

    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    source = MatchHistorySource(data_dir, now=generated_at)

    competition_payloads = []
    match_payloads = []
    available = 0
    for league in LEAGUES:
        result = source.load(league.id)
        if result.status != "unavailable":
            available += 1
        model_health = evaluate_league(league.id, result.matches)
        if model_health["status"] == "not_evaluated":
            model_health.setdefault(
                "message", "该联赛尚未完成严格时间序列回测，不展示未经验证的命中率。"
            )
        competition_payloads.append(
            {
                "id": league.id,
                "name_zh": league.name_zh,
                "name_en": league.name_en,
                "country_zh": league.country_zh,
                "timezone": league.timezone,
                "source_status": result.status,
                "source_message": result.message,
                "latest_event_at": (
                    result.latest_event_at.isoformat() if result.latest_event_at else None
                ),
                "data_quality": result.quality,
                "model_health": model_health,
            }
        )
        match_payloads.extend(match.to_dict() for match in result.matches)

    match_payloads.sort(key=lambda match: match["kickoff_at"], reverse=True)
    return {
        "schema_version": "1.1.0",
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "summary": {
            "finished_matches": sum(item["status"] == "finished" for item in match_payloads),
            "available_competitions": available,
            "unavailable_competitions": len(LEAGUES) - available,
            "evaluated_models": sum(
                item["model_health"]["status"] == "evaluated" for item in competition_payloads
            ),
        },
        "competitions": competition_payloads,
        "matches": match_payloads,
    }
