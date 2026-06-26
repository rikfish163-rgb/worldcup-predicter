"""xG-enriched training pipeline.

Uses Understat club data (5330 matches, 100% xG) + MatchHistory Pinnacle odds
to train the LightGBM ensemble with richer features than the international-only model.

This is the pipeline to run on the 4090 for full training.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, early_stopping, log_evaluation
from sklearn.metrics import accuracy_score, brier_score_loss

from wc_analysis.elo_model import EloModel


def build_xg_enriched_features(
    understat_path: Path = Path("data/understat_enriched/understat_all.csv"),
    matchhistory_dir: Path = Path("data/MatchHistory"),
) -> pd.DataFrame:
    """Build feature matrix from Understat xG + MatchHistory odds."""

    # 1. Load Understat xG data
    xg_df = pd.read_csv(understat_path)
    xg_df["date"] = pd.to_datetime(xg_df["date"])
    xg_df = xg_df.sort_values("date").reset_index(drop=True)

    # 2. Compute rolling xG features per team
    # Reshape to long format for rolling computation
    home = xg_df[["date", "home", "home_score", "away_score", "home_xg", "away_xg",
                  "home_np_xg", "away_np_xg", "home_xpts", "away_xpts",
                  "home_ppda", "away_ppda", "league"]].rename(columns={
        "home": "team", "home_score": "gf", "away_score": "ga",
        "home_xg": "xg_for", "away_xg": "xg_against",
        "home_np_xg": "np_xg_for", "away_np_xg": "np_xg_against",
        "home_xpts": "xpts_for", "away_xpts": "xpts_against",
        "home_ppda": "ppda_def", "away_ppda": "ppda_atk",
    })
    home["venue"] = "H"

    away = xg_df[["date", "away", "away_score", "home_score", "away_xg", "home_xg",
                  "away_np_xg", "home_np_xg", "away_xpts", "home_xpts",
                  "away_ppda", "home_ppda", "league"]].rename(columns={
        "away": "team", "away_score": "gf", "home_score": "ga",
        "away_xg": "xg_for", "home_xg": "xg_against",
        "away_np_xg": "np_xg_for", "home_np_xg": "np_xg_against",
        "away_xpts": "xpts_for", "home_xpts": "xpts_against",
        "away_ppda": "ppda_def", "home_ppda": "ppda_atk",
    })
    away["venue"] = "A"

    long = pd.concat([home, away], ignore_index=True).sort_values(["team", "date"])

    # Rolling features
    for w in [5, 10, 20]:
        long[f"xg_diff_{w}"] = long.groupby("team")["xg_for"].transform(
            lambda x: x.rolling(w, min_periods=1).mean()) - long.groupby("team")["xg_against"].transform(
            lambda x: x.rolling(w, min_periods=1).mean())
        long[f"xg_for_{w}"] = long.groupby("team")["xg_for"].transform(
            lambda x: x.rolling(w, min_periods=1).mean())
        long[f"ppda_atk_{w}"] = long.groupby("team")["ppda_atk"].transform(
            lambda x: x.rolling(w, min_periods=1).mean())
        long[f"form_{w}"] = long.groupby("team").apply(
            lambda g: (g["gf"] > g["ga"]).astype(int).rolling(w, min_periods=1).mean()
        ).reset_index(level=0, drop=True)

    # 3. Build match-level features
    features = []
    for _, m in xg_df.iterrows():
        h = m["home"]; a = m["away"]
        d = m["date"]

        h_past = long[(long["team"] == h) & (long["date"] < d)].tail(20)
        a_past = long[(long["team"] == a) & (long["date"] < d)].tail(20)

        if len(h_past) < 3 or len(a_past) < 3:
            continue

        feat = {
            "date": str(d.date()), "home": h, "away": a, "league": m["league"],
            "home_xg_diff_5": h_past["xg_diff_5"].iloc[-1] if len(h_past) >= 1 else 0,
            "home_xg_for_5": h_past["xg_for_5"].iloc[-1] if len(h_past) >= 1 else 1.3,
            "home_form_5": h_past["form_5"].iloc[-1] if len(h_past) >= 1 else 0.5,
            "home_ppda_5": h_past["ppda_atk_5"].iloc[-1] if len(h_past) >= 1 else 12,
            "away_xg_diff_5": a_past["xg_diff_5"].iloc[-1] if len(a_past) >= 1 else 0,
            "away_xg_for_5": a_past["xg_for_5"].iloc[-1] if len(a_past) >= 1 else 1.3,
            "away_form_5": a_past["form_5"].iloc[-1] if len(a_past) >= 1 else 0.5,
            "away_ppda_5": a_past["ppda_atk_5"].iloc[-1] if len(a_past) >= 1 else 12,
            "xg_diff_ratio": (h_past["xg_diff_5"].iloc[-1] - a_past["xg_diff_5"].iloc[-1]),
            "actual": "H" if m["home_score"] > m["away_score"] else (
                      "D" if m["home_score"] == m["away_score"] else "A"),
            "home_score": int(m["home_score"]), "away_score": int(m["away_score"]),
            "home_xg_match": m["home_xg"], "away_xg_match": m["away_xg"],
        }
        features.append(feat)

    return pd.DataFrame(features)


def train_xg_model(features_df: pd.DataFrame, use_gpu: bool = False) -> dict:
    """Train LightGBM on xG-enriched features."""

    feature_cols = [c for c in features_df.columns
                    if c not in ["date", "home", "away", "league", "actual",
                                 "home_score", "away_score",
                                 "home_xg_match", "away_xg_match"]]

    X = features_df[feature_cols].values
    y = features_df["actual"].values

    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    params = {
        "n_estimators": 500, "learning_rate": 0.03,
        "max_depth": 6, "num_leaves": 47,
        "min_child_samples": 30, "reg_alpha": 0.1, "reg_lambda": 1.0,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "verbose": -1, "n_jobs": -1,
    }
    if use_gpu:
        params["device"] = "gpu"

    models = {}
    classes = ["H", "D", "A"]
    for cls in classes:
        y_bin = (y_tr == cls).astype(int)
        y_val_bin = (y_val == cls).astype(int)
        model = LGBMClassifier(**params, objective="binary")
        model.fit(X_tr, y_bin, eval_set=[(X_val, y_val_bin)],
                  callbacks=[early_stopping(25), log_evaluation(100)])
        models[cls] = model

    # Validate
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

    # Per-class recall
    recalls = {}
    for cls in classes:
        sub = y_val == cls
        if sub.sum() > 0:
            recalls[cls] = float((val_pred[sub] == cls).mean())

    return {
        "models": models, "feature_cols": feature_cols,
        "val_accuracy": val_acc, "val_brier": val_brier,
        "val_recall": recalls, "n_train": len(X_tr), "n_val": len(X_val),
    }


if __name__ == "__main__":
    print("=== Building xG-enriched features ===")
    import time
    t0 = time.time()
    features = build_xg_enriched_features()
    t1 = time.time()
    print(f"Built {len(features)} rows in {t1-t0:.1f}s")
    print(f"Columns: {list(features.columns)}")
    print(f"Actual distribution: {features['actual'].value_counts().to_dict()}")

    print("\n=== Training xG model (CPU) ===")
    t0 = time.time()
    result = train_xg_model(features, use_gpu=False)
    t1 = time.time()
    print(f"Trained in {t1-t0:.1f}s")
    print(f"Val accuracy: {result['val_accuracy']:.1%}")
    print(f"Val brier:    {result['val_brier']:.4f}")
    print(f"Val recall:   {result['val_recall']}")
    print(f"Features:     {result['feature_cols']}")

    # Save
    import pickle
    with open("wc_analysis/data/xg_model.pkl", "wb") as f:
        pickle.dump(result, f)
    print("✅ Saved xg_model.pkl")
