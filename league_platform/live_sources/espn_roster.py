"""Bounded public ESPN team-roster observations.

The event-summary endpoint does not consistently expose a roster.  ESPN's
public web API has a separate team-roster endpoint, so this adapter fetches a
small, time-bounded set of teams around the next matches.  The result is
append-only evidence for research/display; it is never treated as a confirmed
XI, injury report, or model feature.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping
from urllib.parse import urlparse

from league_platform.live_sources.espn import (
    ESPN_CODES,
    _read_limited,
    _espn_rights_block,
    _safe_opener,
    _validate_response_url,
)
from league_platform.source_rights import SourceId, rights_blocked_envelope


ESPN_WEB_HOST = "site.web.api.espn.com"
ESPN_ROSTER_PATH_PREFIX = "/apis/site/v2/sports/soccer/"
DEFAULT_ROSTER_HORIZON_HOURS = 72
# The 72-hour multi-league window can contain more than forty distinct
# teams.  Keep a deliberately finite fan-out with enough headroom for the
# current model-competition priority set; this remains display/audit evidence and
# never becomes a model feature by itself.
MAX_ROSTER_TEAMS = 96
DEFAULT_MAX_ROSTER_TEAMS = MAX_ROSTER_TEAMS
MAX_ROSTER_BYTES = 2 * 1024 * 1024
MAX_ATHLETES = 80
_TEAM_ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")


def _text(value: object, *, max_length: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= max_length else None


def _numeric_id(value: object) -> str | None:
    text = _text(value, max_length=32)
    return text if text and _TEAM_ID_RE.fullmatch(text) else None


def _position(value: object) -> str | None:
    if isinstance(value, Mapping):
        return _text(
            value.get("displayName") or value.get("name") or value.get("abbreviation"),
            max_length=80,
        )
    return _text(value, max_length=80)


def _status(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, Mapping):
        return (
            _text(
                value.get("displayName") or value.get("name") or value.get("abbreviation"),
                max_length=80,
            ),
            _text(value.get("type"), max_length=40),
        )
    return _text(value, max_length=80), None


def _profile_url(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, Mapping):
            continue
        href = _text(item.get("href"), max_length=2_048)
        if not href:
            continue
        parsed = urlparse(href)
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"www.espn.com", "site.web.api.espn.com"}
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        ):
            return href
    return None


def _athlete(row: object) -> dict | None:
    if not isinstance(row, Mapping):
        return None
    provider_id = _numeric_id(row.get("id"))
    name = _text(row.get("displayName") or row.get("fullName"), max_length=160)
    if not provider_id or not name:
        return None
    status, status_type = _status(row.get("status"))
    age = row.get("age")
    if isinstance(age, bool) or not isinstance(age, int) or not 12 <= age <= 60:
        age = None
    jersey = _text(row.get("jersey"), max_length=8)
    if jersey is not None and not re.fullmatch(r"[0-9]{1,3}", jersey):
        jersey = None
    result = {
        "provider_player_id": provider_id,
        "name": name,
        "position": _position(row.get("position")),
        "status": status,
        "status_type": status_type,
        "jersey": jersey,
        "age": age,
        "citizenship": _text(row.get("citizenship"), max_length=80),
        "profile_url": _profile_url(row.get("links")),
        "starter": None,
        "expected_minutes": None,
    }
    return {key: value for key, value in result.items() if value is not None}


def parse_espn_roster(
    payload: bytes,
    *,
    competition_id: str,
    provider_team_id: str,
    scheduled_fixture_ids: list[str],
    scheduled_kickoffs: list[str],
    retrieved_at: datetime,
    url: str,
) -> dict:
    """Parse one bounded roster response without preserving the raw payload."""

    data = json.loads(payload)
    if not isinstance(data, Mapping):
        raise ValueError("ESPN roster JSON root must be an object")
    team = data.get("team") if isinstance(data.get("team"), Mapping) else {}
    team_id = _numeric_id(team.get("id")) or provider_team_id
    if team_id != provider_team_id:
        raise ValueError("ESPN roster team id does not match request")
    team_name = _text(team.get("displayName") or team.get("name"), max_length=160)
    athletes = data.get("athletes")
    if not isinstance(athletes, list):
        raise ValueError("ESPN roster athletes must be a list")
    players = []
    for row in athletes[:MAX_ATHLETES]:
        parsed = _athlete(row)
        if parsed is not None:
            players.append(parsed)
    source = {
        "name": "ESPN team roster",
        "url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }
    provider_timestamp = _text(data.get("timestamp"), max_length=80)
    season = data.get("season") if isinstance(data.get("season"), Mapping) else {}
    return {
        "competition_id": competition_id,
        "provider_team_id": team_id,
        "team_name": team_name,
        "team_abbreviation": _text(team.get("abbreviation"), max_length=16),
        "season": {
            key: value
            for key, value in {
                "year": season.get("year"),
                "display_name": _text(season.get("displayName"), max_length=160),
            }.items()
            if value is not None
        },
        "provider_timestamp": provider_timestamp,
        "scheduled_fixture_ids": sorted(set(scheduled_fixture_ids)),
        "scheduled_kickoffs": sorted(set(scheduled_kickoffs)),
        "athletes": players,
        "athlete_count": len(players),
        "status": "ok" if players else "empty_roster",
        "enters_model": False,
        "model_eligible": False,
        "model_exclusion_reason": "team_roster_not_fixture_specific_or_confirmed_lineup",
        "source": source,
        "retrieved_at": retrieved_at.isoformat(),
    }


def _roster_url(competition_id: str, provider_team_id: str) -> str:
    code = ESPN_CODES.get(competition_id)
    if not code or not _TEAM_ID_RE.fullmatch(provider_team_id):
        raise ValueError("unknown ESPN competition or invalid provider team id")
    return (
        f"https://{ESPN_WEB_HOST}{ESPN_ROSTER_PATH_PREFIX}{code}/teams/{provider_team_id}/roster"
    )


def _candidate_teams(
    fixtures: list[dict],
    *,
    reference_time: datetime,
    horizon_hours: int,
    max_teams: int,
) -> tuple[list[dict], int, int]:
    # Do not let a caller turn the public endpoint into an unbounded crawler.
    # ``bool`` is accepted as an int by Python, but treating it as a request
    # for one team would be surprising and is not useful operationally.
    bounded_max_teams = max(0, min(MAX_ROSTER_TEAMS, int(max_teams)))
    cutoff = reference_time.astimezone(timezone.utc) + timedelta(hours=max(0, horizon_hours))
    grouped: dict[tuple[str, str], dict] = {}
    invalid = 0
    for fixture in fixtures:
        if not isinstance(fixture, Mapping) or fixture.get("status") != "upcoming":
            continue
        competition_id = _text(fixture.get("competition_id"), max_length=80)
        if competition_id not in ESPN_CODES:
            continue
        try:
            kickoff = datetime.fromisoformat(str(fixture.get("kickoff_at")).replace("Z", "+00:00"))
        except ValueError:
            invalid += 1
            continue
        if (
            kickoff.tzinfo is None
            or not reference_time.astimezone(timezone.utc)
            <= kickoff.astimezone(timezone.utc)
            <= cutoff
        ):
            continue
        for side in ("home", "away"):
            team_id = _numeric_id(fixture.get(f"{side}_provider_team_id"))
            if team_id is None:
                invalid += 1
                continue
            key = (competition_id, team_id)
            row = grouped.setdefault(
                key,
                {
                    "competition_id": competition_id,
                    "provider_team_id": team_id,
                    "scheduled_fixture_ids": [],
                    "scheduled_kickoffs": [],
                    "first_kickoff": kickoff.astimezone(timezone.utc),
                },
            )
            fixture_id = _text(fixture.get("id"), max_length=160)
            if fixture_id:
                row["scheduled_fixture_ids"].append(fixture_id)
            row["scheduled_kickoffs"].append(kickoff.astimezone(timezone.utc).isoformat())
            row["first_kickoff"] = min(row["first_kickoff"], kickoff.astimezone(timezone.utc))
    ordered = sorted(
        grouped.values(),
        key=lambda row: (row["first_kickoff"], row["competition_id"], row["provider_team_id"]),
    )
    truncated = max(0, len(ordered) - bounded_max_teams)
    return ordered[:bounded_max_teams], invalid, truncated


def fetch_espn_team_rosters(
    fixtures: list[dict],
    *,
    now: datetime | None = None,
    horizon_hours: int = DEFAULT_ROSTER_HORIZON_HOURS,
    max_teams: int = DEFAULT_MAX_ROSTER_TEAMS,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: str | None = None,
) -> dict:
    """Fetch distinct teams playing soon, with per-team failure isolation."""

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    return {
        **rights_blocked_envelope(
            SourceId.ESPN_TEAM_ROSTERS,
            provider="ESPN team roster",
            checked_at=reference_time.astimezone(timezone.utc).isoformat(),
            empty_fields=("rosters",),
            authorization_reference=authorization_reference,
        ),
        "authorization_required": "express_written_permission",
        "terms_url": "https://disneytermsofuse.com/english/",
        "enters_model": False,
        "model_exclusion_reason": "provider_rights_blocked",
        "horizon_hours": horizon_hours,
        "requested_teams": 0,
        "invalid_candidate_count": 0,
        "truncated_team_count": 0,
    }
    # Retained below as a parser/reference implementation only.  Policy v260
    # deliberately returns above before constructing an opener or request.
    authorization_reference = (
        authorization_reference.strip()
        if isinstance(authorization_reference, str) and authorization_reference.strip()
        else None
    )
    if authorization_reference is None:
        return {
            **_espn_rights_block(
                provider="ESPN team roster",
                checked_at=reference_time,
            ),
            "horizon_hours": horizon_hours,
            "requested_teams": 0,
            "invalid_candidate_count": 0,
            "truncated_team_count": 0,
            "rosters": [],
        }
    candidates, invalid, truncated = _candidate_teams(
        fixtures,
        reference_time=reference_time,
        horizon_hours=horizon_hours,
        max_teams=max_teams,
    )
    fetcher = opener or _safe_opener()

    def fetch_one(candidate: dict) -> dict:
        url = _roster_url(candidate["competition_id"], candidate["provider_team_id"])
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "Matchline/1.0"},
        )
        payload = _read_limited(fetcher, request, max_bytes=MAX_ROSTER_BYTES)
        _validate_response_url(url)
        observed_at = reference_time if now is not None else datetime.now(timezone.utc)
        return parse_espn_roster(
            payload,
            competition_id=candidate["competition_id"],
            provider_team_id=candidate["provider_team_id"],
            scheduled_fixture_ids=candidate["scheduled_fixture_ids"],
            scheduled_kickoffs=candidate["scheduled_kickoffs"],
            retrieved_at=observed_at,
            url=url,
        )

    rosters: list[dict] = []
    errors: list[dict] = []
    observation_times: list[datetime] = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(candidates)))) as executor:
        futures = {executor.submit(fetch_one, candidate): candidate for candidate in candidates}
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                errors.append(
                    {
                        "competition_id": candidate["competition_id"],
                        "provider_team_id": candidate["provider_team_id"],
                        "url": _roster_url(
                            candidate["competition_id"], candidate["provider_team_id"]
                        ),
                        "error": str(exc),
                    }
                )
            else:
                rosters.append(row)
                observation_times.append(datetime.fromisoformat(row["retrieved_at"]))
    status = (
        "fresh"
        if rosters and not errors
        else "degraded"
        if rosters
        else "unavailable"
        if errors
        else "not_requested"
    )
    return {
        "provider": "ESPN team roster",
        "retrieved_at": max(observation_times, default=reference_time).isoformat(),
        "status": status,
        "horizon_hours": horizon_hours,
        "requested_teams": len(candidates),
        "invalid_candidate_count": invalid,
        "truncated_team_count": truncated,
        "rosters": sorted(
            rosters, key=lambda row: (row["competition_id"], row["provider_team_id"])
        ),
        "errors": sorted(errors, key=lambda row: (row["competition_id"], row["provider_team_id"])),
        "access_allowed": True,
        "rights_status": "operator_authorization_reference_supplied",
        "authorization_reference": authorization_reference,
        "enters_model": False,
        "model_exclusion_reason": "team_roster_not_fixture_specific_or_confirmed_lineup",
    }


__all__ = [
    "DEFAULT_MAX_ROSTER_TEAMS",
    "DEFAULT_ROSTER_HORIZON_HOURS",
    "ESPN_ROSTER_PATH_PREFIX",
    "MAX_ROSTER_TEAMS",
    "MAX_ROSTER_BYTES",
    "fetch_espn_team_rosters",
    "parse_espn_roster",
]
