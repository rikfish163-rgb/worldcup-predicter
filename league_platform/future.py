"""As-of future-fixture predictions built from history plus current sources."""

from __future__ import annotations

import math
import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Any

from league_platform.dixon_coles import _one_x_two
from league_platform.dixon_coles import evaluate_dixon_coles
from league_platform.fixture_feed import (
    blocked_fixture_record,
    prediction_coverage,
    upcoming_fixture_rows,
)
from league_platform.identity import canonical_team_name, team_id
from league_platform.intelligence import coverage_level
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_CURRENT_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
)
from league_platform.model import evaluate_league
from league_platform.model_admission import (
    ModelAdmissionBlocked,
    VerifiedOpenFootballCorpus,
    load_verified_openfootball_corpus,
)
from league_platform.openfootball_raw_archive import OpenFootballRawArchiveError
from league_platform.sources.openfootball_verified import VerifiedOpenFootballHistorySource
from league_platform.targets import (
    half_full_distribution,
    one_x_two_from_matrix,
    scoreline_matrix,
    top_scorelines,
    total_goals_distribution,
    total_over_under_distribution,
)
from league_platform.model_factors import build_factor_trace, factor_step
from league_platform.player_shadow import build_player_availability_shadow

FUTURE_RATE_LEARNING_RATE = 0.015
# A team with fewer than this many observed matches is not silently treated as
# a fully learned team.  The research lane may still emit a conservative
# league-prior projection, but the row is explicitly marked low-history and
# remains outside the production/public forecast gate.
TEAM_HISTORY_MIN_SAMPLE = 10
# The model currently evaluates the standard 2.5 total-goals line.  Keep the
# line in the prediction contract instead of making the Sites reader infer it
# from a probability map; a future line change must be visible and auditable.
TOTAL_OVER_UNDER_LINE = 2.5
DEFAULT_PREDICTION_HORIZON = timedelta(days=7)


def _rounded_probability_map(
    values: Mapping[str, float],
    digits: int,
) -> dict[str, float]:
    """Round a normalized map without losing its unit mass at display precision.

    The model keeps full precision internally.  The public contract, however,
    stores probabilities at a fixed number of decimal places.  Rounding each
    cell independently can leave a visible 0.999999/1.000001 residual, so the
    residual is assigned to the largest-mass cell after rounding.  This is a
    serialization correction only; it never changes the model calculation.
    """

    if digits < 0:
        raise ValueError("probability display precision must be non-negative")
    if not values:
        raise ValueError("probability map must not be empty")
    rounded: dict[str, float] = {}
    for key, value in values.items():
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("probability map contains a non-numeric value") from exc
        if not math.isfinite(number) or number < 0.0:
            raise ValueError("probability map contains a non-finite or negative value")
        rounded[key] = round(number, digits)
    residual = round(1.0 - sum(rounded.values()), digits)
    anchor = max(rounded, key=rounded.__getitem__)
    rounded[anchor] = round(rounded[anchor] + residual, digits)
    if rounded[anchor] < 0.0:
        raise ValueError("probability rounding produced a negative anchor")
    return rounded


def _finite_payload(value: object) -> bool:
    """Return whether a prediction payload contains only finite JSON numbers."""

    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(_finite_payload(key) and _finite_payload(item) for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return all(_finite_payload(item) for item in value)
    return True


def _outcome(match: dict) -> float:
    score = match["score"]
    return 1.0 if score["home"] > score["away"] else 0.5 if score["home"] == score["away"] else 0.0


def _history_reliability(sample_n: int, *, minimum: int = TEAM_HISTORY_MIN_SAMPLE) -> float:
    """Return the bounded weight assigned to team-specific history.

    ``0`` means that no team-specific result was observed before the cutoff;
    it therefore falls back to the league baseline.  The function never
    manufactures rows or treats a missing sample as a zero performance.
    """

    if isinstance(sample_n, bool) or not isinstance(sample_n, int) or sample_n < 0:
        raise ValueError("sample_n must be a non-negative integer")
    return min(1.0, sample_n / minimum) if minimum > 0 else 1.0


def _history_status(sample_n: int) -> str:
    """Describe how much team-specific pre-cutoff history is trustworthy."""

    if sample_n >= TEAM_HISTORY_MIN_SAMPLE:
        return "team_history"
    if sample_n > 0:
        return "team_history_shrunk"
    return "league_prior_no_team_history"


def _history_context_for_team(
    history: list[dict[str, object]],
    *,
    team_id_value: str,
    team_name: object,
    limit: int = 8,
) -> dict[str, object]:
    """Summarize the same causal history ledger used by the model.

    ``history`` is already replayed and cutoff-filtered by
    :func:`_verified_league_state`.  Keeping this projection downstream of
    that boundary is important: the workbench must not show a more recent
    result than the model was allowed to see.  The summary is descriptive
    context only; it does not alter the frozen probability calculation.
    """

    bounded_limit = max(1, min(20, int(limit)))
    candidates: list[dict[str, object]] = []
    for match in reversed(history):
        if not isinstance(match, Mapping):
            continue
        home_id = match.get("home_team_id")
        away_id = match.get("away_team_id")
        if home_id != team_id_value and away_id != team_id_value:
            continue
        score = match.get("score")
        if not isinstance(score, Mapping):
            continue
        home_goals = score.get("home")
        away_goals = score.get("away")
        if (
            isinstance(home_goals, bool)
            or isinstance(away_goals, bool)
            or not isinstance(home_goals, int)
            or not isinstance(away_goals, int)
            or home_goals < 0
            or away_goals < 0
        ):
            continue
        is_home = home_id == team_id_value
        goals_for = home_goals if is_home else away_goals
        goals_against = away_goals if is_home else home_goals
        result = "W" if goals_for > goals_against else "L" if goals_for < goals_against else "D"
        points = 3 if result == "W" else 1 if result == "D" else 0
        candidates.append(
            {
                "fixture_id": match.get("id"),
                "kickoff_at": match.get("kickoff_at"),
                "opponent_team": match.get("away_team") if is_home else match.get("home_team"),
                "side": "home" if is_home else "away",
                "goals_for": goals_for,
                "goals_against": goals_against,
                "result": result,
                "points": points,
            }
        )

    recent = candidates[:bounded_limit]
    wins = sum(row["result"] == "W" for row in recent)
    draws = sum(row["result"] == "D" for row in recent)
    losses = sum(row["result"] == "L" for row in recent)
    goals_for = sum(int(row["goals_for"]) for row in recent)
    goals_against = sum(int(row["goals_against"]) for row in recent)
    points = sum(int(row["points"]) for row in recent)
    recent_n = len(recent)
    return {
        "team_id": team_id_value,
        "team_name": team_name,
        "status": "available" if recent else "unavailable_no_pre_cutoff_history",
        "history_sample_n": len(candidates),
        "window_matches": recent_n,
        "form": [str(row["result"]) for row in recent],
        "summary": {
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "goals_for": goals_for,
            "goals_against": goals_against,
            "goal_difference": goals_for - goals_against,
            "points": points,
            "points_per_match": round(points / recent_n, 4) if recent_n else 0.0,
            "clean_sheets": sum(int(row["goals_against"]) == 0 for row in recent),
            "failed_to_score": sum(int(row["goals_for"]) == 0 for row in recent),
        },
        "matches": recent,
    }


def _history_context(
    history: list[dict[str, object]],
    *,
    home_team_id: str,
    home_team_name: object,
    away_team_id: str,
    away_team_name: object,
    observed_before: datetime,
    training_cutoff: object,
) -> dict[str, object]:
    """Build the auditable, causal recent-form context for one fixture."""

    return {
        "schema_version": "matchline.history_context.v1",
        "source": "OpenFootball",
        "source_scope": "verified_openfootball_historical_raw_archive",
        "observed_before": observed_before.isoformat(),
        "training_cutoff": training_cutoff,
        "window_size": 8,
        "teams": {
            "home": _history_context_for_team(
                history,
                team_id_value=home_team_id,
                team_name=home_team_name,
            ),
            "away": _history_context_for_team(
                history,
                team_id_value=away_team_id,
                team_name=away_team_name,
            ),
        },
        "boundary": "只使用 observed_before 前已归档的 verified OpenFootball 赛果；不含赛后信息。",
    }


def _team_identity(fixture: dict, side: str) -> dict[str, object]:
    """Expose the explicit canonical identity used by the historical join."""

    input_name = fixture.get(f"{side}_team")
    competition_id = fixture.get("competition_id")
    canonical_name = (
        canonical_team_name(str(competition_id), str(input_name))
        if isinstance(input_name, str) and competition_id
        else input_name
    )
    return {
        "input_name": input_name,
        "canonical_name": canonical_name,
        "team_id": fixture.get(f"{side}_team_id"),
        "alias_applied": bool(input_name and canonical_name and input_name != canonical_name),
    }


def _elo_state(matches: list[dict], home_advantage: float) -> dict[str, float]:
    ratings: dict[str, float] = defaultdict(lambda: 1500.0)
    for _, group in groupby(matches, key=lambda match: match["kickoff_at"]):
        pending = []
        for match in group:
            home_rating = ratings[match["home_team_id"]]
            away_rating = ratings[match["away_team_id"]]
            expected = 1 / (1 + 10 ** ((away_rating - home_rating - home_advantage) / 400))
            pending.append((match, expected))
        for match, expected in pending:
            change = 20 * (_outcome(match) - expected)
            ratings[match["home_team_id"]] += change
            ratings[match["away_team_id"]] -= change
    return dict(ratings)


def _dc_state(
    matches: list[dict],
    home_average: float,
    away_average: float,
    *,
    learning_rate: float = FUTURE_RATE_LEARNING_RATE,
) -> tuple[dict[str, float], dict[str, float]]:
    attack: dict[str, float] = defaultdict(float)
    defence: dict[str, float] = defaultdict(float)
    for _, group in groupby(matches, key=lambda match: match["kickoff_at"]):
        pending = []
        for match in group:
            home_rate = min(
                4.5,
                max(
                    0.15,
                    home_average
                    * math.exp(attack[match["home_team_id"]] - defence[match["away_team_id"]]),
                ),
            )
            away_rate = min(
                4.5,
                max(
                    0.15,
                    away_average
                    * math.exp(attack[match["away_team_id"]] - defence[match["home_team_id"]]),
                ),
            )
            pending.append((match, home_rate, away_rate))
        for match, home_rate, away_rate in pending:
            home_error = match["score"]["home"] - home_rate
            away_error = match["score"]["away"] - away_rate
            attack[match["home_team_id"]] += learning_rate * home_error
            defence[match["away_team_id"]] -= learning_rate * home_error
            attack[match["away_team_id"]] += learning_rate * away_error
            defence[match["home_team_id"]] -= learning_rate * away_error
    return dict(attack), dict(defence)


def _calibrate(
    probability: tuple[float, float, float], alpha: float, prior: list[float]
) -> tuple[float, float, float]:
    if len(prior) != 3:
        raise ValueError("three-way calibration prior must contain three values")
    if len(probability) != 3:
        raise ValueError("three-way probability must contain three values")
    try:
        alpha_value = float(alpha)
        probability_values = tuple(float(value) for value in probability)
        prior_values = tuple(float(value) for value in prior)
    except (TypeError, ValueError) as exc:
        raise ValueError("three-way calibration values must be numeric") from exc
    if (
        not math.isfinite(alpha_value)
        or not 0.0 <= alpha_value <= 1.0
        or any(not math.isfinite(value) or value < 0.0 for value in probability_values)
        or any(not math.isfinite(value) or value < 0.0 for value in prior_values)
    ):
        raise ValueError("three-way calibration values are invalid")
    values = (
        (1 - alpha_value) * probability_values[0] + alpha_value * prior_values[0],
        (1 - alpha_value) * probability_values[1] + alpha_value * prior_values[1],
        (1 - alpha_value) * probability_values[2] + alpha_value * prior_values[2],
    )
    total = sum(values)
    if not math.isfinite(total) or total <= 0.0:
        raise ValueError("three-way calibration has no finite probability mass")
    return (values[0] / total, values[1] / total, values[2] / total)


def _canonical_three_way_probability(value: object) -> dict[str, float] | None:
    """Convert provider 1X2 keys to the platform's home/draw/away contract."""

    if not isinstance(value, dict):
        return None
    if set(value) == {"home", "draw", "away"}:
        raw = {key: value[key] for key in ("home", "draw", "away")}
    elif set(value) == {"h", "d", "a"}:
        raw = {"home": value["h"], "draw": value["d"], "away": value["a"]}
    else:
        return None
    try:
        numbers = {key: float(number) for key, number in raw.items()}
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(number) or number < 0 for number in numbers.values()):
        return None
    total = sum(numbers.values())
    if total <= 0:
        return None
    return {key: number / total for key, number in numbers.items()}


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    """Parse an optional source timestamp without inventing a fallback."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _freeze_stage(
    kickoff: datetime,
    cutoff: datetime,
    *,
    lineup_available: bool = False,
    lineup_confirmed: bool = False,
    lineup_observed_at: str | None = None,
) -> str:
    """Name the latest *actually observed* standard freeze stage.

    The 90-minute window is not itself an official-lineup observation.  A
    fixture remains at ``t_minus_90m`` until a lineup record with a timestamp
    no later than ``cutoff`` is available.  This avoids labelling a prediction
    as a lineup-confirmation version merely because the clock crossed 90
    minutes.
    """

    minutes = (kickoff - cutoff).total_seconds() / 60
    if lineup_available and lineup_confirmed and lineup_observed_at:
        observed = _parse_utc_timestamp(lineup_observed_at)
        if observed is not None and observed <= cutoff and observed < kickoff:
            return "lineup_confirmation"
    if minutes <= 90:
        return "t_minus_90m"
    if minutes <= 6 * 60:
        return "t_minus_6h"
    if minutes <= 24 * 60:
        return "t_minus_24h"
    return "early_snapshot"


def _freeze_version_id(
    *,
    fixture_id: str | None,
    stage: str,
    cutoff: datetime | None,
    model_version: str | None,
) -> str:
    """Create a stable, content-addressed identifier for one freeze slot."""

    material = "|".join(
        (
            fixture_id or "unknown-fixture",
            stage,
            cutoff.isoformat() if cutoff else "pending",
            model_version or "unknown-model",
        )
    )
    return "freeze-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _freeze_versions(
    kickoff: datetime,
    as_of: datetime,
    *,
    fixture_id: str | None = None,
    lineup_available: bool,
    lineup_confirmed: bool = False,
    lineup_observed_at: str | None,
    model_version: str | None,
) -> list[dict[str, object]]:
    """Describe which immutable freeze versions exist at this as-of point.

    A future stage is marked pending rather than filled with a copy of an
    earlier prediction.  This makes the UI explicit about what was actually
    observed and prevents a later lineup from being backfilled into history.
    """

    stages: list[tuple[str, datetime | None]] = [
        ("t_minus_24h", kickoff - timedelta(hours=24)),
        ("t_minus_6h", kickoff - timedelta(hours=6)),
        ("t_minus_90m", kickoff - timedelta(minutes=90)),
        (
            "lineup_confirmation",
            _parse_utc_timestamp(lineup_observed_at),
        ),
    ]
    versions: list[dict[str, object]] = []
    for stage, cutoff in stages:
        available = bool(
            cutoff
            and cutoff <= as_of
            and (
                stage != "lineup_confirmation"
                or (lineup_available and lineup_confirmed and cutoff < kickoff)
            )
        )
        versions.append(
            {
                "version_id": _freeze_version_id(
                    fixture_id=fixture_id,
                    stage=stage,
                    cutoff=cutoff,
                    model_version=model_version,
                ),
                "stage": stage,
                "cutoff_at": cutoff.isoformat() if cutoff else None,
                "status": "available" if available else "pending",
                "generated_at": as_of.isoformat() if available else None,
                "model_version": model_version,
            }
        )
    return versions


def _player_layer(
    sofascore_feature: dict | None,
    espn_team_status: dict | None = None,
) -> dict[str, object]:
    """Expose player evidence without turning missing impact data into zeros."""

    if not isinstance(sofascore_feature, dict):
        if isinstance(espn_team_status, dict):
            roster_players = []
            full_roster = True
            for side in ("home", "away"):
                team = espn_team_status.get("teams", {}).get(side, {})
                if not isinstance(team, dict):
                    full_roster = False
                    continue
                full_roster = full_roster and bool(team.get("full_roster"))
                for row in team.get("players", []):
                    if not isinstance(row, dict) or not row.get("player_id"):
                        continue
                    roster_players.append(
                        {
                            "team_side": side,
                            "player_id": row["player_id"],
                            "name": row.get("name"),
                            "position": row.get("position"),
                            "status": row.get("status"),
                            "starter": row.get("starter"),
                            "expected_minutes": None,
                            "replacement_value": None,
                            "replacement_value_status": "unavailable_no_validated_impact_baseline",
                        }
                    )
            return {
                "status": "partial" if roster_players else "unavailable",
                "model_eligible": False,
                "full_roster": full_roster and bool(roster_players),
                "confirmed": False,
                "players": roster_players,
                "missing_fields": ["confirmed_lineup", "expected_minutes", "replacement_value"],
                "note": "ESPN 赛事摘要名单仅作证据展示，未视为官方首发或进入最终概率模型。",
            }
        return {
            "status": "unavailable",
            "model_eligible": False,
            "full_roster": False,
            "players": [],
            "missing_fields": [
                "confirmed_lineup",
                "expected_minutes",
                "replacement_value",
            ],
        }
    lineups = sofascore_feature.get("lineups")
    injuries = sofascore_feature.get("injuries")
    if not isinstance(lineups, dict) and not isinstance(injuries, dict):
        return {
            "status": "unavailable",
            "model_eligible": False,
            "full_roster": False,
            "players": [],
            "missing_fields": [
                "confirmed_lineup",
                "expected_minutes",
                "replacement_value",
            ],
        }

    players: list[dict[str, object]] = []
    missing_fields: set[str] = {"expected_minutes", "replacement_value"}
    for side in ("home", "away"):
        lineup_side = lineups.get(side) if isinstance(lineups, dict) else None
        injury_side = injuries.get(side) if isinstance(injuries, dict) else None
        if isinstance(lineup_side, dict):
            for row in lineup_side.get("players", []):
                if not isinstance(row, dict) or not row.get("player_id"):
                    continue
                expected_minutes = row.get("expected_minutes")
                if expected_minutes is not None:
                    missing_fields.discard("expected_minutes")
                players.append(
                    {
                        "team_side": side,
                        "player_id": row["player_id"],
                        "name": row.get("name"),
                        "position": row.get("position"),
                        "status": row.get("status"),
                        "starter": row.get("starter"),
                        "expected_minutes": expected_minutes,
                        "replacement_value": None,
                        "replacement_value_status": "unavailable_no_validated_impact_baseline",
                    }
                )
        if isinstance(injury_side, list):
            for row in injury_side:
                if not isinstance(row, dict) or not row.get("player_id"):
                    continue
                players.append(
                    {
                        "team_side": side,
                        "player_id": row["player_id"],
                        "name": row.get("name"),
                        "position": row.get("position"),
                        "status": row.get("status") or "unavailable",
                        "starter": None,
                        "expected_minutes": None,
                        "replacement_value": None,
                        "replacement_value_status": "unavailable_no_validated_impact_baseline",
                    }
                )
    confirmed = bool(isinstance(lineups, dict) and lineups.get("confirmed") is True)
    if not confirmed:
        missing_fields.add("confirmed_lineup")
    return {
        "status": "partial" if players else "unavailable",
        "model_eligible": False,
        "full_roster": False,
        "confirmed": confirmed,
        "players": players,
        "missing_fields": sorted(missing_fields),
        "note": "球员替代价值尚无通过独立留出验证的影响基线，不进入最终概率。",
    }


def _provider_lineup_feature(team_status: dict) -> dict | None:
    """Normalize an eligible ESPN XI without dropping its observation clock."""

    if not (
        isinstance(team_status, dict)
        and team_status.get("confirmed") is True
        and team_status.get("model_eligible") is True
    ):
        return None
    teams = team_status.get("teams")
    if not isinstance(teams, dict) or not all(
        isinstance(teams.get(side), dict) for side in ("home", "away")
    ):
        return None
    source = dict(team_status.get("source") or {})
    if not isinstance(source.get("retrieved_at"), str) and isinstance(
        team_status.get("retrieved_at"), str
    ):
        source["retrieved_at"] = team_status["retrieved_at"]
    return {
        "lineups": {
            "confirmed": True,
            "model_eligible": True,
            "available": True,
            "home": teams["home"],
            "away": teams["away"],
        },
        "injuries": {"home": [], "away": [], "available": False},
        "source": source,
    }


_VERIFIED_FUTURE_MODEL_VERSION = "dixon_coles_scoreline_matrix_v260_verified_openfootball_v1"
_BLOCKED_MODEL_FACTORS = (
    ("recent_xg", "近期 xG", "model_feature"),
    ("market_1x2", "1X2 市场基线", "market_reference"),
    ("weather", "天气", "context"),
    ("injuries", "伤停/球员状态", "player"),
    ("lineups", "首发/阵容", "lineup"),
)


def _snapshot_current_status(snapshot: object) -> object:
    if not isinstance(snapshot, Mapping):
        return None
    current = snapshot.get("current_data")
    return current.get("status") if isinstance(current, Mapping) else None


def _snapshot_upcoming_candidates(
    snapshot: object,
    *,
    cutoff: datetime,
    prediction_horizon: timedelta | None,
) -> list[dict[str, Any]]:
    """Read upcoming identities only for a non-material coverage ledger.

    The strict replay path never uses these rows to fit or score a model.  We
    consult them only when replay cannot produce a current corpus, so every
    identifiable snapshot fixture still receives an explicit blocked record.
    """

    if not isinstance(snapshot, Mapping):
        return []
    return upcoming_fixture_rows(
        snapshot,
        as_of=cutoff,
        horizon=prediction_horizon,
        include_unparseable_kickoff=True,
    )


def _coverage_rows_from_blocked(
    fixtures: Sequence[Mapping[str, Any]], blocked: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Add ID-only placeholders for blocked corpus rows omitted from fixtures."""

    rows = [dict(row) for row in fixtures]
    known = {
        str(row.get("id"))
        for row in rows
        if isinstance(row.get("id"), str) and row.get("id").strip()
    }
    for item in blocked:
        fixture_id = item.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id.strip() or fixture_id in known:
            continue
        rows.append({"id": fixture_id})
        known.add(fixture_id)
    return rows


def _blocked_snapshot_rows(
    snapshot: object,
    *,
    cutoff: datetime,
    prediction_horizon: timedelta | None,
    reason: str,
    message: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = _snapshot_upcoming_candidates(
        snapshot,
        cutoff=cutoff,
        prediction_horizon=prediction_horizon,
    )
    blocked = [
        blocked_fixture_record(row, reason=reason, message=message)
        for row in candidates
    ]
    return candidates, blocked


def _unavailable_verified_future(
    snapshot: object,
    *,
    observed_before: datetime | None,
    reason: str,
    blocked: Sequence[Mapping[str, Any]] | None = None,
    coverage_candidates: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    blocked_rows = blocked or []
    candidates = coverage_candidates or []
    return {
        "status": "unavailable",
        "as_of": observed_before.isoformat() if observed_before is not None else None,
        "current_data_status": _snapshot_current_status(snapshot),
        "predictions": [],
        "blocked": blocked_rows,
        "coverage": prediction_coverage(candidates, [], blocked_rows),
        "reason": reason,
        "model_input": {
            "status": "unavailable",
            "trust_anchor": "verified_openfootball_raw_archive_replay",
            "snapshot_model_facts_accepted": False,
        },
        "model_health": {},
        "message": "未能现场重放完整且耐久的 OpenFootball raw archive，未来预测不可用。",
    }


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _verified_archive_root(value: Path | str | None) -> tuple[Path | None, str | None]:
    if isinstance(value, Path):
        root = value
    elif isinstance(value, str) and value.strip():
        root = Path(value)
    else:
        return None, "verified_openfootball_archive_missing"
    try:
        if root.is_symlink() or not root.is_dir():
            return None, "verified_openfootball_archive_invalid"
        resolved = root.resolve(strict=True)
        volatile_root = Path("/dev/shm").resolve(strict=True)
        try:
            resolved.relative_to(volatile_root)
        except ValueError:
            pass
        else:
            return None, "verified_openfootball_archive_volatile"
    except OSError:
        return None, "verified_openfootball_archive_invalid"
    return root, None


def _string_field(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"verified current row {field} is invalid")
    return value


def _verified_current_fixtures(
    corpus: VerifiedOpenFootballCorpus,
    *,
    cutoff: datetime,
    prediction_horizon: timedelta | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    fixtures: list[dict[str, object]] = []
    blocked: list[dict[str, object]] = []
    horizon_end = cutoff + prediction_horizon if prediction_horizon is not None else None
    for row in corpus.rows:
        status = row.get("status")
        if status != "upcoming":
            continue
        fixture_id = _string_field(row, "id")
        if row.get("score") is not None:
            raise ValueError("verified upcoming row has a score")
        if row.get("kickoff_time_quality") != "exact":
            # The raw current corpus contains the full season, including many
            # date-only rows whose kickoff is still unknown.  They are only
            # candidates for a bounded prediction window when their calendar
            # date intersects that window; later rows must not inflate the
            # coverage denominator.  We deliberately do not manufacture a
            # timestamp from a date-only value.
            kickoff_date_value = row.get("kickoff_date")
            try:
                kickoff_date = datetime.fromisoformat(str(kickoff_date_value)).date()
            except (TypeError, ValueError):
                kickoff_date = None
            if kickoff_date is not None:
                if kickoff_date < cutoff.date():
                    continue
                if horizon_end is not None and kickoff_date > horizon_end.date():
                    continue
            blocked.append(
                blocked_fixture_record(
                    row,
                    reason="verified_current_kickoff_not_exact",
                )
            )
            continue
        kickoff_value = _string_field(row, "kickoff_at")
        try:
            kickoff = datetime.fromisoformat(kickoff_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("verified current kickoff is invalid") from exc
        if kickoff.tzinfo is None or kickoff.utcoffset() is None:
            raise ValueError("verified current kickoff is naive")
        kickoff = kickoff.astimezone(timezone.utc)
        if kickoff <= cutoff or (horizon_end is not None and kickoff > horizon_end):
            continue
        competition_id = _string_field(row, "competition_id")
        home_team = _string_field(row, "home_team")
        away_team = _string_field(row, "away_team")
        source = row.get("source")
        if not isinstance(source, Mapping) or source.get("name") != "OpenFootball":
            raise ValueError("verified current source is invalid")
        fixtures.append(
            {
                "id": fixture_id,
                "competition_id": competition_id,
                "season": _string_field(row, "season"),
                "kickoff_at": kickoff.isoformat(),
                "home_team": home_team,
                "away_team": away_team,
                "home_team_id": team_id(competition_id, home_team),
                "away_team_id": team_id(competition_id, away_team),
                "status": "upcoming",
                "score": None,
                "source": {
                    "name": "OpenFootball",
                    "source_id": source.get("source_id"),
                    "url": source.get("url"),
                    "retrieved_at": source.get("retrieved_at"),
                    "raw_sha256": source.get("raw_sha256"),
                    "license": source.get("license"),
                },
            }
        )
    # A full-season raw corpus can contain the same canonical fixture in more
    # than one source projection (or a replay may be accidentally appended
    # twice).  Collapse byte-identical rows, but quarantine conflicting facts
    # instead of allowing whichever row happened to appear first to win.
    unique_fixtures: list[dict[str, object]] = []
    fixture_by_id: dict[str, dict[str, object]] = {}
    conflicting_ids: set[str] = set()
    for fixture in fixtures:
        fixture_id = str(fixture["id"])
        previous = fixture_by_id.get(fixture_id)
        if previous is None:
            fixture_by_id[fixture_id] = fixture
            unique_fixtures.append(fixture)
            continue
        if fixture != previous:
            conflicting_ids.add(fixture_id)
    if conflicting_ids:
        unique_fixtures = [
            fixture
            for fixture in unique_fixtures
            if str(fixture["id"]) not in conflicting_ids
        ]
        for fixture_id in sorted(conflicting_ids):
            # ``fixture_by_id`` is guaranteed to contain the first row.
            blocked.append(
                blocked_fixture_record(
                    fixture_by_id[fixture_id],
                    reason="verified_current_duplicate_fixture_id",
                    message="verified current corpus contains conflicting rows for one fixture ID.",
                )
            )

    # Exact kickoff rows carry more information than date-only rows.  If the
    # same ID appears in both projections, retain the exact fixture and emit at
    # most one date-only block when no exact row survived.
    exact_ids = {str(fixture["id"]) for fixture in unique_fixtures}
    unique_blocked: list[dict[str, object]] = []
    blocked_by_id: dict[str, dict[str, object]] = {}
    for item in blocked:
        fixture_id = item.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            unique_blocked.append(dict(item))
            continue
        if fixture_id in exact_ids:
            continue
        previous = blocked_by_id.get(fixture_id)
        if previous is None:
            blocked_by_id[fixture_id] = dict(item)
            unique_blocked.append(dict(item))
    unique_fixtures.sort(key=lambda item: (str(item["kickoff_at"]), str(item["id"])))
    unique_blocked.sort(key=lambda item: str(item.get("fixture_id")))
    return unique_fixtures, unique_blocked


def _verified_league_state(
    source: VerifiedOpenFootballHistorySource,
    *,
    competition_id: str,
    cutoff: datetime,
) -> dict[str, Any] | None:
    try:
        result = source.load(competition_id)
    except OpenFootballRawArchiveError:
        return None
    matches = sorted(
        (
            match
            for match in result.matches
            if match.score is not None and match.kickoff_at.astimezone(timezone.utc) < cutoff
        ),
        key=lambda match: (match.kickoff_at, match.id),
    )
    if len({match.season for match in matches}) < 3:
        return None
    try:
        elo_health = evaluate_league(competition_id, matches)
        dc_health = evaluate_dixon_coles(competition_id, matches)
    except (ArithmeticError, ValueError):
        return None
    if elo_health.get("status") != "evaluated" or dc_health.get("status") != "evaluated":
        return None
    history = [match.to_dict() for match in matches]
    counts: dict[str, int] = defaultdict(int)
    for match in history:
        counts[str(match["home_team_id"])] += 1
        counts[str(match["away_team_id"])] += 1
    health: dict[str, Any] = {
        **elo_health,
        "data_cutoff": history[-1]["kickoff_at"],
        "dixon_coles": dc_health,
        "input_source": "verified_openfootball_historical_raw_archive",
    }
    attack, defence = _dc_state(
        history,
        float(dc_health["home_goal_average"]),
        float(dc_health["away_goal_average"]),
    )
    return {
        "health": health,
        "history": history,
        "counts": counts,
        "elo_ratings": _elo_state(history, float(elo_health["home_advantage_elo"])),
        "attack": attack,
        "defence": defence,
        "training_cutoff": history[-1]["kickoff_at"],
    }


def _rights_blocked_factor_rows() -> list[dict[str, object]]:
    return [
        {
            "id": factor_id,
            "label": label,
            "category": category,
            "status": "rights_blocked_v260",
            "enters_model": False,
            "before_probability_1x2": None,
            "after_probability_1x2": None,
            "delta_probability_1x2": None,
            "before_expected_goals": None,
            "after_expected_goals": None,
            "reference_probability_1x2": None,
            "observed_at": None,
            "source_name": None,
            "source_url": None,
            "note": "v260 中央权利策略阻断；snapshot 自报字段不进入模型。",
        }
        for factor_id, label, category in _BLOCKED_MODEL_FACTORS
    ]


def _verified_prediction(
    fixture: dict[str, object],
    state: dict[str, Any],
    *,
    cutoff: datetime,
) -> dict[str, object]:
    fixture_id = str(fixture["id"])
    kickoff_at = str(fixture["kickoff_at"])
    kickoff = datetime.fromisoformat(kickoff_at).astimezone(timezone.utc)
    health = state["health"]
    dc = health["dixon_coles"]
    counts: Mapping[str, int] = state["counts"]
    home_team_id = str(fixture["home_team_id"])
    away_team_id = str(fixture["away_team_id"])
    home_history_n = counts.get(home_team_id, 0)
    away_history_n = counts.get(away_team_id, 0)
    home_reliability = _history_reliability(home_history_n)
    away_reliability = _history_reliability(away_history_n)
    elo_ratings: Mapping[str, float] = state["elo_ratings"]
    home_elo = 1500.0 + home_reliability * (elo_ratings.get(home_team_id, 1500.0) - 1500.0)
    away_elo = 1500.0 + away_reliability * (elo_ratings.get(away_team_id, 1500.0) - 1500.0)
    non_draw_home = 1 / (
        1 + 10 ** ((away_elo - home_elo - float(health["home_advantage_elo"])) / 400)
    )
    elo_probability = _calibrate(
        (
            (1 - float(health["draw_rate"])) * non_draw_home,
            float(health["draw_rate"]),
            (1 - float(health["draw_rate"])) * (1 - non_draw_home),
        ),
        float(health["calibration_alpha"]),
        [float(value) for value in health["calibration_prior"]],
    )
    attack: Mapping[str, float] = state["attack"]
    defence: Mapping[str, float] = state["defence"]
    home_rate = min(
        4.5,
        max(
            0.15,
            float(dc["home_goal_average"])
            * math.exp(
                home_reliability * attack.get(home_team_id, 0.0)
                - away_reliability * defence.get(away_team_id, 0.0)
            ),
        ),
    )
    away_rate = min(
        4.5,
        max(
            0.15,
            float(dc["away_goal_average"])
            * math.exp(
                away_reliability * attack.get(away_team_id, 0.0)
                - home_reliability * defence.get(home_team_id, 0.0)
            ),
        ),
    )
    dc_probability = _calibrate(
        _one_x_two(home_rate, away_rate, float(dc["rho"])),
        float(dc["calibration_alpha"]),
        [float(value) for value in dc["calibration_prior"]],
    )
    matrix = scoreline_matrix(home_rate, away_rate, rho=float(dc["rho"]))
    matrix_probability = one_x_two_from_matrix(matrix)
    totals = total_goals_distribution(matrix)
    half_full = half_full_distribution(home_rate, away_rate)
    total_ou = total_over_under_distribution(matrix, TOTAL_OVER_UNDER_LINE)
    both_ready = (
        home_history_n >= TEAM_HISTORY_MIN_SAMPLE and away_history_n >= TEAM_HISTORY_MIN_SAMPLE
    )
    coverage = coverage_level(
        expected_features={
            "team_history",
            "recent_xg",
            "market",
            "weather",
            "injuries",
            "lineups",
        },
        available_features={"team_history"} if both_ready else set(),
    )
    factor_trace = build_factor_trace(
        [
            factor_step(
                factor_id="historical_goal_rates",
                label="OpenFootball 历史攻防与主场基线",
                category="model_component",
                before_matrix=None,
                after_matrix=matrix,
                after_expected_goals={"home": home_rate, "away": away_rate},
                source_name="OpenFootball",
                note="仅使用 observed_before 前现场重放并通过中央权利策略的历史 raw rows。",
            )
        ],
        not_applied=_rights_blocked_factor_rows(),
        note="v260 仅允许 OpenFootball verified raw history 进入动态模型。",
    )
    player_layer = _player_layer(None)
    player_shadow = build_player_availability_shadow(
        player_layer,
        observed_at=None,
        as_of=cutoff.isoformat(),
        kickoff_at=kickoff_at,
    )
    history_context = _history_context(
        state["history"],
        home_team_id=home_team_id,
        home_team_name=fixture["home_team"],
        away_team_id=away_team_id,
        away_team_name=fixture["away_team"],
        observed_before=cutoff,
        training_cutoff=state["training_cutoff"],
    )
    home_status = _history_status(home_history_n)
    away_status = _history_status(away_history_n)
    return {
        "fixture_id": fixture_id,
        "competition_id": fixture["competition_id"],
        "kickoff_at": kickoff_at,
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "team_identities": {
            "home": _team_identity(fixture, "home"),
            "away": _team_identity(fixture, "away"),
        },
        "as_of": cutoff.isoformat(),
        "cutoff_at": cutoff.isoformat(),
        "model_version": _VERIFIED_FUTURE_MODEL_VERSION,
        "model_parameters": {
            "dc_rate_learning_rate": FUTURE_RATE_LEARNING_RATE,
            "elo_k": 20.0,
            "ancillary_feature_weights": {
                "recent_xg": 0.0,
                "market": 0.0,
                "weather": 0.0,
                "injuries": 0.0,
                "lineups": 0.0,
            },
        },
        "freeze_stage": _freeze_stage(kickoff, cutoff),
        "freeze_versions": _freeze_versions(
            kickoff,
            cutoff,
            fixture_id=fixture_id,
            lineup_available=False,
            lineup_confirmed=False,
            lineup_observed_at=None,
            model_version=_VERIFIED_FUTURE_MODEL_VERSION,
        ),
        "training_cutoff": state["training_cutoff"],
        "history_context": history_context,
        "status": "research_only",
        "quality_gate": (
            "blocked_for_production_insufficient_team_history"
            if not both_ready
            else "blocked_for_production_v260_ancillary_rights"
        ),
        "providers": ["OpenFootball"],
        "coverage": coverage,
        "history_prior": {
            "mode": (
                "team_history"
                if both_ready
                else "team_history_with_explicit_league_prior_shrinkage"
            ),
            "minimum_team_matches": TEAM_HISTORY_MIN_SAMPLE,
            "home": {
                "team_id": home_team_id,
                "sample_n": home_history_n,
                "status": home_status,
                "reliability": round(home_reliability, 4),
            },
            "away": {
                "team_id": away_team_id,
                "sample_n": away_history_n,
                "status": away_status,
                "reliability": round(away_reliability, 4),
            },
            "enters_model": True,
            "note": (
                "球队样本达到最低门槛，使用 verified OpenFootball 历史状态。"
                if both_ready
                else "样本不足时只使用 verified 联赛先验及按样本量收缩的球队状态；不生成伪造历史。"
            ),
        },
        "player_layer": player_layer,
        "player_availability_shadow": player_shadow,
        "feature_times": [],
        "factor_trace": factor_trace,
        "feature_coverage": {
            "historical_matches_home": home_history_n,
            "historical_matches_away": away_history_n,
            "historical_team_history_ready": both_ready,
            "historical_home_status": home_status,
            "historical_away_status": away_status,
            "historical_home_reliability": round(home_reliability, 4),
            "historical_away_reliability": round(away_reliability, 4),
            "recent_xg": False,
            "current_market": False,
            "public_market_comparison": False,
            "weather": False,
            "injuries": False,
            "lineups": False,
            "lineups_confirmed": False,
            "roster_evidence": False,
            "injury_report_coverage": "rights_blocked_v260",
        },
        "elo_probability": {
            **_rounded_probability_map(
                dict(zip(("home", "draw", "away"), elo_probability, strict=True)),
                6,
            ),
        },
        "dixon_coles_probability": {
            **_rounded_probability_map(
                dict(zip(("home", "draw", "away"), dc_probability, strict=True)),
                6,
            ),
        },
        "primary_model": _VERIFIED_FUTURE_MODEL_VERSION,
        "primary_probability_1x2": _rounded_probability_map(matrix_probability, 6),
        "probability_1x2_from_scoreline": _rounded_probability_map(matrix_probability, 6),
        "scoreline_matrix": _rounded_probability_map(matrix, 8),
        "scoreline_top5": top_scorelines(matrix),
        "total_goals_probability": _rounded_probability_map(totals, 8),
        "half_full_probability": _rounded_probability_map(half_full, 8),
        "handicap_line": None,
        "handicap_probability": None,
        "market_handicap_probability": None,
        "total_over_under_probability": _rounded_probability_map(total_ou, 8),
        "total_over_under_line": TOTAL_OVER_UNDER_LINE,
        "market_total_goals_probability": None,
        "market_half_full_probability": None,
        "market_scoreline_probability": None,
        "market_probability": None,
        "market_provider": None,
        "market_baseline_kind": None,
        "market_retrieved_at": None,
        "expected_goals": {"home": round(home_rate, 4), "away": round(away_rate, 4)},
    }


def _valid_verified_prediction(
    fixture: Mapping[str, object], prediction: object
) -> bool:
    """Check the outer prediction contract before recording a prediction row."""

    if not isinstance(prediction, Mapping):
        return False
    if prediction.get("fixture_id") != fixture.get("id"):
        return False
    if not isinstance(prediction.get("kickoff_at"), str):
        return False
    if not _finite_payload(prediction):
        return False
    # These maps are part of the v260 research contract.  Requiring them here
    # prevents a partially constructed row from counting as coverage merely
    # because it carries a fixture ID.
    for field in (
        "primary_probability_1x2",
        "probability_1x2_from_scoreline",
        "scoreline_matrix",
        "total_goals_probability",
        "half_full_probability",
        "total_over_under_probability",
    ):
        value = prediction.get(field)
        if not isinstance(value, Mapping) or not value:
            return False
        try:
            numbers = [float(item) for item in value.values()]
        except (TypeError, ValueError):
            return False
        if any(not math.isfinite(number) or number < 0.0 for number in numbers):
            return False
        if not math.isfinite(sum(numbers)) or sum(numbers) <= 0.0:
            return False
    return True


def build_future_predictions(
    snapshot: dict,
    *,
    openfootball_raw_archive_dir: Path | str | None = None,
    observed_before: datetime | None = None,
    prediction_horizon: timedelta | None = DEFAULT_PREDICTION_HORIZON,
) -> dict[str, object]:
    """Build v260 research predictions from on-site verified raw replay only.

    Snapshot matches, model-health objects, current features and serialized
    trust/authorization receipts are deliberately outside the model-input API.
    The snapshot survives only as non-material UI status context.
    """

    cutoff = _aware_utc(observed_before)
    if cutoff is None:
        return _unavailable_verified_future(
            snapshot,
            observed_before=None,
            reason="verified_openfootball_invalid_observed_before",
        )
    if prediction_horizon is not None and (
        not isinstance(prediction_horizon, timedelta)
        or prediction_horizon < timedelta(0)
    ):
        return _unavailable_verified_future(
            snapshot,
            observed_before=cutoff,
            reason="verified_openfootball_invalid_prediction_horizon",
        )
    archive_root, archive_reason = _verified_archive_root(openfootball_raw_archive_dir)
    if archive_root is None:
        candidates, blocked = _blocked_snapshot_rows(
            snapshot,
            cutoff=cutoff,
            prediction_horizon=prediction_horizon,
            reason=archive_reason or "verified_openfootball_archive_invalid",
            message="verified OpenFootball raw archive 不可用；该场次没有可审计的模型输入。",
        )
        return _unavailable_verified_future(
            snapshot,
            observed_before=cutoff,
            reason=archive_reason or "verified_openfootball_archive_invalid",
            blocked=blocked,
            coverage_candidates=candidates,
        )
    try:
        history_source = VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=cutoff,
            source_ids=OPENFOOTBALL_HISTORY_SOURCE_IDS,
        )
        history_manifest = history_source.admission_manifest()
        current_corpus = load_verified_openfootball_corpus(
            archive_root,
            source_ids=OPENFOOTBALL_CURRENT_SOURCE_IDS,
            observed_before=cutoff,
        )
        fixtures, blocked = _verified_current_fixtures(
            current_corpus,
            cutoff=cutoff,
            prediction_horizon=prediction_horizon,
        )
    except (
        ModelAdmissionBlocked,
        OpenFootballRawArchiveError,
        OSError,
        TypeError,
        ValueError,
    ):
        candidates, blocked = _blocked_snapshot_rows(
            snapshot,
            cutoff=cutoff,
            prediction_horizon=prediction_horizon,
            reason="verified_openfootball_replay_failed",
            message="verified OpenFootball raw archive 重放失败；该场次没有可审计的模型输入。",
        )
        return _unavailable_verified_future(
            snapshot,
            observed_before=cutoff,
            reason="verified_openfootball_replay_failed",
            blocked=blocked,
            coverage_candidates=candidates,
        )
    if not fixtures:
        coverage_candidates = _coverage_rows_from_blocked(fixtures, blocked)
        return _unavailable_verified_future(
            snapshot,
            observed_before=cutoff,
            reason="verified_openfootball_no_upcoming_fixtures",
            blocked=blocked,
            coverage_candidates=coverage_candidates,
        )

    states: dict[str, dict[str, Any]] = {}
    model_health: dict[str, object] = {}
    for competition_id in sorted({str(fixture["competition_id"]) for fixture in fixtures}):
        try:
            state = _verified_league_state(
                history_source,
                competition_id=competition_id,
                cutoff=cutoff,
            )
        except Exception:
            # A malformed/evaluationally unsafe competition must not abort the
            # other fixtures in the window.  The fixture loop below records an
            # explicit block for every row with no admitted state.
            state = None
        if state is not None:
            states[competition_id] = state
            model_health[competition_id] = state["health"]

    predictions: list[dict[str, object]] = []
    for fixture in fixtures:
        competition_id = str(fixture["competition_id"])
        state = states.get(competition_id)
        if state is None:
            blocked.append(
                blocked_fixture_record(
                    fixture,
                    reason="verified_history_unavailable_for_competition",
                )
            )
            continue
        try:
            prediction = _verified_prediction(fixture, state, cutoff=cutoff)
            if not _valid_verified_prediction(fixture, prediction):
                raise ValueError("verified prediction probability contract is invalid")
        except Exception:
            blocked.append(
                blocked_fixture_record(
                    fixture,
                    reason="verified_prediction_invalid_probability",
                    message="模型输出包含非有限或不完整的概率字段，已阻断该场次。",
                )
            )
            continue
        predictions.append(dict(prediction))
    predictions.sort(key=lambda item: (str(item["kickoff_at"]), str(item["fixture_id"])))
    blocked.sort(key=lambda item: str(item["fixture_id"]))
    if not predictions:
        coverage_candidates = _coverage_rows_from_blocked(fixtures, blocked)
        return {
            **_unavailable_verified_future(
                snapshot,
                observed_before=cutoff,
                reason="verified_openfootball_history_unavailable",
                blocked=blocked,
                coverage_candidates=coverage_candidates,
            ),
            "model_input": {
                "status": "verified_current_only_history_unavailable",
                "trust_anchor": "verified_openfootball_raw_archive_replay",
                "snapshot_model_facts_accepted": False,
                "history_admission_sha256": history_manifest["admission_sha256"],
                "current_corpus_identity_sha256": current_corpus.identity_sha256,
            },
        }
    return {
        "status": "research_only",
        "as_of": cutoff.isoformat(),
        "current_data_status": _snapshot_current_status(snapshot),
        "predictions": predictions,
        "blocked": blocked,
        "coverage": prediction_coverage(
            _coverage_rows_from_blocked(fixtures, blocked), predictions, blocked
        ),
        "reason": None,
        "model_input": {
            "status": "verified",
            "trust_anchor": "verified_openfootball_raw_archive_replay",
            "snapshot_model_facts_accepted": False,
            "history_admission_sha256": history_manifest["admission_sha256"],
            "current_corpus_identity_sha256": current_corpus.identity_sha256,
            "policy_version": current_corpus.policy_version,
            "observed_before": cutoff.isoformat(),
        },
        "model_health": model_health,
        "message": (
            "仅使用 observed_before 前现场重放的 OpenFootball raw archive；"
            "市场、xG、天气、伤停及首发因 v260 权利策略不进入模型。"
        ),
    }
