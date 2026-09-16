"""Read-only FotMob adapter for public pre-match schedules and lineups.

FotMob is treated as a provider-reported public source, not an authoritative
club announcement.  The adapter only uses its public JSON endpoints, keeps
the raw-response hash and observed time, and joins a provider event to a local
fixture by exact kickoff and both team names.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode, urlparse

from league_platform.source_rights import SourceId, rights_blocked_envelope


FOTMOB_HOST = "www.fotmob.com"
FOTMOB_SOURCE_NAME = "FotMob"
FOTMOB_API_ROOT = f"https://{FOTMOB_HOST}/api/data"
FOTMOB_MATCHES_PATH = "/api/data/matches"
FOTMOB_DETAILS_PATH = "/api/data/matchDetails"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_CONTENT_BYTES = 10 * 1024 * 1024
DEFAULT_HORIZON_DAYS = 16
_EVENT_ID_RE = re.compile(r"^[0-9]+$")


def _utc(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _timeout(value: float) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT_SECONDS:g} seconds")
    return result


def _validate_url(url: str, *, path: str | None = None) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != FOTMOB_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or (path is not None and parsed.path != path)
    ):
        raise ValueError("FotMob URL is not allowlisted")




def _normalise(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return "".join(character.lower() for character in text if character.isalnum())


def _source(*, url: str, retrieved_at: datetime, payload: bytes, kind: str) -> dict:
    _validate_url(url)
    return {
        "name": FOTMOB_SOURCE_NAME,
        "url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "effective_at": None,
        "time_basis": "observed_at_no_published_at",
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "source_kind": kind,
    }


def _match_time(row: dict) -> datetime:
    status = row.get("status")
    value = status.get("utcTime") if isinstance(status, dict) else row.get("utcTime")
    if not isinstance(value, str):
        raise ValueError("FotMob match has no valid UTC kickoff")
    return _utc(value, field="FotMob kickoff")


def parse_fotmob_matches_payload(payload: bytes, *, retrieved_at: datetime, url: str) -> list[dict]:
    """Parse the date schedule endpoint into provider event records."""

    _validate_url(url, path=FOTMOB_MATCHES_PATH)
    observed = _utc(retrieved_at, field="retrieved_at")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("FotMob matches response is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("leagues"), list):
        raise ValueError("FotMob matches response is missing leagues")
    source = _source(url=url, retrieved_at=observed, payload=payload, kind="schedule")
    result: list[dict] = []
    for league in data["leagues"]:
        if not isinstance(league, dict) or not isinstance(league.get("matches"), list):
            continue
        for row in league["matches"]:
            if not isinstance(row, dict) or not _EVENT_ID_RE.fullmatch(str(row.get("id", ""))):
                continue
            home = row.get("home") if isinstance(row.get("home"), dict) else {}
            away = row.get("away") if isinstance(row.get("away"), dict) else {}
            if not home.get("name") or not away.get("name"):
                continue
            try:
                kickoff = _match_time(row)
            except ValueError:
                continue
            status = row.get("status") if isinstance(row.get("status"), dict) else {}
            result.append({
                "event_id": str(row["id"]),
                "kickoff_at": kickoff.isoformat(),
                "home_team": str(home["name"]),
                "away_team": str(away["name"]),
                "home_provider_team_id": str(home["id"]) if home.get("id") is not None else None,
                "away_provider_team_id": str(away["id"]) if away.get("id") is not None else None,
                "status": "cancelled" if status.get("cancelled") else "finished" if status.get("finished") else "upcoming",
                "source": source,
            })
    return result


def _player(row: object, *, starter: bool, substitute: bool) -> dict:
    if not isinstance(row, dict) or row.get("id") is None or not row.get("name"):
        return {}
    return {
        "player_id": f"fotmob:{row['id']}",
        "provider_player_id": str(row["id"]),
        "name": str(row["name"]),
        "position_id": row.get("positionId") or row.get("usualPlayingPositionId"),
        "starter": starter,
        "substitute": substitute,
        "status": "starter" if starter else "substitute",
        "expected_minutes": None,
    }


def _lineup_team(value: object) -> tuple[dict, bool]:
    if not isinstance(value, dict):
        return {"players": [], "missing_players": [], "available": False}, False
    starters = [row for row in (_player(item, starter=True, substitute=False) for item in value.get("starters", [])) if row]
    substitutes = [row for row in (_player(item, starter=False, substitute=True) for item in value.get("subs", [])) if row]
    unavailable_raw = value.get("unavailable") or value.get("missingPlayers") or []
    missing = []
    for row in unavailable_raw if isinstance(unavailable_raw, list) else []:
        if isinstance(row, dict):
            missing.append({key: row[key] for key in ("id", "name", "reason") if row.get(key) is not None})
    available = isinstance(value.get("starters"), list) or isinstance(value.get("subs"), list)
    return {
        "provider_team_id": str(value["id"]) if value.get("id") is not None else None,
        "team_name": value.get("name"),
        "formation": value.get("formation"),
        "players": starters + substitutes,
        "starters": starters,
        "substitutes": substitutes,
        "missing_players": missing,
        "available": available,
    }, available


def parse_fotmob_match_details_payload(
    payload: bytes,
    *,
    fixture_id: str,
    event_id: str,
    kickoff_at: datetime | str,
    home_team: str,
    away_team: str,
    retrieved_at: datetime,
    url: str,
) -> dict:
    """Parse one match detail response with a causal eligibility flag."""

    if not _EVENT_ID_RE.fullmatch(str(event_id)):
        raise ValueError("FotMob event ID must be numeric")
    _validate_url(url, path=FOTMOB_DETAILS_PATH)
    observed = _utc(retrieved_at, field="retrieved_at")
    kickoff = _utc(kickoff_at, field="kickoff_at")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("FotMob match details response is not valid JSON") from exc
    lineup = data.get("content", {}).get("lineup") if isinstance(data, dict) else None
    if not isinstance(lineup, dict):
        raise ValueError("FotMob match details response is missing lineup")
    home, home_available = _lineup_team(lineup.get("homeTeam"))
    away, away_available = _lineup_team(lineup.get("awayTeam"))
    lineup_type = lineup.get("lineupType")
    complete = home_available and away_available and len(home["starters"]) == 11 and len(away["starters"]) == 11
    # Only standard lineups with both sides complete can be used as a confirmed
    # pre-match observation.  Predicted/partial data remains display-only.
    confirmed = bool(complete and lineup_type == "standard")
    model_eligible = bool(confirmed and observed < kickoff)
    source = _source(url=url, retrieved_at=observed, payload=payload, kind="lineup_details")
    return {
        "fixture_id": str(fixture_id),
        "event_id": str(event_id),
        "lineups": {
            "confirmed": confirmed,
            "model_eligible": model_eligible,
            "lineup_type": lineup_type,
            "home": home,
            "away": away,
            "available": home_available or away_available,
            "missing_fields": ["expected_minutes", "replacement_value"],
        },
        "injuries": {
            "home": home["missing_players"],
            "away": away["missing_players"],
            "available": bool(home["missing_players"] or away["missing_players"]),
        },
        "join": {
            "status": "strict_event_id_and_schedule_pair",
            "home_team": home_team,
            "away_team": away_team,
            "kickoff_at": kickoff.isoformat(),
        },
        "source": source,
    }


def _schedule_url(day: datetime) -> str:
    return f"{FOTMOB_API_ROOT}{FOTMOB_MATCHES_PATH[len('/api/data'):]}?{urlencode({'date': day.strftime('%Y%m%d')})}"


def _details_url(event_id: str) -> str:
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("FotMob event ID must be numeric")
    return f"{FOTMOB_API_ROOT}{FOTMOB_DETAILS_PATH[len('/api/data'):]}?{urlencode({'matchId': event_id})}"


def _match_event(fixture: dict, events: list[dict]) -> tuple[dict | None, str | None]:
    try:
        kickoff = _utc(fixture["kickoff_at"], field="kickoff_at")
    except (KeyError, TypeError, ValueError):
        return None, "invalid fixture kickoff"
    candidates = [
        event for event in events
        if _normalise(event.get("home_team")) == _normalise(fixture.get("home_team"))
        and _normalise(event.get("away_team")) == _normalise(fixture.get("away_team"))
        and _utc(event["kickoff_at"], field="event kickoff") == kickoff
    ]
    if len(candidates) != 1:
        return None, "no unique exact kickoff/team pair"
    return candidates[0], None


def fetch_fotmob_lineups(
    fixtures: list[dict],
    *,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    max_fixtures: int = 60,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] | object | None = None,
    allow_robots_disallowed: bool = False,
) -> dict:
    """Return the v260 block; the legacy robots override is recorded but ignored."""

    reference = _utc(now or datetime.now(timezone.utc), field="now")
    return rights_blocked_envelope(
        SourceId.FOTMOB_PUBLIC_API,
        provider=FOTMOB_SOURCE_NAME,
        checked_at=reference.isoformat(),
        empty_fields=("lineups",),
        config=(
            {"allow_robots_disallowed": True}
            if allow_robots_disallowed
            else None
        ),
    )


__all__ = [
    "FOTMOB_DETAILS_PATH",
    "FOTMOB_HOST",
    "FOTMOB_MATCHES_PATH",
    "FOTMOB_SOURCE_NAME",
    "fetch_fotmob_lineups",
    "parse_fotmob_match_details_payload",
    "parse_fotmob_matches_payload",
]
