"""Simplified draw logistic regression — 3 features trained on martj42 data.

Per @oracle: the existing draw_model.json requires team-specific features
unavailable in martj42. Train a simpler model on Elo_diff, avg_Elo, Elo_diff².

Usage:
    from wc_analysis.draw_correction import DrawCorrection
    dc = DrawCorrection()
    dc.train(matches_df)  # train on historical data
    p_draw = dc.predict(elo_diff, avg_elo)
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from wc_analysis.elo_model import EloModel


class DrawCorrection:
    """3-feature logistic regression for draw probability."""

    FEATURES = ["elo_diff", "avg_elo", "elo_diff_sq"]

    def __init__(self):
        self.w = np.zeros(3)
        self.b = 0.0
        self.mu = np.zeros(3)
        self.sigma = np.ones(3)
        self.base_draw_rate = 0.26
        self.trained = False

    def _sigmoid(self, z: float) -> float:
        return 1.0 / (1.0 + np.exp(-z))

    def _extract_features(self, elo_diff: float, avg_elo: float) -> np.ndarray:
        return np.array([elo_diff, avg_elo, elo_diff ** 2])

    def train(self, matches_df: pd.DataFrame, start_year: int = 1990,
              data_dir: Path | None = None) -> dict:
        """Train on historical match data with Elo features."""
        df = matches_df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"].dt.year >= start_year]
        df = df.dropna(subset=["home_score", "away_score"])
        df = df.drop_duplicates(subset=["date", "home_team", "away_team"])
        df = df[(df["home_score"] >= 0) & (df["away_score"] >= 0)]
        df = df.sort_values("date").reset_index(drop=True)

        elo = EloModel()
        X, y = [], []
        for _, m in df.iterrows():
            r_h = elo._get_or_init(m["home_team"])
            r_a = elo._get_or_init(m["away_team"])
            elo_diff = r_h - r_a
            avg_elo = (r_h + r_a) / 2
            X.append([elo_diff, avg_elo, elo_diff ** 2])
            y.append(1 if m["home_score"] == m["away_score"] else 0)
            elo.update_match(m["home_team"], m["away_team"],
                             int(m["home_score"]), int(m["away_score"]),
                             m["tournament"], bool(m["neutral"]))

        X = np.array(X)
        y = np.array(y)
        self.mu = X.mean(axis=0)
        self.sigma = X.std(axis=0) + 1e-8
        Xn = (X - self.mu) / self.sigma

        # Gradient descent logistic regression
        lr = 0.1
        n_iter = 500
        n = len(y)
        self.w = np.zeros(3)
        self.b = 0.0
        for _ in range(n_iter):
            z = Xn @ self.w + self.b
            p = self._sigmoid_vec(z)
            grad_w = Xn.T @ (p - y) / n
            grad_b = (p - y).mean()
            self.w -= lr * grad_w
            self.b -= lr * grad_b

        self.base_draw_rate = float(y.mean())
        self.trained = True

        # Training accuracy
        z = Xn @ self.w + self.b
        p_train = self._sigmoid_vec(z)
        train_acc = ((p_train > 0.5).astype(int) == y).mean()

        return {
            "n_train": len(y),
            "draw_rate": self.base_draw_rate,
            "train_accuracy": float(train_acc),
            "w": self.w.tolist(),
            "b": float(self.b),
            "mu": self.mu.tolist(),
            "sigma": self.sigma.tolist(),
        }

    def _sigmoid_vec(self, z: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-z))

    def predict(self, elo_diff: float, avg_elo: float) -> float:
        """Predict P(draw) given Elo features."""
        if not self.trained:
            return self.base_draw_rate
        feat = self._extract_features(elo_diff, avg_elo)
        feat_n = (feat - self.mu) / self.sigma
        z = float(feat_n @ self.w + self.b)
        return self._sigmoid(z)

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps({
            "w": self.w.tolist(), "b": float(self.b),
            "mu": self.mu.tolist(), "sigma": self.sigma.tolist(),
            "base_draw_rate": self.base_draw_rate,
            "features": self.FEATURES, "trained": self.trained,
        }, indent=2))

    def load(self, path: Path) -> None:
        data = json.loads(Path(path).read_text())
        self.w = np.array(data["w"])
        self.b = data["b"]
        self.mu = np.array(data["mu"])
        self.sigma = np.array(data["sigma"])
        self.base_draw_rate = data["base_draw_rate"]
        self.trained = data["trained"]
