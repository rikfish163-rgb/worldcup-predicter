"""Public official Premier League match and lineup adapter.

The Premier League website renders match pages dynamically, but its public
first-party data service exposes match summaries and lineups without an
account.  This adapter is intentionally explicit: callers must provide a
native Premier League match id before a lineup request is made.  It never
guesses an entity join from a team name alone.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


PREMIER_LEAGUE_HOST = "sdp-prem-prod.premier-league-prod.pulselive.com"
PREMIER_LEAGUE_API_PREFIX = "/api/"
PREMIER_LEAGUE_MATCHWEEK_PATH = "/api/v1/competitions/8/seasons/\\d{4}/matchweeks/\\d{1,2}/matches"
PREMIER_LEAGUE_LINEUPS_PATH = "/api/v3/matches/\\d+/lineups"
PREMIER_LEAGUE_MATCH_PATH = "/api/v2/matches/\\d+"
_LONDON = ZoneInfo("Europe/London")


def _validate_response_url(url: str, *, path_pattern: str | None = None) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != PREMIER_LEAGUE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path.startswith(PREMIER_LEAGUE_API_PREFIX)
    ):
        raise ValueError("Premier League response redirected to a non-allowlisted URL")
    if path_pattern is not None:
        import re

        if not re.fullmatch(path_pattern, parsed.path):
            raise ValueError("Premier League response path is not allowlisted")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise urllib.error.URLError("Premier League redirected; redirects are disabled")


def _safe_opener():
    return urllib.request.build_opener(_NoRedirect())


def _read_limited(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    path_pattern: str | None = None,
) -> bytes:
    open_method = getattr(opener, "open", None) or opener
    response = open_method(request, timeout=30)  # type: ignore[operator]
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        _validate_response_url(str(final_url), path_pattern=path_pattern)
        payload = response.read(max_bytes + 1)
    if not isinstance(payload, bytes):
        raise TypeError("Premier League response must be bytes")
    if len(payload) > max_bytes:
        raise ValueError("Premier League response exceeded 10 MiB")
    return payload


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _kickoff(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Premier League match is missing kickoff")
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("Premier League kickoff has an invalid format") from exc
    return parsed.replace(tzinfo=_LONDON).astimezone(timezone.utc).isoformat()


def _raw_source(*, url: str, retrieved_at: datetime, payload: bytes, **extra: object) -> dict:
    return {
        "name": "Premier League official",
        "url": url,
        "retrieved_at": _utc(retrieved_at).isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        **extra,
    }


def _team_name(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise ValueError("Premier League match is missing a team name")
    name = value["name"].strip()
    if not name:
        raise ValueError("Premier League match has an empty team name")
    return name


def _score(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def parse_premier_league_fixtures_payload(
    payload: bytes,
    *,
    season: str,
    competition_id: str = "premier-league",
    retrieved_at: datetime,
    url: str,
) -> list[dict]:
    """Parse one official matchweek response into the common fixture shape."""

    _validate_response_url(url, path_pattern=PREMIER_LEAGUE_MATCHWEEK_PATH)
    observed = _utc(retrieved_at)
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Premier League matchweek response is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Premier League matchweek response is missing data")
    fixtures: list[dict] = []
    for row in data["data"]:
        if not isinstance(row, dict) or not row.get("matchId"):
            continue
        home = row.get("homeTeam")
        away = row.get("awayTeam")
        try:
            kickoff_at = _kickoff(row.get("kickoff"))
            home_name = _team_name(home)
            away_name = _team_name(away)
        except ValueError:
            continue
        period = str(row.get("period") or "").casefold()
        status = "finished" if period in {"fulltime", "full-time"} else "upcoming"
        home_score = _score(home.get("score")) if isinstance(home, dict) else None
        away_score = _score(away.get("score")) if isinstance(away, dict) else None
        score = (
            {"home": home_score, "away": away_score}
            if status == "finished" and home_score is not None and away_score is not None
            else None
        )
        fixtures.append(
            {
                "id": f"premierleague:{row['matchId']}",
                # Keep the native numeric key available for the official
                # lineup endpoint.  The canonical fixture id is deliberately
                # namespaced above; the endpoint itself accepts only the
                # provider's numeric match id.
                "premier_league_match_id": str(row["matchId"]),
                "competition_id": competition_id,
                "season": str(season),
                "kickoff_at": kickoff_at,
                "home_team": home_name,
                "away_team": away_name,
                "home_provider_team_id": str(home.get("id"))
                if isinstance(home, dict) and home.get("id")
                else None,
                "away_provider_team_id": str(away.get("id"))
                if isinstance(away, dict) and away.get("id")
                else None,
                "status": status,
                "score": score,
                "venue": row.get("ground"),
                "source": _raw_source(
                    url=url,
                    retrieved_at=observed,
                    payload=payload,
                    native_fixture_id=str(row["matchId"]),
                    source_kind="official_fixture",
                ),
            }
        )
    return fixtures


def _flatten(values: object) -> set[str]:
    result: set[str] = set()
    if isinstance(values, list):
        for value in values:
            if isinstance(value, list):
                result.update(str(item) for item in value if item is not None)
            elif value is not None:
                result.add(str(value))
    return result


def _official_lineup_side(data: object) -> tuple[dict, bool]:
    if not isinstance(data, dict) or not isinstance(data.get("players"), list):
        return {"players": [], "available": False}, False
    formation = data.get("formation") if isinstance(data.get("formation"), dict) else {}
    starters = _flatten(formation.get("lineup"))
    substitutes = _flatten(formation.get("subs"))
    players: list[dict] = []
    for row in data["players"]:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        player_id = str(row["id"])
        is_starter = player_id in starters
        is_substitute = player_id in substitutes or row.get("position") == "Substitute"
        status = "starter" if is_starter else "substitute" if is_substitute else None
        player = {
            "player_id": f"premierleague:{player_id}",
            "provider_player_id": player_id,
            "name": " ".join(
                str(part).strip() for part in (row.get("firstName"), row.get("lastName")) if part
            ).strip(),
            "position": row.get("subPosition") or row.get("position"),
            "starter": is_starter,
            "substitute": is_substitute,
            "status": status,
            # The official feed confirms selection, but does not provide a
            # pre-match expected-minutes estimate.  Never turn a starter into
            # an invented 90-minute value.
            "expected_minutes": None,
        }
        # Keep explicit nulls for fields the source does not provide.  The
        # downstream contract uses these nulls to distinguish "unknown" from
        # a fabricated zero/minute estimate.
        players.append(player)
    complete = (
        len(starters) == 11
        and len({row.get("provider_player_id") for row in players if row.get("starter")}) == 11
    )
    result = {
        "players": players,
        "available": bool(players),
        "formation": formation.get("formation"),
        "missing_players": [],
    }
    return result, complete


def parse_premier_league_lineups_payload(
    payload: bytes,
    *,
    match_id: str,
    fixture_id: str | None,
    kickoff_at: str,
    retrieved_at: datetime,
    url: str,
) -> dict:
    """Parse official XI/substitutes while retaining conservative time semantics."""

    import re

    if not re.fullmatch(r"\d+", str(match_id)):
        raise ValueError("Premier League match ID must be numeric")
    _validate_response_url(url, path_pattern=PREMIER_LEAGUE_LINEUPS_PATH)
    observed = _utc(retrieved_at)
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Premier League lineup response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Premier League lineup response is not an object")
    home, home_complete = _official_lineup_side(data.get("home_team"))
    away, away_complete = _official_lineup_side(data.get("away_team"))
    confirmed = bool(home_complete and away_complete)
    try:
        kickoff = datetime.fromisoformat(kickoff_at.replace("Z", "+00:00"))
        before_kickoff = kickoff.tzinfo is not None and observed < kickoff.astimezone(timezone.utc)
    except ValueError:
        before_kickoff = False
    return {
        "match_id": str(match_id),
        "fixture_id": str(fixture_id) if fixture_id is not None else None,
        "lineups": {
            "confirmed": confirmed,
            "home": home,
            "away": away,
            "available": bool(home["available"] or away["available"]),
            "model_eligible": bool(confirmed and before_kickoff),
            "missing_fields": ["expected_minutes", "replacement_value"],
        },
        "injuries": {"home": [], "away": [], "available": False},
        "source": _raw_source(
            url=url,
            retrieved_at=observed,
            payload=payload,
            native_match_id=str(match_id),
            source_kind="official_lineup",
            time_basis="observed_at_no_published_at",
            effective_at=None,
        ),
    }


def _request_json(
    opener: Callable[..., object] | object,
    *,
    url: str,
    path_pattern: str,
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Origin": "https://www.premierleague.com",
            "Referer": "https://www.premierleague.com/",
            "User-Agent": "Matchline/1.0",
        },
    )
    return _read_limited(opener, request, path_pattern=path_pattern)


def fetch_premier_league_fixtures(
    matchweeks: Iterable[int],
    *,
    season: str,
    now: datetime | None = None,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    """Fetch explicitly selected official matchweeks.

    The public feed exposes matchweeks rather than one unrestricted season
    endpoint.  Callers therefore choose a bounded set of weeks based on the
    current horizon; this function never guesses a week from a team name and
    isolates one failed week from the others.
    """

    return rights_blocked_fetch_envelope(
        SourceId.PREMIER_LEAGUE_PUBLIC_PAGES,
        provider="Premier League official",
        now=now,
        empty_fields=("fixtures",),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    if not isinstance(season, str) or not season.isdigit() or len(season) != 4:
        raise ValueError("Premier League season must be a four-digit string")
    selected: list[int] = []
    for value in matchweeks:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 38:
            raise ValueError("Premier League matchweek must be an integer from 1 to 38")
        if value not in selected:
            selected.append(value)
    selected.sort()
    reference = _utc(now or datetime.now(timezone.utc))
    fetcher = opener if opener is not None else _safe_opener()
    fixtures: list[dict] = []
    errors: list[dict] = []
    observed_times: list[datetime] = []
    for matchweek in selected:
        url = (
            f"https://{PREMIER_LEAGUE_HOST}/api/v1/competitions/8/"
            f"seasons/{season}/matchweeks/{matchweek}/matches"
        )
        try:
            payload = _request_json(
                fetcher,
                url=url,
                path_pattern=PREMIER_LEAGUE_MATCHWEEK_PATH,
            )
            observed = reference
            observed_times.append(observed)
            fixtures.extend(
                parse_premier_league_fixtures_payload(
                    payload,
                    season=season,
                    retrieved_at=observed,
                    url=url,
                )
            )
        except Exception as exc:  # source-level isolation is part of the contract
            errors.append({"season": season, "matchweek": matchweek, "error": str(exc)})
    fixtures.sort(key=lambda item: (item.get("kickoff_at", ""), item.get("id", "")))
    return {
        "provider": "Premier League official",
        "season": season,
        "matchweeks": selected,
        "retrieved_at": max(observed_times, default=reference).isoformat(),
        "fixtures": fixtures,
        "errors": errors,
        "status": "ok" if fixtures and not errors else "degraded" if fixtures else "unavailable",
    }


def fetch_premier_league_lineups(
    fixtures: Iterable[dict],
    *,
    now: datetime | None = None,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    """Fetch lineups only for fixtures with an explicit native match id."""

    return rights_blocked_fetch_envelope(
        SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS,
        provider="Premier League official",
        now=now,
        empty_fields=("lineups",),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    reference = _utc(now or datetime.now(timezone.utc))
    fetcher = opener if opener is not None else _safe_opener()
    lineups: list[dict] = []
    errors: list[dict] = []
    for fixture in fixtures:
        native_id = fixture.get("premier_league_match_id")
        if native_id is None:
            source = fixture.get("source") if isinstance(fixture.get("source"), dict) else {}
            candidate = source.get("native_fixture_id")
            if isinstance(candidate, str):
                native_id = (
                    candidate.split(":", 1)[1]
                    if candidate.startswith("premierleague:")
                    else candidate
                )
        if native_id is None:
            continue
        url = f"https://{PREMIER_LEAGUE_HOST}/api/v3/matches/{native_id}/lineups"
        try:
            payload = _request_json(fetcher, url=url, path_pattern=PREMIER_LEAGUE_LINEUPS_PATH)
            lineups.append(
                parse_premier_league_lineups_payload(
                    payload,
                    match_id=str(native_id),
                    fixture_id=str(fixture.get("id")) if fixture.get("id") else None,
                    kickoff_at=str(fixture["kickoff_at"]),
                    retrieved_at=reference,
                    url=url,
                )
            )
        except Exception as exc:  # source-level isolation is part of the contract
            errors.append(
                {"fixture_id": fixture.get("id"), "match_id": str(native_id), "error": str(exc)}
            )
    return {
        "provider": "Premier League official",
        "retrieved_at": reference.isoformat(),
        "lineups": lineups,
        "errors": errors,
        "status": "ok" if lineups and not errors else "degraded" if lineups else "unavailable",
    }


__all__ = [
    "PREMIER_LEAGUE_HOST",
    "fetch_premier_league_fixtures",
    "fetch_premier_league_lineups",
    "parse_premier_league_fixtures_payload",
    "parse_premier_league_lineups_payload",
]
