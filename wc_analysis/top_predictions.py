"""Top-3 match prediction system.

Outputs ranked predictions for:
- 1X2 (胜平负): top-3 with probabilities
- Asian handicap (让球胜平负): top-3 cover probabilities
- Correct score (比分): top-6 with probabilities

Also includes an improved draw calibration layer that fixes the systematic
draw underestimation in pure Poisson/Dixon-Coles models.

Usage:
    from wc_analysis.top_predictions import TopPredictor
    predictor = TopPredictor(elo_model, draw_correction)
    result = predictor.predict("Brazil", "Argentina", neutral=True,
                               handicap_line=-0.5, as_of_date="2024-01-01",
                               matches_df=df)
"""

from __future__ import annotations

import json
from math import exp, factorial
from pathlib import Path

import numpy as np
import pandas as pd

from wc_analysis.elo_model import EloModel
from wc_analysis.handicap import prob_cover
from wc_analysis.draw_correction import DrawCorrection


class TopPredictor:
    """Full match prediction with top-N ranked outcomes."""

    def __init__(self, elo_model: EloModel,
                 draw_correction: DrawCorrection | None = None,
                 config: dict | None = None):
        self.elo = elo_model
        self.dc = draw_correction
        self.config = config or {
            "rho": -0.20, "avg_goals": 2.50, "home_adv_elo": 100,
            "draw_blend_min": 0.30, "draw_blend_max": 0.65,
        }

    def _score_matrix(self, lam_h: float, lam_a: float) -> np.ndarray:
        """Build Dixon-Coles corrected score matrix."""
        n = 8
        rho = self.config["rho"]
        mat = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                p = exp(-lam_h) * lam_h**i / factorial(i) * \
                    exp(-lam_a) * lam_a**j / factorial(j)
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
        return mat

    def _lambdas_from_elo(self, r_h: float, r_a: float, neutral: bool) -> tuple[float, float]:
        dr = r_h - r_a + (0 if neutral else self.config["home_adv_elo"])
        we = 1.0 / (1.0 + 10.0 ** (-dr / 400))
        we = max(0.05, min(0.95, we))
        avg = self.config["avg_goals"]
        return max(0.25, avg * we), max(0.25, avg * (1 - we))

    def _calibrate_draw(self, p_draw_dc: float, p_draw_lr: float,
                        elo_gap: float) -> float:
        """Stronger draw calibration than backtest_v2 version."""
        # When Elo gap is small, lean more on LR (which captures draw patterns)
        # When Elo gap is large, DC's low draw is correct
        if elo_gap < 50:
            blend = self.config["draw_blend_max"]  # 0.65
        elif elo_gap < 150:
            blend = 0.50
        elif elo_gap < 300:
            blend = self.config["draw_blend_min"]  # 0.30
        else:
            blend = 0.15
        return (1 - blend) * p_draw_dc + blend * p_draw_lr

    def predict(self, home: str, away: str, neutral: bool = False,
                handicap_line: float | None = None,
                as_of_date: str | None = None,
                matches_df: pd.DataFrame | None = None,
                top_n_1x2: int = 3, top_n_score: int = 6) -> dict:
        """Full prediction with top-N ranked outcomes."""
        # Get Elo ratings
        if as_of_date and matches_df is not None:
            r_h = self.elo.get_rating(home, as_of_date, matches_df)
            r_a = self.elo.get_rating(away, as_of_date, matches_df)
        else:
            r_h = self.elo._get_or_init(home)
            r_a = self.elo._get_or_init(away)

        lam_h, lam_a = self._lambdas_from_elo(r_h, r_a, neutral)
        mat = self._score_matrix(lam_h, lam_a)

        # Base 1X2 from score matrix
        p_home_dc = float(mat[np.tril_indices(8, -1)].sum())
        p_draw_dc = float(np.trace(mat))
        p_away_dc = float(mat[np.triu_indices(8, 1)].sum())

        # Draw correction
        if self.dc is not None:
            avg_elo = (r_h + r_a) / 2
            p_draw_lr = self.dc.predict(r_h - r_a, avg_elo)
            elo_gap = abs(r_h - r_a)
            p_draw = self._calibrate_draw(p_draw_dc, p_draw_lr, elo_gap)
            # Redistribute to H/A proportionally
            ratio = p_home_dc / (p_home_dc + p_away_dc) if (p_home_dc + p_away_dc) > 0 else 0.5
            p_home = (1 - p_draw) * ratio
            p_away = (1 - p_draw) * (1 - ratio)
        else:
            p_home, p_draw, p_away = p_home_dc, p_draw_dc, p_away_dc

        total = p_home + p_draw + p_away
        p_home, p_draw, p_away = p_home/total, p_draw/total, p_away/total

        # Top-N 1X2
        x12 = [("主胜", p_home, "H"), ("平局", p_draw, "D"), ("客胜", p_away, "A")]
        x12.sort(key=lambda x: -x[1])
        top_1x2 = [{"outcome": o, "probability": p, "code": c}
                   for o, p, c in x12[:top_n_1x2]]

        # Correct scores
        scores = []
        for i in range(8):
            for j in range(8):
                scores.append((f"{i}-{j}", float(mat[i][j]), (i, j)))
        scores.sort(key=lambda x: -x[1])
        top_scores = [{"score": s, "probability": p, "goals": (i, j)}
                      for s, p, (i, j) in scores[:top_n_score]]

        # Asian handicap
        ah_result = None
        if handicap_line is not None:
            ah = prob_cover(mat, handicap_line)
            ah_result = {
                "line": handicap_line,
                "home_cover": ah["p_win"] + 0.5 * ah["p_push"],
                "push": ah["p_push"],
                "away_cover": ah["p_loss"] + 0.5 * ah["p_push"],
                "top_outcomes": self._top_ah(ah, handicap_line),
            }

        # Suggested handicap line (auto-pick from matrix)
        suggested_lines = self._suggest_handicap_lines(mat)

        return {
            "home": home, "away": away, "neutral": neutral,
            "elo_home": round(r_h, 0), "elo_away": round(r_a, 0),
            "elo_diff": round(r_h - r_a, 0),
            "lambda_home": round(lam_h, 2), "lambda_away": round(lam_a, 2),
            "p_home": round(p_home, 3), "p_draw": round(p_draw, 3),
            "p_away": round(p_away, 3),
            "top_1x2": top_1x2,
            "top_scores": top_scores,
            "handicap": ah_result,
            "suggested_lines": suggested_lines,
        }

    def _top_ah(self, ah: dict, line: float) -> list:
        outcomes = [
            (f"主队 {line:+.2f} 赢", ah["p_win"]),
            (f"走盘({line:+.2f})", ah["p_push"]),
            (f"客队 {-line:+.2f} 赢", ah["p_loss"]),
        ]
        outcomes.sort(key=lambda x: -x[1])
        return [{"outcome": o, "probability": p} for o, p in outcomes if p > 0.01]

    def _suggest_handicap_lines(self, mat: np.ndarray) -> list:
        """Suggest handicap lines where home cover ~ 50% (fair lines)."""
        from wc_analysis.handicap import prob_cover
        lines = [-2.0, -1.75, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25,
                 0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
        results = []
        for line in lines:
            p = prob_cover(mat, line)
            home_cover = p["p_win"] + 0.5 * p["p_push"]
            results.append({"line": line, "home_cover": round(home_cover, 3),
                            "away_cover": round(1 - home_cover - 0.5 * p["p_push"], 3)})
        # Find line closest to 50/50
        results.sort(key=lambda x: abs(x["home_cover"] - 0.5))
        return results[:3]


def predict_with_top3(home: str, away: str, matches_df: pd.DataFrame,
                      data_dir: Path | None = None,
                      handicap_line: float | None = None,
                      as_of_date: str | None = None) -> dict:
    """Convenience function: build predictor and run."""
    elo = EloModel()
    if as_of_date:
        elo.fit(matches_df[matches_df["date"] < as_of_date])
    else:
        elo.fit(matches_df)

    dc = None
    if data_dir and (data_dir / "draw_correction_v2.json").exists():
        dc = DrawCorrection()
        dc.load(data_dir / "draw_correction_v2.json")

    predictor = TopPredictor(elo, dc)
    return predictor.predict(home, away, handicap_line=handicap_line,
                             as_of_date=as_of_date, matches_df=matches_df)
