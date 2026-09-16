"""Extract bounded player evidence from validated pre-match source snapshots.

The source adapters already retain roster/lineup payloads inside a fixture
observation.  This module creates one immutable observation per player so the
Sites ledger can later bind a player to a canonical entity.  It deliberately
does not turn a provider id into a D1 integer id: identity registration is a
separate, high-confidence operation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlparse

from league_platform.intelligence import Observation, observation_from_payload


MAX_PLAYER_OBSERVATIONS = 5_000
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PROVIDER_PREFIXES = {
    "espn event summary": "espn",
    "espn": "espn",
    "espn injury report": "espn",
    "premier league official": "premierleague",
    "la liga official": "laliga",
    "laliga official": "laliga",
    "bundesliga official": "bundesliga",
    "serie a official": "seriea",
    "fotmob": "fotmob",
}


def _text(value: object, *, max_length: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= max_length else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _provider_prefix(value: object, fallback: str) -> str:
    raw = _text(value, max_length=120) or fallback
    normalized = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
    return _PROVIDER_PREFIXES.get(normalized, re.sub(r"[^a-z0-9]+", "", normalized) or "provider")


def _source(source: object, fallback: str) -> dict[str, str] | None:
    if not isinstance(source, Mapping):
        return None
    url = _text(source.get("url"), max_length=2_048)
    raw_hash = _text(source.get("raw_sha256") or source.get("raw_hash"), max_length=64)
    if not url or not raw_hash or not _SHA256_RE.fullmatch(raw_hash):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        return None
    retrieved_at = _text(source.get("retrieved_at"), max_length=80)
    source_name = _text(source.get("name"), max_length=120) or fallback
    result = {
        "name": source_name,
        "url": url,
        "raw_hash": raw_hash.lower(),
    }
    if retrieved_at:
        result["retrieved_at"] = retrieved_at
    return result


def _observed_at(row: Mapping[str, object], source: Mapping[str, str], fallback: str) -> str:
    value = _text(row.get("retrieved_at"), max_length=80) or source.get("retrieved_at") or fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return fallback
    return parsed.isoformat()


def _published_at(source: Mapping[str, object], observed_at: str) -> str | None:
    value = _text(source.get("effective_at"), max_length=80)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or observed.tzinfo is None or parsed > observed:
        return None
    return parsed.isoformat()


def _player_identity(player: Mapping[str, object], *, default_prefix: str) -> tuple[str, str, str] | None:
    raw_id = _text(player.get("provider_player_id") or player.get("player_id") or player.get("id"), max_length=160)
    name = _text(player.get("name") or player.get("display_name") or player.get("displayName"), max_length=160)
    if not raw_id or not name:
        return None
    if ":" in raw_id:
        prefix, provider_id = raw_id.split(":", 1)
        prefix = _provider_prefix(prefix, default_prefix)
    else:
        prefix, provider_id = _provider_prefix(default_prefix, default_prefix), raw_id
    if not provider_id or len(provider_id) > 120:
        return None
    return f"{prefix}:{provider_id}", provider_id, name


def _normalised_player_payload(
    player: Mapping[str, object],
    *,
    provider_prefix: str,
    fixture_provider_id: str,
    team_provider_id: str | None,
    team_name: str | None,
    team_side: str,
    source_model_eligible: bool,
) -> tuple[str, dict[str, object]] | None:
    identity = _player_identity(player, default_prefix=provider_prefix)
    if identity is None:
        return None
    entity_id, provider_player_id, name = identity
    status = _text(player.get("status") or player.get("state"), max_length=80)
    starter = player.get("starter") if isinstance(player.get("starter"), bool) else None
    if status is None:
        status = "starter" if starter is True else "substitute" if player.get("substitute") is True else "roster"
    expected_minutes = player.get("expected_minutes", player.get("expectedMinutes"))
    if isinstance(expected_minutes, bool) or not isinstance(expected_minutes, (int, float)) or not math.isfinite(float(expected_minutes)) or not 0 <= float(expected_minutes) <= 130:
        expected_minutes = None
    position = _text(player.get("position"), max_length=80)
    status_type = _text(player.get("status_type") or player.get("statusType"), max_length=40)
    jersey = _text(player.get("jersey"), max_length=8)
    citizenship = _text(player.get("citizenship"), max_length=80)
    profile_url = _text(player.get("profile_url") or player.get("profileUrl"), max_length=2_048)
    age = player.get("age")
    if isinstance(age, bool) or not isinstance(age, int) or not 12 <= age <= 60:
        age = None
    payload: dict[str, object] = {
        "provider": provider_prefix,
        "provider_player_id": provider_player_id,
        "name": name,
        "position": position,
        "status": status,
        "status_type": status_type,
        "jersey": jersey,
        "age": age,
        "citizenship": citizenship,
        "profile_url": profile_url,
        "starter": starter,
        "expected_minutes": expected_minutes,
        "fixture_provider_id": fixture_provider_id,
        "team_provider_id": team_provider_id,
        "team_name": team_name,
        "team_side": team_side,
        "source_model_eligible": source_model_eligible,
        # Provider id + fixture/team context is a useful identity lead, not a
        # canonical D1 identity.  The registrar must still confirm the alias.
        "identity_basis": "provider_player_id+fixture_team_context",
        "identity_confidence": 0.98 if team_provider_id else 0.85,
    }
    for key in ("reason", "provider_date", "return_date", "note", "replacement_value"):
        value = player.get(key)
        if key == "replacement_value":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                value = None
        else:
            value = _text(value, max_length=240)
        if value is not None:
            payload[key] = value
    return entity_id, {key: value for key, value in payload.items() if value is not None}


def _append_players(
    output: list[Observation],
    *,
    players: object,
    source: Mapping[str, str],
    source_tier: str,
    provider_prefix: str,
    fixture_provider_id: str,
    team_provider_id: str | None,
    team_name: str | None,
    team_side: str,
    observed_at: str,
    source_model_eligible: bool,
    confirmed: bool,
    canonical_fixture_id: int | None = None,
    canonical_team_id: int | None = None,
) -> None:
    if not isinstance(players, list):
        return
    kind = "player_lineup" if confirmed else "player_roster"
    for raw_player in players:
        if len(output) >= MAX_PLAYER_OBSERVATIONS or not isinstance(raw_player, Mapping):
            break
        normalised = _normalised_player_payload(
            raw_player,
            provider_prefix=provider_prefix,
            fixture_provider_id=fixture_provider_id,
            team_provider_id=team_provider_id,
            team_name=team_name,
            team_side=team_side,
            source_model_eligible=source_model_eligible,
        )
        if normalised is None:
            continue
        entity_id, payload = normalised
        # Never mistake a provider's ``playerId`` for a canonical D1 id.  A
        # canonical binding must be written explicitly by the registrar.
        canonical_player_id = _positive_int(raw_player.get("canonical_player_id") or raw_player.get("canonicalPlayerId"))
        enters_model = bool(source_model_eligible and confirmed and canonical_fixture_id and canonical_team_id and canonical_player_id)
        reason = None if enters_model else "player_identity_not_canonical" if canonical_player_id is None else "player_roster_not_confirmed_lineup"
        try:
            output.append(
                observation_from_payload(
                    entity_type="player",
                    entity_id=entity_id,
                    kind=kind,
                    payload=payload,
                    source_name=source["name"],
                    source_url=source["url"],
                    source_tier=source_tier,
                    observed_at=observed_at,
                    published_at=_published_at(source, observed_at),
                    confidence=0.99 if source_tier == "official" and confirmed else 0.65 if not confirmed else 0.9,
                    enters_model=enters_model,
                    model_exclusion_reason=reason,
                    raw_hash=source["raw_hash"],
                    fixture_id=canonical_fixture_id,
                    team_id=canonical_team_id,
                    player_id=canonical_player_id,
                )
            )
        except (TypeError, ValueError):
            # A malformed player row must not discard sibling players or the
            # parent fixture observation.
            continue


def _team_status_observations(snapshot: Mapping[str, object], fallback: str) -> list[Observation]:
    output: list[Observation] = []
    markets = snapshot.get("espn_markets")
    statuses = markets.get("team_status") if isinstance(markets, Mapping) else None
    if not isinstance(statuses, list):
        return output
    for status in statuses:
        if not isinstance(status, Mapping):
            continue
        fixture_provider_id = _text(status.get("fixture_id"), max_length=160)
        source = _source(status.get("source"), "ESPN event summary")
        if not fixture_provider_id or source is None:
            continue
        observed_at = _observed_at(status, source, fallback)
        confirmed = status.get("confirmed") is True and status.get("status") == "confirmed_lineup"
        source_model_eligible = status.get("model_eligible") is True
        teams = status.get("teams")
        if not isinstance(teams, Mapping):
            continue
        for side in ("home", "away"):
            team = teams.get(side)
            if not isinstance(team, Mapping):
                continue
            team_provider_id = _text(team.get("provider_team_id"), max_length=160)
            team_name = _text(team.get("name"), max_length=160)
            _append_players(
                output,
                players=team.get("players"),
                source=source,
                source_tier="reliable_public_provider",
                provider_prefix="espn",
                fixture_provider_id=fixture_provider_id,
                team_provider_id=team_provider_id,
                team_name=team_name,
                team_side=side,
                observed_at=observed_at,
                source_model_eligible=source_model_eligible,
                confirmed=confirmed,
                canonical_fixture_id=_positive_int(status.get("canonical_fixture_id")),
                canonical_team_id=_positive_int(team.get("canonical_team_id")),
            )
    return output


def _official_lineup_observations(snapshot: Mapping[str, object], fallback: str) -> list[Observation]:
    output: list[Observation] = []
    source_configs = (
        ("premier_league_official", "premierleague", "Premier League official"),
        ("laliga_official", "laliga", "LaLiga official"),
        ("bundesliga_official", "bundesliga", "Bundesliga official"),
        ("serie_a_official", "seriea", "Serie A official"),
        ("ligue1_official", "ligue1", "Ligue 1 official"),
    )
    for source_key, provider_prefix, source_name in source_configs:
        container = snapshot.get(source_key)
        rows = container.get("lineups") if isinstance(container, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            fixture_provider_id = _text(row.get("fixture_id") or row.get("match_id"), max_length=160)
            source = _source(row.get("source"), source_name)
            lineups = row.get("lineups")
            if not fixture_provider_id or source is None or not isinstance(lineups, Mapping):
                continue
            observed_at = _observed_at(row, source, fallback)
            confirmed = lineups.get("confirmed") is True
            source_model_eligible = lineups.get("model_eligible") is True
            for side in ("home", "away"):
                side_payload = lineups.get(side)
                if not isinstance(side_payload, Mapping):
                    continue
                team_provider_id = _text(side_payload.get("provider_team_id"), max_length=160)
                team_name = _text(side_payload.get("team_name"), max_length=160)
                _append_players(
                    output,
                    players=side_payload.get("players"),
                    source=source,
                    source_tier="official",
                    provider_prefix=provider_prefix,
                    fixture_provider_id=fixture_provider_id,
                    team_provider_id=team_provider_id,
                    team_name=team_name,
                    team_side=side,
                    observed_at=observed_at,
                    source_model_eligible=source_model_eligible,
                    confirmed=confirmed,
                    canonical_fixture_id=_positive_int(row.get("canonical_fixture_id")),
                    canonical_team_id=_positive_int(side_payload.get("canonical_team_id")),
                )
    return output


def _espn_roster_observations(snapshot: Mapping[str, object], fallback: str) -> list[Observation]:
    """Project team-level ESPN rosters into player evidence rows.

    A team roster has no fixture-specific availability semantics.  It is
    therefore kept as ``player_roster`` with null canonical foreign keys and
    an explicit display-only exclusion.  A later confirmed lineup or trusted
    registrar binding is required before any player can affect a prediction.
    """

    output: list[Observation] = []
    container = snapshot.get("espn_rosters")
    rows = container.get("rosters") if isinstance(container, Mapping) else None
    if not isinstance(rows, list):
        return output
    for roster in rows:
        if not isinstance(roster, Mapping):
            continue
        team_provider_id = _text(roster.get("provider_team_id"), max_length=160)
        competition_id = _text(roster.get("competition_id"), max_length=80)
        source = _source(roster.get("source"), "ESPN team roster")
        if not team_provider_id or not competition_id or source is None:
            continue
        observed_at = _observed_at(roster, source, fallback)
        fixture_provider_id = f"espn-roster:{competition_id}:{team_provider_id}"
        _append_players(
            output,
            players=roster.get("athletes"),
            source=source,
            source_tier="reliable_public_provider",
            provider_prefix="espn",
            fixture_provider_id=fixture_provider_id,
            team_provider_id=team_provider_id,
            team_name=_text(roster.get("team_name"), max_length=160),
            team_side="team_roster",
            observed_at=observed_at,
            source_model_eligible=False,
            confirmed=False,
        )
    return output


def _espn_injury_observations(snapshot: Mapping[str, object], fallback: str) -> list[Observation]:
    """Project provider-reported absences without implying fixture completeness."""

    output: list[Observation] = []
    container = snapshot.get("espn_injuries")
    reports = container.get("reports") if isinstance(container, Mapping) else None
    if not isinstance(reports, list):
        return output
    for report in reports:
        if not isinstance(report, Mapping):
            continue
        competition_id = _text(report.get("competition_id"), max_length=80)
        team_provider_id = _text(report.get("provider_team_id"), max_length=160)
        source = _source(report.get("source"), "ESPN injury report")
        players = report.get("injuries")
        if not competition_id or not team_provider_id or source is None or not isinstance(players, list):
            continue
        observed_at = _observed_at(report, source, fallback)
        fixture_provider_id = f"espn-injuries:{competition_id}:{team_provider_id}"
        for player in players:
            if len(output) >= MAX_PLAYER_OBSERVATIONS or not isinstance(player, Mapping):
                break
            normalised = _normalised_player_payload(
                player,
                provider_prefix="espn",
                fixture_provider_id=fixture_provider_id,
                team_provider_id=team_provider_id,
                team_name=_text(report.get("team_name"), max_length=160),
                team_side="team_report",
                source_model_eligible=False,
            )
            if normalised is None:
                continue
            entity_id, payload = normalised
            # Absence rows must carry explicit unknowns; omitting these keys
            # makes downstream consumers prone to treating missing as zero.
            payload["expected_minutes"] = None
            payload["replacement_value"] = None
            try:
                output.append(
                    observation_from_payload(
                        entity_type="player",
                        entity_id=entity_id,
                        kind="player_availability",
                        payload=payload,
                        source_name=source["name"],
                        source_url=source["url"],
                        source_tier="reliable_public_provider",
                        observed_at=observed_at,
                        # Provider date is metadata, not a documented row
                        # publication time, so it is deliberately not used as
                        # effective_at.
                        published_at=None,
                        confidence=0.7,
                        enters_model=False,
                        model_exclusion_reason=(
                            "league_report_not_fixture_complete_and_provider_terms_require_review"
                        ),
                        raw_hash=source["raw_hash"],
                    )
                )
            except (TypeError, ValueError):
                continue
    return output


def snapshot_player_observations(snapshot: Mapping[str, object], *, fallback: str) -> list[Observation]:
    """Return deduplicated player observations from the current snapshot."""

    rows = _team_status_observations(snapshot, fallback)
    rows.extend(_official_lineup_observations(snapshot, fallback))
    rows.extend(_espn_roster_observations(snapshot, fallback))
    rows.extend(_espn_injury_observations(snapshot, fallback))
    output: list[Observation] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        fixture_provider_id = str(row.payload.get("fixture_provider_id") or "")
        team_provider_id = str(row.payload.get("team_provider_id") or "")
        key = (row.entity_id, fixture_provider_id, team_provider_id, row.kind, row.observed_at)
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
        if len(output) >= MAX_PLAYER_OBSERVATIONS:
            break
    return output


__all__ = ["MAX_PLAYER_OBSERVATIONS", "snapshot_player_observations"]
