"""Causal multi-target walk-forward evaluation over all reviewed seasons.

This module is intentionally separate from the compact legacy snapshot model.
It emits one prediction row per held-out match only after a configurable
historical warm-up, and every row carries a cutoff immediately before kickoff.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from itertools import groupby
from typing import Any, Mapping

from league_platform.domain import Match, Score
from league_platform.historical_xg import (
    DEFAULT_MIN_SAMPLE,
    DEFAULT_ROLLING_WINDOW,
    HistoricalXGIndex,
)
from league_platform.metrics import (
    assert_no_future_leakage,
    assert_probability_contract,
    brier_score,
    bootstrap_mean_ci,
    count_metrics,
    evaluate_three_way,
    expected_calibration_error,
    log_loss,
    ranked_probability_score,
    top_k_scoreline_hit,
)
from league_platform.targets import (
    half_full_distribution,
    handicap_distribution,
    one_x_two_from_matrix,
    scoreline_matrix,
    top_scorelines,
    total_goals_distribution,
    total_over_under_distribution,
    normalize_probabilities,
)
from league_platform.model_factors import build_factor_trace, factor_step, probability_1x2
from league_platform.player_shadow import build_player_availability_shadow_from_feature_snapshot


PROSPECTIVE_FREEZE_MODEL_VERSION = "strict_dynamic_elo_scoreline_stage_replay_v9_cross_provider_lineup_identity"
PROSPECTIVE_XG_BLEND_WEIGHT = 0.20
PROSPECTIVE_MARKET_BLEND_WEIGHT = 0.30
HISTORICAL_MARKET_TIME_BASES = frozenset(
    {
        "closing_average_pre_kickoff",
        "opening_average_fallback_pre_kickoff",
    }
)


def _lineup_confirmation_fingerprint(feature_snapshot: Mapping[str, Any] | None) -> str | None:
    """Fingerprint the semantic XI without provider IDs, order or polling state.

    A fixture-local shirt number is a strong cross-provider identity: the same
    team cannot field two starters with the same shirt.  Canonical player IDs
    take precedence when a registrar has supplied all 22; complete unique
    shirt sets are next, then normalized source names.  Provider IDs are only
    the final same-provider fallback.  This keeps a real player replacement a
    new XI while allowing official and reliable fallback feeds to confirm the
    same lineup under different names and identifiers.
    """

    if not isinstance(feature_snapshot, Mapping):
        return None
    marker = feature_snapshot.get("lineup_confirmation_event")
    if isinstance(marker, Mapping):
        marked_key = marker.get("provider_key")
        provider_keys = (
            (str(marked_key),)
            if marked_key in {"official_lineup", "fotmob_lineup", "team_status"}
            else ()
        )
    else:
        provider_keys = ("official_lineup", "fotmob_lineup", "team_status")
    for provider_key in provider_keys:
        provider = feature_snapshot.get(provider_key)
        if not isinstance(provider, Mapping):
            continue
        lineups = provider.get("lineups")
        if provider_key == "team_status" and not isinstance(lineups, Mapping):
            teams = provider.get("teams")
            if (
                provider.get("confirmed") is True
                and provider.get("model_eligible") is True
                and isinstance(teams, Mapping)
            ):
                lineups = {
                    "confirmed": True,
                    "model_eligible": True,
                    "home": teams.get("home"),
                    "away": teams.get("away"),
                }
        if not isinstance(lineups, Mapping):
            continue
        if lineups.get("confirmed") is not True or lineups.get("model_eligible") is not True:
            continue
        starters_by_side: dict[str, list[Mapping[str, Any]]] = {}
        complete = True
        for side in ("home", "away"):
            side_data = lineups.get(side)
            players = side_data.get("players") if isinstance(side_data, Mapping) else None
            starters = (
                [
                    player
                    for player in players
                    if isinstance(player, Mapping) and player.get("starter") is True
                ]
                if isinstance(players, list)
                else []
            )
            if len(starters) != 11:
                complete = False
                break
            starters_by_side[side] = starters
        if not complete:
            continue

        def normalized_scalar(value: object) -> str | None:
            if value in (None, "") or isinstance(value, bool):
                return None
            text = str(value).strip()
            if not text:
                return None
            if re.fullmatch(r"\d+", text):
                return str(int(text))
            return text.casefold()

        def normalized_name(value: object) -> str | None:
            if not isinstance(value, str) or not value.strip():
                return None
            ascii_text = "".join(
                char
                for char in unicodedata.normalize("NFKD", value)
                if not unicodedata.combining(char)
            )
            tokens = re.findall(r"[a-z0-9]+", ascii_text.casefold())
            return " ".join(sorted(tokens)) if tokens else None

        def signature(field: str) -> dict[str, list[str]] | None:
            values_by_side: dict[str, list[str]] = {}
            for side, starters in starters_by_side.items():
                values: list[str] = []
                for player in starters:
                    if field == "canonical_player_id":
                        value = normalized_scalar(player.get("canonical_player_id"))
                    elif field == "shirt_number":
                        value = normalized_scalar(
                            player.get("shirt_number") or player.get("jersey")
                        )
                    elif field == "name":
                        candidates = [
                            normalized_name(player.get("name")),
                            normalized_name(player.get("display_name")),
                        ]
                        aliases = player.get("aliases")
                        if isinstance(aliases, list):
                            candidates.extend(normalized_name(alias) for alias in aliases)
                        candidates = sorted(
                            {candidate for candidate in candidates if candidate},
                            key=lambda item: (len(item.split()), len(item), item),
                        )
                        value = candidates[0] if candidates else None
                    else:
                        value = normalized_scalar(
                            player.get("provider_player_id") or player.get("player_id")
                        )
                    if value is None:
                        return None
                    values.append(value)
                if len(set(values)) != 11:
                    return None
                values_by_side[side] = sorted(values)
            return values_by_side

        basis = None
        normalized = None
        for candidate_basis in (
            "canonical_player_id",
            "shirt_number",
            "name",
            "provider_player_id",
        ):
            candidate = signature(candidate_basis)
            if candidate is not None:
                basis = candidate_basis
                normalized = candidate
                break
        if basis is None or normalized is None:
            continue
        payload: dict[str, Any] = {"basis": basis, "lineups": normalized}
        if basis == "provider_player_id":
            payload["provider"] = provider_key
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    return None


def _label(match: Match) -> str:
    assert match.score is not None
    if match.score.home > match.score.away:
        return "home"
    if match.score.home == match.score.away:
        return "draw"
    return "away"


def _result_event_time(match: Match) -> datetime:
    """Return the first instant at which a result may update model state."""

    kickoff = match.kickoff_at.astimezone(timezone.utc)
    observed = None
    if match.result_observed_at:
        try:
            parsed = datetime.fromisoformat(match.result_observed_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
            observed = parsed.astimezone(timezone.utc)
    if observed is None:
        return kickoff
    return max(kickoff, observed)


def _observation_time(value: object) -> datetime | None:
    """Parse one trustworthy observation timestamp without inventing one."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fixture_observation_time(match: Match) -> datetime | None:
    """Return the current source's fixture observation timestamp, if valid."""

    return _observation_time(match.kickoff_time_observed_at)


def _prior_probability(matches: list[Match]) -> tuple[float, float, float]:
    if not matches:
        return (0.46, 0.25, 0.29)
    counts = {label: 1.0 for label in ("home", "draw", "away")}
    for match in matches:
        counts[_label(match)] += 1
    total = sum(counts.values())
    return tuple(counts[label] / total for label in ("home", "draw", "away"))


def _prior_total_probability(matches: list[Match]) -> dict[str, float]:
    counts = {str(index): 1.0 for index in range(5)} | {"5+": 1.0}
    for match in matches:
        if match.score is None:
            continue
        total = match.score.home + match.score.away
        counts[str(total) if total < 5 else "5+"] += 1
    denominator = sum(counts.values())
    return {label: value / denominator for label, value in counts.items()}


def _prior_total_ou_probability(matches: list[Match], line: float = 2.5) -> dict[str, float]:
    counts = {"over": 1.0, "under": 1.0}
    for match in matches:
        if match.score is None:
            continue
        total = match.score.home + match.score.away
        if total > line:
            counts["over"] += 1
        elif total < line:
            counts["under"] += 1
        else:
            counts["over"] += 0.5
            counts["under"] += 0.5
    denominator = sum(counts.values())
    return {label: value / denominator for label, value in counts.items()}


def _prior_half_full_probability(matches: list[Match]) -> dict[str, float]:
    counts = {f"{half}/{full}": 1.0 for half in "HDA" for full in "HDA"}
    for match in matches:
        label = _half_full_actual(match)
        if label is not None:
            counts[label] += 1
    denominator = sum(counts.values())
    return {label: value / denominator for label, value in counts.items()}


def _parameters(matches: list[Match]) -> tuple[float, float]:
    if len(matches) < 20:
        return 0.25, 40.0
    draws = sum(_label(match) == "draw" for match in matches) / len(matches)
    home_wins = sum(_label(match) == "home" for match in matches)
    away_wins = sum(_label(match) == "away" for match in matches)
    advantage = 400 * math.log10((home_wins + 1) / (away_wins + 1))
    return min(0.35, max(0.15, draws)), min(150.0, max(-100.0, advantage))


def _elo_probability(home: float, away: float, draw_rate: float, advantage: float) -> tuple[float, float, float]:
    non_draw_home = 1 / (1 + 10 ** ((away - home - advantage) / 400))
    return (
        (1 - draw_rate) * non_draw_home,
        draw_rate,
        (1 - draw_rate) * (1 - non_draw_home),
    )


def _half_full_actual(match: Match) -> str | None:
    if match.score is None or match.score.halftime_home is None or match.score.halftime_away is None:
        return None
    half = "H" if match.score.halftime_home > match.score.halftime_away else "D" if match.score.halftime_home == match.score.halftime_away else "A"
    full = _label(match)[0].upper()
    return f"{half}/{full}"


def _folds(rows: list[dict], metric_fn) -> list[dict[str, object]]:
    if not rows:
        return []
    size = max(1, math.ceil(len(rows) / 4))
    result = []
    for index in range(0, len(rows), size):
        fold = rows[index : index + size]
        result.append({
            "fold": len(result) + 1,
            "start_at": fold[0]["kickoff_at"],
            "end_at": fold[-1]["kickoff_at"],
            "sample_n": len(fold),
            **metric_fn(fold),
        })
    return result


def _log_loss_values(probabilities: list[dict[str, float]], outcomes: list[str]) -> list[float]:
    return [-math.log(max(1e-12, float(row.get(outcome, 0.0)))) for row, outcome in zip(probabilities, outcomes)]


def _select_scoreline_rho(
    rows: list[tuple[Match, float, float, tuple[float, float, float]]],
    *,
    window: int = 1000,
    objective: str = "three_way",
) -> float:
    """Select Dixon-Coles rho from only previously observed rate rows."""

    if len(rows) < 100:
        return 0.0
    if objective not in {"scoreline", "three_way"}:
        raise ValueError("rho_objective must be scoreline or three_way")
    candidates = [step / 100 for step in range(-15, 16, 3)]
    selected = rows[-window:]
    best_rho = 0.0
    best_loss = math.inf
    for rho in candidates:
        if objective == "scoreline":
            losses = []
            for match, home_rate, away_rate, _ in selected:
                if match.score is None:
                    continue
                matrix = scoreline_matrix(home_rate, away_rate, rho=rho)
                actual = f"{match.score.home}-{match.score.away}"
                losses.append(-math.log(max(1e-12, matrix.get(actual, 0.0))))
        else:
            outcomes = [row[3] for row in selected]
            probabilities = [
                tuple(one_x_two_from_matrix(scoreline_matrix(row[1], row[2], rho=rho)).values())
                for row in selected
            ]
            losses = [
                -sum(actual * math.log(max(1e-12, predicted)) for actual, predicted in zip(outcome, probability))
                for probability, outcome in zip(probabilities, outcomes)
            ]
        if not losses:
            return 0.0
        loss = sum(losses) / len(losses)
        if loss < best_loss:
            best_rho, best_loss = rho, loss
    return best_rho


def _temperature_scale_matrix(matrix: dict[str, float], temperature: float) -> dict[str, float]:
    """Apply a fixed, pre-declared probability temperature without new data."""

    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("scoreline_temperature must be positive and finite")
    if temperature == 1.0:
        return matrix
    values = {key: max(1e-15, float(value)) ** temperature for key, value in matrix.items()}
    denominator = sum(values.values())
    return {key: value / denominator for key, value in values.items()}


def _live_observed_at(value: object) -> datetime | None:
    """Read a source observation timestamp without inventing one."""

    if not isinstance(value, Mapping):
        return None
    candidates = [value.get("observed_at"), value.get("retrieved_at")]
    source = value.get("source")
    if isinstance(source, Mapping):
        candidates.extend((source.get("observed_at"), source.get("retrieved_at")))
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    return None


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _causal_market_observation(
    feature_snapshot: Mapping[str, Any] | None,
    *,
    cutoff_at: datetime,
) -> tuple[dict[str, float] | None, datetime | None, Mapping[str, Any] | None]:
    """Return the normalized market used at a freeze, if it is causal.

    A market feature is accepted only when both its timestamp and its three
    probabilities are valid.  Keeping this check in one place prevents the
    renderer from advertising a ``market_1x2`` feature when the blend was
    actually skipped because the payload was malformed.
    """

    if not isinstance(feature_snapshot, Mapping):
        return None, None, None
    market = feature_snapshot.get("market")
    observed_at = _live_observed_at(market)
    if not isinstance(market, Mapping) or observed_at is None:
        return None, None, None
    if observed_at > cutoff_at.astimezone(timezone.utc):
        return None, None, None
    probability = market.get("probability")
    if not isinstance(probability, Mapping):
        return None, None, None
    values = {
        label: _finite_number(probability.get(label))
        for label in ("home", "draw", "away")
    }
    if any(value is None for value in values.values()):
        return None, None, None
    normalized = normalize_probabilities({label: float(values[label]) for label in values})
    return normalized, observed_at, market


def _market_retrieved_at(
    market: Mapping[str, Any],
    *,
    fallback: datetime,
) -> tuple[datetime, str, str]:
    """Return the strongest retrieval timestamp and its declared basis."""

    candidates = (
        ("source_retrieved_at", market.get("retrieved_at")),
        ("source_retrieved_at", (market.get("source") or {}).get("retrieved_at") if isinstance(market.get("source"), Mapping) else None),
    )
    for basis, value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc), "exact_source_retrieved_at", basis
    return fallback, "exact_observed_at", "source_observed_at"


def _blend_scoreline_matrix_with_market(
    matrix: dict[str, float],
    market_probability: Mapping[str, object],
    *,
    weight: float = PROSPECTIVE_MARKET_BLEND_WEIGHT,
) -> dict[str, float]:
    """Blend a pre-kickoff 1X2 market into the scoreline matrix.

    Scaling each outcome bucket preserves the full scoreline distribution and
    keeps its 1X2 marginal exactly equal to the declared blend.  The market is
    used only when its own observation timestamp passed the causal cutoff.
    """

    if not 0 <= weight <= 1 or not math.isfinite(weight):
        raise ValueError("market blend weight must be between zero and one")
    labels = ("home", "draw", "away")
    values = {label: _finite_number(market_probability.get(label)) for label in labels}
    if any(value is None for value in values.values()):
        return matrix
    target = normalize_probabilities({label: float(values[label]) for label in labels})
    base = one_x_two_from_matrix(matrix)
    blended = {
        label: (1.0 - weight) * base[label] + weight * target[label]
        for label in labels
    }
    output: dict[str, float] = {}
    for score, probability in matrix.items():
        home, away = (int(value) for value in score.split("-", 1))
        label = "home" if home > away else "draw" if home == away else "away"
        denominator = max(1e-12, base[label])
        output[score] = float(probability) * blended[label] / denominator
    return normalize_probabilities(output)


def _live_feature_provenance(
    value: Mapping[str, Any],
    *,
    field: str,
    observed_at: datetime,
) -> dict[str, Any]:
    """Keep the minimum source identity needed to audit an accepted feature."""

    source = value.get("source")
    result: dict[str, Any] = {
        "field": field,
        "observed_at": observed_at.isoformat(),
    }
    if isinstance(source, Mapping):
        for key in (
            "name",
            "url",
            "raw_sha256",
            "wire_sha256",
            "content_sha256",
            "archive_file",
            "archive_sha256",
            "observed_at_basis",
            "timestamp_quality",
        ):
            item = source.get(key)
            if isinstance(item, str) and item:
                result[key] = item
        source_retrieved = source.get("retrieved_at")
        if isinstance(source_retrieved, str) and source_retrieved:
            result["source_retrieved_at"] = source_retrieved
    return result


def _apply_causal_live_features(
    matrix: dict[str, float],
    home_rate: float,
    away_rate: float,
    *,
    feature_snapshot: Mapping[str, Any] | None,
    cutoff_at: datetime,
    scoreline_rho: float = 0.0,
    scoreline_temperature: float = 1.0,
    xg_blend_weight: float = PROSPECTIVE_XG_BLEND_WEIGHT,
) -> tuple[dict[str, float], float, float, list[str], list[str], list[dict[str, Any]]]:
    """Apply only live observations whose source time is before the freeze."""

    if not isinstance(feature_snapshot, Mapping):
        return matrix, home_rate, away_rate, [], [], []
    if not math.isfinite(xg_blend_weight) or not 0 <= xg_blend_weight <= 1:
        raise ValueError("xg_blend_weight must be between zero and one")
    feature_times: list[str] = []
    feature_fields: list[str] = []
    feature_sources: list[dict[str, Any]] = []
    team_features: dict[str, Mapping[str, Any]] = {}
    team_feature_times: dict[str, datetime] = {}
    for side in ("home", "away"):
        value = feature_snapshot.get(side)
        if not isinstance(value, Mapping):
            continue
        observed_at = _live_observed_at(value)
        if observed_at is None or observed_at > cutoff_at.astimezone(timezone.utc):
            continue
        team_features[side] = value
        team_feature_times[side] = observed_at
        feature_times.append(observed_at.isoformat())

    if set(team_features) == {"home", "away"}:
        home_xg_for = _finite_number(team_features["home"].get("xg_for"))
        home_xg_against = _finite_number(team_features["home"].get("xg_against"))
        away_xg_for = _finite_number(team_features["away"].get("xg_for"))
        away_xg_against = _finite_number(team_features["away"].get("xg_against"))
        if None not in (home_xg_for, home_xg_against, away_xg_for, away_xg_against):
            recent_home = math.sqrt(max(0.05, home_xg_for) * max(0.05, away_xg_against))
            recent_away = math.sqrt(max(0.05, away_xg_for) * max(0.05, home_xg_against))
            home_rate = (1.0 - xg_blend_weight) * home_rate + xg_blend_weight * recent_home
            away_rate = (1.0 - xg_blend_weight) * away_rate + xg_blend_weight * recent_away
            matrix = _temperature_scale_matrix(
                scoreline_matrix(home_rate, away_rate, rho=scoreline_rho),
                scoreline_temperature,
            )
            feature_fields.append("recent_xg")
            feature_sources.extend(
                _live_feature_provenance(
                    team_features[side],
                    field=f"recent_xg_{side}",
                    observed_at=team_feature_times[side],
                )
                for side in ("home", "away")
            )

    market_probability, market_at, market = _causal_market_observation(
        feature_snapshot,
        cutoff_at=cutoff_at,
    )
    if market_probability is not None and market_at is not None and market is not None:
        matrix = _blend_scoreline_matrix_with_market(matrix, market_probability)
        feature_times.append(market_at.isoformat())
        feature_fields.append("market_1x2")
        feature_sources.append(
            _live_feature_provenance(
                market,
                field="market_1x2",
                observed_at=market_at,
            )
        )
    return (
        matrix,
        home_rate,
        away_rate,
        sorted(set(feature_times)),
        sorted(set(feature_fields)),
        sorted(feature_sources, key=lambda item: (item["field"], item["observed_at"])),
    )


def _factor_source(
    feature_sources: list[dict[str, Any]],
    field: str,
) -> tuple[str | None, str | None, str | None]:
    """Find source identity for a factor without inventing a provider."""

    for item in feature_sources:
        if item.get("field") != field:
            continue
        return (
            item.get("name") if isinstance(item.get("name"), str) else None,
            item.get("url") if isinstance(item.get("url"), str) else None,
            item.get("observed_at") if isinstance(item.get("observed_at"), str) else None,
        )
    return None, None, None


def _not_applied_factor_rows(
    feature_snapshot: Mapping[str, Any] | None,
    *,
    cutoff_at: datetime,
    accepted_fields: set[str],
    feature_sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose expected evidence that was missing or intentionally display-only.

    Weather, injuries and lineups are visible research evidence today, but the
    strict prospective model does not apply them to probabilities until their
    own validated historical feature contract exists.  The row therefore says
    ``not_applied`` instead of implying a zero effect.
    """

    if not isinstance(feature_snapshot, Mapping):
        feature_snapshot = {}
    cutoff = cutoff_at.astimezone(timezone.utc)
    definitions = (
        ("recent_xg", "近期 xG", "model_feature", "recent_xg"),
        ("market_1x2", "1X2 市场基线", "model_feature", "market"),
        ("weather", "天气", "context", "weather"),
        ("injuries", "伤停/球员状态", "player", "injuries"),
        ("lineups", "首发/阵容", "lineup", "lineups"),
    )
    rows: list[dict[str, Any]] = []
    for field, label, category, source_key in definitions:
        if field in accepted_fields:
            continue
        source_value: object = feature_snapshot.get(source_key)
        if source_value is None and source_key == "injuries":
            espn_injuries = feature_snapshot.get("espn_injuries")
            if isinstance(espn_injuries, Mapping):
                source_value = espn_injuries
            else:
                sofascore = feature_snapshot.get("sofascore")
                source_value = sofascore.get("injuries") if isinstance(sofascore, Mapping) else None
        if source_value is None and source_key == "lineups":
            marker = feature_snapshot.get("lineup_confirmation_event")
            marked_key = marker.get("provider_key") if isinstance(marker, Mapping) else None
            provider_keys = (
                (str(marked_key),)
                if marked_key in {"official_lineup", "fotmob_lineup", "team_status"}
                else ("official_lineup", "fotmob_lineup", "team_status", "sofascore")
            )
            for key in provider_keys:
                candidate = feature_snapshot.get(key)
                if candidate is not None:
                    source_value = candidate
                    break
        observed = _live_observed_at(source_value)
        source_mapping = source_value if isinstance(source_value, Mapping) else {}
        source_meta = source_mapping.get("source")
        if not isinstance(source_meta, Mapping):
            source_meta = source_mapping
        source_name = source_meta.get("name") if isinstance(source_meta.get("name"), str) else None
        source_url = source_meta.get("url") if isinstance(source_meta.get("url"), str) else None
        if observed is None:
            status = "missing" if source_value is None else "timestamp_missing"
            note = "没有可用于截止时点判定的结构化观察时间。"
        elif observed > cutoff:
            status = "post_cutoff"
            note = "来源存在，但观察时间晚于该冻结截止，不得回填。"
        else:
            status = "available_not_applied"
            note = "来源可展示；当前严格模型没有经过独立留出验证的入模系数。"
        rows.append(
            {
                "id": field,
                "label": label,
                "category": category,
                "status": status,
                "enters_model": False,
                "before_probability_1x2": None,
                "after_probability_1x2": None,
                "delta_probability_1x2": None,
                "before_expected_goals": None,
                "after_expected_goals": None,
                "reference_probability_1x2": None,
                "observed_at": observed.isoformat() if observed else None,
                "source_name": source_name,
                "source_url": source_url,
                "note": note,
            }
        )
    return rows


def _model_factor_trace(
    *,
    base_matrix: Mapping[str, Any],
    base_home_rate: float,
    base_away_rate: float,
    final_matrix: Mapping[str, Any],
    final_home_rate: float,
    final_away_rate: float,
    feature_snapshot: Mapping[str, Any] | None,
    cutoff_at: datetime,
    live_feature_fields: list[str],
    live_feature_sources: list[dict[str, Any]],
    scoreline_rho: float,
    scoreline_temperature: float,
    xg_blend_weight: float = PROSPECTIVE_XG_BLEND_WEIGHT,
) -> dict[str, Any]:
    """Build sequential deltas for the strict model's declared steps."""

    steps: list[dict[str, Any]] = []
    base_goals = {"home": base_home_rate, "away": base_away_rate}
    final_goals = {"home": final_home_rate, "away": final_away_rate}
    steps.append(
        factor_step(
            factor_id="historical_goal_rates",
            label="历史攻防与主场基线",
            category="model_component",
            before_matrix=None,
            after_matrix=base_matrix,
            after_expected_goals=base_goals,
            source_name="strict causal historical state",
            note="只使用截止时间前已更新的比赛结果与球队状态。",
        )
    )
    current_matrix: Mapping[str, Any] = base_matrix
    current_home_rate, current_away_rate = base_home_rate, base_away_rate
    if "recent_xg" in live_feature_fields and isinstance(feature_snapshot, Mapping):
        home = feature_snapshot.get("home")
        away = feature_snapshot.get("away")
        if isinstance(home, Mapping) and isinstance(away, Mapping):
            values = {
                "home_xg_for": _finite_number(home.get("xg_for")),
                "home_xg_against": _finite_number(home.get("xg_against")),
                "away_xg_for": _finite_number(away.get("xg_for")),
                "away_xg_against": _finite_number(away.get("xg_against")),
            }
            if all(value is not None for value in values.values()):
                recent_home = math.sqrt(max(0.05, float(values["home_xg_for"])) * max(0.05, float(values["away_xg_against"])))
                recent_away = math.sqrt(max(0.05, float(values["away_xg_for"])) * max(0.05, float(values["home_xg_against"])))
                xg_home = (1.0 - xg_blend_weight) * base_home_rate + xg_blend_weight * recent_home
                xg_away = (1.0 - xg_blend_weight) * base_away_rate + xg_blend_weight * recent_away
                xg_matrix = _temperature_scale_matrix(scoreline_matrix(xg_home, xg_away, rho=scoreline_rho), scoreline_temperature)
                source_name, source_url, observed_at = _factor_source(live_feature_sources, "recent_xg_home")
                if source_name is None:
                    source_name, source_url, observed_at = _factor_source(live_feature_sources, "recent_xg_away")
                steps.append(
                    factor_step(
                        factor_id="recent_xg",
                        label="近期 xG 调整",
                        category="model_feature",
                        before_matrix=current_matrix,
                        after_matrix=xg_matrix,
                        before_expected_goals={"home": base_home_rate, "away": base_away_rate},
                        after_expected_goals={"home": xg_home, "away": xg_away},
                        observed_at=observed_at,
                        source_name=source_name,
                        source_url=source_url,
                        note=f"固定权重 {xg_blend_weight:.0%}；不是临场重新训练。",
                    )
                )
                current_matrix = xg_matrix
                current_home_rate, current_away_rate = xg_home, xg_away
    if "market_1x2" in live_feature_fields:
        market_probability, market_at, market = _causal_market_observation(feature_snapshot, cutoff_at=cutoff_at)
        if market_probability is not None:
            market_matrix = _blend_scoreline_matrix_with_market(current_matrix, market_probability)
            source_name, source_url, _ = _factor_source(live_feature_sources, "market_1x2")
            steps.append(
                factor_step(
                    factor_id="market_1x2",
                    label="1X2 市场混合",
                    category="model_feature",
                    before_matrix=current_matrix,
                    after_matrix=market_matrix,
                    before_expected_goals={"home": current_home_rate, "away": current_away_rate},
                    after_expected_goals={"home": current_home_rate, "away": current_away_rate},
                    observed_at=market_at.isoformat() if market_at else None,
                    source_name=source_name or (market.get("provider") if isinstance(market, Mapping) and isinstance(market.get("provider"), str) else None),
                    source_url=source_url,
                    reference_probability=market_probability,
                    note=f"固定权重 {PROSPECTIVE_MARKET_BLEND_WEIGHT:.0%}；市场只作为校准对照，不等于优势。",
                )
            )
            current_matrix = market_matrix
    not_applied = _not_applied_factor_rows(
        feature_snapshot,
        cutoff_at=cutoff_at,
        accepted_fields=set(live_feature_fields),
        feature_sources=live_feature_sources,
    )
    # Keep the final row in sync with the actual renderer if a future change
    # adds another declared step before this trace is updated.
    if steps and steps[-1].get("after_probability_1x2") != probability_1x2(final_matrix):
        steps.append(
            factor_step(
                factor_id="renderer_final",
                label="渲染器最终矩阵",
                category="audit",
                before_matrix=current_matrix,
                after_matrix=final_matrix,
                before_expected_goals={"home": current_home_rate, "away": current_away_rate},
                after_expected_goals=final_goals,
                enters_model=True,
                note="仅用于核对因素账本与最终输出一致。",
            )
        )
    return build_factor_trace(steps, not_applied=not_applied)


def _three_way_metrics(rows: list[dict]) -> dict[str, object]:
    probabilities = [row["elo_probability"] for row in rows]
    outcomes = [row["outcome_1x2"] for row in rows]
    frequency = [row["frequency_probability"] for row in rows]
    market_rows = [row for row in rows if row["market_probability"] is not None]
    elo_model = evaluate_three_way(probabilities, outcomes)
    scoreline_model = (
        evaluate_three_way([row["scoreline_probability"] for row in rows], outcomes)
        if all(row.get("scoreline_probability") is not None for row in rows)
        else None
    )
    model = scoreline_model or elo_model
    baseline = evaluate_three_way(frequency, outcomes)
    market = (
        evaluate_three_way([row["market_probability"] for row in market_rows], [row["outcome_1x2"] for row in market_rows])
        if market_rows else None
    )
    opening_market_rows = [row for row in rows if row.get("market_opening_probability") is not None]
    closing_market_rows = [row for row in rows if row.get("market_closing_probability") is not None]
    opening_market = (
        evaluate_three_way(
            [row["market_opening_probability"] for row in opening_market_rows],
            [row["outcome_1x2"] for row in opening_market_rows],
        )
        if opening_market_rows else None
    )
    closing_market = (
        evaluate_three_way(
            [row["market_closing_probability"] for row in closing_market_rows],
            [row["outcome_1x2"] for row in closing_market_rows],
        )
        if closing_market_rows else None
    )
    model_on_market = (
        evaluate_three_way(
            [row["scoreline_probability"] if scoreline_model else row["elo_probability"] for row in market_rows],
            [row["outcome_1x2"] for row in market_rows],
        )
        if market_rows else None
    )
    frequency_on_market = (
        evaluate_three_way([row["frequency_probability"] for row in market_rows], [row["outcome_1x2"] for row in market_rows])
        if market_rows else None
    )
    model_brier_values = [
        sum(((row["scoreline_probability"] if scoreline_model else row["elo_probability"])[label] - (label == row["outcome_1x2"])) ** 2 for label in ("home", "draw", "away"))
        for row in rows
    ]
    market_brier_values = [
        sum((row["market_probability"][label] - (label == row["outcome_1x2"])) ** 2 for label in ("home", "draw", "away"))
        for row in market_rows
    ]
    model_on_market_brier_values = [
        sum(
            ((row["scoreline_probability"] if scoreline_model else row["elo_probability"])[label]
             - (label == row["outcome_1x2"])) ** 2
            for label in ("home", "draw", "away")
        )
        for row in market_rows
    ]
    model_minus_market_brier_values = [
        model_value - market_value
        for model_value, market_value in zip(model_on_market_brier_values, market_brier_values)
    ]
    differences = [
        sum((((row["scoreline_probability"] if scoreline_model else row["elo_probability"])[label] - (label == row["outcome_1x2"])) ** 2) for label in ("home", "draw", "away"))
        - sum((row["frequency_probability"][label] - (label == row["outcome_1x2"])) ** 2 for label in ("home", "draw", "away"))
        for row in rows
    ]
    return {
        "model": model,
        "model_kind": "scoreline_matrix_1x2" if scoreline_model else "dynamic_elo",
        "elo_model": elo_model,
        "scoreline_model": scoreline_model,
        "historical_frequency": baseline,
        "market": market,
        "market_opening": opening_market,
        "market_closing": closing_market,
        "model_on_market_sample": model_on_market,
        "historical_frequency_on_market_sample": frequency_on_market,
        "model_brier_ci": bootstrap_mean_ci(model_brier_values),
        "market_brier_ci": bootstrap_mean_ci(market_brier_values) if market_brier_values else None,
        # Paired differences use exactly the same fixtures for model and
        # market, so the CI answers the release question without conflating
        # separate missing-row samples.
        "model_minus_market_brier_ci": (
            bootstrap_mean_ci(model_minus_market_brier_values)
            if model_minus_market_brier_values
            else None
        ),
        "model_minus_frequency_brier_ci": bootstrap_mean_ci(differences),
    }


def _scoreline_metrics(rows: list[dict]) -> dict[str, object]:
    if not rows:
        return {"status": "unavailable", "sample_n": 0}
    score_rows = [row for row in rows if row["actual_score"] is not None]
    total_rows = [row for row in rows if row["actual_total_goals"] is not None]
    half_rows = [row for row in rows if row["actual_half_full"] is not None]
    output: dict[str, object] = {"sample_n": len(rows)}
    if score_rows:
        scoreline_probabilities = [row["scoreline_matrix"] for row in score_rows]
        scoreline_outcomes = [row["actual_score"] for row in score_rows]
        output["scoreline"] = {
            "sample_n": len(score_rows),
            "log_loss": log_loss(scoreline_probabilities, scoreline_outcomes),
            "brier": brier_score(scoreline_probabilities, scoreline_outcomes),
            "rps": ranked_probability_score(scoreline_probabilities, scoreline_outcomes),
            "ece": expected_calibration_error(scoreline_probabilities, scoreline_outcomes),
            "log_loss_ci": bootstrap_mean_ci(_log_loss_values(scoreline_probabilities, scoreline_outcomes)),
            "frequency_log_loss": log_loss(
                [row["scoreline_frequency_probability"] for row in score_rows], scoreline_outcomes
            ),
            "frequency_log_loss_ci": bootstrap_mean_ci(
                _log_loss_values([row["scoreline_frequency_probability"] for row in score_rows], scoreline_outcomes)
            ),
            "top1_hit": top_k_scoreline_hit([row["scoreline_top5"] for row in score_rows], [row["actual_score"] for row in score_rows], 1),
            "exact_hit": top_k_scoreline_hit([row["scoreline_top5"] for row in score_rows], [row["actual_score"] for row in score_rows], 1),
            "top3_hit": top_k_scoreline_hit([row["scoreline_top5"] for row in score_rows], [row["actual_score"] for row in score_rows], 3),
            "top5_hit": top_k_scoreline_hit([row["scoreline_top5"] for row in score_rows], [row["actual_score"] for row in score_rows], 5),
            "frequency_top5_hit": top_k_scoreline_hit(
                [row["scoreline_frequency_top5"] for row in score_rows],
                [row["actual_score"] for row in score_rows],
                5,
            ),
        }
    if total_rows:
        output["totals"] = {
            "sample_n": len(total_rows),
            "log_loss": log_loss([row["total_goals_probability"] for row in total_rows], [row["actual_total_goals"] for row in total_rows]),
            "brier": brier_score([row["total_goals_probability"] for row in total_rows], [row["actual_total_goals"] for row in total_rows]),
            "rps": ranked_probability_score([row["total_goals_probability"] for row in total_rows], [row["actual_total_goals"] for row in total_rows]),
            "ece": expected_calibration_error([row["total_goals_probability"] for row in total_rows], [row["actual_total_goals"] for row in total_rows]),
            "frequency_log_loss": log_loss([row["total_frequency_probability"] for row in total_rows], [row["actual_total_goals"] for row in total_rows]),
            "log_loss_ci": bootstrap_mean_ci(_log_loss_values([row["total_goals_probability"] for row in total_rows], [row["actual_total_goals"] for row in total_rows])),
            "frequency_log_loss_ci": bootstrap_mean_ci(_log_loss_values([row["total_frequency_probability"] for row in total_rows], [row["actual_total_goals"] for row in total_rows])),
            **count_metrics([row["expected_goals"]["home"] + row["expected_goals"]["away"] for row in total_rows], [row["actual_total_goals_numeric"] for row in total_rows]),
        }
    total_ou_rows = [row for row in rows if row["actual_total_ou"] in {"over", "under"}]
    if total_ou_rows:
        model_ou = [row["total_over_under_probability"] for row in total_ou_rows]
        outcomes = [row["actual_total_ou"] for row in total_ou_rows]
        market_rows = [row for row in total_ou_rows if row["total_market_probability"] is not None]
        opening_market_rows = [
            row for row in total_ou_rows if row.get("total_market_opening_probability") is not None
        ]
        closing_market_rows = [
            row for row in total_ou_rows if row.get("total_market_closing_probability") is not None
        ]

        def _binary_market_metrics(rows: list[dict], key: str) -> dict[str, object] | None:
            if not rows:
                return None
            probabilities = [row[key] for row in rows]
            labels = [row["actual_total_ou"] for row in rows]
            return {
                "brier": brier_score(probabilities, labels),
                "log_loss": log_loss(probabilities, labels),
                "rps": ranked_probability_score(probabilities, labels),
                "ece": expected_calibration_error(probabilities, labels),
                "log_loss_ci": bootstrap_mean_ci(_log_loss_values(probabilities, labels)),
                "sample_n": len(rows),
            }

        output["total_over_under"] = {
            "sample_n": len(total_ou_rows),
            "model": {
                "brier": brier_score(model_ou, outcomes),
                "log_loss": log_loss(model_ou, outcomes),
                "rps": ranked_probability_score(model_ou, outcomes),
                "ece": expected_calibration_error(model_ou, outcomes),
                "log_loss_ci": bootstrap_mean_ci(_log_loss_values(model_ou, outcomes)),
            },
            "frequency": {
                "brier": brier_score([row["total_frequency_ou_probability"] for row in total_ou_rows], outcomes),
                "log_loss": log_loss([row["total_frequency_ou_probability"] for row in total_ou_rows], outcomes),
                "rps": ranked_probability_score([row["total_frequency_ou_probability"] for row in total_ou_rows], outcomes),
                "ece": expected_calibration_error([row["total_frequency_ou_probability"] for row in total_ou_rows], outcomes),
                "log_loss_ci": bootstrap_mean_ci(_log_loss_values([row["total_frequency_ou_probability"] for row in total_ou_rows], outcomes)),
            },
            "market": (
                {
                    "brier": brier_score([row["total_market_probability"] for row in market_rows], [row["actual_total_ou"] for row in market_rows]),
                    "log_loss": log_loss([row["total_market_probability"] for row in market_rows], [row["actual_total_ou"] for row in market_rows]),
                    "rps": ranked_probability_score([row["total_market_probability"] for row in market_rows], [row["actual_total_ou"] for row in market_rows]),
                    "ece": expected_calibration_error([row["total_market_probability"] for row in market_rows], [row["actual_total_ou"] for row in market_rows]),
                    "log_loss_ci": bootstrap_mean_ci(_log_loss_values([row["total_market_probability"] for row in market_rows], [row["actual_total_ou"] for row in market_rows])),
                    "sample_n": len(market_rows),
                }
                if market_rows else None
            ),
            "model_minus_market_brier_ci": (
                bootstrap_mean_ci([
                    sum((row["total_over_under_probability"][label] - (label == row["actual_total_ou"])) ** 2 for label in ("over", "under"))
                    - sum((row["total_market_probability"][label] - (label == row["actual_total_ou"])) ** 2 for label in ("over", "under"))
                    for row in market_rows
                ])
                if market_rows else None
            ),
            "market_opening": _binary_market_metrics(
                opening_market_rows, "total_market_opening_probability"
            ),
            "market_closing": _binary_market_metrics(
                closing_market_rows, "total_market_closing_probability"
            ),
        }
    handicap_rows = [row for row in rows if row["actual_handicap"] is not None]
    if handicap_rows:
        outcomes = [row["actual_handicap"] for row in handicap_rows]
        output["handicap"] = {
            "sample_n": len(handicap_rows),
            "model": {
                "brier": brier_score([row["handicap_probability"] for row in handicap_rows], outcomes),
                "log_loss": log_loss([row["handicap_probability"] for row in handicap_rows], outcomes),
                "rps": ranked_probability_score([row["handicap_probability"] for row in handicap_rows], outcomes),
                "ece": expected_calibration_error([row["handicap_probability"] for row in handicap_rows], outcomes),
                "log_loss_ci": bootstrap_mean_ci(_log_loss_values([row["handicap_probability"] for row in handicap_rows], outcomes)),
            },
            "frequency": (
                {
                    "log_loss": log_loss([row["handicap_frequency_probability"] for row in handicap_rows], outcomes),
                    "brier": brier_score([row["handicap_frequency_probability"] for row in handicap_rows], outcomes),
                    "rps": ranked_probability_score([row["handicap_frequency_probability"] for row in handicap_rows], outcomes),
                    "ece": expected_calibration_error([row["handicap_frequency_probability"] for row in handicap_rows], outcomes),
                    "log_loss_ci": bootstrap_mean_ci(_log_loss_values([row["handicap_frequency_probability"] for row in handicap_rows], outcomes)),
                }
                if all(row["handicap_frequency_probability"] is not None for row in handicap_rows)
                else None
            ),
            "market_coverage": sum(row["handicap_market_probability"] is not None for row in handicap_rows) / len(handicap_rows),
        }
    if half_rows:
        output["half_full"] = {
            "sample_n": len(half_rows),
            "log_loss": log_loss([row["half_full_probability"] for row in half_rows], [row["actual_half_full"] for row in half_rows]),
            "brier": brier_score([row["half_full_probability"] for row in half_rows], [row["actual_half_full"] for row in half_rows]),
            "rps": ranked_probability_score([row["half_full_probability"] for row in half_rows], [row["actual_half_full"] for row in half_rows]),
            "ece": expected_calibration_error([row["half_full_probability"] for row in half_rows], [row["actual_half_full"] for row in half_rows]),
            "frequency_log_loss": log_loss([row["half_full_frequency_probability"] for row in half_rows], [row["actual_half_full"] for row in half_rows]),
            "log_loss_ci": bootstrap_mean_ci(_log_loss_values([row["half_full_probability"] for row in half_rows], [row["actual_half_full"] for row in half_rows])),
            "frequency_log_loss_ci": bootstrap_mean_ci(_log_loss_values([row["half_full_frequency_probability"] for row in half_rows], [row["actual_half_full"] for row in half_rows])),
        }
    return output


def evaluate_strict_walk_forward(
    matches: list[Match],
    *,
    warmup_seasons: int = 2,
    elo_k: float = 20.0,
    rate_learning_rate: float = 0.015,
    season_regression: float = 0.0,
    calibration_window: int = 1000,
    calibration_refresh: int = 50,
    scoreline_temperature: float = 1.0,
    rating_halflife_days: float | None = None,
    strength_halflife_days: float | None = None,
    rho_objective: str = "three_way",
    include_targets: bool = True,
    compute_metrics: bool = True,
    historical_xg_index: HistoricalXGIndex | None = None,
    historical_xg_window: int = DEFAULT_ROLLING_WINDOW,
    historical_xg_min_sample: int = DEFAULT_MIN_SAMPLE,
    historical_xg_blend_weight: float = PROSPECTIVE_XG_BLEND_WEIGHT,
) -> dict[str, object]:
    """Evaluate all matches after warm-up using only prior observations."""

    finished = sorted((match for match in matches if match.score is not None), key=lambda item: (item.kickoff_at, item.id))
    seasons = sorted({match.season for match in finished})
    if len(seasons) <= warmup_seasons:
        return {"status": "not_evaluated", "sample_n": 0, "message": "not enough seasons after warm-up"}
    if calibration_refresh < 1:
        raise ValueError("calibration_refresh must be positive")
    if not math.isfinite(scoreline_temperature) or scoreline_temperature <= 0:
        raise ValueError("scoreline_temperature must be positive and finite")
    if not 0 <= season_regression <= 1:
        raise ValueError("season_regression must be between zero and one")
    if rating_halflife_days is not None and (
        not math.isfinite(rating_halflife_days) or rating_halflife_days <= 0
    ):
        raise ValueError("rating_halflife_days must be positive and finite")
    if strength_halflife_days is not None and (
        not math.isfinite(strength_halflife_days) or strength_halflife_days <= 0
    ):
        raise ValueError("strength_halflife_days must be positive and finite")
    if rho_objective not in {"scoreline", "three_way"}:
        raise ValueError("rho_objective must be scoreline or three_way")
    if historical_xg_window < 1:
        raise ValueError("historical_xg_window must be positive")
    if historical_xg_min_sample < 1:
        raise ValueError("historical_xg_min_sample must be positive")
    if not math.isfinite(historical_xg_blend_weight) or not 0 <= historical_xg_blend_weight <= 1:
        raise ValueError("historical_xg_blend_weight must be between zero and one")
    held_out_seasons = set(seasons[warmup_seasons:])
    ratings: dict[str, float] = defaultdict(lambda: 1500.0)
    attack: dict[str, float] = defaultdict(float)
    defence: dict[str, float] = defaultdict(float)
    prior: list[Match] = []
    previous_rows: list[tuple[Match, tuple[float, float, float], tuple[float, float, float]]] = []
    previous_scoreline_rates: list[tuple[Match, float, float, tuple[float, float, float]]] = []
    rows: list[dict] = []
    outcome_counts = {"home": 0, "draw": 0, "away": 0}
    total_home_goals = 0
    total_away_goals = 0
    total_counts = {str(index): 1.0 for index in range(5)} | {"5+": 1.0}
    total_ou_counts = {"over": 1.0, "under": 1.0}
    half_full_counts = {f"{half}/{full}": 1.0 for half in "HDA" for full in "HDA"}
    handicap_counts: dict[float, dict[str, float]] = {}
    scoreline_counts = {
        f"{home}-{away}": 1.0
        for home in range(9)
        for away in range(9)
    }
    last_calibration_at = 0
    last_rho_calibration_at = 0
    scoreline_rho = 0.0
    rho_calibration_refresh = max(250, calibration_refresh * 5)
    alpha = 0.0
    calibration_prior = (0.46, 0.25, 0.29)
    last_team_at: dict[str, datetime] = {}
    last_strength_at: dict[str, datetime] = {}
    last_season: str | None = None
    date_only_dates = {
        match.kickoff_at.date()
        for match in finished
        if match.kickoff_time_quality == "date_only"
    }
    def batch_key(match: Match):
        return (
            ("calendar_date", match.kickoff_at.date())
            if match.kickoff_at.date() in date_only_dates
            else ("timestamp", match.kickoff_at)
        )
    for _, grouped in groupby(finished, key=batch_key):
        batch = list(grouped)
        season = batch[0].season
        if last_season is not None and season != last_season and season_regression:
            carry = 1.0 - season_regression
            for team, rating in list(ratings.items()):
                ratings[team] = 1500.0 + carry * (rating - 1500.0)
            for strengths in (attack, defence):
                for team, value in list(strengths.items()):
                    strengths[team] = carry * value
        last_season = season
        if rating_halflife_days is not None:
            for match in batch:
                for team in (match.home_team_id, match.away_team_id):
                    previous_at = last_team_at.get(team)
                    if previous_at is not None:
                        elapsed_days = max(
                            0.0,
                            (match.kickoff_at - previous_at).total_seconds() / 86400.0,
                        )
                        decay = 2.0 ** (-elapsed_days / rating_halflife_days)
                        ratings[team] = 1500.0 + (ratings[team] - 1500.0) * decay
                    last_team_at[team] = match.kickoff_at
        if strength_halflife_days is not None:
            for match in batch:
                for team in (match.home_team_id, match.away_team_id):
                    previous_at = last_strength_at.get(team)
                    if previous_at is not None:
                        elapsed_days = max(
                            0.0,
                            (match.kickoff_at - previous_at).total_seconds() / 86400.0,
                        )
                        decay = 2.0 ** (-elapsed_days / strength_halflife_days)
                        attack[team] *= decay
                        defence[team] *= decay
                    last_strength_at[team] = match.kickoff_at
        if len(prior) < 20:
            draw_rate, advantage = 0.25, 40.0
        else:
            draws = outcome_counts["draw"] / len(prior)
            advantage = 400 * math.log10((outcome_counts["home"] + 1) / (outcome_counts["away"] + 1))
            draw_rate = min(0.35, max(0.15, draws))
            advantage = min(150.0, max(-100.0, advantage))
        if prior:
            denominator = sum(value + 1.0 for value in outcome_counts.values())
            frequency = tuple((outcome_counts[label] + 1.0) / denominator for label in ("home", "draw", "away"))
        else:
            frequency = _prior_probability([])
        if previous_rows and len(previous_rows) >= 100 and (
            len(previous_rows) - last_calibration_at >= calibration_refresh
        ):
            alpha, calibration_prior = _calibrate_previous(previous_rows, window=calibration_window)
            last_calibration_at = len(previous_rows)
        elif not previous_rows or len(previous_rows) < 100:
            alpha, calibration_prior = 0.0, frequency
        if include_targets and previous_scoreline_rates and len(previous_scoreline_rates) >= 100 and (
            len(previous_scoreline_rates) - last_rho_calibration_at >= rho_calibration_refresh
        ):
            scoreline_rho = _select_scoreline_rho(
                previous_scoreline_rates,
                window=calibration_window,
                objective=rho_objective,
            )
            last_rho_calibration_at = len(previous_scoreline_rates)
        home_average = total_home_goals / len(prior) if prior else 1.45
        away_average = total_away_goals / len(prior) if prior else 1.15
        if include_targets:
            total_denominator = sum(total_counts.values())
            total_frequency = {label: value / total_denominator for label, value in total_counts.items()}
            total_ou_denominator = sum(total_ou_counts.values())
            total_frequency_ou = {label: value / total_ou_denominator for label, value in total_ou_counts.items()}
            half_full_denominator = sum(half_full_counts.values())
            half_full_frequency = {label: value / half_full_denominator for label, value in half_full_counts.items()}
        pending_updates = []
        pending_scoreline_rates = []
        for match in batch:
            home_rating = ratings[match.home_team_id]
            away_rating = ratings[match.away_team_id]
            raw_elo = _elo_probability(home_rating, away_rating, draw_rate, advantage)
            elo_probability = tuple((1 - alpha) * raw_elo[index] + alpha * calibration_prior[index] for index in range(3))
            home_rate = min(4.5, max(0.15, home_average * math.exp(attack[match.home_team_id] - defence[match.away_team_id])))
            away_rate = min(4.5, max(0.15, away_average * math.exp(attack[match.away_team_id] - defence[match.home_team_id])))
            matrix = (
                _temperature_scale_matrix(
                    scoreline_matrix(home_rate, away_rate, rho=scoreline_rho),
                    scoreline_temperature,
                )
                if include_targets
                else None
            )
            cutoff_at = match.kickoff_at - timedelta(seconds=1)
            historical_xg_snapshot = None
            live_feature_times: list[str] = []
            live_feature_fields: list[str] = []
            live_feature_sources: list[dict[str, Any]] = []
            if matrix is not None and historical_xg_index is not None:
                historical_xg_snapshot = historical_xg_index.snapshot_for_match(
                    match,
                    cutoff_at=cutoff_at,
                    window=historical_xg_window,
                    min_sample=historical_xg_min_sample,
                )
                (
                    matrix,
                    home_rate,
                    away_rate,
                    live_feature_times,
                    live_feature_fields,
                    live_feature_sources,
                ) = _apply_causal_live_features(
                    matrix,
                    home_rate,
                    away_rate,
                    feature_snapshot=historical_xg_snapshot,
                    cutoff_at=cutoff_at,
                    scoreline_rho=scoreline_rho,
                    scoreline_temperature=scoreline_temperature,
                    xg_blend_weight=historical_xg_blend_weight,
                )
            independent_matrix = matrix
            historical_market_eligible = bool(
                include_targets
                and matrix is not None
                and match.market_probability is not None
                and match.market_time_basis in HISTORICAL_MARKET_TIME_BASES
            )
            if historical_market_eligible:
                matrix = _blend_scoreline_matrix_with_market(
                    matrix,
                    {
                        "home": match.market_probability.home,
                        "draw": match.market_probability.draw,
                        "away": match.market_probability.away,
                    },
                    weight=PROSPECTIVE_MARKET_BLEND_WEIGHT,
                )
                live_feature_times.append(cutoff_at.isoformat())
                live_feature_fields.append("market_1x2")
            live_feature_times = sorted(set(live_feature_times))
            live_feature_fields = sorted(set(live_feature_fields))
            live_feature_sources = sorted(
                live_feature_sources,
                key=lambda item: (item.get("field", ""), item.get("observed_at", "")),
            )
            actual_score = f"{match.score.home}-{match.score.away}"
            actual_total = match.score.home + match.score.away
            total_label = str(actual_total) if actual_total < 5 else "5+"
            handicap_line = match.handicap_line if include_targets else None
            total_line = match.total_line if include_targets else None
            actual_handicap = None
            handicap_frequency = None
            if handicap_line is not None:
                from league_platform.targets import settle_handicap

                actual_handicap = settle_handicap(match.score.home, match.score.away, handicap_line)
                line_key = round(handicap_line, 2)
                counts = handicap_counts.setdefault(
                    line_key,
                    {label: 1.0 for label in ("win", "push", "loss", "half_win", "half_loss")},
                )
                denominator = sum(counts.values())
                handicap_frequency = {label: value / denominator for label, value in counts.items()}
            actual_total_ou = None
            if total_line is not None:
                actual_total_ou = "over" if actual_total > total_line else "under" if actual_total < total_line else "push"
            row = {
                "fixture_id": match.id,
                "competition_id": match.competition_id,
                "season": match.season,
                "kickoff_at": match.kickoff_at.isoformat(),
                "kickoff_time_quality": match.kickoff_time_quality,
                "kickoff_time_source": match.kickoff_time_source,
                "kickoff_time_observed_at": match.kickoff_time_observed_at,
                "cutoff_at": (match.kickoff_at - timedelta(seconds=1)).isoformat(),
                "model_training_cutoff": prior[-1].kickoff_at.isoformat() if prior else None,
                "feature_times": live_feature_times,
                # football-data.co.uk does not expose the original HTTP
                # observation timestamp.  Do not fabricate one: preserve the
                # exact timestamp as null and record only the provider's
                # semantic pre-kickoff bound for the leakage audit.
                "market_retrieved_at": None,
                "market_time_bound_at": (
                    (match.kickoff_at - timedelta(seconds=1)).isoformat()
                    if match.market_probability is not None
                    else None
                ),
                "market_time_quality": (
                    "semantic_pre_kickoff_bound"
                    if match.market_probability is not None
                    else "unavailable"
                ),
                "market_time_basis": match.market_time_basis,
                "elo_probability": {"home": elo_probability[0], "draw": elo_probability[1], "away": elo_probability[2]},
                "frequency_probability": {"home": frequency[0], "draw": frequency[1], "away": frequency[2]},
                "market_probability": ({"home": match.market_probability.home, "draw": match.market_probability.draw, "away": match.market_probability.away} if match.market_probability else None),
                "market_opening_probability": ({"home": match.market_opening_probability.home, "draw": match.market_opening_probability.draw, "away": match.market_opening_probability.away} if match.market_opening_probability else None),
                "market_closing_probability": ({"home": match.market_closing_probability.home, "draw": match.market_closing_probability.draw, "away": match.market_closing_probability.away} if match.market_closing_probability else None),
                "live_feature_fields": live_feature_fields,
                "live_feature_sources": live_feature_sources,
                "live_xg_blend_weight": (
                    historical_xg_blend_weight if "recent_xg" in live_feature_fields else 0.0
                ),
                "live_market_blend_weight": (
                    PROSPECTIVE_MARKET_BLEND_WEIGHT if historical_market_eligible else 0.0
                ),
                "outcome_1x2": _label(match),
                "expected_goals": {"home": home_rate, "away": away_rate},
                "historical_xg": (
                    {
                        "status": historical_xg_snapshot.get("status"),
                        "schema": historical_xg_snapshot.get("schema"),
                        "window": historical_xg_snapshot.get("window"),
                        "min_sample": historical_xg_snapshot.get("min_sample"),
                        "observed_at_exact": historical_xg_snapshot.get("observed_at_exact"),
                        "observed_at_basis": historical_xg_snapshot.get("observed_at_basis"),
                        "home_sample_n": (historical_xg_snapshot.get("home") or {}).get("sample_n"),
                        "away_sample_n": (historical_xg_snapshot.get("away") or {}).get("sample_n"),
                    }
                    if historical_xg_snapshot is not None
                    else None
                ),
            }
            if include_targets:
                scoreline_denominator = sum(scoreline_counts.values())
                scoreline_frequency = {
                    label: value / scoreline_denominator for label, value in scoreline_counts.items()
                }
                row.update({
                    "scoreline_top5": top_scorelines(matrix),
                    "scoreline_matrix": matrix,
                    "scoreline_probability": one_x_two_from_matrix(matrix),
                    "independent_scoreline_matrix": independent_matrix,
                    "independent_scoreline_probability": one_x_two_from_matrix(independent_matrix),
                    "scoreline_rho": scoreline_rho,
                    "scoreline_rho_training_n": len(previous_scoreline_rates),
                    "scoreline_rho_training_cutoff": (
                        previous_scoreline_rates[-1][0].kickoff_at.isoformat()
                        if previous_scoreline_rates
                        else None
                    ),
                    "scoreline_frequency_probability": scoreline_frequency,
                    "scoreline_frequency_top5": top_scorelines(scoreline_frequency),
                    "total_goals_probability": total_goals_distribution(matrix),
                    "independent_total_goals_probability": total_goals_distribution(independent_matrix),
                    "total_frequency_probability": total_frequency,
                    "total_over_under_line": total_line,
                    "total_over_under_probability": total_over_under_distribution(matrix, total_line or 2.5),
                    "independent_total_over_under_probability": total_over_under_distribution(independent_matrix, total_line or 2.5),
                    "total_frequency_ou_probability": total_frequency_ou,
                    "total_market_probability": (
                        {"over": match.total_probability["over"], "under": match.total_probability["under"]}
                        if match.total_probability else None
                    ),
                    "total_market_opening_probability": match.total_opening_probability,
                    "total_market_closing_probability": match.total_closing_probability,
                    "half_full_probability": half_full_distribution(home_rate, away_rate),
                    "half_full_frequency_probability": half_full_frequency,
                    "actual_score": actual_score,
                    "actual_total_goals": total_label,
                    "actual_total_goals_numeric": actual_total,
                    "actual_half_full": _half_full_actual(match),
                    "handicap_probability": handicap_distribution(matrix, handicap_line or 0.0),
                    "handicap_frequency_probability": handicap_frequency,
                    "handicap_market_probability": (
                        {"win": match.handicap_probability["win"], "loss": match.handicap_probability["loss"]}
                        if match.handicap_probability else None
                    ),
                    "actual_handicap": actual_handicap,
                    "actual_total_ou": actual_total_ou,
                })
            if match.season in held_out_seasons:
                rows.append(row)
            previous_rows.append((match, raw_elo, _outcome_tuple(match)))
            if include_targets:
                pending_scoreline_rates.append((match, home_rate, away_rate, _outcome_tuple(match)))
            pending_updates.append((match, raw_elo))
        for match, raw_elo in pending_updates:
            actual = _outcome_tuple(match)
            expected_home = (raw_elo[0] / max(1e-9, 1 - draw_rate)) if draw_rate < 1 else 0.5
            ratings[match.home_team_id] += elo_k * ((actual[0] + 0.5 * actual[1]) - expected_home)
            ratings[match.away_team_id] -= elo_k * ((actual[0] + 0.5 * actual[1]) - expected_home)
            home_rate = min(4.5, max(0.15, home_average * math.exp(attack[match.home_team_id] - defence[match.away_team_id])))
            away_rate = min(4.5, max(0.15, away_average * math.exp(attack[match.away_team_id] - defence[match.home_team_id])))
            attack[match.home_team_id] += rate_learning_rate * (match.score.home - home_rate)
            defence[match.away_team_id] -= rate_learning_rate * (match.score.home - home_rate)
            attack[match.away_team_id] += rate_learning_rate * (match.score.away - away_rate)
            defence[match.home_team_id] -= rate_learning_rate * (match.score.away - away_rate)
            prior.append(match)
            label = _label(match)
            outcome_counts[label] += 1
            total_home_goals += match.score.home
            total_away_goals += match.score.away
            if include_targets:
                total = match.score.home + match.score.away
                actual_scoreline = f"{match.score.home}-{match.score.away}"
                if actual_scoreline in scoreline_counts:
                    scoreline_counts[actual_scoreline] += 1
                total_counts[str(total) if total < 5 else "5+"] += 1
                if total > 2.5:
                    total_ou_counts["over"] += 1
                elif total < 2.5:
                    total_ou_counts["under"] += 1
                else:
                    total_ou_counts["over"] += 0.5
                    total_ou_counts["under"] += 0.5
                half_full_label = _half_full_actual(match)
                if half_full_label is not None:
                    half_full_counts[half_full_label] += 1
                if match.handicap_line is not None:
                    from league_platform.targets import settle_handicap

                    line_key = round(match.handicap_line, 2)
                    counts = handicap_counts.setdefault(
                        line_key,
                        {label: 1.0 for label in ("win", "push", "loss", "half_win", "half_loss")},
                    )
                    counts[settle_handicap(match.score.home, match.score.away, match.handicap_line)] += 1
        previous_scoreline_rates.extend(pending_scoreline_rates)
    three_way = _three_way_metrics(rows) if compute_metrics else {
        "status": "not_computed",
        "sample_n": len(rows),
    }
    target_metrics = _scoreline_metrics(rows) if include_targets and compute_metrics else {}
    return {
        "status": "evaluated",
        "model": (
            "strict_dynamic_elo_scoreline_v2_causal_rho+historical_xg_candidate"
            if historical_xg_index is not None
            else "strict_dynamic_elo_scoreline_v2_causal_rho"
        ),
        "hyperparameters": {
            "elo_k": elo_k,
            "rate_learning_rate": rate_learning_rate,
            "season_regression": season_regression,
            "calibration_window": calibration_window,
            "calibration_refresh": calibration_refresh,
            "rho_calibration_refresh": rho_calibration_refresh,
            "scoreline_temperature": scoreline_temperature,
            "rating_halflife_days": rating_halflife_days,
            "strength_halflife_days": strength_halflife_days,
            "rho_objective": rho_objective,
            "historical_xg_window": historical_xg_window,
            "historical_xg_min_sample": historical_xg_min_sample,
            "historical_xg_blend_weight": historical_xg_blend_weight,
        },
        "historical_xg_index": (
            historical_xg_index.diagnostics() if historical_xg_index is not None else None
        ),
        "warmup_seasons": seasons[:warmup_seasons],
        "held_out_seasons": sorted(held_out_seasons),
        "sample_n": len(rows),
        "data_cutoff": finished[-1].kickoff_at.isoformat(),
        "three_way": three_way,
        "targets": target_metrics,
        "walk_forward_folds": (
            _folds(rows, lambda fold: {"three_way_brier": _three_way_metrics(fold)["model"]["brier"]})
            if compute_metrics
            else []
        ),
        "rows": rows,
    }


_FREEZE_STAGE_OFFSETS = {
    "t_minus_24h": timedelta(hours=24),
    "t_minus_6h": timedelta(hours=6),
    "t_minus_90m": timedelta(minutes=90),
}


def _stage_prediction_row(
    match: Match,
    *,
    stage: str,
    cutoff_at: datetime,
    ratings: dict[str, float],
    attack: dict[str, float],
    defence: dict[str, float],
    prior: list[Match],
    previous_rows: list[tuple[Match, tuple[float, float, float], tuple[float, float, float]]],
    previous_scoreline_rates: list[tuple[Match, float, float, tuple[float, float, float]]],
    outcome_counts: dict[str, int],
    total_home_goals: int,
    total_away_goals: int,
    total_counts: dict[str, float],
    total_ou_counts: dict[str, float],
    half_full_counts: dict[str, float],
    scoreline_counts: dict[str, float],
    calibration_window: int,
    calibration_refresh: int,
    last_calibration_at: int,
    last_rho_calibration_at: int,
    scoreline_rho: float,
    rho_calibration_refresh: int,
    alpha: float,
    calibration_prior: tuple[float, float, float],
    elo_k: float,
    rate_learning_rate: float,
    scoreline_temperature: float,
    feature_snapshot: Mapping[str, Any] | None = None,
    model_version: str = "strict_dynamic_elo_scoreline_stage_replay_v3_causal_rho",
) -> dict:
    """Build one stage-scoped row from state observed strictly before cutoff.

    This helper deliberately excludes closing market, handicap and lineup
    fields: the historical sources do not provide their observation time at
    each earlier freeze slot, so carrying them backward would be leakage.
    """

    del elo_k, rate_learning_rate, last_calibration_at, last_rho_calibration_at
    if len(prior) < 20:
        draw_rate, advantage = 0.25, 40.0
    else:
        draws = outcome_counts["draw"] / len(prior)
        advantage = 400 * math.log10((outcome_counts["home"] + 1) / (outcome_counts["away"] + 1))
        draw_rate = min(0.35, max(0.15, draws))
        advantage = min(150.0, max(-100.0, advantage))
    denominator = sum(value + 1.0 for value in outcome_counts.values())
    frequency = tuple(
        (outcome_counts[label] + 1.0) / denominator
        for label in ("home", "draw", "away")
    ) if prior else _prior_probability([])

    home_rating = ratings[match.home_team_id]
    away_rating = ratings[match.away_team_id]
    raw_elo = _elo_probability(home_rating, away_rating, draw_rate, advantage)
    # Stage replay intentionally uses a fixed, predeclared calibration policy.
    # Recomputing the final-kickoff calibrator at every earlier event would be
    # both expensive and an easy way to accidentally borrow later features.
    # The stage model therefore uses the historical-frequency prior with no
    # late-stage calibration blend; it remains a separately named model.
    del previous_rows, calibration_window, calibration_refresh, alpha, calibration_prior
    stage_alpha, stage_prior = 0.0, frequency
    elo_probability = tuple(
        (1 - stage_alpha) * raw_elo[index] + stage_alpha * stage_prior[index]
        for index in range(3)
    )
    home_average = total_home_goals / len(prior) if prior else 1.45
    away_average = total_away_goals / len(prior) if prior else 1.15
    home_rate = min(4.5, max(0.15, home_average * math.exp(attack[match.home_team_id] - defence[match.away_team_id])))
    away_rate = min(4.5, max(0.15, away_average * math.exp(attack[match.away_team_id] - defence[match.home_team_id])))
    del rho_calibration_refresh
    if not math.isfinite(scoreline_rho):
        raise ValueError("scoreline_rho must be finite")
    # The caller updates this value only from rows strictly before the stage
    # cutoff. Keeping it in the row makes freeze replay auditable instead of
    # silently falling back to an independent scoreline model with rho=0.
    stage_rho = scoreline_rho
    base_home_rate, base_away_rate = home_rate, away_rate
    base_matrix = _temperature_scale_matrix(
        scoreline_matrix(home_rate, away_rate, rho=stage_rho), scoreline_temperature
    )
    matrix = base_matrix
    (
        matrix,
        home_rate,
        away_rate,
        live_feature_times,
        live_feature_fields,
        live_feature_sources,
    ) = _apply_causal_live_features(
        matrix,
        home_rate,
        away_rate,
        feature_snapshot=feature_snapshot,
        cutoff_at=cutoff_at,
        scoreline_rho=stage_rho,
        scoreline_temperature=scoreline_temperature,
    )
    market_probability, market_observed_at, market_feature = _causal_market_observation(
        feature_snapshot,
        cutoff_at=cutoff_at,
    )
    if "market_1x2" not in live_feature_fields:
        # The feature renderer is authoritative: malformed or post-cutoff
        # market payloads must remain unavailable in the exported contract.
        market_probability = None
        market_observed_at = None
    factor_trace = _model_factor_trace(
        base_matrix=base_matrix,
        base_home_rate=base_home_rate,
        base_away_rate=base_away_rate,
        final_matrix=matrix,
        final_home_rate=home_rate,
        final_away_rate=away_rate,
        feature_snapshot=feature_snapshot,
        cutoff_at=cutoff_at,
        live_feature_fields=live_feature_fields,
        live_feature_sources=live_feature_sources,
        scoreline_rho=stage_rho,
        scoreline_temperature=scoreline_temperature,
    )
    handicap_line: float | None = None
    handicap_market_probability: dict[str, float] | None = None
    lottery_market = (
        feature_snapshot.get("lottery_market")
        if isinstance(feature_snapshot, Mapping)
        else None
    )
    lottery_observed_at = _live_observed_at(lottery_market)
    if (
        isinstance(lottery_market, Mapping)
        and lottery_observed_at is not None
        and lottery_observed_at <= cutoff_at.astimezone(timezone.utc)
    ):
        try:
            candidate_line = float(lottery_market.get("hhad_line"))
        except (TypeError, ValueError):
            candidate_line = math.nan
        if math.isfinite(candidate_line):
            handicap_line = candidate_line
            raw_market = lottery_market.get("hhad_probability")
            if isinstance(raw_market, Mapping):
                market_values = {
                    "win": _finite_number(raw_market.get("h")),
                    "push": _finite_number(raw_market.get("d")),
                    "loss": _finite_number(raw_market.get("a")),
                }
                if all(value is not None for value in market_values.values()):
                    handicap_market_probability = normalize_probabilities(
                        {key: float(value) for key, value in market_values.items()}
                    )
            live_feature_times.append(lottery_observed_at.isoformat())
            live_feature_fields.append("sports_lottery_hhad")
            live_feature_sources.append(
                _live_feature_provenance(
                    lottery_market,
                    field="sports_lottery_hhad",
                    observed_at=lottery_observed_at,
                )
            )
            live_feature_times[:] = sorted(set(live_feature_times))
            live_feature_fields[:] = sorted(set(live_feature_fields))
            live_feature_sources[:] = sorted(
                live_feature_sources,
                key=lambda item: (item["field"], item["observed_at"]),
            )
    total_frequency = {label: value / sum(total_counts.values()) for label, value in total_counts.items()}
    total_frequency_ou = {label: value / sum(total_ou_counts.values()) for label, value in total_ou_counts.items()}
    half_full_frequency = {label: value / sum(half_full_counts.values()) for label, value in half_full_counts.items()}
    scoreline_frequency = {label: value / sum(scoreline_counts.values()) for label, value in scoreline_counts.items()}
    actual_score = f"{match.score.home}-{match.score.away}"
    actual_total = match.score.home + match.score.away
    scoreline_probability = one_x_two_from_matrix(matrix)
    market_retrieved_at = None
    market_time_quality = "unavailable_stage_timestamp"
    market_time_basis = "unavailable_stage_timestamp"
    stage_market_note = "历史源没有该冻结时点的市场观察时间，市场字段保持不可用。"
    probability_delta_model_minus_market = None
    if market_probability is not None and market_observed_at is not None:
        retrieved_at, market_time_quality, market_time_basis = _market_retrieved_at(
            market_feature,
            fallback=market_observed_at,
        )
        market_retrieved_at = retrieved_at.isoformat()
        market_time_bound_at = market_observed_at.isoformat()
        probability_delta_model_minus_market = {
            label: scoreline_probability[label] - market_probability[label]
            for label in ("home", "draw", "away")
        }
        stage_market_note = "市场基线在冻结截止前观测，并已按固定权重进入比分矩阵。"
    else:
        market_time_bound_at = None
    lineup_confirmation_fingerprint = (
        _lineup_confirmation_fingerprint(feature_snapshot)
        if stage == "lineup_confirmation"
        else None
    )
    player_availability_shadow = build_player_availability_shadow_from_feature_snapshot(
        feature_snapshot,
        as_of=cutoff_at.isoformat(),
        kickoff_at=match.kickoff_at.isoformat(),
    )
    return {
        "fixture_id": match.id,
        "competition_id": match.competition_id,
        "season": match.season,
        "home_team": match.home_team,
        "away_team": match.away_team,
        "home_team_id": match.home_team_id,
        "away_team_id": match.away_team_id,
        "source_name": match.source_name,
        "source_license_status": match.source_license_status,
        "source_file": match.source_file,
        "source_sha256": match.source_sha256,
        "provider_fixture_id": match.provider_fixture_id,
        "kickoff_at": match.kickoff_at.isoformat(),
        "kickoff_time_quality": match.kickoff_time_quality,
        "kickoff_time_source": match.kickoff_time_source,
        "kickoff_time_observed_at": match.kickoff_time_observed_at,
        "cutoff_at": cutoff_at.isoformat(),
        "freeze_stage": stage,
        "model_version": model_version,
        "model_training_cutoff": prior[-1].kickoff_at.isoformat() if prior else None,
        "feature_times": live_feature_times,
        "live_feature_fields": live_feature_fields,
        "live_feature_sources": live_feature_sources,
        "factor_trace": factor_trace,
        "live_xg_blend_weight": PROSPECTIVE_XG_BLEND_WEIGHT if "recent_xg" in live_feature_fields else 0.0,
        "live_market_blend_weight": PROSPECTIVE_MARKET_BLEND_WEIGHT if "market_1x2" in live_feature_fields else 0.0,
        "market_retrieved_at": market_retrieved_at,
        "market_time_bound_at": market_time_bound_at,
        "market_time_quality": market_time_quality,
        "market_time_basis": market_time_basis,
        "market_probability": market_probability,
        "probability_delta_model_minus_market": probability_delta_model_minus_market,
        "elo_probability": {"home": elo_probability[0], "draw": elo_probability[1], "away": elo_probability[2]},
        "frequency_probability": {"home": frequency[0], "draw": frequency[1], "away": frequency[2]},
        "outcome_1x2": _label(match),
        "expected_goals": {"home": home_rate, "away": away_rate},
        "scoreline_rho": stage_rho,
        "scoreline_rho_training_n": len(previous_scoreline_rates),
        "scoreline_rho_training_cutoff": (
            previous_scoreline_rates[-1][0].kickoff_at.isoformat()
            if previous_scoreline_rates
            else None
        ),
        "scoreline_top5": top_scorelines(matrix),
        "scoreline_matrix": matrix,
        "scoreline_probability": scoreline_probability,
        "scoreline_frequency_probability": scoreline_frequency,
        "scoreline_frequency_top5": top_scorelines(scoreline_frequency),
        "total_goals_probability": total_goals_distribution(matrix),
        "total_frequency_probability": total_frequency,
        "total_over_under_line": 2.5,
        "total_over_under_probability": total_over_under_distribution(matrix, 2.5),
        "total_frequency_ou_probability": total_frequency_ou,
        "total_market_probability": None,
        "half_full_probability": half_full_distribution(home_rate, away_rate),
        "half_full_frequency_probability": half_full_frequency,
        "handicap_line": handicap_line,
        "handicap_probability": (
            handicap_distribution(matrix, handicap_line)
            if handicap_line is not None
            else None
        ),
        "handicap_frequency_probability": None,
        "handicap_market_probability": handicap_market_probability,
        "actual_handicap": None,
        "actual_total_ou": "over" if actual_total > 2.5 else "under" if actual_total < 2.5 else "push",
        "actual_score": actual_score,
        "actual_total_goals": str(actual_total) if actual_total < 5 else "5+",
        "actual_total_goals_numeric": actual_total,
        "actual_half_full": _half_full_actual(match),
        "stage_market_note": stage_market_note,
        "lineup_confirmation_fingerprint": lineup_confirmation_fingerprint,
        "player_availability_shadow": player_availability_shadow,
    }


def evaluate_freeze_stage_walk_forward(
    matches: list[Match],
    *,
    warmup_seasons: int = 2,
    elo_k: float = 20.0,
    rate_learning_rate: float = 0.015,
    calibration_window: int = 1000,
    calibration_refresh: int = 50,
    scoreline_temperature: float = 1.0,
) -> dict[str, object]:
    """Replay the three pre-kickoff freezes on a causal event timeline.

    Prediction events are processed before any result at the same timestamp;
    result updates are then applied in a same-kickoff batch.  The lineup
    confirmation stage is intentionally reported as unavailable because the
    reviewed historical sources do not carry an official-lineup observation
    timestamp.  This is stricter than copying the final pre-kickoff row into
    earlier slots.
    """

    finished = sorted(
        (match for match in matches if match.score is not None),
        key=lambda item: (item.kickoff_at, item.id),
    )
    seasons = sorted({match.season for match in finished})
    if len(seasons) <= warmup_seasons:
        return {
            "status": "not_evaluated",
            "stages": {
                stage: {"status": "not_evaluated", "sample_n": 0}
                for stage in (*_FREEZE_STAGE_OFFSETS, "lineup_confirmation")
            },
        }
    held_out_seasons = set(seasons[warmup_seasons:])
    targets_by_cutoff: dict[datetime, list[tuple[Match, str]]] = defaultdict(list)
    for match in finished:
        if match.season not in held_out_seasons:
            continue
        for stage, offset in _FREEZE_STAGE_OFFSETS.items():
            targets_by_cutoff[match.kickoff_at - offset].append((match, stage))
    date_only_dates = {
        match.kickoff_at.date()
        for match in finished
        if match.kickoff_time_quality == "date_only"
    }
    kickoffs_by_time: dict[datetime, list[Match]] = defaultdict(list)
    for match in finished:
        result_time = _result_event_time(match).astimezone(match.kickoff_at.tzinfo)
        if match.kickoff_at.date() in date_only_dates:
            # At least one match on this date lacks a kickoff time.  Delay all
            # results for the affected date until the next UTC date boundary;
            # this prevents an unknown-time result from entering a later
            # same-day pre-match freeze.
            result_time = max(
                result_time,
                datetime.combine(
                match.kickoff_at.date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=match.kickoff_at.tzinfo,
                ),
            )
        kickoffs_by_time[result_time].append(match)

    ratings: dict[str, float] = defaultdict(lambda: 1500.0)
    attack: dict[str, float] = defaultdict(float)
    defence: dict[str, float] = defaultdict(float)
    prior: list[Match] = []
    previous_rows: list[tuple[Match, tuple[float, float, float], tuple[float, float, float]]] = []
    previous_scoreline_rates: list[tuple[Match, float, float, tuple[float, float, float]]] = []
    outcome_counts = {"home": 0, "draw": 0, "away": 0}
    total_home_goals = total_away_goals = 0
    total_counts = {str(index): 1.0 for index in range(5)} | {"5+": 1.0}
    total_ou_counts = {"over": 1.0, "under": 1.0}
    half_full_counts = {f"{half}/{full}": 1.0 for half in "HDA" for full in "HDA"}
    scoreline_counts = {f"{home}-{away}": 1.0 for home in range(9) for away in range(9)}
    scoreline_rho = 0.0
    last_rho_calibration_at = 0
    rho_calibration_refresh = 250
    rows_by_stage: dict[str, list[dict]] = {stage: [] for stage in _FREEZE_STAGE_OFFSETS}
    event_times = sorted(set(targets_by_cutoff) | set(kickoffs_by_time))
    for event_time in event_times:
        if len(previous_scoreline_rates) >= 100 and (
            len(previous_scoreline_rates) - last_rho_calibration_at >= rho_calibration_refresh
        ):
            scoreline_rho = _select_scoreline_rho(
                previous_scoreline_rates,
                window=1000,
                objective="scoreline",
            )
            last_rho_calibration_at = len(previous_scoreline_rates)
        for match, stage in targets_by_cutoff.get(event_time, []):
            rows_by_stage[stage].append(
                _stage_prediction_row(
                    match,
                    stage=stage,
                    cutoff_at=event_time,
                    ratings=ratings,
                    attack=attack,
                    defence=defence,
                    prior=prior,
                    previous_rows=previous_rows,
                    previous_scoreline_rates=previous_scoreline_rates,
                    outcome_counts=outcome_counts,
                    total_home_goals=total_home_goals,
                    total_away_goals=total_away_goals,
                    total_counts=total_counts,
                    total_ou_counts=total_ou_counts,
                    half_full_counts=half_full_counts,
                    scoreline_counts=scoreline_counts,
                    calibration_window=calibration_window,
                    calibration_refresh=calibration_refresh,
                    last_calibration_at=0,
                    last_rho_calibration_at=last_rho_calibration_at,
                    scoreline_rho=scoreline_rho,
                    rho_calibration_refresh=rho_calibration_refresh,
                    alpha=0.0,
                    calibration_prior=(0.46, 0.25, 0.29),
                    elo_k=elo_k,
                    rate_learning_rate=rate_learning_rate,
                    scoreline_temperature=scoreline_temperature,
                )
            )
        batch = kickoffs_by_time.get(event_time, [])
        pending_updates = []
        pending_scoreline_rates = []
        draw_rate = min(0.35, max(0.15, outcome_counts["draw"] / len(prior))) if len(prior) >= 20 else 0.25
        advantage = 400 * math.log10((outcome_counts["home"] + 1) / (outcome_counts["away"] + 1)) if len(prior) >= 20 else 40.0
        advantage = min(150.0, max(-100.0, advantage))
        home_average = total_home_goals / len(prior) if prior else 1.45
        away_average = total_away_goals / len(prior) if prior else 1.15
        for match in batch:
            raw_elo = _elo_probability(ratings[match.home_team_id], ratings[match.away_team_id], draw_rate, advantage)
            home_rate = min(4.5, max(0.15, home_average * math.exp(attack[match.home_team_id] - defence[match.away_team_id])))
            away_rate = min(4.5, max(0.15, away_average * math.exp(attack[match.away_team_id] - defence[match.home_team_id])))
            pending_updates.append((match, raw_elo, home_rate, away_rate))
            pending_scoreline_rates.append((match, home_rate, away_rate, _outcome_tuple(match)))
        for match, raw_elo, home_rate, away_rate in pending_updates:
            actual = _outcome_tuple(match)
            expected_home = raw_elo[0] / max(1e-9, 1 - draw_rate) if draw_rate < 1 else 0.5
            ratings[match.home_team_id] += elo_k * ((actual[0] + 0.5 * actual[1]) - expected_home)
            ratings[match.away_team_id] -= elo_k * ((actual[0] + 0.5 * actual[1]) - expected_home)
            attack[match.home_team_id] += rate_learning_rate * (match.score.home - home_rate)
            defence[match.away_team_id] -= rate_learning_rate * (match.score.home - home_rate)
            attack[match.away_team_id] += rate_learning_rate * (match.score.away - away_rate)
            defence[match.home_team_id] -= rate_learning_rate * (match.score.away - away_rate)
            prior.append(match)
            outcome_counts[_label(match)] += 1
            total_home_goals += match.score.home
            total_away_goals += match.score.away
            total = match.score.home + match.score.away
            total_counts[str(total) if total < 5 else "5+"] += 1
            if total > 2.5:
                total_ou_counts["over"] += 1
            elif total < 2.5:
                total_ou_counts["under"] += 1
            else:
                total_ou_counts["over"] += 0.5
                total_ou_counts["under"] += 0.5
            half_full = _half_full_actual(match)
            if half_full is not None:
                half_full_counts[half_full] += 1
            actual_score = f"{match.score.home}-{match.score.away}"
            if actual_score in scoreline_counts:
                scoreline_counts[actual_score] += 1
        previous_rows.extend((match, raw_elo, _outcome_tuple(match)) for match, raw_elo, _, _ in pending_updates)
        previous_scoreline_rates.extend(pending_scoreline_rates)

    stages: dict[str, dict[str, object]] = {}
    for stage, rows in rows_by_stage.items():
        time_error = None
        try:
            assert_no_future_leakage(rows)
            assert_probability_contract(rows)
        except ValueError as exc:
            time_error = str(exc)
        stages[stage] = {
            "status": "evaluated" if rows else "not_evaluated",
            "sample_n": len(rows),
            "model": "strict_dynamic_elo_scoreline_stage_replay_v3_causal_rho",
            "cutoff_policy": f"{stage} before kickoff; prediction events precede same-time result updates",
            "three_way": _three_way_metrics(rows) if rows else {"sample_n": 0},
            "targets": _scoreline_metrics(rows) if rows else {"sample_n": 0},
            "time_audit": {
                "status": "pass" if time_error is None else "blocked",
                "future_leakage_violations": 0 if time_error is None else 1,
                "market_bound_violations": 0,
                "market_sample_n": 0,
                "market_status": "unavailable_stage_timestamp",
                "error": time_error,
            },
        }
    stages["lineup_confirmation"] = {
        "status": "unavailable",
        "sample_n": 0,
        "model": "strict_dynamic_elo_scoreline_stage_replay_v3_causal_rho",
        "reason": "历史数据源没有官方首发的 observed_at/effective_at，不能回放首发确认冻结。",
        "time_audit": {"status": "blocked", "reason": "missing_lineup_observation_timestamps"},
    }
    return {
        "status": "evaluated",
        "warmup_seasons": seasons[:warmup_seasons],
        "held_out_seasons": sorted(held_out_seasons),
        "stages": stages,
    }


def build_strict_prospective_predictions(
    history: list[Match],
    upcoming: list[Match],
    *,
    as_of: datetime,
    warmup_seasons: int = 2,
    elo_k: float = 20.0,
    rate_learning_rate: float = 0.015,
    scoreline_temperature: float = 1.0,
    lineup_observed_at: dict[str, datetime] | None = None,
    feature_snapshots: Mapping[str, Mapping[str, Any]] | None = None,
    evaluation_window_started_at: datetime | None = None,
) -> dict[str, object]:
    """Build causal strict-model freezes for future fixtures.

    This is the forward counterpart of :func:`evaluate_freeze_stage_walk_forward`.
    Historical matches update the state only at their result event; a future
    fixture is scored at a freeze cutoff before that fixture's kickoff.  The
    target is passed through the existing stage renderer with a private dummy
    score and every result field is removed before returning, so no fabricated
    outcome can enter an archived prediction.

    ``lineup_observed_at`` is an explicit, independently observed timestamp
    keyed by fixture id.  A predicted or undated lineup cannot unlock the
    lineup-confirmation stage.
    """

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if evaluation_window_started_at is not None and (
        evaluation_window_started_at.tzinfo is None
        or evaluation_window_started_at.utcoffset() is None
    ):
        raise ValueError("evaluation_window_started_at must be timezone-aware")
    if not math.isfinite(scoreline_temperature) or scoreline_temperature <= 0:
        raise ValueError("scoreline_temperature must be positive and finite")
    reference = as_of.astimezone(timezone.utc)
    evaluation_window_start = (
        evaluation_window_started_at.astimezone(reference.tzinfo)
        if evaluation_window_started_at is not None
        else None
    )
    active_competitions = {
        match.competition_id
        for match in upcoming
        if match.score is None and match.kickoff_at.astimezone(timezone.utc) > reference
    }
    if not active_competitions:
        return {
            "status": "evaluated",
            "as_of": reference.isoformat(),
            "predictions": [],
            "blocked": [{"reason": "no_upcoming_fixture_after_as_of"}],
        }
    if len(active_competitions) > 1:
        combined_predictions: list[dict] = []
        combined_blocked: list[dict] = []
        for competition_id in sorted(active_competitions):
            result = build_strict_prospective_predictions(
                [match for match in history if match.competition_id == competition_id],
                [match for match in upcoming if match.competition_id == competition_id],
                as_of=reference,
                warmup_seasons=warmup_seasons,
                elo_k=elo_k,
                rate_learning_rate=rate_learning_rate,
                scoreline_temperature=scoreline_temperature,
                lineup_observed_at={
                    fixture_id: observed
                    for fixture_id, observed in (lineup_observed_at or {}).items()
                    if any(match.id == fixture_id for match in upcoming if match.competition_id == competition_id)
                },
                feature_snapshots={
                    fixture_id: snapshot
                    for fixture_id, snapshot in (feature_snapshots or {}).items()
                    if any(match.id == fixture_id for match in upcoming if match.competition_id == competition_id)
                },
                evaluation_window_started_at=evaluation_window_start,
            )
            combined_predictions.extend(result.get("predictions", []))
            for blocked in result.get("blocked", []):
                if isinstance(blocked, dict):
                    blocked_with_competition = dict(blocked)
                    blocked_with_competition.setdefault("competition_id", competition_id)
                    combined_blocked.append(blocked_with_competition)
                else:
                    combined_blocked.append(
                        {"reason": str(blocked), "competition_id": competition_id}
                    )
        combined_predictions.sort(
            key=lambda row: (row["kickoff_at"], row["fixture_id"], row["freeze_cutoff_at"])
        )
        return {
            "status": "evaluated",
            "as_of": reference.isoformat(),
            "model": PROSPECTIVE_FREEZE_MODEL_VERSION,
            "predictions": combined_predictions,
            "blocked": combined_blocked,
        }
    only_competition = next(iter(active_competitions))
    history = [match for match in history if match.competition_id == only_competition]
    upcoming = [match for match in upcoming if match.competition_id == only_competition]
    finished = sorted(
        (
            match
            for match in history
            if match.score is not None
            and match.kickoff_at.astimezone(as_of.tzinfo) < reference
            and _result_event_time(match) <= reference
        ),
        key=lambda item: (item.kickoff_at, item.id),
    )
    seasons = sorted({match.season for match in finished})
    if len(seasons) <= warmup_seasons:
        return {
            "status": "not_evaluated",
            "as_of": reference.isoformat(),
            "predictions": [],
            "blocked": [{
                "reason": "not_enough_historical_seasons",
                "competition_id": only_competition,
            }],
        }

    targets: list[tuple[Match, str, datetime, datetime, str]] = []
    blocked: list[dict[str, object]] = []
    offsets = {
        "t_minus_24h": timedelta(hours=24),
        "t_minus_6h": timedelta(hours=6),
        "t_minus_90m": timedelta(minutes=90),
    }
    lineup_times = lineup_observed_at or {}
    live_features = feature_snapshots or {}
    for match in upcoming:
        if match.score is not None or match.kickoff_at <= reference:
            continue
        kickoff = match.kickoff_at.astimezone(reference.tzinfo)
        fixture_observed_at = _fixture_observation_time(match)
        fixture_archive = live_features.get(match.id)
        archived_observations = (
            fixture_archive.get("fixture_observed_at_by_stage")
            if isinstance(fixture_archive, Mapping)
            else None
        )
        archived_schedules = (
            fixture_archive.get("fixture_by_stage")
            if isinstance(fixture_archive, Mapping)
            else None
        )

        def archived_stage_match(stage: str) -> tuple[Match, datetime] | None:
            schedule = (
                archived_schedules.get(stage)
                if isinstance(archived_schedules, Mapping)
                else None
            )
            if not isinstance(schedule, Mapping):
                return None
            archived_kickoff = _observation_time(schedule.get("kickoff_at"))
            archived_observed = _observation_time(
                schedule.get("kickoff_time_observed_at")
            )
            if (
                archived_kickoff is None
                or archived_observed is None
                or archived_kickoff.astimezone(reference.tzinfo) != kickoff
            ):
                return None
            return (
                replace(
                    match,
                    kickoff_at=archived_kickoff.astimezone(match.kickoff_at.tzinfo),
                    kickoff_time_quality=str(
                        schedule.get("kickoff_time_quality")
                        or match.kickoff_time_quality
                    ),
                    kickoff_time_source=str(
                        schedule.get("kickoff_time_source")
                        or match.kickoff_time_source
                    ),
                    kickoff_time_observed_at=archived_observed.isoformat(),
                ),
                archived_observed,
            )

        def eligible_observation(
            stage: str,
            cutoff: datetime,
        ) -> tuple[datetime, str, Match] | None:
            candidates: list[tuple[datetime, str, Match]] = []
            if fixture_observed_at is not None and fixture_observed_at <= reference:
                candidates.append(
                    (fixture_observed_at, "current_fixture_source", match)
                )
            archived = (
                _observation_time(archived_observations.get(stage))
                if isinstance(archived_observations, Mapping)
                else None
            )
            archived_match = archived_stage_match(stage)
            if archived is not None and archived <= reference and archived_match is not None:
                stage_match, field_observed = archived_match
                candidates.append(
                    (
                        max(archived, field_observed),
                        "archived_fixture_snapshot",
                        stage_match,
                    )
                )
            causal = [candidate for candidate in candidates if candidate[0] <= cutoff]
            return min(causal, key=lambda candidate: candidate[0]) if causal else None

        def block_stage(
            stage: str,
            cutoff: datetime,
            reason: str,
            **extra: object,
        ) -> None:
            blocked.append({
                "reason": reason,
                "fixture_id": match.id,
                "competition_id": match.competition_id,
                "freeze_stage": stage,
                "freeze_cutoff_at": cutoff.isoformat(),
                "kickoff_at": kickoff.isoformat(),
                "kickoff_time_observed_at": match.kickoff_time_observed_at,
                **extra,
            })

        def archived_kickoff(stage: str) -> datetime | None:
            schedule = (
                archived_schedules.get(stage)
                if isinstance(archived_schedules, Mapping)
                else None
            )
            return (
                _observation_time(schedule.get("kickoff_at"))
                if isinstance(schedule, Mapping)
                else None
            )

        for stage, offset in offsets.items():
            cutoff = kickoff - offset
            if cutoff <= reference and cutoff < kickoff:
                if evaluation_window_start is not None and cutoff < evaluation_window_start:
                    block_stage(
                        stage,
                        cutoff,
                        "freeze_cutoff_before_evaluation_window",
                        evaluation_window_started_at=evaluation_window_start.isoformat(),
                    )
                    continue
                observation = eligible_observation(stage, cutoff)
                if observation is not None:
                    targets.append(
                        (
                            observation[2],
                            stage,
                            cutoff,
                            observation[0],
                            observation[1],
                        )
                    )
                elif (
                    (archived_value := archived_kickoff(stage)) is not None
                    and archived_value.astimezone(reference.tzinfo) != kickoff
                ):
                    block_stage(
                        stage,
                        cutoff,
                        "fixture_kickoff_changed_after_freeze_cutoff",
                        archived_kickoff_at=archived_value.isoformat(),
                        current_kickoff_at=kickoff.isoformat(),
                    )
                elif fixture_observed_at is None:
                    block_stage(stage, cutoff, "fixture_observation_time_unavailable")
                elif fixture_observed_at > reference:
                    block_stage(stage, cutoff, "fixture_observation_after_as_of")
                else:
                    block_stage(stage, cutoff, "fixture_first_observed_after_freeze_cutoff")
        observed = lineup_times.get(match.id)
        if observed is not None:
            observed = observed.astimezone(reference.tzinfo)
            if observed <= reference and observed < kickoff:
                if evaluation_window_start is not None and observed < evaluation_window_start:
                    block_stage(
                        "lineup_confirmation",
                        observed,
                        "freeze_cutoff_before_evaluation_window",
                        evaluation_window_started_at=evaluation_window_start.isoformat(),
                    )
                    continue
                lineup_features = fixture_archive
                if isinstance(fixture_archive, Mapping):
                    archived_by_stage = fixture_archive.get("by_stage")
                    if isinstance(archived_by_stage, Mapping):
                        lineup_features = archived_by_stage.get("lineup_confirmation")
                if _lineup_confirmation_fingerprint(lineup_features) is None:
                    block_stage(
                        "lineup_confirmation",
                        observed,
                        "lineup_confirmation_evidence_unavailable",
                    )
                    continue
                observation = eligible_observation("lineup_confirmation", observed)
                if observation is not None:
                    targets.append(
                        (
                            observation[2],
                            "lineup_confirmation",
                            observed,
                            observation[0],
                            observation[1],
                        )
                    )
                elif fixture_observed_at is None:
                    block_stage(
                        "lineup_confirmation",
                        observed,
                        "fixture_observation_time_unavailable",
                    )
                elif fixture_observed_at > reference:
                    block_stage(
                        "lineup_confirmation",
                        observed,
                        "fixture_observation_after_as_of",
                    )
                else:
                    block_stage(
                        "lineup_confirmation",
                        observed,
                        "fixture_first_observed_after_freeze_cutoff",
                    )

    if not targets:
        if blocked:
            return {
                "status": "evaluated",
                "as_of": reference.isoformat(),
                "predictions": [],
                "blocked": blocked,
            }
        return {
            "status": "evaluated",
            "as_of": reference.isoformat(),
            "predictions": [],
            "blocked": [{
                "reason": "no_freeze_cutoff_observed_before_as_of",
                "competition_id": only_competition,
            }],
        }

    date_only_dates = {
        match.kickoff_at.date()
        for match in finished
        if match.kickoff_time_quality == "date_only"
    }

    def result_time(match: Match) -> datetime:
        kickoff = match.kickoff_at
        event_time = _result_event_time(match)
        if kickoff.date() in date_only_dates:
            event_time = max(
                event_time,
                datetime.combine(
                    kickoff.date() + timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=kickoff.tzinfo,
                ),
            )
        return event_time.astimezone(reference.tzinfo)

    results_by_time: dict[datetime, list[Match]] = defaultdict(list)
    for match in finished:
        results_by_time[result_time(match)].append(match)
    targets_by_time: dict[
        datetime,
        list[tuple[Match, str, datetime, datetime, str]],
    ] = defaultdict(list)
    for target in targets:
        targets_by_time[target[2].astimezone(reference.tzinfo)].append(target)

    ratings: dict[str, float] = defaultdict(lambda: 1500.0)
    attack: dict[str, float] = defaultdict(float)
    defence: dict[str, float] = defaultdict(float)
    prior: list[Match] = []
    previous_rows: list[tuple[Match, tuple[float, float, float], tuple[float, float, float]]] = []
    previous_scoreline_rates: list[tuple[Match, float, float, tuple[float, float, float]]] = []
    outcome_counts = {"home": 0, "draw": 0, "away": 0}
    total_home_goals = 0
    total_away_goals = 0
    total_counts = {str(index): 1.0 for index in range(5)} | {"5+": 1.0}
    total_ou_counts = {"over": 1.0, "under": 1.0}
    half_full_counts = {f"{half}/{full}": 1.0 for half in "HDA" for full in "HDA"}
    scoreline_counts = {f"{home}-{away}": 1.0 for home in range(9) for away in range(9)}
    scoreline_rho = 0.0
    last_rho_calibration_at = 0
    rho_calibration_refresh = 250
    predictions: list[dict] = []
    event_times = sorted(set(results_by_time) | set(targets_by_time))

    for event_time in event_times:
        if len(previous_scoreline_rates) >= 100 and (
            len(previous_scoreline_rates) - last_rho_calibration_at >= rho_calibration_refresh
        ):
            scoreline_rho = _select_scoreline_rho(
                previous_scoreline_rates,
                window=1000,
                objective="scoreline",
            )
            last_rho_calibration_at = len(previous_scoreline_rates)
        for match, stage, cutoff, fixture_freeze_observed_at, fixture_observation_basis in sorted(
            targets_by_time.get(event_time, []), key=lambda item: (item[0].id, item[1])
        ):
            # _stage_prediction_row only reads score to populate actual-result
            # fields.  The dummy is never added to any state update, and those
            # fields are deleted immediately below.
            render_match = replace(match, score=Score(home=0, away=0))
            stage_features = live_features.get(match.id)
            if isinstance(stage_features, Mapping):
                archived_by_stage = stage_features.get("by_stage")
                if isinstance(archived_by_stage, Mapping):
                    # Capture selects the newest complete source snapshot at
                    # each cutoff.  A missing stage deliberately falls back to
                    # no live features instead of borrowing a later snapshot.
                    stage_features = archived_by_stage.get(stage)
            row = _stage_prediction_row(
                render_match,
                stage=stage,
                cutoff_at=cutoff,
                ratings=ratings,
                attack=attack,
                defence=defence,
                prior=prior,
                previous_rows=previous_rows,
                previous_scoreline_rates=previous_scoreline_rates,
                outcome_counts=outcome_counts,
                total_home_goals=total_home_goals,
                total_away_goals=total_away_goals,
                total_counts=total_counts,
                total_ou_counts=total_ou_counts,
                half_full_counts=half_full_counts,
                scoreline_counts=scoreline_counts,
                calibration_window=1000,
                calibration_refresh=50,
                last_calibration_at=0,
                last_rho_calibration_at=last_rho_calibration_at,
                scoreline_rho=scoreline_rho,
                rho_calibration_refresh=rho_calibration_refresh,
                alpha=0.0,
                calibration_prior=(0.46, 0.25, 0.29),
                elo_k=elo_k,
                rate_learning_rate=rate_learning_rate,
                scoreline_temperature=scoreline_temperature,
                feature_snapshot=stage_features,
                model_version=PROSPECTIVE_FREEZE_MODEL_VERSION,
            )
            for key in (
                "outcome_1x2",
                "actual_score",
                "actual_total_goals",
                "actual_total_goals_numeric",
                "actual_half_full",
                "actual_total_ou",
                "actual_handicap",
            ):
                row.pop(key, None)
            row["as_of"] = reference.isoformat()
            row["prediction_observed_at"] = reference.isoformat()
            row["freeze_cutoff_at"] = cutoff.isoformat()
            row["fixture_freeze_observed_at"] = fixture_freeze_observed_at.isoformat()
            row["fixture_observation_basis"] = fixture_observation_basis
            if stage == "lineup_confirmation":
                row["lineup_observed_at"] = cutoff.isoformat()
            predictions.append(row)

        batch = results_by_time.get(event_time, [])
        pending_updates = []
        pending_scoreline_rates = []
        draw_rate = (
            min(0.35, max(0.15, outcome_counts["draw"] / len(prior)))
            if len(prior) >= 20
            else 0.25
        )
        advantage = (
            400 * math.log10((outcome_counts["home"] + 1) / (outcome_counts["away"] + 1))
            if len(prior) >= 20
            else 40.0
        )
        advantage = min(150.0, max(-100.0, advantage))
        home_average = total_home_goals / len(prior) if prior else 1.45
        away_average = total_away_goals / len(prior) if prior else 1.15
        for match in batch:
            raw_elo = _elo_probability(
                ratings[match.home_team_id], ratings[match.away_team_id], draw_rate, advantage
            )
            home_rate = min(
                4.5,
                max(0.15, home_average * math.exp(attack[match.home_team_id] - defence[match.away_team_id])),
            )
            away_rate = min(
                4.5,
                max(0.15, away_average * math.exp(attack[match.away_team_id] - defence[match.home_team_id])),
            )
            pending_updates.append((match, raw_elo, home_rate, away_rate))
            pending_scoreline_rates.append((match, home_rate, away_rate, _outcome_tuple(match)))
        for match, raw_elo, home_rate, away_rate in pending_updates:
            actual = _outcome_tuple(match)
            expected_home = raw_elo[0] / max(1e-9, 1 - draw_rate) if draw_rate < 1 else 0.5
            ratings[match.home_team_id] += elo_k * ((actual[0] + 0.5 * actual[1]) - expected_home)
            ratings[match.away_team_id] -= elo_k * ((actual[0] + 0.5 * actual[1]) - expected_home)
            attack[match.home_team_id] += rate_learning_rate * (match.score.home - home_rate)
            defence[match.away_team_id] -= rate_learning_rate * (match.score.home - home_rate)
            attack[match.away_team_id] += rate_learning_rate * (match.score.away - away_rate)
            defence[match.home_team_id] -= rate_learning_rate * (match.score.away - away_rate)
            prior.append(match)
            outcome_counts[_label(match)] += 1
            total_home_goals += match.score.home
            total_away_goals += match.score.away
            total = match.score.home + match.score.away
            total_counts[str(total) if total < 5 else "5+"] += 1
            if total > 2.5:
                total_ou_counts["over"] += 1
            elif total < 2.5:
                total_ou_counts["under"] += 1
            else:
                total_ou_counts["over"] += 0.5
                total_ou_counts["under"] += 0.5
            half_full = _half_full_actual(match)
            if half_full is not None:
                half_full_counts[half_full] += 1
            actual_score = f"{match.score.home}-{match.score.away}"
            if actual_score in scoreline_counts:
                scoreline_counts[actual_score] += 1
        previous_rows.extend(
            (match, raw_elo, _outcome_tuple(match))
            for match, raw_elo, _, _ in pending_updates
        )
        previous_scoreline_rates.extend(pending_scoreline_rates)

    predictions.sort(key=lambda row: (row["kickoff_at"], row["fixture_id"], row["freeze_cutoff_at"]))
    return {
        "status": "evaluated",
        "as_of": reference.isoformat(),
        "model": PROSPECTIVE_FREEZE_MODEL_VERSION,
        "predictions": predictions,
        "blocked": blocked,
    }


def _outcome_tuple(match: Match) -> tuple[float, float, float]:
    label = _label(match)
    return (float(label == "home"), float(label == "draw"), float(label == "away"))


def _calibrate_previous(
    rows: list[tuple[Match, tuple[float, float, float], tuple[float, float, float]]],
    *,
    window: int = 1000,
) -> tuple[float, tuple[float, float, float]]:
    if window < 100:
        raise ValueError("calibration_window must be at least 100")
    if len(rows) < 100:
        return 0.0, _prior_probability([row[0] for row in rows])
    from league_platform.model import _calibrate

    return _calibrate(rows[-window:])


__all__ = [
    "build_strict_prospective_predictions",
    "evaluate_freeze_stage_walk_forward",
    "evaluate_strict_walk_forward",
]
