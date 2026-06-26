"""Fusion predictor: blends 4 models for VPS prediction.

Models:
- Elo+DC (w=0.20): base Elo+DC model
- LightGBM (w=0.25): gradient boosting
- PyTorch rich (w=0.45): deep model with 37 rich features (37 features)
- PyTorch simple (w=0.10): deep model with 9 simple features

Blend: weighted geometric mean of class probabilities, then renormalize.
"""

from __future__ import annotations

import json
import pickle
import sys
from math import exp, factorial
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

WC_DIR = Path(__file__).parent
DATA_DIR = WC_DIR / "data"
sys.path.insert(0, str(WC_DIR.parent))


class RichModel(nn.Module):
    """Matches train_rich.py architecture: 5-layer with BatchNorm + GELU."""
    def __init__(self, in_dim, hidden=512, n_classes=3, dropout=0.4):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, hidden), nn.GELU(), nn.BatchNorm1d(hidden), nn.Dropout(dropout),
            nn.Linear(hidden, hidden), nn.GELU(), nn.BatchNorm1d(hidden), nn.Dropout(dropout),
            nn.Linear(hidden, hidden // 2), nn.GELU(), nn.BatchNorm1d(hidden // 2), nn.Dropout(dropout),
            nn.Linear(hidden // 2, hidden // 4), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(hidden // 4, n_classes),
        )
    def forward(self, x):
        return self.net(x)


def load_pytorch_model(path: Path) -> dict:
    """Load PyTorch model. Try pickle first (rich model), then torch.load (simple model)."""
    try:
        return pickle.load(open(path, "rb"))
    except (pickle.UnpicklingError, UnicodeDecodeError):
        return torch.load(path, map_location="cpu", weights_only=False)


def _build_arch(in_dim: int) -> nn.Module:
    """Build the rich model architecture."""
    return RichModel(in_dim, hidden=512)


def pytorch_predict(model_dict: dict, X: np.ndarray) -> np.ndarray:
    """Run PyTorch model on pre-built feature matrix X."""
    mean = np.array(model_dict["mean"])
    std = np.array(model_dict["std"])
    x = torch.tensor((X - mean) / (std + 1e-8), dtype=torch.float32)

    state = model_dict["model_state"]
    in_dim = x.shape[1]
    model = _build_arch(in_dim)
    try:
        model.load_state_dict(state)
    except RuntimeError as e:
        raise RuntimeError(f"Cannot load model: {e}")

    model.eval()
    with torch.no_grad():
        logits = model(x)
        probs = F.softmax(logits, dim=1).numpy()
    return probs


def elo_dc_predict(elo_model, home: str, away: str, neutral: bool) -> np.ndarray:
    """Get P(H,D,A) from Elo+DC base model."""
    r_h = elo_model._get_or_init(home)
    r_a = elo_model._get_or_init(away)
    dr = r_h - r_a + (0 if neutral else 100)
    we = max(0.05, min(0.95, 1.0 / (1.0 + 10.0 ** (-dr / 400))))
    lam_h = max(0.25, 2.5 * we)
    lam_a = max(0.25, 2.5 * (1 - we))

    n = 8; rho = -0.20
    pmf_h = np.array([exp(-lam_h) * lam_h**k / factorial(k) for k in range(n)])
    pmf_a = np.array([exp(-lam_a) * lam_a**k / factorial(k) for k in range(n)])
    mat = np.outer(pmf_h, pmf_a)
    mat[0, 0] *= 1 - lam_h * lam_a * rho
    mat[1, 0] *= 1 + lam_a * rho
    mat[0, 1] *= 1 + lam_h * rho
    mat[1, 1] *= 1 - rho
    mat = np.maximum(mat, 0); mat /= mat.sum()

    p_home_dc = float(mat[np.tril_indices(n, -1)].sum())
    p_draw_dc = float(np.trace(mat))
    p_away_dc = float(mat[np.triu_indices(n, 1)].sum())

    total = p_home_dc + p_draw_dc + p_away_dc
    return np.array([p_home_dc / total, p_draw_dc / total, p_away_dc / total])


def lgbm_predict(lgbm_model: dict, X: np.ndarray) -> np.ndarray:
    """Get P(H,D,A) from LightGBM model."""
    probs = np.column_stack([
        lgbm_model["model"]["H"].predict_proba(X)[:, 1],
        lgbm_model["model"]["D"].predict_proba(X)[:, 1],
        lgbm_model["model"]["A"].predict_proba(X)[:, 1],
    ])
    return probs / probs.sum(axis=1, keepdims=True)


class FusionPredictor:
    """4-model ensemble: Elo+DC + LightGBM + PyTorch rich + PyTorch simple."""

    # Default weights (sum=1.0): Elo+DC 0.20, LGBM 0.25, Rich 0.45, Simple 0.10
    DEFAULT_WEIGHTS = np.array([0.20, 0.25, 0.45, 0.10])

    def __init__(self):
        self.elo_model = None
        self.lgbm_model = None
        self.pytorch_rich = None
        self.pytorch_simple = None
        self.weights = self.DEFAULT_WEIGHTS.copy()
        self.loaded = False
        self._matches_df = None

    def load_all(self, data_dir: Path = DATA_DIR):
        from wc_analysis.elo_model import EloModel

        df = pd.read_csv(data_dir.parent.parent / "data" / "international_results.csv")
        df["date"] = pd.to_datetime(df["date"])
        self._matches_df = df

        self.elo_model = EloModel()
        self.elo_model.fit(df)
        self._last_elo_date = df["date"].max()

        try:
            self.lgbm_model = pickle.load(open(data_dir / "model_lightgbm.pkl", "rb"))
        except Exception:
            pass

        try:
            self.pytorch_rich = load_pytorch_model(data_dir / "model_pytorch_rich.pt")
        except Exception:
            pass

        try:
            self.pytorch_simple = load_pytorch_model(data_dir / "model_pytorch.pt")
        except Exception:
            pass

        self.loaded = True
        return self

    def predict(self, home: str, away: str, neutral: bool = True,
                as_of_date: str | None = None) -> np.ndarray:
        """Return P(H,D,A) from fused ensemble."""
        if not self.loaded:
            raise RuntimeError("Call load_all() first")

        if as_of_date is None:
            as_of_date = str(self._last_elo_date.date())

        # Always rebuild Elo up to as_of_date (prevent leakage)
        from wc_analysis.elo_model import EloModel
        df = self._matches_df
        elo = EloModel()
        past = df[df["date"] < pd.Timestamp(as_of_date)]
        if len(past) > 0:
            elo.fit(past)

        # 1. Elo+DC base
        log_probs = np.zeros(3)
        total_w = 0.0
        try:
            p_dc = elo_dc_predict(elo, home, away, neutral)
            log_probs += self.weights[0] * np.log(np.maximum(p_dc, 0.01))
            total_w += self.weights[0]
        except Exception:
            pass

        # 2. LightGBM (9 simple features)
        if self.lgbm_model is not None:
            try:
                X_lgbm = _build_lgbm_features(elo, home, away, neutral)[None, :]
                p_lgbm = lgbm_predict(self.lgbm_model, X_lgbm)[0]
                log_probs += self.weights[1] * np.log(np.maximum(p_lgbm, 0.01))
                total_w += self.weights[1]
            except Exception:
                pass

        # 3. PyTorch rich (37 features)
        if self.pytorch_rich is not None:
            try:
                X_rich = _build_rich_features(elo, home, away, neutral,
                                             as_of_date, df)[None, :]
                p_rich = pytorch_predict(self.pytorch_rich, X_rich)[0]
                log_probs += self.weights[2] * np.log(np.maximum(p_rich, 0.01))
                total_w += self.weights[2]
            except Exception:
                pass

        # 4. PyTorch simple (9 simple features, like rich but different arch)
        if self.pytorch_simple is not None:
            try:
                X_simple = _build_simple_features(elo, home, away, neutral)[None, :]
                # Use different architecture for simple model
                p_simple = _pytorch_simple_predict(self.pytorch_simple, X_simple)[0]
                log_probs += self.weights[3] * np.log(np.maximum(p_simple, 0.01))
                total_w += self.weights[3]
            except Exception:
                pass

        if total_w < 1e-6:
            return np.array([0.33, 0.34, 0.33])

        log_p = log_probs / total_w
        p = np.exp(log_p - log_p.max())  # numerical stability
        return p / p.sum()


def _build_rich_features(elo_model, home: str, away: str, neutral: bool,
                        as_of_date: str, matches_df: pd.DataFrame) -> np.ndarray:
    """Build all 37 features for the rich model."""
    from wc_analysis.rich_features import compute_rich_features
    feat = compute_rich_features(home, away, neutral, as_of_date, matches_df, elo_model)
    cols = ["elo_diff", "elo_avg", "lam_diff", "lam_sum",
            "p_home_base", "p_draw_base", "p_away_base", "ah_home_minus_0_5",
            "fifa_pts_diff", "fifa_rank_diff", "p_win_pts_table", "p_draw_pts_table",
            "p_win_rank_table", "p_draw_rank_table", "status_score", "psychology",
            "coach_diff", "coach_home", "coach_away", "h2h_diff",
            "continental_bonus", "home_conf_UEFA", "away_conf_UEFA", "geo_advantage",
            "form_home", "form_away", "dark_horse", "squad_diff", "squad_home",
            "squad_away", "rest_diff", "rest_home", "rest_away", "dynamic_draw",
            "strength_diff", "tournament_stage", "neutral"]
    return np.array([[feat.get(c, 0) for c in cols]])


def _build_lgbm_features(elo_model, home: str, away: str, neutral: bool) -> np.ndarray:
    """Build 9 simple features for LightGBM."""
    r_h = elo_model._get_or_init(home)
    r_a = elo_model._get_or_init(away)
    dr = r_h - r_a + (0 if neutral else 100)
    we = max(0.05, min(0.95, 1.0 / (1.0 + 10.0 ** (-dr / 400))))
    lam_h = max(0.25, 2.5 * we)
    lam_a = max(0.25, 2.5 * (1 - we))

    n = 8; rho = -0.20
    pmf_h = np.array([exp(-lam_h) * lam_h**k / factorial(k) for k in range(n)])
    pmf_a = np.array([exp(-lam_a) * lam_a**k / factorial(k) for k in range(n)])
    mat = np.outer(pmf_h, pmf_a)
    mat[0, 0] *= 1 - lam_h * lam_a * rho
    mat[1, 0] *= 1 + lam_a * rho
    mat[0, 1] *= 1 + lam_h * rho
    mat[1, 1] *= 1 - rho
    mat = np.maximum(mat, 0); mat /= mat.sum()
    p_home = float(mat[np.tril_indices(n, -1)].sum())
    p_draw = float(np.trace(mat))
    p_away = float(mat[np.triu_indices(n, 1)].sum())
    total = p_home + p_draw + p_away
    return np.array([[r_h - r_a, (r_h + r_a) / 2, p_home / total, p_draw / total,
                     p_away / total, lam_h, lam_a, int(neutral), 3]])


def _build_simple_features(elo_model, home: str, away: str, neutral: bool) -> np.ndarray:
    """Build 9 simple features for the old PyTorch model."""
    r_h = elo_model._get_or_init(home)
    r_a = elo_model._get_or_init(away)
    dr = r_h - r_a + (0 if neutral else 100)
    we = max(0.05, min(0.95, 1.0 / (1.0 + 10.0 ** (-dr / 400))))
    lam_h = max(0.25, 2.5 * we)
    lam_a = max(0.25, 2.5 * (1 - we))
    n = 8; rho = -0.20
    pmf_h = np.array([exp(-lam_h) * lam_h**k / factorial(k) for k in range(n)])
    pmf_a = np.array([exp(-lam_a) * lam_a**k / factorial(k) for k in range(n)])
    mat = np.outer(pmf_h, pmf_a)
    mat[0, 0] *= 1 - lam_h * lam_a * rho
    mat[1, 0] *= 1 + lam_a * rho
    mat[0, 1] *= 1 + lam_h * rho
    mat[1, 1] *= 1 - rho
    mat = np.maximum(mat, 0); mat /= mat.sum()
    ah = float(mat[np.tril_indices(n, -1)].sum() + 0.5 * float(np.trace(mat)))
    return np.array([[r_h - r_a, (r_h + r_a) / 2, lam_h - lam_a, lam_h + lam_a,
                     int(neutral), ah, 3, 0, 0]])


def _pytorch_simple_predict(model_dict: dict, X: np.ndarray) -> np.ndarray:
    """Run simple PyTorch model with its own architecture."""
    mean = np.array(model_dict["mean"])
    std = np.array(model_dict["std"])
    x = torch.tensor((X - mean) / (std + 1e-8), dtype=torch.float32)
    state = model_dict["model_state"]
    in_dim = x.shape[1]

    class SimpleModel(nn.Module):
        def __init__(self, in_dim, hidden=256):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(in_dim, hidden), nn.ReLU(), nn.Dropout(0.3),
                nn.Linear(hidden, hidden), nn.ReLU(), nn.Dropout(0.3),
                nn.Linear(hidden, hidden // 2), nn.ReLU(), nn.Dropout(0.3),
                nn.Linear(hidden // 2, 3),
            )
        def forward(self, x):
            return self.net(x)

    model = SimpleModel(in_dim)
    try:
        model.load_state_dict(state)
    except RuntimeError:
        # Try as RichModel
        model = RichModel(in_dim, hidden=256)
        model.load_state_dict(state)
    model.eval()
    with torch.no_grad():
        logits = model(x)
        probs = F.softmax(logits, dim=1).numpy()
    return probs


if __name__ == "__main__":
    fusion = FusionPredictor().load_all()
    print("✅ Fusion predictor loaded (4 models)")
    print(f"   Weights: Elo+DC={fusion.weights[0]:.2f} "
          f"LGBM={fusion.weights[1]:.2f} "
          f"Rich={fusion.weights[2]:.2f} "
          f"Simple={fusion.weights[3]:.2f}")
    print(f"   Available: Elo+DC=✓ LGBM={'✓' if fusion.lgbm_model else '✗'} "
          f"Rich={'✓' if fusion.pytorch_rich else '✗'} "
          f"Simple={'✓' if fusion.pytorch_simple else '✗'}")
    for h, a in [("Brazil", "Argentina"), ("Japan", "Sweden"),
                 ("France", "Morocco"), ("England", "Germany")]:
        p = fusion.predict(h, a)
        print(f"  {h:12s} vs {a:12s}: {p[0]:.1%}/{p[1]:.1%}/{p[2]:.1%}")
