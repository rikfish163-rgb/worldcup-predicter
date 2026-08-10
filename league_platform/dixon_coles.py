"""Chronological Dixon-Coles-inspired score model for historical evaluation."""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import groupby

from league_platform.domain import Match
from league_platform.model import _calibrate, _log_loss, _metrics, _outcome


def _poisson_mass(rate: float, maximum: int = 8) -> list[float]:
    values = [math.exp(-rate)]
    for goals in range(1, maximum + 1):
        values.append(values[-1] * rate / goals)
    return values


def _one_x_two(home_rate: float, away_rate: float, rho: float) -> tuple[float, float, float]:
    home_mass = _poisson_mass(home_rate)
    away_mass = _poisson_mass(away_rate)
    totals = [0.0, 0.0, 0.0]
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
            probability = max(0.0, home_probability * away_probability * adjustment)
            result_index = 0 if home_goals > away_goals else 1 if home_goals == away_goals else 2
            totals[result_index] += probability
    total = sum(totals)
    return tuple(value / total for value in totals)


def _rate_rows(
    matches: list[Match], home_average: float, away_average: float
) -> list[tuple[Match, float, float, tuple[float, float, float]]]:
    attack: dict[str, float] = defaultdict(float)
    defence: dict[str, float] = defaultdict(float)
    rows = []
    ordered = sorted(matches, key=lambda match: (match.kickoff_at, match.id))
    for _, group in groupby(ordered, key=lambda match: match.kickoff_at):
        pending = []
        for match in group:
            home_rate = min(
                4.5,
                max(
                    0.15,
                    home_average
                    * math.exp(attack[match.home_team_id] - defence[match.away_team_id]),
                ),
            )
            away_rate = min(
                4.5,
                max(
                    0.15,
                    away_average
                    * math.exp(attack[match.away_team_id] - defence[match.home_team_id]),
                ),
            )
            outcome = _outcome(match)
            rows.append((match, home_rate, away_rate, outcome))
            pending.append((match, home_rate, away_rate))
        for match, home_rate, away_rate in pending:
            home_error = match.score.home - home_rate
            away_error = match.score.away - away_rate
            learning_rate = 0.025
            attack[match.home_team_id] += learning_rate * home_error
            defence[match.away_team_id] -= learning_rate * home_error
            attack[match.away_team_id] += learning_rate * away_error
            defence[match.home_team_id] -= learning_rate * away_error
    return rows


def evaluate_dixon_coles(league_id: str, matches: list[Match]) -> dict:
    finished = [match for match in matches if match.score is not None]
    seasons = sorted({match.season for match in finished})
    if len(seasons) < 3:
        return {"status": "not_evaluated", "sample_n": 0}
    warmup_season, calibration_season, evaluation_season = seasons[-3:]
    warmup = [match for match in finished if match.season == warmup_season]
    home_average = sum(match.score.home for match in warmup) / len(warmup)
    away_average = sum(match.score.away for match in warmup) / len(warmup)
    relevant = [match for match in finished if match.season in seasons[-3:]]
    rows = _rate_rows(relevant, home_average, away_average)
    calibration_rows = [row for row in rows if row[0].season == calibration_season]
    evaluation_rows = [row for row in rows if row[0].season == evaluation_season]

    best_rho = 0.0
    best_alpha = 0.0
    best_prior = (1 / 3, 1 / 3, 1 / 3)
    best_loss = math.inf
    for step in range(-15, 16):
        rho = step / 100
        candidate_rows = [
            (row[0], _one_x_two(row[1], row[2], rho), row[3]) for row in calibration_rows
        ]
        alpha, prior = _calibrate(candidate_rows)
        probabilities = [
            tuple((1 - alpha) * value + alpha * prior[index] for index, value in enumerate(row[1]))
            for row in candidate_rows
        ]
        loss = _log_loss(probabilities, [row[2] for row in candidate_rows])
        if loss < best_loss:
            best_rho, best_alpha, best_prior, best_loss = rho, alpha, prior, loss

    raw_probabilities = [_one_x_two(row[1], row[2], best_rho) for row in evaluation_rows]
    probabilities = [
        tuple(
            (1 - best_alpha) * value + best_alpha * best_prior[index]
            for index, value in enumerate(probability)
        )
        for probability in raw_probabilities
    ]
    outcomes = [row[3] for row in evaluation_rows]
    metrics = _metrics(probabilities, outcomes)
    return {
        "status": "evaluated",
        "model": "online_dixon_coles_v1",
        "league_id": league_id,
        "warmup_season": warmup_season,
        "calibration_season": calibration_season,
        "evaluation_season": evaluation_season,
        "sample_n": len(evaluation_rows),
        "home_goal_average": round(home_average, 6),
        "away_goal_average": round(away_average, 6),
        "rho": best_rho,
        "calibration_alpha": best_alpha,
        "calibration_prior": [round(value, 8) for value in best_prior],
        "calibration_log_loss": round(best_loss, 6),
        "prediction_time_rule": "pre_kickoff_group_update",
        **metrics,
    }
