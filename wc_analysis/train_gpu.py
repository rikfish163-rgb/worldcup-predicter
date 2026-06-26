#!/usr/bin/env python3
"""4090 GPU training script for the ensemble model.

Run on the 4090 box via SSH:
    ssh user@4090-host 'cd ~/soccerdata && python wc_analysis/train_gpu.py --epochs 1000'

Uses LightGBM GPU backend + optional PyTorch neural net for deeper ensemble.
The 4090's 24GB VRAM allows larger feature sets and neural architectures.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, early_stopping, log_evaluation
from sklearn.metrics import accuracy_score, brier_score_loss

from wc_analysis.ensemble_model import EnsembleModel
from wc_analysis.draw_correction import DrawCorrection
from wc_analysis.backtest_v2 import brier_score, log_loss, rps, actual_result, smart_argmax, top3_hit


def train_gpu_lightgbm(features_df: pd.DataFrame, use_gpu: bool = True) -> dict:
    """Train LightGBM with GPU acceleration."""
    base_features = ["elo_diff", "elo_avg", "p_home_base", "p_draw_base",
                     "p_away_base", "lam_h", "lam_a", "neutral", "tournament_stage"]

    X = features_df[base_features].values
    y = features_df["actual"].values

    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    params = {
        "n_estimators": 1000,  # More estimators for GPU
        "learning_rate": 0.02,  # Lower LR for better convergence
        "max_depth": 7,
        "num_leaves": 63,
        "min_child_samples": 30,
        "reg_alpha": 0.1,
        "reg_lambda": 1.0,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "verbose": -1, "n_jobs": -1,
    }
    if use_gpu:
        params["device"] = "gpu"
        params["gpu_platform_id"] = 0
        params["gpu_device_id"] = 0

    models = {}
    classes = ["H", "D", "A"]
    for cls in classes:
        print(f"  Training {cls} classifier...")
        y_bin = (y_tr == cls).astype(int)
        y_val_bin = (y_val == cls).astype(int)
        model = LGBMClassifier(**params, objective="binary")
        t0 = time.time()
        model.fit(X_tr, y_bin, eval_set=[(X_val, y_val_bin)],
                  callbacks=[early_stopping(30), log_evaluation(100)])
        t1 = time.time()
        print(f"    Done in {t1-t0:.1f}s (best iter: {model.best_iteration_})")
        models[cls] = model

    # Validation
    probs = np.column_stack([
        models["H"].predict_proba(X_val)[:, 1],
        models["D"].predict_proba(X_val)[:, 1],
        models["A"].predict_proba(X_val)[:, 1],
    ])
    probs = probs / probs.sum(axis=1, keepdims=True)
    val_pred = np.array([classes[i] for i in np.argmax(probs, axis=1)])
    val_acc = accuracy_score(y_val, val_pred)
    val_brier = np.mean([
        brier_score_loss((y_val == c).astype(int), probs[:, i])
        for i, c in enumerate(classes)
    ])

    return {"models": models, "val_accuracy": val_acc, "val_brier": val_brier,
            "features": base_features, "n_train": len(X_tr)}


def train_pytorch_neural_net(features_df: pd.DataFrame, epochs: int = 200,
                              hidden_dim: int = 128, use_gpu: bool = True) -> dict:
    """Optional PyTorch neural net for deeper ensemble."""
    try:
        import torch
        import torch.nn as nn
        from torch.utils.data import DataLoader, TensorDataset
    except ImportError:
        print("PyTorch not installed, skipping neural net")
        return {}

    device = torch.device("cuda" if use_gpu and torch.cuda.is_available() else "cpu")
    print(f"  Using device: {device}")

    base_features = ["elo_diff", "elo_avg", "p_home_base", "p_draw_base",
                     "p_away_base", "lam_h", "lam_a", "neutral", "tournament_stage"]
    X = features_df[base_features].values.astype(np.float32)
    y_map = {"H": 0, "D": 1, "A": 2}
    y = np.array([y_map[a] for a in features_df["actual"].values])

    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    # Normalize
    mean, std = X_tr.mean(0), X_tr.std(0) + 1e-8
    X_tr = (X_tr - mean) / std
    X_val = (X_val - mean) / std

    train_ds = TensorDataset(torch.tensor(X_tr), torch.tensor(y_tr, dtype=torch.long))
    train_dl = DataLoader(train_ds, batch_size=512, shuffle=True)

    class MatchNet(nn.Module):
        def __init__(self, in_dim, hidden, n_classes=3):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(in_dim, hidden), nn.ReLU(), nn.Dropout(0.3),
                nn.Linear(hidden, hidden), nn.ReLU(), nn.Dropout(0.3),
                nn.Linear(hidden, n_classes),
            )
        def forward(self, x):
            return self.net(x)

    model = MatchNet(len(base_features), hidden_dim).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-4)
    criterion = nn.CrossEntropyLoss()

    best_val_brier = 1.0
    for epoch in range(epochs):
        model.train()
        for xb, yb in train_dl:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            optimizer.step()

        if epoch % 20 == 0:
            model.eval()
            with torch.no_grad():
                logits = model(torch.tensor(X_val, device=device))
                probs = torch.softmax(logits, dim=1).cpu().numpy()
            val_pred = np.argmax(probs, axis=1)
            val_acc = (val_pred == y_val).mean()
            val_brier = np.mean([
                brier_score_loss((y_val == i).astype(int), probs[:, i])
                for i in range(3)
            ])
            print(f"    Epoch {epoch}: acc={val_acc:.3f} brier={val_brier:.4f}")
            if val_brier < best_val_brier:
                best_val_brier = val_brier
                best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    return {"val_brier": best_val_brier, "mean": mean.tolist(), "std": std.tolist()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--no-gpu", action="store_true")
    parser.add_argument("--neural-net", action="store_true")
    args = parser.parse_args()

    import pandas as pd
    df = pd.read_csv("data/international_results.csv")
    df["date"] = pd.to_datetime(df["date"])

    dc = DrawCorrection()
    dc.load(Path("wc_analysis/data/draw_correction_v2.json"))

    ensemble = EnsembleModel()
    print("=== Building features ===")
    features = ensemble.build_features(df, dc=dc, start_year=1990)
    print(f"Built {len(features)} rows")

    print("\n=== Training LightGBM (GPU) ===")
    t0 = time.time()
    lgbm_result = train_gpu_lightgbm(features, use_gpu=not args.no_gpu)
    t1 = time.time()
    print(f"LightGBM done in {t1-t0:.1f}s")
    print(f"  Val accuracy: {lgbm_result['val_accuracy']:.1%}")
    print(f"  Val brier:    {lgbm_result['val_brier']:.4f}")

    if args.neural_net:
        print("\n=== Training PyTorch neural net ===")
        nn_result = train_pytorch_neural_net(features, epochs=args.epochs,
                                             use_gpu=not args.no_gpu)
        if nn_result:
            print(f"  NN best val brier: {nn_result['val_brier']:.4f}")

    # Save
    ensemble.models = lgbm_result["models"]
    ensemble.feature_names = lgbm_result["features"]
    ensemble.trained = True
    ensemble.save(Path("wc_analysis/data/ensemble_model_gpu.pkl"))
    print("\n✅ Saved ensemble_model_gpu.pkl")


if __name__ == "__main__":
    main()
