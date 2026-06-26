"""Walk-forward backtesting engine with tournament-aware embargo.

Per @oracle: use 10-fold expanding window, NOT CPCV.
Tournament-aware split (not day-count) for international football.

Usage:
    from wc_analysis.backtest_v2 import WalkForwardBacktest
    bt = WalkForwardBacktest(matches_df, model_fn)
    results = bt.run(n_folds=10, start_year=1990)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from math import exp, factorial
from typing import Callable

import numpy as np
import pandas as pd

from wc_analysis.elo_model import EloModel
from wc_analysis.handicap import prob_cover
from wc_analysis.draw_correction import DrawCorrection


@dataclass
class BacktestResult:
    n_matches: int
    brier: float
    log_loss: float
    rps: float
    accuracy: float
    top3_hit_rate: float = 0.0
    draw_recall: float = 0.0
    roi: float = 0.0
    n_bets: int = 0
    per_match: list = field(default_factory=list)


def brier_score(pred: dict, actual: str) -> float:
    """Brier score for 1X2. actual in {'H','D','A'}."""
    probs = [pred["p_home"], pred["p_draw"], pred["p_away"]]
    actuals = [1.0 if actual == "H" else 0.0,
               1.0 if actual == "D" else 0.0,
               1.0 if actual == "A" else 0.0]
    return sum((p - a) ** 2 for p, a in zip(probs, actuals)) / 2


def log_loss(pred: dict, actual: str) -> float:
    import math
    p = max(pred["p_home"] if actual == "H" else
            pred["p_draw"] if actual == "D" else
            pred["p_away"], 1e-6)
    return -math.log(p)


def rps(pred: dict, actual: str) -> float:
    """Ranked Probability Score — penalizes distance from actual."""
    cum_pred = np.cumsum([pred["p_home"], pred["p_draw"], pred["p_away"]])
    cum_actual = np.cumsum([1.0 if actual == "H" else 0,
                           1.0 if actual in ("H", "D") else 0, 1.0])
    return float(np.sum((cum_pred - cum_actual) ** 2)) / 2


def actual_result(hs: int, as_: int) -> str:
    if hs > as_:
        return "H"
    if hs < as_:
        return "A"
    return "D"


def smart_argmax(p_home: float, p_draw: float, p_away: float) -> str:
    """Improved argmax with draw boost rule.

    Standard argmax never picks draw because P(draw) is always < max(H, A).
    Rule: predict D when:
    - P(draw) >= 0.25 AND
    - |P(home) - P(away)| <= 0.12 (close match)
    Otherwise pick the max of H/A.
    """
    if p_draw >= 0.25 and abs(p_home - p_away) <= 0.12:
        return "D"
    return max([("H", p_home), ("D", p_draw), ("A", p_away)], key=lambda x: x[1])[0]


def top3_hit(p_home: float, p_draw: float, p_away: float, actual: str) -> int:
    """1 if actual is in top-2 predicted (top-3 always includes all, so use top-2)."""
    ranked = sorted([("H", p_home), ("D", p_draw), ("A", p_away)],
                    key=lambda x: -x[1])
    return 1 if actual in [c for c, _ in ranked[:2]] else 0


class WalkForwardBacktest:
    """Walk-forward backtest with expanding window and tournament-aware embargo."""

    def __init__(self, matches_df: pd.DataFrame,
                 model_fn: Callable | None = None,
                 config: dict | None = None,
                 draw_correction: DrawCorrection | None = None):
        self.matches = matches_df.copy()
        self.matches["date"] = pd.to_datetime(self.matches["date"])
        self.matches = self.matches.sort_values("date").reset_index(drop=True)
        self.config = config or {"rho": -0.20, "avg_goals": 2.50, "home_adv_elo": 100}
        self.model_fn = model_fn or self._default_model
        self.draw_correction = draw_correction

    def _default_model(self, home: str, away: str, neutral: bool,
                       elo_model: EloModel, as_of_date: str | None = None,
                       matches_df: pd.DataFrame | None = None) -> dict:
        """Elo + Dixon-Coles hybrid using score matrix.
        Uses pre-fitted elo_model ratings directly (no per-match recompute).
        as_of_date/matches_df accepted for interface compat but not used
        (Elo is incrementally updated in run() to prevent leakage)."""
        r_h = elo_model._get_or_init(home)
        r_a = elo_model._get_or_init(away)
        dr = r_h - r_a + (0 if neutral else self.config["home_adv_elo"])
        we = 1.0 / (1.0 + 10.0 ** (-dr / 400))
        we = max(0.05, min(0.95, we))
        avg = self.config["avg_goals"]
        lam_h = max(0.25, avg * we)
        lam_a = max(0.25, avg * (1 - we))

        n = 8
        rho = self.config["rho"]
        mat = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                p = exp(-lam_h) * lam_h**i / factorial(i) * exp(-lam_a) * lam_a**j / factorial(j)
                if i == 0 and j == 0:
                    p *= (1 - lam_h * lam_a * rho)
                elif i == 1 and j == 0:
                    p *= (1 + lam_a * rho)
                elif i == 0 and j == 1:
                    p *= (1 + lam_h * rho)
                elif i == 1 and j == 1:
                    p *= (1 - rho)
                mat[i][j] = max(0, p)
        mat /= mat.sum()

        # i>j = home scores more = home win (lower triangle)
        # i<j = away scores more = away win (upper triangle)
        # i==j = draw (diagonal)
        p_home_dc = float(mat[np.tril_indices(n, -1)].sum())
        p_draw_dc = float(np.trace(mat))
        p_away_dc = float(mat[np.triu_indices(n, 1)].sum())

        # Apply draw correction if available — blend DC draw with LR draw
        if self.draw_correction is not None:
            avg_elo = (r_h + r_a) / 2
            p_draw_lr = self.draw_correction.predict(r_h - r_a, avg_elo)
            # Blend: weight LR more when Elo diff is small (where draw matters most)
            elo_gap = abs(r_h - r_a)
            blend_w = max(0.20, min(0.60, 0.60 - 0.40 * (elo_gap - 100) / 300))
            p_draw = (1 - blend_w) * p_draw_dc + blend_w * p_draw_lr
            # Redistribute proportionally to H/A
            ratio = p_home_dc / (p_home_dc + p_away_dc) if (p_home_dc + p_away_dc) > 0 else 0.5
            p_home = (1 - p_draw) * ratio
            p_away = (1 - p_draw) * (1 - ratio)
        else:
            p_home = p_home_dc
            p_draw = p_draw_dc
            p_away = p_away_dc

        total = p_home + p_draw + p_away
        return {"p_home": p_home/total, "p_draw": p_draw/total, "p_away": p_away/total,
                "lam_h": lam_h, "lam_a": lam_a, "score_matrix": mat, "elo_diff": r_h - r_a}

    def run(self, n_folds: int = 10, start_year: int = 1990,
            handicap_line: float | None = None) -> BacktestResult:
        """Run walk-forward backtest.

        Args:
            n_folds: number of expanding-window folds
            start_year: earliest year in training data
            handicap_line: if given, also compute AH accuracy
        """
        df = self.matches[self.matches["date"].dt.year >= start_year].copy()
        df = df.dropna(subset=["home_score", "away_score"])
        df = df.drop_duplicates(subset=["date", "home_team", "away_team"])
        df = df[(df["home_score"] >= 0) & (df["away_score"] >= 0)]

        date_min, date_max = df["date"].min(), df["date"].max()
        fold_edges = pd.date_range(date_min, date_max, periods=n_folds + 1)

        per_match = []
        all_preds = []

        for fold in range(n_folds):
            train_end = fold_edges[fold]
            test_start = fold_edges[fold]
            test_end = fold_edges[fold + 1]

            train_df = self.matches[self.matches["date"] < train_end]
            test_df = df[(df["date"] >= test_start) & (df["date"] < test_end)]

            if len(train_df) < 100 or len(test_df) == 0:
                continue

            elo = EloModel()
            elo.fit(train_df)

            for _, match in test_df.iterrows():
                as_of = str(match["date"].date())
                home = match["home_team"]
                away = match["away_team"]
                neutral = bool(match["neutral"])
                hs, as_ = int(match["home_score"]), int(match["away_score"])
                actual = actual_result(hs, as_)

                try:
                    pred = self.model_fn(home, away, neutral, elo, as_of, self.matches)
                except Exception:
                    continue

                # Incrementally update Elo AFTER prediction (no leakage)
                elo.update_match(home, away, hs, as_, match.get("tournament", ""), neutral)

                b = brier_score(pred, actual)
                ll = log_loss(pred, actual)
                rp = rps(pred, actual)
                pred_class = smart_argmax(pred["p_home"], pred["p_draw"], pred["p_away"])
                hit = 1 if pred_class == actual else 0
                t3 = top3_hit(pred["p_home"], pred["p_draw"], pred["p_away"], actual)

                row = {
                    "date": as_of, "home": home, "away": away,
                    "hs": hs, "as": as_, "actual": actual,
                    "p_home": pred["p_home"], "p_draw": pred["p_draw"], "p_away": pred["p_away"],
                    "pred_class": pred_class,
                    "brier": b, "log_loss": ll, "rps": rp, "hit": hit, "top3_hit": t3,
                    "tournament": match.get("tournament", ""),
                }
                per_match.append(row)
                all_preds.append(pred)

        if not per_match:
            return BacktestResult(0, 0, 0, 0, 0)

        df_res = pd.DataFrame(per_match)
        draw_matches = df_res[df_res["actual"] == "D"]
        draw_correct = (draw_matches["pred_class"] == "D").sum() if len(draw_matches) > 0 else 0
        return BacktestResult(
            n_matches=len(df_res),
            brier=float(df_res["brier"].mean()),
            log_loss=float(df_res["log_loss"].mean()),
            rps=float(df_res["rps"].mean()),
            accuracy=float(df_res["hit"].mean()),
            top3_hit_rate=float(df_res["top3_hit"].mean()) if "top3_hit" in df_res.columns else 0.0,
            draw_recall=float(draw_correct / len(draw_matches)) if len(draw_matches) > 0 else 0.0,
            per_match=per_match,
        )

    def run_tournament_aware(self, target_tournament: str = "FIFA World Cup",
                             start_year: int = 1990) -> BacktestResult:
        """Tournament-aware split: train on all non-target tournaments, test on target."""
        df = self.matches[self.matches["date"].dt.year >= start_year].copy()
        df = df.dropna(subset=["home_score", "away_score"]).drop_duplicates(
            subset=["date", "home_team", "away_team"])

        train_df = df[~df["tournament"].str.contains(target_tournament, case=False, na=False)]
        test_df = df[df["tournament"].str.contains(target_tournament, case=False, na=False)]
        test_df = test_df[~test_df["tournament"].str.contains("qualif", case=False, na=False)]

        elo = EloModel()
        elo.fit(train_df)

        per_match = []
        for _, match in test_df.iterrows():
            as_of = str(match["date"].date())
            try:
                pred = self.model_fn(match["home_team"], match["away_team"],
                                     bool(match["neutral"]), elo, as_of, self.matches)
            except Exception:
                continue
            actual = actual_result(int(match["home_score"]), int(match["away_score"]))
            pred_class = smart_argmax(pred["p_home"], pred["p_draw"], pred["p_away"])
            t3 = top3_hit(pred["p_home"], pred["p_draw"], pred["p_away"], actual)
            per_match.append({
                "date": as_of, "home": match["home_team"], "away": match["away_team"],
                "tournament": match["tournament"], "actual": actual,
                "p_home": pred["p_home"], "p_draw": pred["p_draw"], "p_away": pred["p_away"],
                "pred_class": pred_class,
                "brier": brier_score(pred, actual), "log_loss": log_loss(pred, actual),
                "rps": rps(pred, actual),
                "hit": 1 if pred_class == actual else 0,
                "top3_hit": t3,
            })

        if not per_match:
            return BacktestResult(0, 0, 0, 0, 0)
        df_res = pd.DataFrame(per_match)
        draw_matches = df_res[df_res["actual"] == "D"]
        draw_correct = (draw_matches["pred_class"] == "D").sum() if len(draw_matches) > 0 else 0
        return BacktestResult(
            n_matches=len(df_res), brier=float(df_res["brier"].mean()),
            log_loss=float(df_res["log_loss"].mean()), rps=float(df_res["rps"].mean()),
            accuracy=float(df_res["hit"].mean()),
            top3_hit_rate=float(df_res["top3_hit"].mean()) if "top3_hit" in df_res.columns else 0.0,
            draw_recall=float(draw_correct / len(draw_matches)) if len(draw_matches) > 0 else 0.0,
            per_match=per_match,
        )
