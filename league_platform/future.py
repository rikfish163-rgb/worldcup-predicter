"""As-of future-fixture predictions built from history plus current sources."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from itertools import groupby

from league_platform.dixon_coles import _one_x_two


def _outcome(match: dict) -> float:
    score = match["score"]
    return 1.0 if score["home"] > score["away"] else 0.5 if score["home"] == score["away"] else 0.0


def _history(snapshot: dict, competition_id: str, as_of: datetime) -> list[dict]:
    return sorted(
        (
            match
            for match in snapshot["matches"]
            if match["competition_id"] == competition_id
            and match["status"] == "finished"
            and match["score"] is not None
            and datetime.fromisoformat(match["kickoff_at"]).astimezone(timezone.utc) < as_of
        ),
        key=lambda match: (match["kickoff_at"], match["id"]),
    )


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
    matches: list[dict], home_average: float, away_average: float
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
            attack[match["home_team_id"]] += 0.025 * home_error
            defence[match["away_team_id"]] -= 0.025 * home_error
            attack[match["away_team_id"]] += 0.025 * away_error
            defence[match["home_team_id"]] -= 0.025 * away_error
    return dict(attack), dict(defence)


def _calibrate(
    probability: tuple[float, float, float], alpha: float, prior: list[float]
) -> tuple[float, float, float]:
    values = tuple(
        (1 - alpha) * value + alpha * prior[index] for index, value in enumerate(probability)
    )
    total = sum(values)
    return tuple(value / total for value in values)


def build_future_predictions(snapshot: dict) -> dict:
    current = snapshot.get("current_data", {})
    as_of = current.get("as_of")
    if current.get("status") != "fresh" or not as_of:
        return {
            "status": "unavailable",
            "as_of": as_of,
            "predictions": [],
            "blocked": [],
            "message": "当前多源快照缺失或过期，未来预测被阻断。",
        }
    as_of_dt = datetime.fromisoformat(as_of).astimezone(timezone.utc)
    competitions = {item["id"]: item for item in snapshot["competitions"]}
    states = {}
    for competition_id, competition in competitions.items():
        health = competition["model_health"]
        data_cutoff = health.get("data_cutoff")
        if (
            not data_cutoff
            or datetime.fromisoformat(data_cutoff).astimezone(timezone.utc) >= as_of_dt
        ):
            continue
        history = _history(snapshot, competition_id, as_of_dt)
        if not history or health.get("status") != "evaluated":
            continue
        counts: dict[str, int] = defaultdict(int)
        for match in history:
            counts[match["home_team_id"]] += 1
            counts[match["away_team_id"]] += 1
        dc = health["dixon_coles"]
        attack, defence = _dc_state(history, dc["home_goal_average"], dc["away_goal_average"])
        states[competition_id] = {
            "health": health,
            "history": history,
            "counts": counts,
            "elo_ratings": _elo_state(history, health["home_advantage_elo"]),
            "attack": attack,
            "defence": defence,
            "training_cutoff": history[-1]["kickoff_at"],
        }
    predictions = []
    blocked = []
    for fixture in snapshot["matches"]:
        if fixture["status"] != "upcoming":
            continue
        kickoff = datetime.fromisoformat(fixture["kickoff_at"]).astimezone(timezone.utc)
        if kickoff <= as_of_dt:
            continue
        state = states.get(fixture["competition_id"])
        if state is None:
            health = competitions[fixture["competition_id"]]["model_health"]
            data_cutoff = health.get("data_cutoff")
            reason = (
                "historical_model_not_causal"
                if data_cutoff
                and datetime.fromisoformat(data_cutoff).astimezone(timezone.utc) >= as_of_dt
                else "historical_model_not_evaluated"
            )
            blocked.append({"fixture_id": fixture["id"], "reason": reason})
            continue
        health = state["health"]
        history = state["history"]
        counts = state["counts"]
        missing_history = [
            team
            for team in (fixture["home_team_id"], fixture["away_team_id"])
            if counts[team] < 10
        ]
        if missing_history:
            blocked.append(
                {
                    "fixture_id": fixture["id"],
                    "reason": "insufficient_promoted_team_history",
                    "team_ids": missing_history,
                }
            )
            continue
        elo_ratings = state["elo_ratings"]
        non_draw_home = 1 / (
            1
            + 10
            ** (
                (
                    elo_ratings[fixture["away_team_id"]]
                    - elo_ratings[fixture["home_team_id"]]
                    - health["home_advantage_elo"]
                )
                / 400
            )
        )
        elo_probability = _calibrate(
            (
                (1 - health["draw_rate"]) * non_draw_home,
                health["draw_rate"],
                (1 - health["draw_rate"]) * (1 - non_draw_home),
            ),
            health["calibration_alpha"],
            health["calibration_prior"],
        )
        dc = health["dixon_coles"]
        attack = state["attack"]
        defence = state["defence"]
        home_rate = min(
            4.5,
            max(
                0.15,
                dc["home_goal_average"]
                * math.exp(
                    attack.get(fixture["home_team_id"], 0)
                    - defence.get(fixture["away_team_id"], 0)
                ),
            ),
        )
        away_rate = min(
            4.5,
            max(
                0.15,
                dc["away_goal_average"]
                * math.exp(
                    attack.get(fixture["away_team_id"], 0)
                    - defence.get(fixture["home_team_id"], 0)
                ),
            ),
        )
        current_features = fixture.get("current_features", {})
        home_feature = current_features.get("home")
        away_feature = current_features.get("away")
        market_feature = current_features.get("market")
        if home_feature and away_feature:
            home_recent = (home_feature["xg_for"] + away_feature["xg_against"]) / 2
            away_recent = (away_feature["xg_for"] + home_feature["xg_against"]) / 2
            home_rate = 0.75 * home_rate + 0.25 * home_recent
            away_rate = 0.75 * away_rate + 0.25 * away_recent
        dc_probability = _calibrate(
            _one_x_two(home_rate, away_rate, dc["rho"]),
            dc["calibration_alpha"],
            dc["calibration_prior"],
        )
        providers = [fixture["source"]["name"], history[-1]["source"]["name"]]
        if home_feature and away_feature:
            providers.append("Understat")
        if market_feature:
            providers.extend([market_feature["source"]["name"], market_feature["provider"]])
        predictions.append(
            {
                "fixture_id": fixture["id"],
                "competition_id": fixture["competition_id"],
                "kickoff_at": fixture["kickoff_at"],
                "home_team": fixture["home_team"],
                "away_team": fixture["away_team"],
                "as_of": as_of,
                "training_cutoff": state["training_cutoff"],
                "status": "research_only",
                "quality_gate": (
                    "blocked_for_production_without_current_injuries_and_confirmed_lineups"
                    if market_feature
                    else "blocked_for_production_without_current_market_injuries_and_lineups"
                ),
                "providers": sorted(set(providers)),
                "feature_coverage": {
                    "historical_matches_home": counts[fixture["home_team_id"]],
                    "historical_matches_away": counts[fixture["away_team_id"]],
                    "recent_xg": bool(home_feature and away_feature),
                    "current_market": bool(market_feature),
                    "lineups": False,
                },
                "elo_probability": {
                    "home": round(elo_probability[0], 6),
                    "draw": round(elo_probability[1], 6),
                    "away": round(elo_probability[2], 6),
                },
                "dixon_coles_probability": {
                    "home": round(dc_probability[0], 6),
                    "draw": round(dc_probability[1], 6),
                    "away": round(dc_probability[2], 6),
                },
                "market_probability": (market_feature["probability"] if market_feature else None),
                "expected_goals": {"home": round(home_rate, 4), "away": round(away_rate, 4)},
            }
        )
    predictions.sort(key=lambda item: item["kickoff_at"])
    blocked.sort(key=lambda item: item["fixture_id"])
    return {
        "status": "research_only",
        "as_of": as_of,
        "predictions": predictions,
        "blocked": blocked,
        "message": "只预测 as_of 之后的赛程；缺少当前市场、伤停和首发时不得用于生产决策。",
    }
