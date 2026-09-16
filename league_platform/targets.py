"""Unified football target distributions and official-style settlements."""

from __future__ import annotations

import math
from collections import defaultdict


def normalize_probabilities(values: dict[str, float]) -> dict[str, float]:
    clean = {key: max(0.0, float(value)) for key, value in values.items()}
    total = sum(clean.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("probabilities must have a positive finite total")
    return {key: value / total for key, value in clean.items()}


def _poisson(rate: float, maximum: int) -> list[float]:
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("goal rate must be finite and non-negative")
    values = [math.exp(-rate)]
    for goals in range(1, maximum + 1):
        values.append(values[-1] * rate / goals)
    return values


def scoreline_matrix(
    home_rate: float,
    away_rate: float,
    *,
    rho: float = 0.0,
    maximum_goals: int = 8,
) -> dict[str, float]:
    """Return a normalized 0..maximum score matrix with Dixon-Coles correction."""

    if maximum_goals < 2:
        raise ValueError("maximum_goals must be at least 2")
    home_mass = _poisson(home_rate, maximum_goals)
    away_mass = _poisson(away_rate, maximum_goals)
    matrix: dict[str, float] = {}
    for home_goals, home_probability in enumerate(home_mass):
        for away_goals, away_probability in enumerate(away_mass):
            adjustment = 1.0
            if home_goals == 0 and away_goals == 0:
                adjustment = 1 - home_rate * away_rate * rho
            elif home_goals == 0 and away_goals == 1:
                adjustment = 1 + home_rate * rho
            elif home_goals == 1 and away_goals == 0:
                adjustment = 1 + away_rate * rho
            elif home_goals == 1 and away_goals == 1:
                adjustment = 1 - rho
            matrix[f"{home_goals}-{away_goals}"] = max(
                0.0, home_probability * away_probability * adjustment
            )
    total = sum(matrix.values())
    if total <= 0:
        raise ValueError("scoreline matrix has no probability mass")
    return {key: value / total for key, value in matrix.items()}


def top_scorelines(matrix: dict[str, float], limit: int = 5) -> list[dict[str, float | str]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    rows = sorted(matrix.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{"score": score, "probability": round(probability, 8)} for score, probability in rows]


def total_goals_distribution(matrix: dict[str, float]) -> dict[str, float]:
    values: dict[str, float] = defaultdict(float)
    for score, probability in matrix.items():
        home, away = (int(value) for value in score.split("-", 1))
        total = home + away
        values[str(total) if total < 5 else "5+"] += probability
    return normalize_probabilities(dict(values))


def handicap_distribution(matrix: dict[str, float], line: float) -> dict[str, float]:
    """Aggregate scoreline mass by official Asian handicap settlement."""

    values: dict[str, float] = defaultdict(float)
    for score, probability in matrix.items():
        home, away = (int(value) for value in score.split("-", 1))
        values[settle_handicap(home, away, line)] += probability
    return normalize_probabilities(dict(values))


def total_over_under_distribution(matrix: dict[str, float], line: float = 2.5) -> dict[str, float]:
    """Aggregate scoreline mass by over/under settlement for a total line."""

    values = {"over": 0.0, "under": 0.0}
    for score, probability in matrix.items():
        home, away = (int(value) for value in score.split("-", 1))
        total = home + away
        over = settle_total(total, line, over=True)
        under = settle_total(total, line, over=False)
        if over == "win":
            values["over"] += probability
        elif under == "win":
            values["under"] += probability
        else:
            values["over"] += probability / 2
            values["under"] += probability / 2
    return normalize_probabilities(dict(values))


def one_x_two_from_matrix(matrix: dict[str, float]) -> dict[str, float]:
    values = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for score, probability in matrix.items():
        home, away = (int(value) for value in score.split("-", 1))
        result = "home" if home > away else "draw" if home == away else "away"
        values[result] += probability
    return normalize_probabilities(values)


def half_full_distribution(
    home_rate: float,
    away_rate: float,
    *,
    first_half_share: float = 0.46,
    maximum_goals: int = 6,
) -> dict[str, float]:
    """Approximate joint half-time/full-time distribution from independent periods."""

    if not 0 < first_half_share < 1:
        raise ValueError("first_half_share must be between zero and one")
    first_home = _poisson(home_rate * first_half_share, maximum_goals)
    first_away = _poisson(away_rate * first_half_share, maximum_goals)
    second_home = _poisson(home_rate * (1 - first_half_share), maximum_goals)
    second_away = _poisson(away_rate * (1 - first_half_share), maximum_goals)
    values: dict[str, float] = defaultdict(float)
    for h1, p_h1 in enumerate(first_home):
        for a1, p_a1 in enumerate(first_away):
            half = "H" if h1 > a1 else "D" if h1 == a1 else "A"
            for h2, p_h2 in enumerate(second_home):
                for a2, p_a2 in enumerate(second_away):
                    full_home, full_away = h1 + h2, a1 + a2
                    full = "H" if full_home > full_away else "D" if full_home == full_away else "A"
                    values[f"{half}/{full}"] += p_h1 * p_a1 * p_h2 * p_a2
    return normalize_probabilities(dict(values))


def _single_handicap(home_goals: int, away_goals: int, line: float) -> str:
    delta = home_goals + line - away_goals
    if delta > 1e-9:
        return "win"
    if delta < -1e-9:
        return "loss"
    return "push"


def _split_line(line: float) -> list[float]:
    doubled = round(line * 2)
    if abs(line * 2 - doubled) < 1e-9:
        return [line]
    lower = math.floor(line * 2) / 2
    return [lower, lower + 0.5]


def _combine_settlements(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if values.count("win") == 2:
        return "win"
    if values.count("loss") == 2:
        return "loss"
    if "win" in values and "push" in values:
        return "half_win"
    if "loss" in values and "push" in values:
        return "half_loss"
    return "push"


def settle_handicap(home_goals: int, away_goals: int, line: float) -> str:
    """Settle a home handicap including quarter-ball half outcomes."""

    return _combine_settlements([_single_handicap(home_goals, away_goals, part) for part in _split_line(line)])


def settle_total(total_goals: int, line: float, *, over: bool = True) -> str:
    """Settle an over/under total line including quarter lines."""

    outcomes = []
    for part in _split_line(line):
        delta = total_goals - part if over else part - total_goals
        outcomes.append("win" if delta > 1e-9 else "loss" if delta < -1e-9 else "push")
    return _combine_settlements(outcomes)


__all__ = [
    "handicap_distribution",
    "half_full_distribution",
    "normalize_probabilities",
    "one_x_two_from_matrix",
    "scoreline_matrix",
    "settle_handicap",
    "settle_total",
    "top_scorelines",
    "total_goals_distribution",
    "total_over_under_distribution",
]
