"""Canonical competition catalog for Matchline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class League:
    """Stable league identity and source mapping."""

    id: str
    name_zh: str
    name_en: str
    country_zh: str
    timezone: str
    match_history_code: str | None
    football_data_code: str | None


LEAGUES = (
    League(
        "premier-league",
        "英超",
        "Premier League",
        "英格兰",
        "Europe/London",
        "E0",
        "PL",
    ),
    League("la-liga", "西甲", "La Liga", "西班牙", "Europe/Madrid", "SP1", "PD"),
    League(
        "bundesliga",
        "德甲",
        "Bundesliga",
        "德国",
        "Europe/Berlin",
        "D1",
        "BL1",
    ),
    League("serie-a", "意甲", "Serie A", "意大利", "Europe/Rome", "I1", "SA"),
    League("ligue-1", "法甲", "Ligue 1", "法国", "Europe/Paris", "F1", "FL1"),
    League("csl", "中超", "Chinese Super League", "中国", "Asia/Shanghai", None, None),
)

_LEAGUE_BY_ID = {league.id: league for league in LEAGUES}


def get_league(league_id: str) -> League:
    """Return a league by its stable platform identifier."""

    try:
        return _LEAGUE_BY_ID[league_id]
    except KeyError as exc:
        raise ValueError(f"Unknown league: {league_id}") from exc
