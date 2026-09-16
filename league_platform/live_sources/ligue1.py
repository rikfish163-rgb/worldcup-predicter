"""Public Ligue 1 official match API adapter.

The Ligue 1 web application exposes a first-party, public JSON API from its
browser bundle.  This adapter uses that API only to discover exact fixture
identities and to observe the official match sheet.  A pre-match roster is
not a confirmed XI: it remains visible for audit/display until both sides
contain eleven unique ``formationPlace`` values.  No published timestamp is
provided by the API, so ``observed_at`` is retained as the causal time basis.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from unidecode import unidecode

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


LIGUE1_WEB_HOST = "ligue1.com"
LIGUE1_API_HOST = "ma-api.ligue1.fr"
LIGUE1_HOME_URL = f"https://{LIGUE1_WEB_HOST}/en/calendar/ligue1"
LIGUE1_API_ROOT = f"https://{LIGUE1_API_HOST}"
LIGUE1_CHAMPIONSHIP_ID = 1
LIGUE1_COMPETITION_ID = "ligue-1"
LIGUE1_CRAWL_DELAY_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_BYTES = 12 * 1024 * 1024
API_MAX_BYTES = 4 * 1024 * 1024

LIGUE1_CALENDAR_PATH = re.compile(r"/championship-calendar/1")
LIGUE1_GAMEWEEK_PATH = re.compile(r"/championship-matches/championship/1/game-week/[1-9]\d*")
LIGUE1_MATCH_PATH = re.compile(r"/championship-match/l1_championship_match_\d+")
LIGUE1_NATIVE_MATCH_ID = re.compile(r"l1_championship_match_\d+")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Fail closed instead of following an API redirect to an unknown host."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise urllib.error.URLError("Ligue 1 redirect is disabled")


def _safe_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Ligue 1 {field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Ligue 1 {field} is invalid") from exc
    return _utc(parsed)


def _validate_url(url: str, *, kind: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Ligue 1 URL is not allowlisted")
    path = parsed.path
    if kind == "home":
        if parsed.hostname != LIGUE1_WEB_HOST or path != "/en/calendar/ligue1":
            raise ValueError("Ligue 1 home URL is not allowlisted")
        return
    if parsed.hostname != LIGUE1_API_HOST:
        raise ValueError("Ligue 1 API URL is not allowlisted")
    pattern = {
        "calendar": LIGUE1_CALENDAR_PATH,
        "gameweek": LIGUE1_GAMEWEEK_PATH,
        "match": LIGUE1_MATCH_PATH,
    }.get(kind)
    if pattern is None or not pattern.fullmatch(path):
        raise ValueError("Ligue 1 API path is not allowlisted")


def _read_json(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
    *,
    kind: str,
) -> tuple[dict, bytes]:
    open_method = getattr(opener, "open", None) or opener
    response = open_method(request, timeout=DEFAULT_TIMEOUT_SECONDS)  # type: ignore[operator]
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        _validate_url(str(final_url).split("?", 1)[0], kind=kind)
        payload = response.read(API_MAX_BYTES + 1)
    if not isinstance(payload, bytes):
        raise TypeError("Ligue 1 API response must be bytes")
    if len(payload) > API_MAX_BYTES:
        raise ValueError("Ligue 1 API response exceeded 4 MiB")
    try:
        data = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Ligue 1 API response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Ligue 1 API response root is not an object")
    return data, payload


def _normalise(value: object) -> str:
    return "".join(ch for ch in unidecode(str(value or "")).lower() if ch.isalnum())


_GENERIC_TEAM_TOKENS = {
    "ac",
    "as",
    "club",
    "fc",
    "ogc",
    "rc",
    "sc",
    "stade",
}
_TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "marseille": ("olympiquedemarseille", "om"),
    "olympiquedemarseille": ("marseille", "om"),
    "strasbourg": ("rcstrasbourgalsace", "rcstrasbourg", "strasbourgalsace"),
    "parissaintgermain": ("psg", "parissg", "parissaintgermain"),
    "psg": ("parissaintgermain", "parissg"),
    "monaco": ("asmonaco", "monaco"),
    "asmonaco": ("monaco",),
    "rennes": ("staderennais", "rennes"),
    "staderennais": ("rennes",),
    "lehavre": ("lehavreac", "lehavre"),
    "lehavreac": ("lehavre",),
    "nice": ("ogcnice", "nice"),
    "ogcnice": ("nice",),
}


def _team_matches(expected: object, official: object) -> bool:
    expected_text = _normalise(expected)
    official_text = _normalise(official)
    if not expected_text or not official_text:
        return False
    if expected_text in official_text or official_text in expected_text:
        return True
    aliases = _TEAM_ALIASES.get(expected_text, ())
    if any(alias in official_text or official_text in alias for alias in aliases):
        return True
    expected_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", unidecode(str(expected or "")).lower())
        if token not in _GENERIC_TEAM_TOKENS and len(token) >= 3
    }
    official_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", unidecode(str(official or "")).lower())
        if token not in _GENERIC_TEAM_TOKENS and len(token) >= 3
    }
    return bool(expected_tokens & official_tokens)


def _club_name(side: object) -> str | None:
    if not isinstance(side, dict):
        return None
    raw_identity = side.get("clubIdentity")
    identity: dict[str, Any] = raw_identity if isinstance(raw_identity, dict) else {}
    for key in ("officialName", "name", "shortName", "displayName", "businessName", "trigram"):
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _player(row: object, *, status: str, ordinal: int | None = None) -> dict | None:
    if not isinstance(row, dict):
        return None
    raw_identity = row.get("playerIdentity")
    identity: dict[str, Any] = raw_identity if isinstance(raw_identity, dict) else {}
    provider_id = row.get("id") or identity.get("id")
    if not provider_id:
        return None
    name = identity.get("knownName") or " ".join(
        part
        for part in (identity.get("firstName"), identity.get("lastName"))
        if isinstance(part, str) and part.strip()
    )
    if not name:
        return None
    formation_place = row.get("formationPlace")
    try:
        formation_place = int(formation_place) if formation_place is not None else None
    except (TypeError, ValueError):
        formation_place = None
    shirt_number = identity.get("jerseyNumber") or row.get("shirtNumber")
    return {
        "player_id": f"ligue1:{provider_id}",
        "provider_player_id": str(provider_id),
        "name": str(name).strip(),
        "position": row.get("position") or row.get("realUltraPosition"),
        "shirt_number": shirt_number,
        "starter": status == "starter",
        "substitute": status == "substitute",
        "status": status,
        "formation_place": formation_place,
        "ordinal": ordinal,
        "expected_minutes": None,
    }


def _lineup_side(value: object, *, period: str) -> tuple[dict, bool]:
    if not isinstance(value, dict):
        return {"players": [], "available": False, "missing_players": []}, False
    raw_players = value.get("players")
    if isinstance(raw_players, dict):
        rows = list(raw_players.values())
    elif isinstance(raw_players, list):
        rows = raw_players
    else:
        rows = []
    players: list[dict] = []
    for index, row in enumerate(rows):
        formation_place = row.get("formationPlace") if isinstance(row, dict) else None
        try:
            place = int(formation_place) if formation_place is not None else 0
        except (TypeError, ValueError):
            place = 0
        # Pre-match roster entries have no formationPlace.  They are useful
        # for display, but cannot be promoted to a confirmed XI.
        status = (
            "starter"
            if place > 0
            else "roster"
            if period.casefold() == "prematch"
            else "substitute"
        )
        item = _player(row, status=status, ordinal=index)
        if item is not None:
            players.append(item)
    starter_ids = {item["provider_player_id"] for item in players if item.get("starter")}
    starter_places = {
        item.get("formation_place")
        for item in players
        if item.get("starter") and isinstance(item.get("formation_place"), int)
    }
    complete = len(starter_ids) == 11 and starter_places == set(range(1, 12))
    return {
        "players": players,
        "available": bool(players),
        "formation": value.get("formation"),
        "missing_players": [],
    }, complete


def _diagnostic(
    home: dict[str, Any], away: dict[str, Any], *, confirmed: bool, before_kickoff: bool
) -> dict[str, Any]:
    raw_home_players = home.get("players")
    raw_away_players = away.get("players")
    home_players: list[Any] = raw_home_players if isinstance(raw_home_players, list) else []
    away_players: list[Any] = raw_away_players if isinstance(raw_away_players, list) else []
    home_starters = sum(
        1 for row in home_players if isinstance(row, dict) and row.get("starter") is True
    )
    away_starters = sum(
        1 for row in away_players if isinstance(row, dict) and row.get("starter") is True
    )
    if not before_kickoff:
        status, reason = "post_kickoff_observation", "official_api_observed_after_kickoff"
    elif confirmed:
        status, reason = "confirmed", "both_sides_have_11_formation_places"
    elif not home_players and not away_players:
        status, reason = "not_published", "official_feed_returned_no_lineup_rows"
    elif home_starters == 0 and away_starters == 0:
        status, reason = "not_published", "official_feed_returned_roster_only"
    else:
        status, reason = "partial", "official_feed_has_incomplete_lineup_rows"
    return {
        "status": status,
        "reason": reason,
        "home_player_count": len(home_players),
        "away_player_count": len(away_players),
        "home_starter_count": home_starters,
        "away_starter_count": away_starters,
    }


def parse_ligue1_match_detail(
    data: dict,
    *,
    fixture_id: str,
    kickoff_at: str,
    retrieved_at: datetime,
    url: str,
    raw_payload: bytes,
    expected_home: str | None = None,
    expected_away: str | None = None,
) -> dict:
    """Normalize one official match-sheet detail with strict causal checks."""

    _validate_url(url, kind="match")
    observed = _utc(retrieved_at)
    kickoff = _timestamp(kickoff_at, field="kickoff_at")
    official_kickoff = _timestamp(data.get("date"), field="date")
    if abs((official_kickoff - kickoff).total_seconds()) > 90:
        raise ValueError("Ligue 1 official kickoff does not match canonical fixture")
    native_id = data.get("id")
    if (
        not isinstance(native_id, str)
        or not LIGUE1_MATCH_PATH.fullmatch(urlparse(url).path)
        or urlparse(url).path.rsplit("/", 1)[-1] != native_id
    ):
        raise ValueError("Ligue 1 official match id does not match requested route")
    home_name = _club_name(data.get("home"))
    away_name = _club_name(data.get("away"))
    if expected_home and not _team_matches(expected_home, home_name):
        raise ValueError("Ligue 1 official home team does not match canonical fixture")
    if expected_away and not _team_matches(expected_away, away_name):
        raise ValueError("Ligue 1 official away team does not match canonical fixture")
    period = str(data.get("period") or "")
    home, home_complete = _lineup_side(data.get("home"), period=period)
    away, away_complete = _lineup_side(data.get("away"), period=period)
    confirmed = bool(home_complete and away_complete)
    before_kickoff = observed < kickoff
    status = (
        "finished" if period.casefold() in {"fulltime", "full_time", "finished"} else "upcoming"
    )
    source = {
        "name": "Ligue 1 official",
        "url": url,
        "retrieved_at": observed.isoformat(),
        "raw_sha256": hashlib.sha256(raw_payload).hexdigest(),
        "native_match_id": native_id,
        "source_kind": "official_lineup_api"
        if any(home["players"] or away["players"])
        else "official_match_detail",
        "time_basis": "observed_at_no_published_at",
        "effective_at": None,
    }
    lineups = {
        "confirmed": confirmed,
        "home": home,
        "away": away,
        "available": bool(home["available"] or away["available"]),
        "model_eligible": bool(confirmed and before_kickoff),
        "missing_fields": ["expected_minutes", "replacement_value"],
        "diagnostic": _diagnostic(home, away, confirmed=confirmed, before_kickoff=before_kickoff),
        "period": period,
    }
    official_fixture = {
        "id": fixture_id,
        "competition_id": LIGUE1_COMPETITION_ID,
        "kickoff_at": official_kickoff.isoformat(),
        "home_team": home_name,
        "away_team": away_name,
        "status": status,
        "source": source,
    }
    return {
        "fixture_id": fixture_id,
        "match_id": native_id,
        "official_fixture": official_fixture,
        "lineups": lineups,
        "source": source,
    }


def _summary_row(data: dict) -> list[dict]:
    for key in ("matches", "data", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _summary_teams(row: dict) -> tuple[str | None, str | None]:
    home = row.get("home") if isinstance(row.get("home"), dict) else {}
    away = row.get("away") if isinstance(row.get("away"), dict) else {}
    return _club_name(home), _club_name(away)


def _season_for_fixture(fixture: dict, kickoff: datetime) -> int:
    raw = fixture.get("season")
    try:
        season = int(raw) if isinstance(raw, (int, str)) else None
    except (TypeError, ValueError):
        season = None
    return (
        season if season is not None else kickoff.year if kickoff.month >= 7 else kickoff.year - 1
    )


def _week_numbers(calendar: dict, *, start: datetime, end: datetime, max_weeks: int) -> list[int]:
    values = calendar.get("gameWeeks")
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, list):
        return []
    result: list[int] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            raw_number = item.get("number") or item.get("gameWeekNumber") or item.get("weekNumber")
            if not isinstance(raw_number, (int, str)):
                continue
            number = int(raw_number)
        except (TypeError, ValueError):
            continue
        try:
            week_start = _timestamp(item.get("startDate"), field="gameWeek.startDate")
            week_end = _timestamp(item.get("endDate"), field="gameWeek.endDate")
        except ValueError:
            week_start, week_end = start, end
        if week_end >= start and week_start <= end:
            result.append(number)
    return sorted(set(result))[:max_weeks]


def fetch_ligue1_lineups(
    fixtures: Iterable[dict],
    *,
    now: datetime | None = None,
    horizon_hours: int = 48,
    max_weeks: int = 4,
    max_matches: int = 8,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    """Fetch bounded Ligue 1 official observations for near-term fixtures."""

    return rights_blocked_fetch_envelope(
        SourceId.OFFICIAL_LIGUE1_LINEUPS,
        provider="Ligue 1 official",
        now=now,
        empty_fields=("fixtures", "lineups"),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    reference = _utc(now or datetime.now(timezone.utc))
    horizon = reference + timedelta(hours=horizon_hours)
    candidates: list[dict] = []
    for fixture in fixtures:
        if (
            fixture.get("competition_id") != LIGUE1_COMPETITION_ID
            or fixture.get("status") != "upcoming"
        ):
            continue
        try:
            kickoff = _timestamp(fixture.get("kickoff_at"), field="kickoff_at")
        except ValueError:
            continue
        if reference <= kickoff <= horizon:
            candidates.append(fixture)
    candidates.sort(key=lambda row: (str(row.get("kickoff_at", "")), str(row.get("id", ""))))
    result: dict[str, Any] = {
        "provider": "Ligue 1 official",
        "retrieved_at": reference.isoformat(),
        "home_url": LIGUE1_HOME_URL,
        "api_root": LIGUE1_API_ROOT,
        "fixtures": [],
        "lineups": [],
        "errors": [],
        "match_discovery": {
            "status": "not_requested" if not candidates else "unavailable",
            "calendar_requests": 0,
            "gameweek_requests": 0,
            "records_seen": 0,
            "matched_count": 0,
            "unmatched_count": 0,
            "errors": [],
        },
        "status": "not_requested" if not candidates else "unavailable",
    }
    if not candidates:
        return result
    fetcher = opener if opener is not None else _safe_opener()
    real_opener = opener is None
    try:
        first_kickoff = _timestamp(candidates[0]["kickoff_at"], field="kickoff_at")
        season = _season_for_fixture(candidates[0], first_kickoff)
        calendar_url = (
            f"{LIGUE1_API_ROOT}/championship-calendar/{LIGUE1_CHAMPIONSHIP_ID}?season={season}"
        )
        calendar_request = urllib.request.Request(
            calendar_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Matchline/1.0",
                "platform": "web",
                "application": "ligue1",
            },
        )
        calendar, calendar_payload = _read_json(fetcher, calendar_request, kind="calendar")
        result["match_discovery"]["calendar_requests"] = 1
        result["match_discovery"]["calendar_raw_sha256"] = hashlib.sha256(
            calendar_payload
        ).hexdigest()
        week_numbers = _week_numbers(calendar, start=reference, end=horizon, max_weeks=max_weeks)
        if not week_numbers:
            # A missing week range is an explicit discovery failure, not a
            # reason to fetch arbitrary rounds or guess a native id.
            raise ValueError("Ligue 1 calendar has no overlapping game weeks")
    except Exception as exc:
        result["errors"].append({"stage": "calendar", "error": str(exc)})
        result["match_discovery"]["errors"].append({"stage": "calendar", "error": str(exc)})
        return result

    summaries: list[tuple[dict, str, str]] = []
    last_request = 0.0
    for number in week_numbers:
        try:
            if real_opener and last_request:
                time.sleep(
                    max(0.0, LIGUE1_CRAWL_DELAY_SECONDS - (time.monotonic() - last_request))
                )
            url = f"{LIGUE1_API_ROOT}/championship-matches/championship/{LIGUE1_CHAMPIONSHIP_ID}/game-week/{number}?season={season}"
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Matchline/1.0",
                    "platform": "web",
                    "application": "ligue1",
                },
            )
            data, raw = _read_json(fetcher, request, kind="gameweek")
            last_request = time.monotonic()
            rows = _summary_row(data)
            result["match_discovery"]["gameweek_requests"] += 1
            result["match_discovery"]["records_seen"] += len(rows)
            for row in rows:
                summaries.append((row, url, hashlib.sha256(raw).hexdigest()))
        except Exception as exc:
            error = {"stage": "gameweek", "game_week": number, "error": str(exc)}
            result["errors"].append(error)
            result["match_discovery"]["errors"].append(error)

    selected: list[tuple[dict, dict, str, str]] = []
    for fixture in candidates:
        try:
            kickoff = _timestamp(fixture["kickoff_at"], field="kickoff_at")
        except ValueError:
            continue
        matches: list[tuple[dict, str, str]] = []
        for row, url, raw_sha in summaries:
            try:
                official_time = _timestamp(row.get("date"), field="match.date")
            except ValueError:
                continue
            home_name, away_name = _summary_teams(row)
            if (
                abs((official_time - kickoff).total_seconds()) <= 90
                and _team_matches(fixture.get("home_team"), home_name)
                and _team_matches(fixture.get("away_team"), away_name)
            ):
                matches.append((row, url, raw_sha))
        if len(matches) != 1:
            reason = "not_found" if not matches else "ambiguous"
            result["errors"].append(
                {"fixture_id": fixture.get("id"), "stage": "match_join", "reason": reason}
            )
            result["match_discovery"]["unmatched_count"] += 1
            continue
        row, url, raw_sha = matches[0]
        native_id = row.get("matchId") or row.get("id")
        if not isinstance(native_id, str) or not LIGUE1_NATIVE_MATCH_ID.fullmatch(native_id):
            result["errors"].append(
                {
                    "fixture_id": fixture.get("id"),
                    "stage": "match_join",
                    "reason": "invalid_native_match_id",
                }
            )
            result["match_discovery"]["unmatched_count"] += 1
            continue
        selected.append((fixture, row, url, raw_sha))
    result["match_discovery"]["matched_count"] = len(selected)
    for fixture, summary, summary_url, summary_sha in selected[:max_matches]:
        native_id = str(summary.get("matchId") or summary.get("id"))
        detail_url = f"{LIGUE1_API_ROOT}/championship-match/{native_id}"
        try:
            if real_opener and last_request:
                time.sleep(
                    max(0.0, LIGUE1_CRAWL_DELAY_SECONDS - (time.monotonic() - last_request))
                )
            request = urllib.request.Request(
                detail_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Matchline/1.0",
                    "platform": "web",
                    "application": "ligue1",
                },
            )
            detail, raw = _read_json(fetcher, request, kind="match")
            last_request = time.monotonic()
            row = parse_ligue1_match_detail(
                detail,
                fixture_id=str(fixture["id"]),
                kickoff_at=str(fixture["kickoff_at"]),
                retrieved_at=reference,
                url=detail_url,
                raw_payload=raw,
                expected_home=str(fixture.get("home_team") or ""),
                expected_away=str(fixture.get("away_team") or ""),
            )
            row["official_fixture"]["source"]["summary_url"] = summary_url
            row["official_fixture"]["source"]["summary_raw_sha256"] = summary_sha
            row["source"]["summary_url"] = summary_url
            row["source"]["summary_raw_sha256"] = summary_sha
            result["fixtures"].append(row["official_fixture"])
            result["lineups"].append(row)
        except Exception as exc:
            result["errors"].append(
                {
                    "fixture_id": fixture.get("id"),
                    "stage": "match_detail",
                    "url": detail_url,
                    "error": str(exc),
                }
            )
    result["status"] = (
        "ok"
        if result["lineups"] and not result["errors"]
        else "degraded"
        if result["lineups"]
        else "unavailable"
    )
    result["match_discovery"]["status"] = (
        "ok" if result["match_discovery"]["matched_count"] else "unavailable"
    )
    return result


__all__ = [
    "LIGUE1_API_HOST",
    "LIGUE1_API_ROOT",
    "LIGUE1_CALENDAR_PATH",
    "LIGUE1_GAMEWEEK_PATH",
    "LIGUE1_HOME_URL",
    "LIGUE1_MATCH_PATH",
    "LIGUE1_NATIVE_MATCH_ID",
    "fetch_ligue1_lineups",
    "parse_ligue1_match_detail",
]
