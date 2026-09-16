"""Read-only OpenLigaDB Bundesliga fixture/result cross-check.

OpenLigaDB is used as an independent public schedule/result source only.  It
does not provide odds, injuries or confirmed lineups, and its rows are kept
separate from the ESPN fixture identity until an explicit entity match is
performed.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from league_platform.identity import team_id
from league_platform.live_sources.http_utils import read_response_bounded


OPENLIGADB_HOST = "api.openligadb.de"
OPENLIGADB_PATH = re.compile(r"^/getmatchdata/bl1/\d{4}$")
OPENLIGADB_MATCH_PATH = re.compile(r"^/getmatchdata/(\d{1,12})$")
MAX_CONTENT_BYTES = 10 * 1024 * 1024
MAX_MATCH_CONTENT_BYTES = 1024 * 1024
MAX_LIVE_MATCHES = 12
MAX_LIVE_LOOKBACK_MINUTES = 360
RESPONSE_READ_TIMEOUT_SECONDS = 20.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # noqa: D401
        self,
        request: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise ValueError("OpenLigaDB redirected; refusing to follow it")


def _validate_url(url: str, *, kind: str = "season") -> None:
    parsed = urlparse(url)
    path_pattern = OPENLIGADB_PATH if kind == "season" else OPENLIGADB_MATCH_PATH
    if (
        parsed.scheme != "https"
        or parsed.hostname != OPENLIGADB_HOST
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not path_pattern.fullmatch(parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("OpenLigaDB URL is not allowlisted")


def _parse_utc(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _score(result: object, *, result_type_id: int) -> dict[str, int] | None:
    if not isinstance(result, list):
        return None
    for row in result:
        if not isinstance(row, dict) or row.get("resultTypeID") != result_type_id:
            continue
        try:
            home = int(row["pointsTeam1"])
            away = int(row["pointsTeam2"])
        except (KeyError, TypeError, ValueError):
            continue
        if home < 0 or away < 0:
            continue
        return {"home": home, "away": away}
    return None


def _text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] if normalized else None


def _identifier(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value).strip()
    return normalized if normalized and normalized.isdigit() else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _venue(value: object) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    location_id = _identifier(value.get("locationID"))
    city = _text(value.get("locationCity"), limit=160)
    stadium = _text(value.get("locationStadium"), limit=200)
    if location_id is None and city is None and stadium is None:
        return None
    return {
        "provider_location_id": location_id,
        "city": city,
        "stadium": stadium,
    }


def _goals(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in value[:100]:
        if not isinstance(row, dict):
            continue
        goal_id = _identifier(row.get("goalID"))
        home_score = _nonnegative_int(row.get("scoreTeam1"))
        away_score = _nonnegative_int(row.get("scoreTeam2"))
        if goal_id is None or goal_id in seen or home_score is None or away_score is None:
            continue
        seen.add(goal_id)
        result.append(
            {
                "provider_goal_id": goal_id,
                "home_score": home_score,
                "away_score": away_score,
                "minute": _nonnegative_int(row.get("matchMinute")),
                "provider_scorer_id": _identifier(row.get("goalGetterID")),
                "scorer": _text(row.get("goalGetterName"), limit=160),
                "provider_scoring_team_id": _identifier(row.get("scoringTeamId")),
                "penalty": row.get("isPenalty") if isinstance(row.get("isPenalty"), bool) else None,
                "own_goal": row.get("isOwnGoal") if isinstance(row.get("isOwnGoal"), bool) else None,
                "overtime": row.get("isOvertime") if isinstance(row.get("isOvertime"), bool) else None,
                "comment": _text(row.get("comment"), limit=240),
            }
        )
    return result


def _source(payload: bytes, *, retrieved_at: datetime, url: str) -> dict[str, Any]:
    return {
        "name": "OpenLigaDB",
        "url": url,
        "retrieved_at": retrieved_at.astimezone(timezone.utc).isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "license": "ODbL-1.0",
        "license_url": "https://www.openligadb.de/lizenz",
        "attribution_required": True,
        "share_alike_required": True,
        "source_kind": "community_fixture_and_result",
    }


def _record(row: object, *, source: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    kickoff_at = _parse_utc(row.get("matchDateTimeUTC"))
    raw_team1 = row.get("team1")
    raw_team2 = row.get("team2")
    team1: dict[str, Any] = raw_team1 if isinstance(raw_team1, dict) else {}
    team2: dict[str, Any] = raw_team2 if isinstance(raw_team2, dict) else {}
    home = _text(team1.get("teamName"), limit=160)
    away = _text(team2.get("teamName"), limit=160)
    match_id = _identifier(row.get("matchID"))
    if not kickoff_at or not home or not away or not match_id:
        return None
    finished = bool(row.get("matchIsFinished"))
    return {
        "id": f"openligadb:{match_id}",
        "provider_match_id": match_id,
        "competition_id": "bundesliga",
        "season": str(row.get("leagueSeason") or ""),
        "kickoff_at": kickoff_at,
        "home_team": home,
        "away_team": away,
        "home_provider_team_id": _identifier(team1.get("teamId")) or "",
        "away_provider_team_id": _identifier(team2.get("teamId")) or "",
        "status": "finished" if finished else "upcoming",
        "score": _score(row.get("matchResults"), result_type_id=2) if finished else None,
        "halftime_score": _score(row.get("matchResults"), result_type_id=1),
        "provider_updated_at": _parse_utc(row.get("lastUpdateDateTime")),
        "venue": _venue(row.get("location")),
        "goals": _goals(row.get("goals")),
        "source": source,
    }


def parse_openligadb_payload(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str,
) -> list[dict[str, Any]]:
    """Parse one Bundesliga season while preserving source timing and hashes."""

    _validate_url(url)
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("OpenLigaDB response exceeded 10 MiB")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("OpenLigaDB response is not valid JSON") from exc
    if not isinstance(data, list):
        raise ValueError("OpenLigaDB response must be a list")
    source = _source(payload, retrieved_at=retrieved_at, url=url)
    return [record for row in data if (record := _record(row, source=source)) is not None]


def parse_openligadb_match_payload(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str,
    expected_match_id: str,
) -> dict[str, Any]:
    """Parse one match endpoint and bind both URL and body to its expected ID."""

    _validate_url(url, kind="match")
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    if len(payload) > MAX_MATCH_CONTENT_BYTES:
        raise ValueError("OpenLigaDB match response exceeded 1 MiB")
    expected = _identifier(expected_match_id)
    url_match = OPENLIGADB_MATCH_PATH.fullmatch(urlparse(url).path)
    if expected is None or url_match is None or url_match.group(1) != expected:
        raise ValueError("OpenLigaDB match identity does not match URL")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("OpenLigaDB match response is not valid JSON") from exc
    source = _source(payload, retrieved_at=retrieved_at, url=url)
    record = _record(data, source=source)
    if record is None or record.get("provider_match_id") != expected:
        raise ValueError("OpenLigaDB match identity does not match payload")
    return record


def _same_identity(target: dict[str, Any], observed: dict[str, Any]) -> bool:
    try:
        target_kickoff = datetime.fromisoformat(str(target["kickoff_at"]).replace("Z", "+00:00"))
        observed_kickoff = datetime.fromisoformat(str(observed["kickoff_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    if target_kickoff.tzinfo is None or observed_kickoff.tzinfo is None:
        return False
    target_home = _text(target.get("home_team"), limit=160)
    target_away = _text(target.get("away_team"), limit=160)
    observed_home = _text(observed.get("home_team"), limit=160)
    observed_away = _text(observed.get("away_team"), limit=160)
    if None in {target_home, target_away, observed_home, observed_away}:
        return False
    assert target_home is not None
    assert target_away is not None
    assert observed_home is not None
    assert observed_away is not None
    return (
        target.get("competition_id") == "bundesliga"
        and target_kickoff.astimezone(timezone.utc) == observed_kickoff.astimezone(timezone.utc)
        and team_id("bundesliga", target_home)
        == team_id("bundesliga", observed_home)
        and team_id("bundesliga", target_away)
        == team_id("bundesliga", observed_away)
    )


def _latest_goal_score(
    goals: list[dict[str, Any]],
) -> tuple[dict[str, int] | None, int | None]:
    if not goals:
        return None, None
    latest = max(
        goals,
        key=lambda row: (
            int(row["home_score"]) + int(row["away_score"]),
            row.get("minute") if isinstance(row.get("minute"), int) else -1,
            int(row["provider_goal_id"]),
        ),
    )
    return {
        "home": int(latest["home_score"]),
        "away": int(latest["away_score"]),
    }, latest.get("minute") if isinstance(latest.get("minute"), int) else None


def fetch_openligadb_live_updates(
    targets: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    opener: Any = None,
    max_matches: int = MAX_LIVE_MATCHES,
) -> dict[str, Any]:
    """Fetch individually joined Bundesliga matches for display-only updates."""

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    source_summary: dict[str, Any] = {
        "name": "OpenLigaDB",
        "role": "live_display_only",
        "license": "ODbL-1.0",
        "license_url": "https://www.openligadb.de/lizenz",
        "attribution_required": True,
        "share_alike_required": True,
    }
    result: dict[str, Any] = {
        "provider": "OpenLigaDB",
        "retrieved_at": reference.isoformat(),
        "source": source_summary,
        "fixture_updates": [],
        "incidents": [],
        "match_stats": [],
        "errors": [],
        "network_opened": False,
    }
    capped = max(0, min(MAX_LIVE_MATCHES, int(max_matches)))
    rows = [row for row in targets if isinstance(row, dict)][:capped]
    if not rows:
        return result
    client = opener or urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    for target in rows:
        fixture_id = _text(target.get("id"), limit=120)
        provider_match_id = _identifier(target.get("provider_match_id"))
        if fixture_id is None or provider_match_id is None or target.get("provider") != "OpenLigaDB":
            result["errors"].append({
                "fixture_id": fixture_id,
                "error_code": "invalid_target",
            })
            continue
        try:
            target_kickoff = datetime.fromisoformat(
                str(target["kickoff_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            target_kickoff = None
        if (
            target_kickoff is None
            or target_kickoff.tzinfo is None
            or reference < target_kickoff.astimezone(timezone.utc)
            or reference - target_kickoff.astimezone(timezone.utc)
            > timedelta(minutes=MAX_LIVE_LOOKBACK_MINUTES)
        ):
            result["errors"].append({
                "fixture_id": fixture_id,
                "provider_match_id": provider_match_id,
                "error_code": "outside_live_window",
            })
            continue
        url = f"https://{OPENLIGADB_HOST}/getmatchdata/{provider_match_id}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "Matchline/1.0 (+public-source)"},
        )
        try:
            result["network_opened"] = True
            with client.open(request, timeout=20) as response:
                final_url = response.geturl()
                _validate_url(final_url, kind="match")
                payload = read_response_bounded(
                    response,
                    MAX_MATCH_CONTENT_BYTES,
                    timeout_seconds=RESPONSE_READ_TIMEOUT_SECONDS,
                )
            observed = parse_openligadb_match_payload(
                payload,
                retrieved_at=reference,
                url=final_url,
                expected_match_id=provider_match_id,
            )
            if not _same_identity(target, observed):
                raise LookupError("strict fixture identity mismatch")
        except LookupError as exc:
            result["errors"].append({
                "fixture_id": fixture_id,
                "provider_match_id": provider_match_id,
                "error_code": "identity_mismatch",
                "error": str(exc),
            })
            continue
        except TimeoutError as exc:
            result["errors"].append({
                "fixture_id": fixture_id,
                "provider_match_id": provider_match_id,
                "error_code": "read_timeout",
                "error": str(exc),
            })
            continue
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            result["errors"].append({
                "fixture_id": fixture_id,
                "provider_match_id": provider_match_id,
                "error_code": "fetch_or_parse",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        provider_updated = _parse_utc(observed.get("provider_updated_at"))
        provider_updated_time = (
            datetime.fromisoformat(provider_updated)
            if provider_updated is not None
            else None
        )
        if (
            provider_updated_time is not None
            and provider_updated_time.astimezone(timezone.utc)
            > reference + timedelta(minutes=5)
        ):
            result["errors"].append({
                "fixture_id": fixture_id,
                "provider_match_id": provider_match_id,
                "error_code": "provider_time_future",
            })
            continue
        raw_goals = observed.get("goals")
        goals = (
            [goal for goal in raw_goals if isinstance(goal, dict)]
            if isinstance(raw_goals, list)
            else []
        )
        goal_score, clock = _latest_goal_score(goals)
        score = observed.get("score") if isinstance(observed.get("score"), dict) else goal_score
        source = observed["source"]
        # ``lastUpdateDateTime`` is record metadata.  A schedule, venue or
        # other edit after kickoff does not prove that play is active.  Keep a
        # kickoff-window row explicitly unconfirmed until a match event is
        # present; the provider's finished flag remains authoritative below.
        provider_live_observed = bool(goals)
        status = (
            "finished"
            if observed.get("status") == "finished"
            else "in_progress"
            if provider_live_observed
            else "in_progress_window"
        )
        result["fixture_updates"].append({
            "fixture_id": fixture_id,
            "provider_match_id": provider_match_id,
            "status": status,
            "score": score,
            "halftime_score": observed.get("halftime_score"),
            "result_scope": (
                "90_minute_including_stoppage_excluding_extra_time_and_penalties"
                if observed.get("status") == "finished"
                else None
            ),
            "clock": clock,
            "period": None,
            "status_text": (
                "provider_marked_finished"
                if observed.get("status") == "finished"
                else "provider_live_observation"
                if provider_live_observed
                else "kickoff_window_provider_not_finished"
            ),
            "provider_updated_at": observed.get("provider_updated_at"),
            "observed_at": reference.isoformat(),
            "source": source,
        })
        result["incidents"].append({
            "fixture_id": fixture_id,
            "provider_match_id": provider_match_id,
            "retrieved_at": reference.isoformat(),
            "events": [
                {
                    "event_id": f"openligadb:{goal['provider_goal_id']}",
                    "type": "goal",
                    "minute": goal.get("minute"),
                    "home_score": goal["home_score"],
                    "away_score": goal["away_score"],
                    "scorer": goal.get("scorer"),
                    "provider_scorer_id": goal.get("provider_scorer_id"),
                    "provider_scoring_team_id": goal.get("provider_scoring_team_id"),
                    "penalty": goal.get("penalty"),
                    "own_goal": goal.get("own_goal"),
                    "overtime": goal.get("overtime"),
                    "comment": goal.get("comment"),
                }
                for goal in goals
            ],
            "source": source,
        })
    return result


def _season_for(reference: datetime) -> int:
    return reference.year if reference.month >= 7 else reference.year - 1


def fetch_openligadb_fixtures(
    *, now: datetime | None = None, season: int | None = None, opener: Any = None
) -> dict[str, Any]:
    """Fetch one current Bundesliga season with bounded, non-following I/O."""

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        reference = reference.replace(tzinfo=timezone.utc)
    season_value = season if season is not None else _season_for(reference.astimezone(timezone.utc))
    if not isinstance(season_value, int) or not 1900 <= season_value <= 2200:
        raise ValueError("OpenLigaDB season is invalid")
    url = f"https://{OPENLIGADB_HOST}/getmatchdata/bl1/{season_value}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Matchline/1.0 (+public-source)",
        },
    )
    try:
        client = opener or urllib.request.build_opener(
            _NoRedirect,
            urllib.request.ProxyHandler({}),
        )
        with client.open(request, timeout=20) as response:
            final_url = response.geturl()
            _validate_url(final_url)
            payload = read_response_bounded(
                response,
                MAX_CONTENT_BYTES,
                timeout_seconds=RESPONSE_READ_TIMEOUT_SECONDS,
            )
        if len(payload) > MAX_CONTENT_BYTES:
            raise ValueError("OpenLigaDB response exceeded 10 MiB")
        matches = parse_openligadb_payload(payload, retrieved_at=reference, url=final_url)
        return {
            "provider": "OpenLigaDB",
            "season": season_value,
            "retrieved_at": reference.astimezone(timezone.utc).isoformat(),
            "matches": matches,
            "errors": [],
        }
    except TimeoutError as exc:
        return {
            "provider": "OpenLigaDB",
            "season": season_value,
            "retrieved_at": reference.astimezone(timezone.utc).isoformat(),
            "matches": [],
            "errors": [{"stage": "fetch", "error_code": "read_timeout", "error": str(exc)}],
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "provider": "OpenLigaDB",
            "season": season_value,
            "retrieved_at": reference.astimezone(timezone.utc).isoformat(),
            "matches": [],
            "errors": [{"stage": "fetch_or_parse", "error": str(exc)}],
        }


__all__ = [
    "OPENLIGADB_HOST",
    "OPENLIGADB_MATCH_PATH",
    "OPENLIGADB_PATH",
    "fetch_openligadb_fixtures",
    "fetch_openligadb_live_updates",
    "parse_openligadb_match_payload",
    "parse_openligadb_payload",
]
