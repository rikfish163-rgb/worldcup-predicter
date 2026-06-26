"""LightGBM ensemble model for match prediction.

Stacks on top of Elo + Dixon-Coles base predictions, adding market odds features.
Designed to train on GPU (4090) via SSH for larger feature sets.

Two-stage approach:
1. Base: Elo + Dixon-Coles → p_home_base, p_draw_base, p_away_base, lam_h, lam_a
2. Meta: LightGBM takes base probs + market features → calibrated p_home/draw/away

For international matches without market odds, falls back to base model only.
For club matches with Pinnacle odds, uses full ensemble (much stronger).
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, early_stopping, log_evaluation
from sklearn.metrics import accuracy_score, brier_score_loss

from wc_analysis.elo_model import EloModel
from wc_analysis.draw_correction import DrawCorrection
from wc_analysis.backtest_v2 import (
    WalkForwardBacktest, brier_score, log_loss, rps, actual_result,
    smart_argmax, top3_hit,
)


class EnsembleModel:
    """LightGBM ensemble stacking Elo+DC base with market features."""

    def __init__(self, config: dict | None = None):
        self.config = config or {
            "rho": -0.20, "avg_goals": 2.50, "home_adv_elo": 100,
            "n_estimators": 300, "learning_rate": 0.05, "max_depth": 5,
            "num_leaves": 31, "min_child_samples": 50,
        }
        self.models = {}  # one per target class
        self.feature_names = []
        self.trained = False

    def _base_predict(self, home: str, away: str, neutral: bool,
                      elo: EloModel, dc: DrawCorrection | None) -> dict:
        """Elo + DC base prediction (same as backtest_v2._default_model)."""
        from math import exp, factorial
        r_h = elo._get_or_init(home)
        r_a = elo._get_or_init(away)
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

        p_home_dc = float(mat[np.tril_indices(n, -1)].sum())
        p_draw_dc = float(np.trace(mat))
        p_away_dc = float(mat[np.triu_indices(n, 1)].sum())

        if dc is not None:
            avg_elo = (r_h + r_a) / 2
            p_draw_lr = dc.predict(r_h - r_a, avg_elo)
            elo_gap = abs(r_h - r_a)
            blend = max(0.30, min(0.65, 0.65 - 0.40 * (elo_gap - 100) / 300))
            p_draw = (1 - blend) * p_draw_dc + blend * p_draw_lr
            ratio = p_home_dc / (p_home_dc + p_away_dc) if (p_home_dc + p_away_dc) > 0 else 0.5
            p_home = (1 - p_draw) * ratio
            p_away = (1 - p_draw) * (1 - ratio)
        else:
            p_home, p_draw, p_away = p_home_dc, p_draw_dc, p_away_dc

        total = p_home + p_draw + p_away
        return {
            "p_home_base": p_home/total, "p_draw_base": p_draw/total,
            "p_away_base": p_away/total, "lam_h": lam_h, "lam_a": lam_a,
            "elo_diff": r_h - r_a, "elo_avg": (r_h + r_a) / 2,
        }

    def build_features(self, matches_df: pd.DataFrame, market_df: pd.DataFrame | None = None,
                       dc: DrawCorrection | None = None, start_year: int = 1990) -> pd.DataFrame:
        """Build feature matrix with base predictions + market features."""
        df = matches_df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"].dt.year >= start_year].sort_values("date").reset_index(drop=True)
        df = df.dropna(subset=["home_score", "away_score"])
        df = df.drop_duplicates(subset=["date", "home_team", "away_team"])

        elo = EloModel()
        pre = df[df["date"].dt.year < start_year]
        if len(pre) > 0:
            elo.fit(pre)

        rows = []
        for _, m in df.iterrows():
            base = self._base_predict(m["home_team"], m["away_team"],
                                      bool(m["neutral"]), elo, dc)
            feat = {
                "elo_diff": base["elo_diff"],
                "elo_avg": base["elo_avg"],
                "p_home_base": base["p_home_base"],
                "p_draw_base": base["p_draw_base"],
                "p_away_base": base["p_away_base"],
                "lam_h": base["lam_h"],
                "lam_a": base["lam_a"],
                "neutral": int(m["neutral"]),
                "tournament_stage": _tournament_stage(m.get("tournament", "")),
            }
            # Add market features if available
            if market_df is not None:
                mf = _match_market_features(m, market_df)
                feat.update(mf)

            feat["actual"] = actual_result(int(m["home_score"]), int(m["away_score"]))
            feat["home_score"] = int(m["home_score"])
            feat["away_score"] = int(m["away_score"])
            feat["date"] = str(m["date"].date())
            feat["home"] = m["home_team"]
            feat["away"] = m["away_team"]
            rows.append(feat)

            elo.update_match(m["home_team"], m["away_team"],
                             int(m["home_score"]), int(m["away_score"]),
                             m["tournament"], bool(m["neutral"]))

        return pd.DataFrame(rows)

    def train(self, features_df: pd.DataFrame, use_market: bool = True) -> dict:
        """Train 3 binary LightGBM classifiers (one-vs-rest for H/D/A)."""
        base_features = ["elo_diff", "elo_avg", "p_home_base", "p_draw_base",
                         "p_away_base", "lam_h", "lam_a", "neutral", "tournament_stage"]
        market_features = ["pin_p_home", "pin_p_draw", "pin_p_away",
                           "ah_home_cover", "ah_away_cover",
                           "odds_implied_home", "odds_implied_draw", "odds_implied_away"]

        self.feature_names = base_features.copy()
        if use_market and all(f in features_df.columns for f in market_features):
            has_market = features_df[market_features].notna().any(axis=1)
            if has_market.sum() > 100:
                self.feature_names += market_features
                features_df[market_features] = features_df[market_features].fillna(0.33)

        X = features_df[self.feature_names].values
        y = features_df["actual"].values

        # Time-based split: last 20% as validation
        split = int(len(X) * 0.8)
        X_tr, X_val = X[:split], X[split:]
        y_tr, y_val = y[:split], y[split:]

        params = {
            "n_estimators": self.config["n_estimators"],
            "learning_rate": self.config["learning_rate"],
            "max_depth": self.config["max_depth"],
            "num_leaves": self.config["num_leaves"],
            "min_child_samples": self.config["min_child_samples"],
            "verbose": -1, "n_jobs": -1,
        }

        # One-vs-rest: train 3 binary classifiers
        self.models = {}
        for cls in ["H", "D", "A"]:
            y_bin = (y_tr == cls).astype(int)
            y_val_bin = (y_val == cls).astype(int)
            model = LGBMClassifier(**params, objective="binary")
            model.fit(
                X_tr, y_bin, eval_set=[(X_val, y_val_bin)],
                callbacks=[early_stopping(20), log_evaluation(0)],
            )
            self.models[cls] = model

        self.trained = True

        # Validation metrics
        val_probs = self.predict_proba(X_val)
        classes = ["H", "D", "A"]
        val_pred = np.array([classes[i] for i in np.argmax(val_probs, axis=1)])
        val_acc = accuracy_score(y_val, val_pred)
        # Brier for each class
        val_brier = np.mean([
            brier_score_loss((y_val == c).astype(int), val_probs[:, i])
            for i, c in enumerate(["H", "D", "A"])
        ])

        return {"val_accuracy": val_acc, "val_brier": val_brier, "n_train": len(X_tr)}

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Return (N, 3) array of [p_home, p_draw, p_away]."""
        probs = np.column_stack([
            self.models["H"].predict_proba(X)[:, 1],
            self.models["D"].predict_proba(X)[:, 1],
            self.models["A"].predict_proba(X)[:, 1],
        ])
        # Normalize to sum to 1
        probs = probs / probs.sum(axis=1, keepdims=True)
        return probs

    def predict_match(self, home: str, away: str, neutral: bool, tournament: str,
                      elo: EloModel, dc: DrawCorrection | None = None,
                      market_feats: dict | None = None) -> dict:
        """Predict a single match."""
        base = self._base_predict(home, away, neutral, elo, dc)
        feat = [base["elo_diff"], base["elo_avg"], base["p_home_base"],
                base["p_draw_base"], base["p_away_base"], base["lam_h"],
                base["lam_a"], int(neutral), _tournament_stage(tournament)]
        if "pin_p_home" in self.feature_names and market_feats:
            feat += [market_feats.get("pin_p_home", 0.33),
                     market_feats.get("pin_p_draw", 0.33),
                     market_feats.get("pin_p_away", 0.33),
                     market_feats.get("ah_home_cover", 0.5),
                     market_feats.get("ah_away_cover", 0.5),
                     market_feats.get("odds_implied_home", 0.33),
                     market_feats.get("odds_implied_draw", 0.33),
                     market_feats.get("odds_implied_away", 0.33)]
        elif "pin_p_home" in self.feature_names:
            feat += [0.33, 0.33, 0.33, 0.5, 0.5, 0.33, 0.33, 0.33]

        X = np.array([feat])
        probs = self.predict_proba(X)[0]
        return {"p_home": float(probs[0]), "p_draw": float(probs[1]),
                "p_away": float(probs[2]), "elo_diff": base["elo_diff"],
                "lam_h": base["lam_h"], "lam_a": base["lam_a"]}

    def save(self, path: Path) -> None:
        with open(path, "wb") as f:
            pickle.dump({
                "models": self.models, "feature_names": self.feature_names,
                "config": self.config, "trained": self.trained,
            }, f)

    def load(self, path: Path) -> None:
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.models = data["models"]
        self.feature_names = data["feature_names"]
        self.config = data["config"]
        self.trained = data["trained"]


def _tournament_stage(tournament: str) -> int:
    t = tournament.lower()
    if "friendly" in t: return 0
    if "qualif" in t: return 1
    if "world cup" in t and "qualif" not in t: return 3
    if any(x in t for x in ["euro", "copa america", "afcon", "asian cup", "gold cup"]):
        return 3 if "qualif" not in t else 1
    if "nations league" in t: return 2
    return 1


def _match_market_features(match: pd.Series, market_df: pd.DataFrame) -> dict:
    """Find matching market odds for an international match (best-effort)."""
    # International matches usually don't have direct market data in MatchHistory
    # Return defaults — this is a hook for future enrichment
    return {k: None for k in ["pin_p_home", "pin_p_draw", "pin_p_away",
                              "ah_home_cover", "ah_away_cover",
                              "odds_implied_home", "odds_implied_draw", "odds_implied_away"]}
