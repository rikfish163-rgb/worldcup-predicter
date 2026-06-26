#!/usr/bin/env python3
"""RL-style training on GPU using conda env rl_env.

Designed for local machine with:
    conda activate rl_env
    python wc_analysis/train_rl_env.py

Uses PyTorch + LightGBM GPU + comprehensive features.
Self-evolving: each run pulls latest data, retrains, deploys to VPS.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

WC_DIR = Path("wc_analysis")
DATA_DIR = WC_DIR / "data"


def detect_gpu() -> dict:
    """Detect GPU availability."""
    try:
        import torch
        info = {
            "cuda_available": torch.cuda.is_available(),
            "device_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
        }
        if info["cuda_available"]:
            info["device_name"] = torch.cuda.get_device_name(0)
            info["vram_gb"] = torch.cuda.get_device_properties(0).total_memory / 1e9
        return info
    except ImportError:
        return {"cuda_available": False, "torch": "not installed"}


def build_comprehensive_features(matches_df: pd.DataFrame, fifa_data: dict | None = None) -> pd.DataFrame:
    """Build full feature matrix for training. Vectorized for speed."""
    from wc_analysis.elo_model import EloModel

    df = matches_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["home_score", "away_score"])
    df = df.drop_duplicates(subset=["date", "home_team", "away_team"])
    df = df[df["date"].dt.year >= 1990].sort_values("date").reset_index(drop=True)

    elo = EloModel()
    features_list = []

    # Pre-compute FIFA lookups
    home_pts = np.array([(fifa_data or {}).get("points", {}).get(t, 1500)
                         for t in df["home_team"]])
    away_pts = np.array([(fifa_data or {}).get("points", {}).get(t, 1500)
                         for t in df["away_team"]])
    home_rank = np.array([(fifa_data or {}).get("ranks", {}).get(t, 50)
                         for t in df["home_team"]])
    away_rank = np.array([(fifa_data or {}).get("ranks", {}).get(t, 50)
                         for t in df["away_team"]])

    for i, m in df.iterrows():
        home, away = m["home_team"], m["away_team"]
        r_h = elo._get_or_init(home)
        r_a = elo._get_or_init(away)

        dr = r_h - r_a + (100 if not m["neutral"] else 0)
        we = max(0.05, min(0.95, 1.0 / (1.0 + 10.0 ** (-dr / 400))))
        lam_h = max(0.25, 2.5 * we)
        lam_a = max(0.25, 2.5 * (1 - we))

        # 1D Poisson PMF (vectorized over goals)
        from math import exp, factorial
        n = 8
        pmf_h = np.array([exp(-lam_h) * lam_h**k / factorial(k) for k in range(n)])
        pmf_a = np.array([exp(-lam_a) * lam_a**k / factorial(k) for k in range(n)])
        mat = np.outer(pmf_h, pmf_a)
        # DC low-score correction
        rho = -0.20
        mat[0, 0] *= 1 - lam_h * lam_a * rho
        mat[1, 0] *= 1 + lam_a * rho
        mat[0, 1] *= 1 + lam_h * rho
        mat[1, 1] *= 1 - rho
        mat = np.maximum(mat, 0)
        mat /= mat.sum()

        ah_home_minus_0_5 = float(mat[np.tril_indices(n, -1)].sum() + 0.5 * mat[0, 0].sum())

        hs, as_ = int(m["home_score"]), int(m["away_score"])
        feat = {
            "date": str(m["date"].date()),
            "home": home, "away": away,
            "elo_diff": r_h - r_a, "elo_avg": (r_h + r_a) / 2,
            "lam_diff": lam_h - lam_a, "lam_sum": lam_h + lam_a,
            "neutral": int(m["neutral"]),
            "fifa_pts_diff": home_pts[i] - away_pts[i],
            "fifa_rank_diff": away_rank[i] - home_rank[i],
            "ah_home_minus_0_5": ah_home_minus_0_5,
            "tournament_stage": _tournament_stage(m.get("tournament", "")),
            "actual": "H" if hs > as_ else ("D" if hs == as_ else "A"),
            "home_score": hs, "away_score": as_,
        }
        features_list.append(feat)

        elo.update_match(home, away, hs, as_, m["tournament"], bool(m["neutral"]))

    return pd.DataFrame(features_list)


def _tournament_stage(t: str) -> int:
    t = t.lower()
    if "friendly" in t: return 0
    if "qualif" in t: return 1
    if "world cup" in t and "qualif" not in t: return 3
    return 2


class DeepMatchPredictor(nn.Module if False else object):
    """Placeholder for PyTorch model. Real impl in train_pytorch below."""
    pass


def train_pytorch(features_df: pd.DataFrame, epochs: int = 200,
                  use_gpu: bool = True) -> dict:
    """Train PyTorch neural net for match outcome prediction."""
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    feature_cols = [c for c in features_df.columns
                    if c not in ["date", "home", "away", "actual",
                                 "home_score", "away_score"]]
    X = features_df[feature_cols].values.astype(np.float32)
    y_map = {"H": 0, "D": 1, "A": 2}
    y = np.array([y_map[a] for a in features_df["actual"].values])

    # Time split
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    # Normalize
    mean, std = X_tr.mean(0), X_tr.std(0) + 1e-8
    X_tr = (X_tr - mean) / std
    X_val = (X_val - mean) / std

    device = torch.device("cuda" if use_gpu and torch.cuda.is_available() else "cpu")
    print(f"  Device: {device}")

    train_ds = TensorDataset(torch.tensor(X_tr), torch.tensor(y_tr, dtype=torch.long))
    train_dl = DataLoader(train_ds, batch_size=512, shuffle=True)

    class MatchPredictor(nn.Module):
        def __init__(self, in_dim, hidden=256, n_classes=3, dropout=0.3):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(in_dim, hidden), nn.ReLU(), nn.Dropout(dropout),
                nn.Linear(hidden, hidden), nn.ReLU(), nn.Dropout(dropout),
                nn.Linear(hidden, hidden // 2), nn.ReLU(), nn.Dropout(dropout),
                nn.Linear(hidden // 2, n_classes),
            )
        def forward(self, x):
            return self.net(x)

    model = MatchPredictor(len(feature_cols), hidden=256).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.CrossEntropyLoss(label_smoothing=0.05)

    best_brier = 1.0
    best_state = None

    print(f"  Training {epochs} epochs, {len(X_tr)} samples, {X.shape[1]} features...")
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        for xb, yb in train_dl:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            logits = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total_loss += loss.item() * xb.size(0)
        scheduler.step()

        if epoch % 20 == 0 or epoch == epochs - 1:
            model.eval()
            with torch.no_grad():
                logits = model(torch.tensor(X_val, device=device))
                probs = torch.softmax(logits, dim=1).cpu().numpy()
            val_pred = np.argmax(probs, axis=1)
            val_acc = (val_pred == y_val).mean()
            from sklearn.metrics import brier_score_loss
            val_brier = np.mean([
                brier_score_loss((y_val == i).astype(int), probs[:, i])
                for i in range(3)
            ])
            print(f"    Epoch {epoch:3d}: loss={total_loss/len(X_tr):.4f} acc={val_acc:.3f} brier={val_brier:.4f}")
            if val_brier < best_brier:
                best_brier = val_brier
                best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    # Final eval
    model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        logits = model(torch.tensor(X_val, device=device))
        probs = torch.softmax(logits, dim=1).cpu().numpy()
    val_pred = np.argmax(probs, axis=1)
    val_acc = (val_pred == y_val).mean()
    from sklearn.metrics import brier_score_loss
    val_brier = np.mean([
        brier_score_loss((y_val == i).astype(int), probs[:, i])
        for i in range(3)
    ])

    return {
        "model_state": best_state,
        "mean": mean.tolist(), "std": std.tolist(),
        "feature_cols": feature_cols,
        "val_accuracy": float(val_acc), "val_brier": float(val_brier),
        "n_train": len(X_tr), "n_val": len(X_val),
    }


def save_artifact(result: dict, path: Path) -> None:
    """Save PyTorch model artifact."""
    import torch
    model_state = result["model_state"]
    mean = torch.tensor(result["mean"])
    std = torch.tensor(result["std"])

    artifact = {
        "model_state": model_state,
        "mean": mean, "std": std,
        "feature_cols": result["feature_cols"],
        "val_accuracy": result["val_accuracy"],
        "val_brier": result["val_brier"],
    }
    torch.save(artifact, path)
    print(f"  Saved to {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--no-gpu", action="store_true")
    parser.add_argument("--use-lightgbm", action="store_true",
                        help="Use LightGBM CPU instead of PyTorch (faster)")
    args = parser.parse_args()

    print("=" * 60)
    print("RL ENV TRAINING (conda activate rl_env)")
    print("=" * 60)

    # Detect GPU
    gpu_info = detect_gpu()
    print(f"\nGPU info: {gpu_info}")
    use_gpu = gpu_info.get("cuda_available", False) and not args.no_gpu

    # Build features
    print("\n=== Building features ===")
    # Need to add parent dir to path for wc_analysis imports
    import sys
    if "." not in sys.path:
        sys.path.insert(0, ".")
    df = pd.read_csv("data/international_results.csv")
    df["date"] = pd.to_datetime(df["date"])
    # Sample last 10 years for speed
    cutoff = pd.Timestamp("2015-01-01")
    df = df[df["date"] >= cutoff].reset_index(drop=True)
    print(f"Using {len(df)} matches since {cutoff.date()}")

    # Load FIFA data if available
    fifa_data = {}
    try:
        rankings = json.loads((DATA_DIR / "fifa_rankings.json").read_text())
        fifa_data = rankings
    except Exception:
        pass

    t0 = time.time()
    features = build_comprehensive_features(df, fifa_data)
    print(f"Built {len(features)} rows in {time.time()-t0:.1f}s")

    if args.use_lightgbm:
        from wc_analysis.xg_training import train_xg_model
        print("\n=== Training LightGBM ===")
        t0 = time.time()
        result = train_xg_model(features, use_gpu=use_gpu)
        print(f"Trained in {time.time()-t0:.1f}s")
        with open(DATA_DIR / "model_lightgbm.pkl", "wb") as f:
            pickle.dump(result, f)
        print(f"Saved to {DATA_DIR / 'model_lightgbm.pkl'}")
        print(f"Val accuracy: {result['val_accuracy']:.1%}")
        print(f"Val brier:    {result['val_brier']:.4f}")
    else:
        print("\n=== Training PyTorch neural net ===")
        t0 = time.time()
        result = train_pytorch(features, epochs=args.epochs, use_gpu=use_gpu)
        print(f"\nTrained in {time.time()-t0:.1f}s")
        print(f"Val accuracy: {result['val_accuracy']:.1%}")
        print(f"Val brier:    {result['val_brier']:.4f}")
        save_artifact(result, DATA_DIR / "model_pytorch.pt")

    print("\n✅ Done. Run `python wc_analysis/self_evolving_loop.py` to deploy.")


if __name__ == "__main__":
    main()
