"""Public Sports Lottery football feed adapter.

This adapter uses one fixed HTTPS endpoint from the existing project. It does
not rotate identities, bypass challenges, or follow redirects. The default
opener uses a direct connection for this allowlisted public host because the
workstation proxy currently returns a non-HTTP 567 response; this is a
transport choice, not an access-control or challenge bypass.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from league_platform.source_rights import SourceId, rights_blocked_envelope


SPORTS_LOTTERY_HOST = "webapi.sporttery.cn"
SPORTS_LOTTERY_URL = (
    "https://webapi.sporttery.cn/gateway/jc/football/"
    "getMatchCalculatorV1.qry?channel=c&poolCode=hhad,had,crs,ttg,hafu"
)
SPORTS_LOTTERY_FALLBACK_URL = (
    "https://webapi.sporttery.cn/gateway/uniform/football/"
    "getMatchCalculatorV1.qry?channel=c&poolCode=hhad,had,crs,ttg,hafu"
)
SPORTS_LOTTERY_ALLOWED_PATHS = {
    "/gateway/jc/football/getMatchCalculatorV1.qry",
    "/gateway/uniform/football/getMatchCalculatorV1.qry",
}
MAX_CONTENT_BYTES = 10 * 1024 * 1024




def _devig(odds: dict[str, float]) -> dict[str, float]:
    inverse = {key: 1 / value for key, value in odds.items() if value > 0 and math.isfinite(value)}
    total = sum(inverse.values())
    if not inverse or total <= 0:
        return {}
    return {key: round(value / total, 8) for key, value in inverse.items()}


def _pool_odds(pool: dict, keys: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key in keys:
        try:
            value = float(pool.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            result[key] = value
    return result


def _kickoff(match: dict, fallback_date: str) -> str | None:
    date = str(match.get("matchDate") or fallback_date or "").strip()
    time = str(match.get("matchTime") or "").strip()
    if not date or not time:
        return None
    try:
        # Sports Lottery publishes local China time.  Treating this as UTC
        # shifts the fixture by eight hours and can place it in the wrong
        # freeze window, so convert explicitly to UTC here.
        parsed = (
            datetime.fromisoformat(f"{date} {time}")
            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            .astimezone(timezone.utc)
        )
    except ValueError:
        return None
    return parsed.isoformat()


def _source(*, retrieved_at: datetime, payload: bytes, url: str) -> dict:
    return {
        "name": "Sports Lottery",
        "url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }




def parse_sports_lottery_payload(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str = SPORTS_LOTTERY_URL,
) -> list[dict]:
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != SPORTS_LOTTERY_HOST
        or parsed_url.path not in SPORTS_LOTTERY_ALLOWED_PATHS
    ):
        raise ValueError("Sports Lottery URL is not allowlisted")
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware")
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("Sports Lottery response exceeded 10 MiB")
    data = json.loads(payload)
    days = data.get("value", {}).get("matchInfoList", []) if isinstance(data, dict) else []
    if not isinstance(days, list):
        raise ValueError("Sports Lottery payload has no matchInfoList")
    source = _source(
        retrieved_at=retrieved_at.astimezone(timezone.utc),
        payload=payload,
        url=url,
    )
    result: list[dict] = []
    for day in days:
        if not isinstance(day, dict):
            continue
        fallback_date = str(day.get("matchDate") or "")
        for match in day.get("subMatchList", []):
            if not isinstance(match, dict):
                continue
            kickoff = _kickoff(match, fallback_date)
            if not kickoff:
                continue
            had = _pool_odds(match.get("had") or {}, ["h", "d", "a"])
            hhad = _pool_odds(match.get("hhad") or {}, ["h", "d", "a"])
            ttg = _pool_odds(match.get("ttg") or {}, [f"s{i}" for i in range(8)])
            hafu = _pool_odds(match.get("hafu") or {}, ["hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa"])
            crs = _pool_odds(match.get("crs") or {}, ["s00", "s10", "s20", "s01", "s02", "s11", "s21", "s12", "s22"])
            result.append({
                "match_id": str(match.get("matchId") or ""),
                "match_num": str(match.get("matchNumStr") or ""),
                "league": str(match.get("leagueAbbName") or ""),
                "home_team": str(match.get("homeTeamAllName") or ""),
                "away_team": str(match.get("awayTeamAllName") or ""),
                "home_team_en": str(match.get("homeTeamAbbEnName") or ""),
                "away_team_en": str(match.get("awayTeamAbbEnName") or ""),
                "kickoff_at": kickoff,
                "had_odds": had,
                "had_probability": _devig(had),
                "hhad_line": (match.get("hhad") or {}).get("goalLineValue"),
                "hhad_odds": hhad,
                "hhad_probability": _devig(hhad),
                "ttg_odds": ttg,
                "ttg_probability": _devig(ttg),
                "hafu_odds": hafu,
                "hafu_probability": _devig(hafu),
                "crs_odds": crs,
                "crs_probability": _devig(crs),
                "source": source,
            })
    return result


def fetch_sports_lottery(*, now: datetime | None = None, opener=None) -> dict:
    """Return the v260 rights block before selecting an endpoint or opener."""

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    return rights_blocked_envelope(
        SourceId.SPORTS_LOTTERY_OFFICIAL,
        provider="Sports Lottery",
        checked_at=reference.isoformat(),
        empty_fields=("matches", "fallbacks"),
    )


__all__ = [
    "SPORTS_LOTTERY_FALLBACK_URL",
    "SPORTS_LOTTERY_URL",
    "fetch_sports_lottery",
    "parse_sports_lottery_payload",
]
