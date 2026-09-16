"""Time-safe player-availability shadow features.

The current Matchline forecast deliberately does not invent player impact or
replacement value.  This module creates the next, auditable layer instead:
it deduplicates provider player rows, records bounded availability counts and
expected-minute coverage, and applies an explicit observation-time gate.

The result is a validation-only feature vector.  It never produces a
probability delta and is never model eligible until a separate prospective
evaluation proves that its coefficients improve a held-out baseline.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
from typing import Any, Mapping


_UNAVAILABLE_STATUS_TOKENS = frozenset(
    {
        "injured",
        "injury",
        "out",
        "unavailable",
        "suspended",
        "suspension",
        "ruled_out",
        "doubtful_out",
    }
)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_minutes(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 130.0:
        return None
    return number


def _status(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    return value.strip().casefold().replace(" ", "_")


def _is_unavailable(value: object) -> bool:
    normalized = _status(value)
    return normalized in _UNAVAILABLE_STATUS_TOKENS or any(
        token in normalized for token in ("injur", "suspend", "unavailable", "ruled_out")
    )


def _player_key(row: Mapping[str, Any], index: int) -> str:
    side = row.get("team_side") or row.get("teamSide") or "unknown"
    player_id = row.get("player_id") or row.get("playerId") or row.get("provider_player_id")
    if isinstance(player_id, str) and player_id.strip():
        return f"{side}:{player_id.strip()}"
    # Rows without identity are retained for coverage diagnostics, but each
    # row stays separate so we never merge two anonymous players.
    return f"{side}:anonymous:{index}"


def _deduplicate_players(players: object) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(players, list):
        return [], 0
    merged: dict[str, dict[str, Any]] = {}
    conflict_count = 0
    for index, raw in enumerate(players):
        if not isinstance(raw, Mapping):
            continue
        key = _player_key(raw, index)
        status = _status(raw.get("status"))
        minutes = _safe_minutes(raw.get("expected_minutes", raw.get("expectedMinutes")))
        starter = raw.get("starter") is True
        existing = merged.get(key)
        if existing is None:
            merged[key] = {
                "team_side": raw.get("team_side") or raw.get("teamSide") or "unknown",
                "player_id": raw.get("player_id") or raw.get("playerId") or raw.get("provider_player_id"),
                "status": status,
                "starter": starter,
                "expected_minutes": minutes,
            }
            continue
        if existing["status"] != status or existing["expected_minutes"] != minutes:
            conflict_count += 1
        # An unavailable status is more conservative than a later generic
        # roster status; preserve a confirmed starter flag if any source has
        # one, and keep the larger bounded minute estimate only when values do
        # not conflict.
        if _is_unavailable(status):
            existing["status"] = status
        existing["starter"] = existing["starter"] or starter
        if existing["expected_minutes"] is None:
            existing["expected_minutes"] = minutes
    return list(merged.values()), conflict_count


def _team_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(row["status"] for row in rows)
    identity_count = sum(
        1
        for row in rows
        if isinstance(row.get("player_id"), (str, int)) and str(row.get("player_id")).strip()
    )
    minutes = [row["expected_minutes"] for row in rows if row["expected_minutes"] is not None]
    unavailable = sum(1 for row in rows if _is_unavailable(row["status"]))
    starters = sum(1 for row in rows if row["starter"] is True)
    return {
        "roster_count": len(rows),
        "identity_count": identity_count,
        "identity_coverage": round(identity_count / len(rows), 6) if rows else None,
        "starter_count": starters,
        "unavailable_count": unavailable,
        "expected_minutes_known_count": len(minutes),
        "expected_minutes_known_share": round(len(minutes) / len(rows), 6) if rows else None,
        "expected_minutes_sum": round(sum(minutes), 4) if minutes else None,
        "status_counts": dict(sorted(statuses.items())),
    }


def build_player_availability_shadow(
    player_layer: Mapping[str, Any] | None,
    *,
    observed_at: str | None,
    as_of: str | None,
    kickoff_at: str | None,
) -> dict[str, Any]:
    """Build a validation-only player availability projection.

    ``observed_at`` is required for admissibility.  A missing or late clock
    keeps the descriptive counts visible but sets ``feature_vector`` to
    ``None`` so the row cannot be consumed as a pre-match feature.
    """

    players = player_layer.get("players") if isinstance(player_layer, Mapping) else None
    deduplicated, conflict_count = _deduplicate_players(players)
    base: dict[str, Any] = {
        "schema_version": "matchline.player_availability_shadow.v1",
        "status": "unavailable" if not deduplicated else "blocked",
        "model_boundary": "validation_only",
        "model_eligible": False,
        "enters_model": False,
        "impact_status": "not_estimated_no_validated_baseline",
        "impact_delta_probability_1x2": None,
        "replacement_value_status": "not_estimated_no_validated_baseline",
        "observed_at": observed_at,
        "as_of": as_of,
        "kickoff_at": kickoff_at,
        "deduplicated_player_count": len(deduplicated),
        "duplicate_conflict_count": conflict_count,
        "teams": {"home": _team_summary([]), "away": _team_summary([])},
        "feature_vector": None,
        "missing_fields": [],
        "reason_code": "no_player_rows" if not deduplicated else "observation_time_not_verified",
    }
    if not deduplicated:
        base["missing_fields"] = ["player_rows", "observed_at", "expected_minutes", "replacement_value"]
        return base

    teams: dict[str, list[dict[str, Any]]] = {"home": [], "away": []}
    for row in deduplicated:
        side = row.get("team_side")
        if side in teams:
            teams[side].append(row)
    team_summaries = {side: _team_summary(rows) for side, rows in teams.items()}
    base["teams"] = team_summaries
    observed = _parse_time(observed_at)
    as_of_dt = _parse_time(as_of)
    kickoff_dt = _parse_time(kickoff_at)
    if observed is None:
        base["missing_fields"] = ["observed_at", "replacement_value"]
        base["reason_code"] = "missing_or_invalid_observed_at"
        return base
    if as_of_dt is not None and observed > as_of_dt:
        base["missing_fields"] = ["observation_after_cutoff", "replacement_value"]
        base["reason_code"] = "observation_after_cutoff"
        return base
    if kickoff_dt is not None and observed >= kickoff_dt:
        base["missing_fields"] = ["observation_after_kickoff", "replacement_value"]
        base["reason_code"] = "observation_after_kickoff"
        return base

    base["status"] = "shadow_ready"
    base["reason_code"] = "pre_cutoff_player_rows_ready_for_validation"
    base["feature_vector"] = {
        "home_roster_count": team_summaries["home"]["roster_count"],
        "away_roster_count": team_summaries["away"]["roster_count"],
        "home_starter_count": team_summaries["home"]["starter_count"],
        "away_starter_count": team_summaries["away"]["starter_count"],
        "home_unavailable_count": team_summaries["home"]["unavailable_count"],
        "away_unavailable_count": team_summaries["away"]["unavailable_count"],
        "home_expected_minutes_known_share": team_summaries["home"]["expected_minutes_known_share"],
        "away_expected_minutes_known_share": team_summaries["away"]["expected_minutes_known_share"],
        "home_identity_coverage": team_summaries["home"]["identity_coverage"],
        "away_identity_coverage": team_summaries["away"]["identity_coverage"],
    }
    base["missing_fields"] = ["replacement_value", "validated_impact_coefficient"]
    return base


def _feature_observed_at(feature: object) -> str | None:
    if not isinstance(feature, Mapping):
        return None
    candidates: list[object] = [feature]
    source = feature.get("source")
    if isinstance(source, Mapping):
        candidates.append(source)
    for candidate in candidates:
        for key in ("observed_at", "observedAt", "retrieved_at", "retrievedAt", "published_at", "publishedAt"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _feature_is_pre_cutoff(
    feature: object,
    *,
    as_of: str | None,
    kickoff_at: str | None,
) -> bool:
    observed = _parse_time(_feature_observed_at(feature))
    cutoff = _parse_time(as_of)
    kickoff = _parse_time(kickoff_at)
    return bool(
        observed is not None
        and (cutoff is None or observed <= cutoff)
        and (kickoff is None or observed < kickoff)
    )


def _provider_has_player_rows(provider: Mapping[str, Any]) -> bool:
    lineups = provider.get("lineups")
    injuries = provider.get("injuries")
    for payload in (lineups, injuries):
        if isinstance(payload, Mapping):
            for side in ("home", "away"):
                side_payload = payload.get(side)
                if isinstance(side_payload, Mapping) and isinstance(side_payload.get("players"), list) and side_payload["players"]:
                    return True
                if isinstance(side_payload, list) and side_payload:
                    return True
        elif isinstance(payload, list) and payload:
            return True
    teams = provider.get("teams")
    if isinstance(teams, Mapping):
        return any(
            isinstance(teams.get(side), Mapping)
            and isinstance(teams[side].get("players"), list)
            and bool(teams[side]["players"])
            for side in ("home", "away")
        )
    return False


def _provider_to_player_layer(provider: Mapping[str, Any]) -> dict[str, Any]:
    players: list[dict[str, Any]] = []
    lineups = provider.get("lineups")
    injuries = provider.get("injuries")
    teams = provider.get("teams")
    for side in ("home", "away"):
        side_added = False
        lineup_side = lineups.get(side) if isinstance(lineups, Mapping) else None
        lineup_rows = lineup_side.get("players") if isinstance(lineup_side, Mapping) else lineup_side
        if isinstance(lineup_rows, list):
            for row in lineup_rows:
                if not isinstance(row, Mapping):
                    continue
                player_id = row.get("player_id", row.get("playerId", row.get("provider_player_id", row.get("id"))))
                if player_id is None or (isinstance(player_id, str) and not player_id.strip()):
                    continue
                players.append({
                    "team_side": side,
                    "player_id": player_id,
                    "status": row.get("status"),
                    "starter": row.get("starter") is True,
                    "expected_minutes": row.get("expected_minutes", row.get("expectedMinutes")),
                })
                side_added = True
        injury_side = injuries.get(side) if isinstance(injuries, Mapping) else None
        if isinstance(injury_side, Mapping):
            injury_rows = injury_side.get("players", [])
        else:
            injury_rows = injury_side
        if isinstance(injury_rows, list):
            for row in injury_rows:
                if not isinstance(row, Mapping):
                    continue
                player_id = row.get("player_id", row.get("playerId", row.get("provider_player_id", row.get("id"))))
                if player_id is None or (isinstance(player_id, str) and not player_id.strip()):
                    continue
                players.append({
                    "team_side": side,
                    "player_id": player_id,
                    "status": row.get("status") or "unavailable",
                    "starter": False,
                    "expected_minutes": None,
                })
                side_added = True
        if not side_added and isinstance(teams, Mapping):
            team = teams.get(side)
            team_rows = team.get("players") if isinstance(team, Mapping) else None
            if isinstance(team_rows, list):
                for row in team_rows:
                    if not isinstance(row, Mapping):
                        continue
                    player_id = row.get("player_id", row.get("playerId", row.get("provider_player_id", row.get("id"))))
                    if player_id is None or (isinstance(player_id, str) and not player_id.strip()):
                        continue
                    players.append({
                        "team_side": side,
                        "player_id": player_id,
                        "status": row.get("status"),
                        "starter": row.get("starter") is True,
                        "expected_minutes": row.get("expected_minutes", row.get("expectedMinutes")),
                    })
                    side_added = True
    return {"players": players}


def build_player_availability_shadow_from_feature_snapshot(
    feature_snapshot: Mapping[str, Any] | None,
    *,
    as_of: str | None,
    kickoff_at: str | None,
) -> dict[str, Any]:
    """Project player evidence from one causal freeze snapshot.

    Provider selection is conservative and mirrors the product boundary:
    official/confirmed lineup data wins, then an explicitly captured public
    lineup, then provider team status.  The returned shadow remains
    validation-only regardless of which source supplied the rows.
    """

    if not isinstance(feature_snapshot, Mapping):
        return build_player_availability_shadow({}, observed_at=None, as_of=as_of, kickoff_at=kickoff_at)
    marker = feature_snapshot.get("lineup_confirmation_event")
    marked_key = marker.get("provider_key") if isinstance(marker, Mapping) else None
    if marked_key in {"official_lineup", "fotmob_lineup", "team_status"}:
        # A lineup freeze is established by one exact provider response.  Do
        # not let a cached, higher-priority provider from the predecessor
        # snapshot replace that event in the validation shadow.
        providers = [feature_snapshot.get(str(marked_key))]
    else:
        providers = [
            feature_snapshot.get("official_lineup"),
            feature_snapshot.get("fotmob_lineup"),
            feature_snapshot.get("sofascore"),
            feature_snapshot.get("team_status"),
        ]
    selected: Mapping[str, Any] | None = None
    # A confirmed but playerless envelope is useful audit evidence, not a
    # reason to hide a lower-priority provider that actually carries player
    # rows.  Prefer the first player-bearing provider; only fall back to a
    # confirmation-only envelope when no player rows exist anywhere.
    for candidate in providers:
        if not isinstance(candidate, Mapping):
            continue
        if _provider_has_player_rows(candidate):
            selected = candidate
            break
    if selected is None:
        for candidate in providers:
            if not isinstance(candidate, Mapping):
                continue
            if record_bool(candidate.get("lineups"), "confirmed") or candidate.get("confirmed") is True:
                selected = candidate
                break
    if selected is None:
        supplemental = feature_snapshot.get("espn_injuries")
        if not (
            isinstance(supplemental, Mapping)
            and _provider_has_player_rows(supplemental)
            and _feature_is_pre_cutoff(
                supplemental,
                as_of=as_of,
                kickoff_at=kickoff_at,
            )
        ):
            return build_player_availability_shadow(
                {}, observed_at=None, as_of=as_of, kickoff_at=kickoff_at
            )
        layer = _provider_to_player_layer(supplemental)
        return build_player_availability_shadow(
            layer,
            observed_at=_feature_observed_at(supplemental),
            as_of=as_of,
            kickoff_at=kickoff_at,
        )

    layer = _provider_to_player_layer(selected)
    selected_observed_at = _feature_observed_at(selected)
    supplemental = feature_snapshot.get("espn_injuries")
    if (
        isinstance(supplemental, Mapping)
        and _provider_has_player_rows(supplemental)
        and _feature_is_pre_cutoff(
            supplemental,
            as_of=as_of,
            kickoff_at=kickoff_at,
        )
    ):
        supplemental_layer = _provider_to_player_layer(supplemental)
        layer["players"].extend(supplemental_layer["players"])
        supplemental_observed_at = _feature_observed_at(supplemental)
        selected_time = _parse_time(selected_observed_at)
        supplemental_time = _parse_time(supplemental_observed_at)
        # Do not let a valid supplemental clock launder a lineup with a missing
        # or invalid observation time. When both clocks are valid, the merged
        # vector belongs to the later of the two observations.
        if selected_time is not None and supplemental_time is not None:
            selected_observed_at = max(selected_time, supplemental_time).isoformat()
    return build_player_availability_shadow(
        layer,
        observed_at=selected_observed_at,
        as_of=as_of,
        kickoff_at=kickoff_at,
    )


def record_bool(value: object, key: str) -> bool:
    return isinstance(value, Mapping) and value.get(key) is True


__all__ = [
    "build_player_availability_shadow",
    "build_player_availability_shadow_from_feature_snapshot",
]
