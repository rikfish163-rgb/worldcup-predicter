"""Read-only SofaScore adapter for pre-match events, lineups and absences.

SofaScore's public JSON endpoint is not treated as an authoritative injury
registry: the adapter exposes only the provider's ``missingPlayers`` field
when it is present, and keeps an unavailable/unknown state otherwise.  It
does not use browser automation, authentication, challenge bypasses, or
arbitrary URLs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

from league_platform.source_rights import SourceId, rights_blocked_envelope


SOFASCORE_HOST = "api.sofascore.com"
SOFASCORE_API_ROOT = f"https://{SOFASCORE_HOST}/api/v1"
SOFASCORE_SOURCE_NAME = "SofaScore"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_CONTENT_BYTES = 10 * 1024 * 1024
DEFAULT_HORIZON_DAYS = 16
_EVENT_ID_RE = re.compile(r"^[0-9]+$")


def _utc_datetime(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _timestamp_datetime(value: object) -> datetime:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        return _utc_datetime(value, field="SofaScore event start time")
    raise ValueError("SofaScore event has no valid start time")


def _validated_timeout(timeout: float) -> float:
    value = float(timeout)
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT_SECONDS:g} seconds")
    return value


def _validate_url(url: str, *, path_prefix: str | None = None) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SOFASCORE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or (path_prefix is not None and not parsed.path.startswith(path_prefix))
    ):
        raise ValueError("SofaScore URL is not allowlisted")


def _validate_api_url(url: str, *, path_pattern: str) -> None:
    _validate_url(url)
    if re.fullmatch(path_pattern, urlparse(url).path) is None:
        raise ValueError("SofaScore API path is not allowlisted")




def _source(*, url: str, retrieved_at: datetime, payload: bytes) -> dict:
    _validate_url(url, path_prefix="/api/v1/")
    return {
        "name": SOFASCORE_SOURCE_NAME,
        "url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _team_name(team: object) -> str:
    if not isinstance(team, dict):
        return ""
    value = team.get("name") or team.get("shortName") or team.get("slug")
    return str(value) if value is not None else ""


def _normalise_team(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return "".join(character.lower() for character in normalized if character.isalnum())


def _event_status(event: dict) -> str:
    status = event.get("status")
    if not isinstance(status, dict):
        return "unknown"
    kind = str(status.get("type") or status.get("description") or "").lower()
    if any(token in kind for token in ("notstarted", "not started", "scheduled", "pending")):
        return "upcoming"
    if any(token in kind for token in ("inprogress", "in progress", "live", "halftime")):
        return "live"
    if any(token in kind for token in ("finished", "ended", "after")):
        return "finished"
    if "postpon" in kind:
        return "postponed"
    if any(token in kind for token in ("cancel", "abandon")):
        return "cancelled"
    return "unknown"


def _event_object(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("SofaScore response is not an object")
    event = data.get("event", data)
    if not isinstance(event, dict):
        raise ValueError("SofaScore response is missing event data")
    if event.get("id") is None:
        raise ValueError("SofaScore event is missing a native ID")
    return event


def _event_record(event: dict, *, retrieved_at: datetime, url: str, payload: bytes) -> dict:
    if event.get("id") is None or not _EVENT_ID_RE.fullmatch(str(event["id"])):
        raise ValueError("SofaScore event is missing a numeric native ID")
    kickoff = _timestamp_datetime(event.get("startTimestamp", event.get("startTime")))
    home = event.get("homeTeam")
    away = event.get("awayTeam")
    home_name = _team_name(home)
    away_name = _team_name(away)
    if not home_name or not away_name:
        raise ValueError("SofaScore event is missing home/away teams")
    return {
        "event_id": str(event["id"]),
        "kickoff_at": kickoff.isoformat(),
        "home_team": home_name,
        "away_team": away_name,
        "home_provider_team_id": str(home.get("id"))
        if isinstance(home, dict) and home.get("id") is not None
        else None,
        "away_provider_team_id": str(away.get("id"))
        if isinstance(away, dict) and away.get("id") is not None
        else None,
        "status": _event_status(event),
        "source": _source(url=url, retrieved_at=retrieved_at, payload=payload),
    }


def parse_sofascore_event_payload(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str,
) -> dict:
    """Parse one event object while preserving the raw-response hash."""

    _validate_api_url(url, path_pattern=r"/api/v1/event/\d+")
    observed = _utc_datetime(retrieved_at, field="retrieved_at")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("SofaScore event response is not valid JSON") from exc
    event = _event_object(data)
    return _event_record(event, retrieved_at=observed, url=url, payload=payload)


def parse_sofascore_scheduled_events_payload(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str,
) -> list[dict]:
    """Parse the date schedule endpoint into the same event contract."""

    _validate_api_url(
        url,
        path_pattern=r"/api/v1/sport/football/scheduled-events/\d{4}-\d{2}-\d{2}",
    )
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("SofaScore schedule response is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("SofaScore schedule response is missing events")
    observed = _utc_datetime(retrieved_at, field="retrieved_at")
    return [
        _event_record(event, retrieved_at=observed, url=url, payload=payload)
        for event in data["events"]
        if isinstance(event, dict)
    ]


def _player_row(row: object) -> dict:
    if not isinstance(row, dict):
        return {}
    player = row.get("player") if isinstance(row.get("player"), dict) else row
    expected_minutes = None
    for candidate in (
        row.get("expectedMinutes"),
        row.get("expected_minutes"),
        row.get("minutes"),
    ):
        try:
            numeric = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric) and 0 <= numeric <= 120:
            expected_minutes = round(numeric, 2)
            break
    result = {
        "player_id": str(player["id"]) if player.get("id") is not None else None,
        "name": player.get("name") or player.get("shortName"),
        "position": row.get("position") or player.get("position"),
        "starter": row.get("starter"),
        "substitute": row.get("substitute"),
        "status": row.get("status")
        or ("starter" if row.get("starter") is True else "substitute" if row.get("substitute") is True else None),
        "expected_minutes": expected_minutes,
    }
    return {key: value for key, value in result.items() if value is not None}


def _missing_player(row: object) -> dict:
    if not isinstance(row, dict):
        return {}
    player = row.get("player") if isinstance(row.get("player"), dict) else row
    result = {
        "player_id": str(player["id"]) if player.get("id") is not None else None,
        "name": player.get("name") or player.get("shortName"),
        "position": row.get("position") or player.get("position"),
        "reason": row.get("reason") or row.get("description"),
        "status": row.get("status"),
    }
    return {key: value for key, value in result.items() if value is not None}


def _lineup_side(data: object) -> tuple[dict, bool]:
    if not isinstance(data, dict):
        return {"players": [], "missing_players": [], "available": False}, False
    raw_players = data.get("players")
    raw_missing = data.get("missingPlayers")
    players = [row for row in (_player_row(item) for item in (raw_players or [])) if row]
    missing = [row for row in (_missing_player(item) for item in (raw_missing or [])) if row]
    available = isinstance(raw_players, list) or isinstance(raw_missing, list)
    result = {
        "players": players,
        "missing_players": missing,
        "available": available,
    }
    if data.get("formation") is not None:
        result["formation"] = data["formation"]
    return result, available


def parse_sofascore_lineups_payload(
    payload: bytes,
    *,
    event_id: str,
    retrieved_at: datetime,
    url: str,
    fixture_id: str | None = None,
) -> dict:
    """Parse confirmed/predicted lineups and provider-reported missing players."""

    if not _EVENT_ID_RE.fullmatch(str(event_id)):
        raise ValueError("SofaScore event ID must be numeric")
    _validate_api_url(url, path_pattern=r"/api/v1/event/\d+/lineups")
    observed = _utc_datetime(retrieved_at, field="retrieved_at")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("SofaScore lineup response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("SofaScore lineup response is not an object")
    home, home_available = _lineup_side(data.get("home"))
    away, away_available = _lineup_side(data.get("away"))
    confirmed = data.get("confirmed")
    if confirmed is not None and not isinstance(confirmed, bool):
        confirmed = None
    return {
        "event_id": str(event_id),
        "fixture_id": str(fixture_id) if fixture_id is not None else None,
        "lineups": {
            "confirmed": confirmed,
            "home": home,
            "away": away,
            "available": home_available or away_available,
        },
        "injuries": {
            "home": home["missing_players"],
            "away": away["missing_players"],
            "available": home_available or away_available,
        },
        "source": _source(url=url, retrieved_at=observed, payload=payload),
    }


# Singular spelling is convenient for callers and keeps compatibility with
# existing soccerdata method names.
parse_sofascore_lineup_payload = parse_sofascore_lineups_payload


def _event_id_from_fixture(fixture: dict) -> str | None:
    source = fixture.get("source")
    candidates = (
        fixture.get("sofascore_event_id"),
        fixture.get("provider_event_id"),
        source.get("sofascore_event_id") if isinstance(source, dict) else None,
    )
    for value in candidates:
        if value is not None and _EVENT_ID_RE.fullmatch(str(value)):
            return str(value)
    return None


def _event_url(event_id: str) -> str:
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("SofaScore event ID must be numeric")
    return f"{SOFASCORE_API_ROOT}/event/{event_id}"


def _lineups_url(event_id: str) -> str:
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("SofaScore event ID must be numeric")
    return f"{SOFASCORE_API_ROOT}/event/{event_id}/lineups"


def _schedule_url(day: datetime) -> str:
    return f"{SOFASCORE_API_ROOT}/sport/football/scheduled-events/{day:%Y-%m-%d}"


def _teams_match(left: dict, right: dict) -> bool:
    return _normalise_team(left.get("home_team", "")) == _normalise_team(
        right.get("home_team", "")
    ) and _normalise_team(left.get("away_team", "")) == _normalise_team(
        right.get("away_team", "")
    )


def _match_event(fixture: dict, events: list[dict]) -> dict | None:
    try:
        kickoff = _utc_datetime(fixture["kickoff_at"], field="kickoff_at")
    except (KeyError, TypeError, ValueError):
        return None
    possible = []
    for event in events:
        if not _teams_match(fixture, event):
            continue
        try:
            distance = abs(
                (_utc_datetime(event["kickoff_at"], field="kickoff_at") - kickoff).total_seconds()
            )
        except (KeyError, TypeError, ValueError):
            continue
        if distance <= 6 * 60 * 60:
            possible.append((distance, event))
    return min(possible, key=lambda item: item[0])[1] if possible else None


def fetch_sofascore_prematch(
    fixtures: list[dict],
    *,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    max_fixtures: int = 120,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] | object | None = None,
) -> dict:
    """Return the v260 rights block before inspecting fixtures or the opener."""

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_time = reference_time.astimezone(timezone.utc)
    return rights_blocked_envelope(
        SourceId.SOFASCORE_PREMATCH,
        provider=SOFASCORE_SOURCE_NAME,
        checked_at=reference_time.isoformat(),
        empty_fields=("events",),
    )


# Explicit plural alias for code that calls the source by its result shape.
fetch_sofascore_pre_match = fetch_sofascore_prematch

__all__ = [
    "DEFAULT_HORIZON_DAYS",
    "MAX_CONTENT_BYTES",
    "SOFASCORE_API_ROOT",
    "SOFASCORE_HOST",
    "fetch_sofascore_pre_match",
    "fetch_sofascore_prematch",
    "parse_sofascore_event_payload",
    "parse_sofascore_lineup_payload",
    "parse_sofascore_lineups_payload",
    "parse_sofascore_scheduled_events_payload",
]
