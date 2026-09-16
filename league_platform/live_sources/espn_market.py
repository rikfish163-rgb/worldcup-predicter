"""Per-fixture current market adapter from ESPN event summaries."""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from league_platform.live_sources.espn import (
    ESPN_CODES,
    RESULT_SCOPE_MAP,
    STATUS_MAP,
    _read_limited,
    _espn_rights_block,
    _safe_opener,
)
from league_platform.source_rights import SourceId, rights_blocked_envelope


ESPN_WEB_HOST = "site.web.api.espn.com"
DEFAULT_POSTMATCH_REPLAY_DAYS = 2
DEFAULT_POSTMATCH_REPLAY_LIMIT = 24


def _american_probability(odds: float) -> float:
    if odds == 0:
        raise ValueError("American odds cannot be zero")
    return -odds / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def _period_score(value: object) -> int | None:
    """Read one ESPN soccer period score without coercing invalid values."""

    raw = value
    if isinstance(value, dict):
        raw = value.get("displayValue", value.get("value", value.get("score")))
    try:
        number = int(float(raw))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _halftime_score(competitors: dict[str, dict], *, status_name: object) -> dict[str, int] | None:
    """Return the first-period score only after the first period is complete.

    ESPN exposes soccer ``linescores`` as period goal counts.  During the
    first half the first entry is still provisional, so it is deliberately
    ignored until halftime/second-half/full-time status is reported.
    """

    if status_name not in {
        "STATUS_HALFTIME",
        "STATUS_SECOND_HALF",
        "STATUS_FULL_TIME",
        "STATUS_FINAL_AET",
        "STATUS_FINAL_PEN",
    }:
        return None
    values: dict[str, int] = {}
    for side in ("home", "away"):
        linescores = competitors[side].get("linescores")
        if not isinstance(linescores, list) or not linescores:
            return None
        score = _period_score(linescores[0])
        if score is None:
            return None
        values[side] = score
    return values


def _espn_player(row: object) -> dict | None:
    """Keep a small, non-authoritative player row from an ESPN roster."""

    if not isinstance(row, dict):
        return None
    athlete = row.get("athlete") if isinstance(row.get("athlete"), dict) else {}
    player_id = athlete.get("id") or row.get("id")
    raw_names = (
        athlete.get("displayName"),
        athlete.get("fullName"),
        athlete.get("shortName"),
        row.get("name"),
    )
    aliases = list(
        dict.fromkeys(
            str(value).strip() for value in raw_names if isinstance(value, str) and value.strip()
        )
    )
    name = aliases[0] if aliases else None
    if player_id is None or not name:
        return None
    position = row.get("position")
    if isinstance(position, dict):
        position = (
            position.get("displayName") or position.get("name") or position.get("abbreviation")
        )
    status = row.get("status")
    if not isinstance(status, str) or not status.strip():
        status = (
            "starter"
            if row.get("starter") is True
            else "active_roster"
            if row.get("active") is not False
            else "inactive"
        )
    result = {
        "player_id": str(player_id),
        "name": str(name),
        "aliases": aliases,
        "position": str(position) if position else None,
        "shirt_number": (
            str(row.get("jersey") or athlete.get("jersey")).strip()
            if row.get("jersey") not in (None, "") or athlete.get("jersey") not in (None, "")
            else None
        ),
        "starter": row.get("starter") if isinstance(row.get("starter"), bool) else None,
        "status": status,
    }
    return {key: value for key, value in result.items() if value is not None}


def _team_status(
    data: dict,
    *,
    source: dict,
    retrieved_at: datetime,
    kickoff_at: str | None = None,
) -> dict | None:
    """Parse ESPN roster evidence and promote only an explicit causal XI.

    ESPN is a reliable public provider, not a club/league first-party source.
    Its roster payload is therefore eligible as a fallback only when both
    sides expose exactly eleven explicit ``starter=true`` rows and the
    response was observed before the canonical kickoff.  A normal roster,
    an incomplete response, or a post-kickoff response remains display-only.
    """

    rosters = data.get("rosters")
    if not isinstance(rosters, list):
        return None
    teams: dict[str, dict] = {}
    for roster in rosters:
        if not isinstance(roster, dict) or roster.get("homeAway") not in {"home", "away"}:
            continue
        team = roster.get("team") if isinstance(roster.get("team"), dict) else {}
        players = [
            player
            for player in (_espn_player(row) for row in (roster.get("roster") or []))
            if player is not None
        ]
        if not players and not team:
            continue
        teams[roster["homeAway"]] = {
            "provider_team_id": str(team["id"]) if team.get("id") is not None else None,
            "name": team.get("displayName") or team.get("shortDisplayName"),
            "players": players,
            "full_roster": len(players) >= 11,
            "starter_count": sum(player.get("starter") is True for player in players),
        }
    if not teams:
        return None
    starter_complete = all(
        isinstance(teams.get(side), dict) and teams[side].get("starter_count") == 11
        for side in ("home", "away")
    )
    before_kickoff = False
    if isinstance(kickoff_at, str) and kickoff_at.strip():
        try:
            kickoff = datetime.fromisoformat(kickoff_at.replace("Z", "+00:00"))
            before_kickoff = kickoff.tzinfo is not None and retrieved_at < kickoff.astimezone(
                timezone.utc
            )
        except ValueError:
            before_kickoff = False
    confirmed = bool(starter_complete)
    model_eligible = bool(confirmed and before_kickoff)
    return {
        "provider": "ESPN event summary",
        "confirmed": confirmed,
        "model_eligible": model_eligible,
        "status": "confirmed_lineup" if confirmed else "roster_evidence_only",
        "teams": teams,
        "source": source,
        "note": (
            "双方均有 11 名明确 starter 且观测早于开赛，作为可靠公开提供方的首发回退证据。"
            if model_eligible
            else "赛事摘要名单不满足双方赛前 11 人首发条件，未进入最终概率模型。"
        ),
    }


def parse_espn_team_status(
    payload: bytes,
    *,
    fixture_id: str,
    retrieved_at: datetime,
    url: str,
    kickoff_at: str | None = None,
) -> dict | None:
    """Parse roster evidence even when an event has no usable market."""

    data = json.loads(payload)
    source = {
        "name": "ESPN event summary",
        "url": url,
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }
    status = _team_status(
        data,
        source=source,
        retrieved_at=retrieved_at,
        kickoff_at=kickoff_at,
    )
    if status is None:
        return None
    return {
        "fixture_id": fixture_id,
        "retrieved_at": retrieved_at.isoformat(),
        **status,
    }


def parse_espn_market(
    payload: bytes,
    *,
    fixture_id: str,
    retrieved_at: datetime,
    url: str,
    kickoff_at: str | None = None,
) -> dict | None:
    data = json.loads(payload)
    candidates = data.get("pickcenter") or []
    if not candidates:
        return None
    market = candidates[0]
    try:
        raw = {
            "home": float(market["homeTeamOdds"]["moneyLine"]),
            "draw": float(market["drawOdds"]["moneyLine"]),
            "away": float(market["awayTeamOdds"]["moneyLine"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value != 0 for value in raw.values()):
        return None
    implied = {key: _american_probability(value) for key, value in raw.items()}
    total = sum(implied.values())
    team_status = parse_espn_team_status(
        payload,
        fixture_id=fixture_id,
        retrieved_at=retrieved_at,
        url=url,
        kickoff_at=kickoff_at,
    )
    source = (
        team_status["source"]
        if team_status
        else {
            "name": "ESPN event summary",
            "url": url,
            "raw_sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    return {
        "fixture_id": fixture_id,
        "provider": market.get("provider", {}).get("name", "unknown"),
        "american_odds": raw,
        "probability": {key: round(value / total, 6) for key, value in implied.items()},
        "retrieved_at": retrieved_at.isoformat(),
        "source": source,
        "team_status": team_status,
    }


def parse_espn_event_update(
    payload: bytes,
    *,
    fixture_id: str,
    retrieved_at: datetime,
    url: str,
    kickoff_at: str | None = None,
) -> dict | None:
    """Parse causal event status/score from an ESPN summary response.

    The scoreboard feed can lag while the per-event summary already reports
    live or full-time state.  Keep this update separate from market parsing so
    a public summary can refresh result status without making a score from a
    live/half-time response eligible for final scoring.
    """

    data = json.loads(payload)
    header = data.get("header") if isinstance(data.get("header"), dict) else {}
    competitions = header.get("competitions")
    if not isinstance(competitions, list) or not competitions:
        return None
    competition = competitions[0]
    if not isinstance(competition, dict):
        return None
    status_value = competition.get("status")
    status_type = status_value.get("type") if isinstance(status_value, dict) else {}
    status_name = status_type.get("name") if isinstance(status_type, dict) else None
    status = STATUS_MAP.get(status_name)
    if status is None:
        return None
    competitors = competition.get("competitors")
    if not isinstance(competitors, list):
        return None
    by_side = {
        item.get("homeAway"): item
        for item in competitors
        if isinstance(item, dict) and item.get("homeAway") in {"home", "away"}
    }
    if set(by_side) != {"home", "away"}:
        return None

    score = None
    score_values: dict[str, int] = {}
    for side in ("home", "away"):
        value = by_side[side].get("score")
        try:
            numeric = int(float(value))
        except (TypeError, ValueError):
            numeric = -1
        if numeric < 0:
            break
        score_values[side] = numeric
    if len(score_values) == 2:
        score = score_values
    halftime_score = _halftime_score(by_side, status_name=status_name)
    source = {
        "name": "ESPN event summary",
        "url": url,
        "native_fixture_id": str(fixture_id).split(":", 1)[-1],
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }
    update = {
        "fixture_id": fixture_id,
        "status": status,
        "result_scope": RESULT_SCOPE_MAP.get(status_name),
        "score": score if status == "finished" else score,
        "observed_at": retrieved_at.isoformat(),
        "source": source,
    }
    if isinstance(status_value, dict):
        clock = _incident_clock(
            status_value.get("displayClock")
            or status_value.get("clock")
            or status_value.get("detail")
        )
        period = status_value.get("period")
        status_text = _incident_text(
            status_value.get("shortDetail")
            or status_value.get("detail")
            or status_type.get("shortDetail")
            or status_type.get("detail")
            or status_type.get("name")
        )
        if clock:
            update["clock"] = clock
        if isinstance(period, (int, float)) and not isinstance(period, bool) and period >= 0:
            update["period"] = int(period)
        if status_text:
            update["status_text"] = status_text
    if halftime_score is not None:
        update["halftime_score"] = halftime_score
    return update


def _incident_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:500]
    if isinstance(value, dict):
        for key in ("text", "displayValue", "displayName", "fullName", "name", "abbreviation"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:500]
    return None


def _incident_clock(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("displayValue", "text", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:40]
            if isinstance(candidate, (int, float)) and math.isfinite(float(candidate)):
                return str(candidate)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()[:40]
    return None


def _incident_coordinate(value: object) -> float | None:
    """Return a provider-declared field coordinate in ESPN's 0..100 range.

    Coordinates are kept only when the provider supplies a finite numeric
    value.  We intentionally do not coerce arbitrary strings or synthesize a
    missing axis, because the event map is an audit/display surface rather
    than a model feature.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 100:
        return None
    return round(numeric, 3)


def _incident_location(raw: dict) -> dict | None:
    """Project only explicit ESPN event coordinates.

    ESPN has used fieldPositionX/Y and fieldPosition2X/Y for event origins and
    targets.  ``goalPositionY`` is retained as a separate ordinate when
    present; no goal-line X coordinate is invented from it.
    """

    origin_x = _incident_coordinate(raw.get("fieldPositionX"))
    origin_y = _incident_coordinate(raw.get("fieldPositionY"))
    target_x = _incident_coordinate(raw.get("fieldPosition2X"))
    target_y = _incident_coordinate(raw.get("fieldPosition2Y"))
    goal_y = _incident_coordinate(raw.get("goalPositionY"))
    origin = (
        {"x": origin_x, "y": origin_y} if origin_x is not None and origin_y is not None else None
    )
    target = (
        {"x": target_x, "y": target_y} if target_x is not None and target_y is not None else None
    )
    if origin is None and target is None and goal_y is None:
        return None
    location = {
        "coordinate_system": "espn_field_percent",
        "source": "provider_declared",
        "provider_declared": True,
        "origin": origin,
        "target": target,
        "goal_y": goal_y,
        "model_eligible": False,
    }
    return {key: value for key, value in location.items() if value is not None}


_DISPLAY_STATS = frozenset(
    {
        "foulsCommitted",
        "yellowCards",
        "redCards",
        "offsides",
        "wonCorners",
        "saves",
        "possessionPct",
        "totalShots",
        "shotsOnTarget",
        "shotPct",
        "penaltyKickGoals",
        "penaltyKickShots",
        "accuratePasses",
        "totalPasses",
        "passPct",
        "accurateCrosses",
        "totalCrosses",
        "crossPct",
        "totalLongBalls",
        "accurateLongBalls",
        "longBallPct",
        "blockedShots",
        "tacklesWon",
        "tackles",
        "tacklePct",
        "clearance",
        "interceptions",
        "effectiveClearance",
        "goalConceded",
    }
)

# ESPN's player boxscore schema differs slightly between soccer competitions.
# Keep a small numeric allowlist and never expose the provider's unbounded
# athlete payload verbatim. These facts are display/audit-only.
_DISPLAY_PLAYER_STATS = frozenset(
    {
        "minutesPlayed",
        "starts",
        "appearances",
        "goals",
        "totalGoals",
        "assists",
        "goalAssists",
        "shots",
        "totalShots",
        "shotsOnTarget",
        "foulsCommitted",
        "foulsSuffered",
        "yellowCards",
        "redCards",
        "saves",
        "savesInsideBox",
        "offsides",
        "tackles",
        "tacklesWon",
        "effectiveTackles",
        "interceptions",
        "clearances",
        "totalClearance",
        "blockedShots",
        "totalPasses",
        "accuratePasses",
        "keyPasses",
        "touches",
        "duelsWon",
        "aerialDuelsWon",
        "dribblesWon",
        "defensiveInterventions",
        "subIns",
        "ownGoals",
        "goalsConceded",
        "shotsFaced",
    }
)

# The event-summary API uses a few provider-specific names in its roster
# statistics.  Normalize only aliases that have an unambiguous meaning in the
# existing display contract, while retaining the provider name for audit.
_PLAYER_STAT_CANONICAL_NAMES = {
    "totalGoals": "goals",
    "goalAssists": "assists",
    "totalShots": "shots",
    "effectiveTackles": "tacklesWon",
    "totalClearance": "clearances",
}


def _stat_number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().rstrip("%")
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _player_identity(value: object) -> tuple[str, str, str | None, str | None] | None:
    athlete = value if isinstance(value, dict) else {}
    nested = athlete.get("athlete") if isinstance(athlete.get("athlete"), dict) else {}
    player_id = nested.get("id") or athlete.get("id")
    name = (
        nested.get("displayName")
        or nested.get("fullName")
        or athlete.get("displayName")
        or athlete.get("fullName")
        or athlete.get("name")
    )
    if player_id in (None, "") or not isinstance(name, str) or not name.strip():
        return None
    position = nested.get("position") or athlete.get("position")
    if isinstance(position, dict):
        position = (
            position.get("displayName") or position.get("name") or position.get("abbreviation")
        )
    starter = (
        nested.get("starter")
        if isinstance(nested.get("starter"), bool)
        else athlete.get("starter")
    )
    return (
        str(player_id),
        name.strip()[:160],
        str(position)[:80] if position else None,
        starter if isinstance(starter, bool) else None,
    )


def _player_statistics(raw_players: object, team_by_id: dict[str, dict]) -> list[dict]:
    if not isinstance(raw_players, list):
        return []
    merged: dict[str, dict] = {}
    for team_block in raw_players[:4]:
        if not isinstance(team_block, dict):
            continue
        team = team_block.get("team") if isinstance(team_block.get("team"), dict) else {}
        team_id = str(team.get("id")) if team.get("id") not in (None, "") else None
        team_name = _incident_text(team.get("displayName")) or _incident_text(
            team.get("shortDisplayName")
        )
        if team_name is None and team_id is not None:
            team_name = team_by_id.get(team_id, {}).get("name")
        team_side = (
            team_block.get("homeAway") if team_block.get("homeAway") in {"home", "away"} else None
        )
        blocks = team_block.get("statistics")
        if not isinstance(blocks, list):
            blocks = [team_block]
        for block in blocks[:8]:
            if not isinstance(block, dict):
                continue
            keys = block.get("keys")
            athletes = block.get("athletes")
            if not isinstance(athletes, list):
                athletes = block.get("players") if isinstance(block.get("players"), list) else []
            keys = [key for key in keys] if isinstance(keys, list) else []
            for athlete_row in athletes[:80]:
                identity = _player_identity(athlete_row)
                if identity is None:
                    continue
                player_id, name, position, starter = identity
                entry = merged.setdefault(
                    player_id,
                    {
                        "player_id": player_id,
                        "name": name,
                        "position": position,
                        "starter": starter,
                        "provider_team_id": team_id,
                        "team_name": team_name,
                        "team_side": team_side,
                        "statistics": [],
                    },
                )
                if entry.get("position") is None and position is not None:
                    entry["position"] = position
                if entry.get("starter") is None and starter is not None:
                    entry["starter"] = starter
                if entry.get("provider_team_id") is None and team_id is not None:
                    entry["provider_team_id"] = team_id
                if entry.get("team_name") is None and team_name is not None:
                    entry["team_name"] = team_name
                if entry.get("team_side") is None and team_side is not None:
                    entry["team_side"] = team_side
                raw_values = athlete_row.get("stats") if isinstance(athlete_row, dict) else None
                display_values = (
                    athlete_row.get("displayValues") if isinstance(athlete_row, dict) else None
                )
                if isinstance(raw_values, dict):
                    pairs = [
                        (str(key), value, raw_values.get(key)) for key, value in raw_values.items()
                    ]
                elif isinstance(raw_values, list) and keys:
                    pairs = [
                        (
                            str(key),
                            raw_values[index],
                            display_values[index]
                            if isinstance(display_values, list) and index < len(display_values)
                            else None,
                        )
                        for index, key in enumerate(keys[:32])
                        if index < len(raw_values)
                    ]
                else:
                    direct = (
                        athlete_row.get("statistics") if isinstance(athlete_row, dict) else None
                    )
                    pairs = []
                    if isinstance(direct, list):
                        for item in direct[:32]:
                            if isinstance(item, dict):
                                pairs.append(
                                    (
                                        str(item.get("name", "")),
                                        item.get("value", item.get("displayValue")),
                                        item.get("displayValue"),
                                    )
                                )
                known = {item["name"] for item in entry["statistics"]}
                for key, raw_value, display_value in pairs:
                    if key not in _DISPLAY_PLAYER_STATS or key in known:
                        continue
                    value = _stat_number(raw_value)
                    if value is None:
                        continue
                    canonical_name = _PLAYER_STAT_CANONICAL_NAMES.get(key, key)
                    if canonical_name in known:
                        continue
                    entry["statistics"].append(
                        {
                            "name": canonical_name,
                            "provider_name": key if canonical_name != key else None,
                            "label": key,
                            "value": value,
                            "display_value": _incident_text(display_value) or str(value),
                        }
                    )
                    known.add(canonical_name)
    players = [entry for entry in merged.values() if entry["statistics"]]
    for entry in players:
        entry["statistics"] = entry["statistics"][:24]
    return players[:60]


def _roster_player_statistics(raw_rosters: object, team_by_id: dict[str, dict]) -> list[dict]:
    """Parse ESPN's current ``rosters[*].roster[*].stats`` shape.

    ESPN has published player match statistics in the event ``rosters`` block
    rather than ``boxscore.players`` for several soccer competitions.  This
    path is intentionally separate from lineup evidence: it is only called
    for the live/post-match display projection and never changes the
    pre-match team-status eligibility decision.
    """

    if not isinstance(raw_rosters, list):
        return []
    merged: dict[str, dict] = {}
    for roster_block in raw_rosters[:2]:
        if not isinstance(roster_block, dict):
            continue
        team = roster_block.get("team") if isinstance(roster_block.get("team"), dict) else {}
        team_id = str(team.get("id")) if team.get("id") not in (None, "") else None
        team_name = _incident_text(team.get("displayName")) or _incident_text(
            team.get("shortDisplayName")
        )
        if team_name is None and team_id is not None:
            team_name = team_by_id.get(team_id, {}).get("name")
        team_side = (
            roster_block.get("homeAway")
            if roster_block.get("homeAway") in {"home", "away"}
            else None
        )
        roster = roster_block.get("roster")
        if not isinstance(roster, list):
            continue
        for athlete_row in roster[:60]:
            if not isinstance(athlete_row, dict):
                continue
            identity = _player_identity(athlete_row)
            if identity is None:
                continue
            player_id, name, position, starter = identity
            entry = merged.setdefault(
                player_id,
                {
                    "player_id": player_id,
                    "name": name,
                    "position": position,
                    "starter": starter,
                    "provider_team_id": team_id,
                    "team_name": team_name,
                    "team_side": team_side,
                    "statistics": [],
                },
            )
            if entry.get("position") is None and position is not None:
                entry["position"] = position
            if entry.get("starter") is None and starter is not None:
                entry["starter"] = starter
            if entry.get("provider_team_id") is None and team_id is not None:
                entry["provider_team_id"] = team_id
            if entry.get("team_name") is None and team_name is not None:
                entry["team_name"] = team_name
            if entry.get("team_side") is None and team_side is not None:
                entry["team_side"] = team_side
            raw_stats = athlete_row.get("stats")
            if not isinstance(raw_stats, list):
                continue
            known = {item["name"] for item in entry["statistics"]}
            for raw_stat in raw_stats[:32]:
                if not isinstance(raw_stat, dict):
                    continue
                provider_name = raw_stat.get("name")
                if (
                    not isinstance(provider_name, str)
                    or provider_name not in _DISPLAY_PLAYER_STATS
                ):
                    continue
                value = _stat_number(raw_stat.get("value", raw_stat.get("displayValue")))
                if value is None:
                    continue
                canonical_name = _PLAYER_STAT_CANONICAL_NAMES.get(provider_name, provider_name)
                if canonical_name in known:
                    continue
                entry["statistics"].append(
                    {
                        "name": canonical_name,
                        "provider_name": provider_name
                        if canonical_name != provider_name
                        else None,
                        "label": _incident_text(raw_stat.get("displayName"))
                        or _incident_text(raw_stat.get("shortDisplayName"))
                        or provider_name,
                        "value": value,
                        "display_value": _incident_text(raw_stat.get("displayValue"))
                        or str(value),
                    }
                )
                known.add(canonical_name)
    players = [entry for entry in merged.values() if entry["statistics"]]
    for entry in players:
        entry["statistics"] = entry["statistics"][:24]
    return players[:60]


def _merge_player_statistics(*groups: list[dict]) -> list[dict]:
    """Merge bounded player projections without duplicating metric names."""

    merged: dict[str, dict] = {}
    for group in groups:
        for row in group:
            if not isinstance(row, dict) or not row.get("player_id"):
                continue
            player_id = str(row["player_id"])
            entry = merged.setdefault(player_id, {**row, "statistics": []})
            for key in (
                "name",
                "position",
                "starter",
                "provider_team_id",
                "team_name",
                "team_side",
            ):
                if entry.get(key) is None and row.get(key) is not None:
                    entry[key] = row[key]
            known = {item.get("name") for item in entry["statistics"] if isinstance(item, dict)}
            for stat in row.get("statistics", []):
                if not isinstance(stat, dict) or stat.get("name") in known:
                    continue
                entry["statistics"].append(stat)
                known.add(stat.get("name"))
            entry["statistics"] = entry["statistics"][:24]
    return [entry for entry in merged.values() if entry.get("statistics")][:60]


def parse_espn_match_stats(
    payload: bytes,
    *,
    fixture_id: str,
    retrieved_at: datetime,
    url: str,
) -> dict | None:
    """Project provider-reported match stats for display and post-match audit.

    ESPN's boxscore is deliberately kept outside the pre-match model.  The
    parser accepts only a bounded allowlist of numeric team statistics and
    retains the provider's display value alongside the normalized number.
    """

    data = json.loads(payload)
    boxscore = data.get("boxscore") if isinstance(data.get("boxscore"), dict) else {}
    raw_teams = boxscore.get("teams")
    # Some ESPN competitions publish the player block without a team
    # statistics block.  Keep that bounded player evidence instead of
    # discarding the whole snapshot; the Sites projection still treats it as
    # display-only and does not infer missing team metrics.
    if not isinstance(raw_teams, list):
        raw_teams = []
    teams: dict[str, dict] = {}
    for raw_team in raw_teams[:2]:
        if not isinstance(raw_team, dict) or raw_team.get("homeAway") not in {"home", "away"}:
            continue
        statistics = raw_team.get("statistics")
        if not isinstance(statistics, list):
            continue
        values: list[dict] = []
        for raw_stat in statistics[:60]:
            if not isinstance(raw_stat, dict) or raw_stat.get("name") not in _DISPLAY_STATS:
                continue
            value = _stat_number(raw_stat.get("displayValue", raw_stat.get("value")))
            if value is None:
                continue
            item = {
                "name": str(raw_stat["name"]),
                "label": _incident_text(raw_stat.get("label")) or str(raw_stat["name"]),
                "value": value,
                "display_value": _incident_text(raw_stat.get("displayValue")) or str(value),
            }
            values.append(item)
        if not values:
            continue
        team = raw_team.get("team") if isinstance(raw_team.get("team"), dict) else {}
        teams[raw_team["homeAway"]] = {
            "provider_team_id": str(team["id"]) if team.get("id") is not None else None,
            "name": _incident_text(team.get("displayName"))
            or _incident_text(team.get("shortDisplayName")),
            "statistics": values,
        }
    team_by_id = {
        str(team.get("provider_team_id")): team
        for team in teams.values()
        if team.get("provider_team_id") is not None
    }
    # Preserve team names/sides even when a provider returns an empty team
    # statistics list but a populated roster block.
    for raw_team in raw_teams[:2]:
        if not isinstance(raw_team, dict):
            continue
        team = raw_team.get("team") if isinstance(raw_team.get("team"), dict) else {}
        team_id = team.get("id")
        if team_id in (None, ""):
            continue
        team_by_id.setdefault(
            str(team_id),
            {
                "provider_team_id": str(team_id),
                "name": _incident_text(team.get("displayName"))
                or _incident_text(team.get("shortDisplayName")),
                "team_side": raw_team.get("homeAway"),
            },
        )
    players = _merge_player_statistics(
        _player_statistics(boxscore.get("players"), team_by_id),
        _roster_player_statistics(data.get("rosters"), team_by_id),
    )
    if not teams and not players:
        return None
    source = {
        "name": "ESPN event summary",
        "url": url,
        "native_fixture_id": str(fixture_id).split(":", 1)[-1],
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }
    return {
        "fixture_id": fixture_id,
        "retrieved_at": retrieved_at.isoformat(),
        "teams": teams,
        "players": players,
        "source": source,
        "enters_model": False,
        "model_exclusion_reason": "live_or_postmatch_event",
    }


def parse_espn_incidents(
    payload: bytes,
    *,
    fixture_id: str,
    retrieved_at: datetime,
    url: str,
) -> dict | None:
    """Keep a bounded incident projection for live/finished match displays.

    ESPN's summary schema has used both ``plays`` and ``keyEvents``.  The
    projection deliberately retains only provider-declared text, clock,
    participant, score and (when present) bounded event coordinates.  It is
    always display-only and never a pre-match feature, even when the same
    response also contains markets.
    """

    data = json.loads(payload)
    raw_events = data.get("plays")
    if not isinstance(raw_events, list):
        raw_events = data.get("keyEvents")
    if not isinstance(raw_events, list) or not raw_events:
        return None
    events: list[dict] = []
    for raw in raw_events[:200]:
        if not isinstance(raw, dict):
            continue
        event_type = _incident_text(raw.get("type")) or _incident_text(raw.get("eventType"))
        text = _incident_text(raw.get("text")) or _incident_text(raw.get("description"))
        clock = _incident_clock(raw.get("clock")) or _incident_clock(raw.get("time"))
        team = raw.get("team") if isinstance(raw.get("team"), dict) else {}
        participants = raw.get("participants")
        names: list[str] = []
        participant_ids: list[str] = []
        if isinstance(participants, list):
            for participant in participants[:6]:
                athlete = participant.get("athlete") if isinstance(participant, dict) else None
                name = _incident_text(athlete) or _incident_text(participant)
                if name:
                    names.append(name)
                provider_id = athlete.get("id") if isinstance(athlete, dict) else None
                if provider_id is None and isinstance(participant, dict):
                    provider_id = participant.get("id")
                if isinstance(provider_id, bool) or not isinstance(provider_id, (str, int)):
                    provider_id = None
                if provider_id is not None:
                    provider_id = str(provider_id).strip()
                    if (
                        provider_id
                        and len(provider_id) <= 80
                        and provider_id not in participant_ids
                    ):
                        participant_ids.append(provider_id)
        home_score = raw.get("homeScore")
        away_score = raw.get("awayScore")
        score = None
        if isinstance(home_score, (int, float)) and isinstance(away_score, (int, float)):
            if all(
                math.isfinite(float(value)) and float(value) >= 0
                for value in (home_score, away_score)
            ):
                score = {"home": int(home_score), "away": int(away_score)}
        location = _incident_location(raw)
        event_id = raw.get("id")
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id.strip()) > 80:
            event_id = None
        period = raw.get("period")
        period_number = None
        if isinstance(period, dict):
            period = period.get("number")
        if (
            isinstance(period, (int, float))
            and not isinstance(period, bool)
            and math.isfinite(float(period))
            and 0 <= period <= 10
        ):
            period_number = int(period)
        scoring_play = raw.get("scoringPlay")
        if not isinstance(scoring_play, bool):
            scoring_play = None
        wallclock = raw.get("wallclock")
        if not isinstance(wallclock, str) or not wallclock.strip() or len(wallclock.strip()) > 80:
            wallclock = None
        if not any((event_type, text, clock, names, score)):
            continue
        event = {
            "event_id": event_id,
            "type": event_type,
            "text": text,
            "minute": clock,
            "team": {
                key: team[key]
                for key in ("id", "displayName", "shortDisplayName")
                if key in team and team[key] not in (None, "")
            },
            "participants": names,
            "participant_ids": participant_ids,
            "score": score,
            "location": location,
            "period": period_number,
            "scoring_play": scoring_play,
            "wallclock": wallclock,
        }
        events.append(
            {key: value for key, value in event.items() if value not in (None, "", [], {})}
        )
    if not events:
        return None
    source = {
        "name": "ESPN event summary",
        "url": url,
        "native_fixture_id": str(fixture_id).split(":", 1)[-1],
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }
    return {
        "fixture_id": fixture_id,
        "retrieved_at": retrieved_at.isoformat(),
        "events": events,
        "source": source,
        "enters_model": False,
        "model_exclusion_reason": "live_or_postmatch_event",
    }


def fetch_espn_markets(
    fixtures: list[dict],
    *,
    now: datetime | None = None,
    horizon_days: int = 45,
    max_fixtures: int = 120,
    postmatch_days: int = DEFAULT_POSTMATCH_REPLAY_DAYS,
    max_postmatch_fixtures: int = DEFAULT_POSTMATCH_REPLAY_LIMIT,
    opener=None,
    authorization_reference: str | None = None,
) -> dict:
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    return {
        **rights_blocked_envelope(
            SourceId.ESPN_MARKET_SUMMARY,
            provider="ESPN event summary",
            checked_at=reference_time.astimezone(timezone.utc).isoformat(),
            empty_fields=(
                "markets",
                "fixture_updates",
                "team_status",
                "incidents",
                "match_stats",
                "fallbacks",
            ),
            authorization_reference=authorization_reference,
        ),
        "authorization_required": "express_written_permission",
        "terms_url": "https://disneytermsofuse.com/english/",
        "enters_model": False,
        "model_exclusion_reason": "provider_rights_blocked",
        "requested_fixtures": 0,
        "active_fixtures": 0,
        "postmatch_fixtures": 0,
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
                provider="ESPN event summary",
                checked_at=reference_time,
            ),
            "requested_fixtures": 0,
            "active_fixtures": 0,
            "postmatch_fixtures": 0,
            "markets": [],
            "fixture_updates": [],
            "team_status": [],
            "incidents": [],
            "match_stats": [],
            "fallbacks": [],
        }
    cutoff = reference_time.astimezone(timezone.utc) + timedelta(days=horizon_days)
    active_candidates = sorted(
        (
            fixture
            for fixture in fixtures
            if fixture["status"] in {"upcoming", "live"}
            and datetime.fromisoformat(fixture["kickoff_at"]).astimezone(timezone.utc) <= cutoff
        ),
        key=lambda fixture: fixture["kickoff_at"],
    )[:max_fixtures]
    postmatch_cutoff = reference_time.astimezone(timezone.utc) - timedelta(
        days=max(0, postmatch_days)
    )
    postmatch_candidates = sorted(
        (
            fixture
            for fixture in fixtures
            if fixture["status"] == "finished"
            and postmatch_cutoff
            <= datetime.fromisoformat(fixture["kickoff_at"]).astimezone(timezone.utc)
            <= reference_time.astimezone(timezone.utc)
        ),
        key=lambda fixture: fixture["kickoff_at"],
    )[-max(0, max_postmatch_fixtures) :]
    candidates = active_candidates + postmatch_candidates
    fetcher = opener or _safe_opener()

    def fetch_one(fixture: dict) -> tuple[dict, bytes, str, list[str]]:
        code = ESPN_CODES[fixture["competition_id"]]
        native_id = fixture["source"]["native_fixture_id"]
        urls = [
            (
                "https://site.api.espn.com/apis/site/v2/sports/soccer/"
                f"{code}/summary?event={native_id}"
            ),
            (
                f"https://{ESPN_WEB_HOST}/apis/site/v2/sports/soccer/"
                f"{code}/summary?event={native_id}"
            ),
        ]
        errors = []
        for url in urls:
            request = urllib.request.Request(  # noqa: S310 - fixed HTTPS ESPN hosts
                url,
                headers={"Accept": "application/json", "User-Agent": "Matchline/1.0"},
            )
            try:
                payload = _read_limited(fetcher, request, max_bytes=10 * 1024 * 1024)
                return fixture, payload, url, errors
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        raise OSError("; ".join(errors))

    observations = []
    fixture_updates = []
    team_statuses = []
    incidents = []
    match_stats = []
    errors = []
    fallbacks = []
    observation_times = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_one, fixture): fixture for fixture in candidates}
        for future in as_completed(futures):
            fixture = futures[future]
            try:
                _, payload, url, fallback_errors = future.result()
                if fallback_errors:
                    fallbacks.append(
                        {
                            "fixture_id": fixture["id"],
                            "primary_host": "site.api.espn.com",
                            "fallback_host": ESPN_WEB_HOST,
                            "errors": fallback_errors,
                        }
                    )
                observed_at = reference_time if now is not None else datetime.now(timezone.utc)
                observation_times.append(observed_at)
                event_update = parse_espn_event_update(
                    payload,
                    fixture_id=fixture["id"],
                    retrieved_at=observed_at,
                    url=url,
                    kickoff_at=fixture.get("kickoff_at"),
                )
                if event_update is not None:
                    fixture_updates.append(event_update)
                incident = parse_espn_incidents(
                    payload,
                    fixture_id=fixture["id"],
                    retrieved_at=observed_at,
                    url=url,
                )
                if incident is not None:
                    incidents.append(incident)
                stats = parse_espn_match_stats(
                    payload,
                    fixture_id=fixture["id"],
                    retrieved_at=observed_at,
                    url=url,
                )
                if stats is not None:
                    match_stats.append(stats)
                # Only upcoming fixtures contribute pre-match market and
                # roster evidence.  Live fixtures are fetched here solely to
                # repair lagging status/score state; their in-play payloads
                # must not become pre-match market baselines.  Recent finished
                # fixtures are replayed only for display/audit incidents and
                # boxscore statistics, never for model features or markets.
                if fixture["status"] == "upcoming":
                    observation = parse_espn_market(
                        payload,
                        fixture_id=fixture["id"],
                        retrieved_at=observed_at,
                        url=url,
                        kickoff_at=fixture.get("kickoff_at"),
                    )
                    team_status = parse_espn_team_status(
                        payload,
                        fixture_id=fixture["id"],
                        retrieved_at=observed_at,
                        url=url,
                        kickoff_at=fixture.get("kickoff_at"),
                    )
                    if team_status:
                        team_statuses.append(team_status)
                    if observation:
                        observations.append(observation)
            except Exception as exc:
                errors.append({"fixture_id": fixture["id"], "error": str(exc)})
    observations.sort(key=lambda item: item["fixture_id"])
    return {
        "provider": "ESPN event summary",
        "retrieved_at": max(observation_times, default=reference_time).isoformat(),
        "requested_fixtures": len(candidates),
        "active_fixtures": len(active_candidates),
        "postmatch_fixtures": len(postmatch_candidates),
        "markets": observations,
        "fixture_updates": sorted(fixture_updates, key=lambda item: item["fixture_id"]),
        "team_status": sorted(team_statuses, key=lambda item: item["fixture_id"]),
        "incidents": sorted(incidents, key=lambda item: item["fixture_id"]),
        "match_stats": sorted(match_stats, key=lambda item: item["fixture_id"]),
        "fallbacks": sorted(fallbacks, key=lambda item: item["fixture_id"]),
        "errors": errors,
        "access_allowed": True,
        "rights_status": "operator_authorization_reference_supplied",
        "authorization_reference": authorization_reference,
    }
