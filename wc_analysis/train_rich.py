"""Rich feature training using all reference system factors.

Trains a PyTorch model on the 4090 with 37 features extracted from:
- Elo base + Dixon-Coles
- FIFA 5/8档查表
- 状态/教练/20年战绩/大洲/地理/黑马/梯队
- 休息天数/动态平局/赛事阶段

Usage:
    python wc_analysis/train_rich.py --epochs 500
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

WC_DIR = Path(__file__).parent
DATA_DIR = WC_DIR / "data"
sys.path.insert(0, str(WC_DIR.parent))

from wc_analysis.elo_model import EloModel
from wc_analysis.rich_features import compute_rich_features


class RichMatchPredictor(nn.Module):
    """Deep model for rich feature set."""
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


def build_rich_dataset(matches_df: pd.DataFrame, start_year: int = 1990) -> pd.DataFrame:
    """Build full feature matrix for all matches using compute_rich_features."""
    df = matches_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"].dt.year >= start_year]
    df = df.dropna(subset=["home_score", "away_score"])
    df = df.drop_duplicates(subset=["date", "home_team", "away_team"])
    df = df.sort_values("date").reset_index(drop=True)

    elo = EloModel()
    pre = df[df["date"].dt.year < start_year]
    if len(pre) > 0:
        elo.fit(pre)

    rows = []
    for i, m in df.iterrows():
        as_of = str(m["date"].date())
        try:
            feat = compute_rich_features(
                m["home_team"], m["away_team"],
                neutral=bool(m["neutral"]),
                as_of_date=as_of, matches_df=df, elo_model=elo,
            )
        except Exception as e:
            continue
        hs, as_ = int(m["home_score"]), int(m["away_score"])
        feat["date"] = as_of
        feat["home"] = m["home_team"]
        feat["away"] = m["away_team"]
        feat["actual"] = "H" if hs > as_ else ("D" if hs == as_ else "A")
        rows.append(feat)
        # Update Elo AFTER computing features
        elo.update_match(m["home_team"], m["away_team"], hs, as_,
                         m["tournament"], bool(m["neutral"]))

    return pd.DataFrame(rows)


def train_rich_model(features_df: pd.DataFrame, epochs: int = 500,
                     use_gpu: bool = True, batch_size: int = 1024) -> dict:
    """Train RichMatchPredictor on rich features."""
    feature_cols = [c for c in features_df.columns
                    if c not in ["date", "home", "away", "actual"]]
    X = features_df[feature_cols].values.astype(np.float32)
    y_map = {"H": 0, "D": 1, "A": 2}
    y = np.array([y_map[a] for a in features_df["actual"].values])

    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    mean, std = X_tr.mean(0), X_tr.std(0) + 1e-8
    X_tr = (X_tr - mean) / std
    X_val = (X_val - mean) / std

    device = torch.device("cuda" if use_gpu and torch.cuda.is_available() else "cpu")
    print(f"  Device: {device}")

    train_ds = TensorDataset(torch.tensor(X_tr), torch.tensor(y_tr, dtype=torch.long))
    train_dl = DataLoader(train_ds, batch_size=batch_size, shuffle=True)

    model = RichMatchPredictor(len(feature_cols), hidden=512).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=2e-3,
        total_steps=len(train_dl) * epochs,
    )
    criterion = nn.CrossEntropyLoss(label_smoothing=0.05)

    best_brier = 1.0
    best_state = None
    best_epoch = 0
    best_acc = 0.0
    best_class_acc = {}

    from sklearn.metrics import brier_score_loss

    print(f"  Training: {len(X_tr)} samples, {X.shape[1]} features, {epochs} epochs")
    for epoch in range(epochs):
        model.train()
        for xb, yb in train_dl:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()

        if (epoch + 1) % 20 == 0 or epoch == epochs - 1:
            model.eval()
            with torch.no_grad():
                logits = model(torch.tensor(X_val, device=device))
                probs = F.softmax(logits, dim=1).cpu().numpy()
            val_pred = np.argmax(probs, axis=1)
            val_acc = (val_pred == y_val).mean()
            val_brier = np.mean([
                brier_score_loss((y_val == i).astype(int), probs[:, i])
                for i in range(3)
            ])
            class_acc = {c: float((val_pred[y_val == i] == i).mean()) if (y_val == i).any() else 0
                         for i, c in enumerate(["H", "D", "A"])}
            print(f"  Epoch {epoch+1:3d}: acc={val_acc:.4f} brier={val_brier:.4f} | "
                  f"H={class_acc['H']:.2f} D={class_acc['D']:.2f} A={class_acc['A']:.2f}")
            if val_brier < best_brier:
                best_brier = val_brier
                best_acc = val_acc
                best_epoch = epoch + 1
                best_class_acc = class_acc
                best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    print(f"\nBest: epoch={best_epoch} brier={best_brier:.4f} acc={best_acc:.4f}")
    print(f"  Class accuracy: H={best_class_acc.get('H', 0):.2f} "
          f"D={best_class_acc.get('D', 0):.2f} A={best_class_acc.get('A', 0):.2f}")

    return {
        "model_state": best_state, "mean": mean.tolist(), "std": std.tolist(),
        "feature_cols": feature_cols, "val_accuracy": best_acc, "val_brier": best_brier,
        "best_epoch": best_epoch, "class_acc": best_class_acc,
        "n_train": len(X_tr), "n_val": len(X_val),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=500)
    parser.add_argument("--no-gpu", action="store_true")
    parser.add_argument("--start-year", type=int, default=1990)
    parser.add_argument("--out", default="model_pytorch_rich.pt")
    args = parser.parse_args()

    print("=" * 60)
    print("RICH FEATURE MODEL TRAINING")
    print("=" * 60)

    df = pd.read_csv("data/international_results.csv")
    df["date"] = pd.to_datetime(df["date"])

    print(f"\n=== Building rich features (post-{args.start_year}) ===")
    t0 = time.time()
    features = build_rich_dataset(df, start_year=args.start_year)
    print(f"Built {len(features)} rows in {time.time()-t0:.1f}s")
    print(f"Feature count: {len([c for c in features.columns if c not in ['date','home','away','actual']])}")
    print(f"Actual distribution: {features['actual'].value_counts().to_dict()}")

    print(f"\n=== Training (epochs={args.epochs}) ===")
    t0 = time.time()
    result = train_rich_model(features, epochs=args.epochs, use_gpu=not args.no_gpu)
    print(f"Trained in {time.time()-t0:.1f}s")

    out_path = DATA_DIR / args.out
    with open(out_path, "wb") as f:
        pickle.dump(result, f)
    print(f"\n✅ Saved to {out_path}")


if __name__ == "__main__":
    main()
