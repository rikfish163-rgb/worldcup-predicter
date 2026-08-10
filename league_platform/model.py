"""Leakage-aware dynamic Elo baseline and chronological calibration."""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import groupby

from league_platform.domain import Match


CLASSES = ("home", "draw", "away")


def _outcome(match: Match) -> tuple[float, float, float]:
    if match.score is None:
        raise ValueError("evaluation requires a finished match")
    if match.score.home > match.score.away:
        return (1.0, 0.0, 0.0)
    if match.score.home == match.score.away:
        return (0.0, 1.0, 0.0)
    return (0.0, 0.0, 1.0)


def _league_parameters(matches: list[Match]) -> tuple[float, float]:
    outcomes = [_outcome(match) for match in matches]
    draw_rate = sum(item[1] for item in outcomes) / len(outcomes)
    home_wins = sum(item[0] for item in outcomes)
    away_wins = sum(item[2] for item in outcomes)
    home_advantage = 400 * math.log10((home_wins + 1) / (away_wins + 1))
    return min(0.35, max(0.15, draw_rate)), min(150.0, max(-100.0, home_advantage))


def _elo_predictions(
    matches: list[Match], *, draw_rate: float, home_advantage: float
) -> list[tuple[Match, tuple[float, float, float], tuple[float, float, float]]]:
    ratings: dict[str, float] = defaultdict(lambda: 1500.0)
    rows = []
    ordered = sorted(matches, key=lambda match: (match.kickoff_at, match.id))
    for _, group in groupby(ordered, key=lambda match: match.kickoff_at):
        batch = list(group)
        pending = []
        for match in batch:
            home_rating = ratings[match.home_team_id]
            away_rating = ratings[match.away_team_id]
            non_draw_home = 1 / (1 + 10 ** ((away_rating - home_rating - home_advantage) / 400))
            probabilities = (
                (1 - draw_rate) * non_draw_home,
                draw_rate,
                (1 - draw_rate) * (1 - non_draw_home),
            )
            outcome = _outcome(match)
            rows.append((match, probabilities, outcome))
            pending.append((match, non_draw_home, outcome))
        for match, expected_home, outcome in pending:
            actual_home = outcome[0] + 0.5 * outcome[1]
            change = 20.0 * (actual_home - expected_home)
            ratings[match.home_team_id] += change
            ratings[match.away_team_id] -= change
    return rows


def _log_loss(
    probabilities: list[tuple[float, float, float]], outcomes: list[tuple[float, float, float]]
) -> float:
    return -sum(
        sum(actual * math.log(max(1e-12, predicted)) for actual, predicted in zip(y, p))
        for p, y in zip(probabilities, outcomes)
    ) / len(outcomes)


def _calibrate(
    calibration_rows: list[tuple[Match, tuple[float, float, float], tuple[float, float, float]]],
) -> tuple[float, tuple[float, float, float]]:
    count = len(calibration_rows)
    prior = tuple(sum(row[2][index] for row in calibration_rows) / count for index in range(3))
    best_alpha = 0.0
    best_loss = math.inf
    for step in range(21):
        alpha = step / 20
        calibrated = [
            tuple((1 - alpha) * value + alpha * prior[index] for index, value in enumerate(row[1]))
            for row in calibration_rows
        ]
        loss = _log_loss(calibrated, [row[2] for row in calibration_rows])
        if loss < best_loss:
            best_alpha, best_loss = alpha, loss
    return best_alpha, prior


def _metrics(
    probabilities: list[tuple[float, float, float]], outcomes: list[tuple[float, float, float]]
) -> dict[str, float]:
    count = len(outcomes)
    brier = (
        sum(
            sum((p - y) ** 2 for p, y in zip(predicted, actual))
            for predicted, actual in zip(probabilities, outcomes)
        )
        / count
    )
    rps = (
        sum(
            sum(
                (sum(predicted[: index + 1]) - sum(actual[: index + 1])) ** 2 for index in range(2)
            )
            / 2
            for predicted, actual in zip(probabilities, outcomes)
        )
        / count
    )
    bins: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for predicted, actual in zip(probabilities, outcomes):
        selected = max(range(3), key=predicted.__getitem__)
        confidence = predicted[selected]
        bins[min(9, int(confidence * 10))].append((confidence, actual[selected]))
    ece = sum(
        len(items)
        / count
        * abs(
            sum(item[0] for item in items) / len(items)
            - sum(item[1] for item in items) / len(items)
        )
        for items in bins.values()
    )
    return {
        "brier_score": round(brier, 6),
        "log_loss": round(_log_loss(probabilities, outcomes), 6),
        "rps": round(rps, 6),
        "ece": round(ece, 6),
    }


def evaluate_league(league_id: str, matches: list[Match]) -> dict:
    """Evaluate season three after season-one warmup and season-two calibration."""

    finished = [match for match in matches if match.score is not None]
    seasons = sorted({match.season for match in finished})
    if len(seasons) < 3:
        return {
            "status": "not_evaluated",
            "message": "至少需要三个完整赛季用于预热、校准和留出评估。",
            "sample_n": 0,
        }
    warmup_season, calibration_season, evaluation_season = seasons[-3:]
    warmup = [match for match in finished if match.season == warmup_season]
    relevant = [match for match in finished if match.season in seasons[-3:]]
    draw_rate, home_advantage = _league_parameters(warmup)
    rows = _elo_predictions(relevant, draw_rate=draw_rate, home_advantage=home_advantage)
    calibration_rows = [row for row in rows if row[0].season == calibration_season]
    evaluation_rows = [row for row in rows if row[0].season == evaluation_season]
    alpha, prior = _calibrate(calibration_rows)
    calibration_probabilities = [
        tuple((1 - alpha) * value + alpha * prior[index] for index, value in enumerate(row[1]))
        for row in calibration_rows
    ]
    calibration_log_loss = _log_loss(
        calibration_probabilities, [row[2] for row in calibration_rows]
    )
    probabilities = [
        tuple((1 - alpha) * value + alpha * prior[index] for index, value in enumerate(row[1]))
        for row in evaluation_rows
    ]
    outcomes = [row[2] for row in evaluation_rows]
    metrics = _metrics(probabilities, outcomes)
    training_rows = [row for row in rows if row[0].season != evaluation_season]
    training_prior = tuple(
        sum(row[2][index] for row in training_rows) / len(training_rows) for index in range(3)
    )
    frequency_metrics = _metrics([training_prior] * len(outcomes), outcomes)
    market_pairs = [
        (
            (
                row[0].market_probability.home,
                row[0].market_probability.draw,
                row[0].market_probability.away,
            ),
            row[2],
        )
        for row in evaluation_rows
        if row[0].market_probability is not None
    ]
    market_metrics = (
        _metrics(
            [pair[0] for pair in market_pairs],
            [pair[1] for pair in market_pairs],
        )
        if market_pairs
        else None
    )
    fold_size = math.ceil(len(evaluation_rows) / 4)
    folds = []
    for index in range(0, len(evaluation_rows), fold_size):
        fold_rows = evaluation_rows[index : index + fold_size]
        fold_probabilities = probabilities[index : index + fold_size]
        fold_outcomes = outcomes[index : index + fold_size]
        folds.append(
            {
                "fold": len(folds) + 1,
                "start_at": fold_rows[0][0].kickoff_at.isoformat(),
                "end_at": fold_rows[-1][0].kickoff_at.isoformat(),
                "sample_n": len(fold_rows),
                **_metrics(fold_probabilities, fold_outcomes),
            }
        )
    quality_gate = "research_only_no_market_baseline"
    if market_metrics:
        quality_gate = (
            "beats_market_baseline"
            if metrics["brier_score"] < market_metrics["brier_score"]
            else "research_only_underperforms_market"
        )
    return {
        "status": "evaluated",
        "model": "dynamic_elo_three_way_v1",
        "league_id": league_id,
        "warmup_season": warmup_season,
        "calibration_season": calibration_season,
        "evaluation_season": evaluation_season,
        "sample_n": len(evaluation_rows),
        "draw_rate": round(draw_rate, 6),
        "home_advantage_elo": round(home_advantage, 3),
        "calibration_alpha": alpha,
        "calibration_log_loss": round(calibration_log_loss, 6),
        "prediction_time_rule": "pre_kickoff_group_update",
        "walk_forward_folds": folds,
        "baselines": {
            "historical_frequency": {"sample_n": len(outcomes), **frequency_metrics},
            "market": (
                {"sample_n": len(market_pairs), **market_metrics} if market_metrics else None
            ),
        },
        "quality_gate": quality_gate,
        **metrics,
    }
