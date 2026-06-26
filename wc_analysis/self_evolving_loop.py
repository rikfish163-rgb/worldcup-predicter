#!/usr/bin/env python3
"""Self-evolving closed-loop pipeline for match prediction.

Cycle (runs daily via crontab on user's local machine or VPS):
1. SCRAPE: Fetch latest odds (sporttery), latest results (martj42 update)
2. PREDICT: Run comprehensive predictor on upcoming matches
3. SERVE: Update predictions.json + auto-trigger VPS refresh
4. RECONCILE: After matches complete, fetch actual results
5. LEARN: Save results + retrain LightGBM on extended dataset
6. DEPLOY: Update model artifacts + restart predict.py --serve

Designed to run unattended; all errors logged to wc_analysis/data/loop.log.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

WC_DIR = Path("wc_analysis")
DATA_DIR = WC_DIR / "data"
LOOP_LOG = DATA_DIR / "loop.log"

# Ensure wc_analysis is importable as a package
import os
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOOP_LOG, "a") as f:
        f.write(line + "\n")


def step1_scrape_odds() -> int:
    """Step 1: Scrape latest sporttery odds + martj42 updates."""
    log("STEP 1: Scraping latest odds...")
    try:
        # Use existing fetch_sporttery logic
        result = subprocess.run(
            [sys.executable, "-c",
             "from predict import fetch_sporttery; fetch_sporttery()"],
            cwd="wc_analysis",
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log("  ✅ sporttery odds fetched")
        else:
            log(f"  ⚠️ sporttery fetch failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"  ⚠️ sporttery fetch error: {e}")

    # Update martj42 dataset (if newer commits exist)
    try:
        result = subprocess.run(
            ["git", "pull", "--depth=1"],
            cwd="data", capture_output=True, text=True, timeout=30,
        )
        log(f"  martj42 update: {result.stdout[:100] or 'no change'}")
    except Exception as e:
        log(f"  ⚠️ martj42 update skipped: {e}")

    return 0


def step2_predict() -> dict:
    """Step 2: Generate top-3 predictions for upcoming matches."""
    log("STEP 2: Generating predictions...")
    try:
        from wc_analysis.top_predictions import predict_with_top3
        from wc_analysis.elo_model import EloModel
        from wc_analysis.draw_correction import DrawCorrection
        from wc_analysis.comprehensive_predictor import ComprehensiveMatchPredictor

        df = pd.read_csv("data/international_results.csv")
        df["date"] = pd.to_datetime(df["date"])

        elo = EloModel()
        elo.fit(df[df["date"] < datetime.now().strftime("%Y-%m-%d")])

        dc = DrawCorrection()
        try:
            dc.load(DATA_DIR / "draw_correction_v2.json")
        except Exception:
            pass

        # Load FIFA rankings/points if available
        try:
            rankings = json.loads((DATA_DIR / "fifa_rankings.json").read_text())
        except Exception:
            rankings = {}

        predictor = ComprehensiveMatchPredictor(
            fifa_points=rankings.get("points", {}),
            fifa_ranks=rankings.get("ranks", {}),
            confederations=rankings.get("confederations", {}),
            elo_model=elo, draw_correction=dc,
        )

        # Load sporttery matches
        matches = json.loads((DATA_DIR / "odds_parsed.json").read_text())
        predictions = []
        for m in matches:
            home, away = m.get("home_en"), m.get("away_en")
            if not home or not away:
                continue
            try:
                hline = float(m.get("hhad_line", 0))
            except Exception:
                hline = None

            top3 = predict_with_top3(home, away, df, data_dir=DATA_DIR,
                                     handicap_line=hline)
            comp = predictor.predict(home, away, neutral=True)
            # Blend comprehensive + top-3
            predictions.append({
                "home": home, "away": away, "date": m.get("date"),
                "time": m.get("time"), "league": m.get("league"),
                "handicap_line": hline,
                "elo_diff": top3["elo_diff"],
                "p_home": comp["p_home"], "p_draw": comp["p_draw"], "p_away": comp["p_away"],
                "top_1x2": top3["top_1x2"], "top_scores": top3["top_scores"][:6],
                "handicap": top3.get("handicap"),
                "model": "comprehensive_v1",
            })

        (DATA_DIR / "top3_predictions.json").write_text(
            json.dumps(predictions, indent=2, ensure_ascii=False))
        log(f"  ✅ {len(predictions)} predictions written to top3_predictions.json")
        return {"n": len(predictions)}
    except Exception as e:
        log(f"  ❌ predict failed: {e}")
        return {"error": str(e)}


def step3_serve() -> int:
    """Step 3: Auto-trigger VPS refresh (if remote configured)."""
    log("STEP 3: Triggering VPS refresh...")
    try:
        # Use scp + curl to push predictions to VPS
        cfg_path = DATA_DIR / "vps_push.env"
        if cfg_path.exists():
            cfg = dict(line.split("=", 1) for line in cfg_path.read_text().splitlines() if "=" in line)
            host = cfg.get("VPS_HOST", "")
            if host:
                result = subprocess.run(
                    ["ssh", host, "curl -s -X POST https://predict.hetaisheng.ccwu.cc/api/refresh"],
                    capture_output=True, text=True, timeout=20,
                )
                log(f"  VPS refresh: {result.stdout[:100] or result.stderr[:100]}")
                return 0
        log("  ⚠️ VPS push not configured (set wc_analysis/data/vps_push.env)")
    except Exception as e:
        log(f"  ⚠️ VPS push failed: {e}")
    return 1


def step4_reconcile() -> dict:
    """Step 4: After matches complete, fetch actual results."""
    log("STEP 4: Reconciling results...")
    try:
        from match_results import fetch_recent_results
        results = fetch_recent_results(days=2)
        log(f"  ✅ {len(results)} recent results fetched")
        return {"n": len(results), "results": results}
    except Exception as e:
        log(f"  ⚠️ reconcile failed: {e}")
        return {"error": str(e)}


def step5_learn() -> int:
    """Step 5: Append results to historical data, retrain model."""
    log("STEP 5: Learning (retrain)...")
    try:
        # Append new results to martj42
        from match_results import update_historical
        n_added = update_historical()
        log(f"  ✅ {n_added} new matches added to history")

        # Retrain draw correction + ensemble (lightweight CPU version)
        result = subprocess.run(
            [sys.executable, "wc_analysis/xg_training.py"],
            cwd=".",
            env={**__import__("os").environ, "PYTHONPATH": "."},
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            log("  ✅ Models retrained")
        else:
            log(f"  ⚠️ Retrain stderr: {result.stderr[:200]}")
        return 0
    except Exception as e:
        log(f"  ❌ learn failed: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", type=int, default=0,
                        help="Run only specific step (0=all)")
    parser.add_argument("--skip-serve", action="store_true",
                        help="Skip VPS push (local-only mode)")
    args = parser.parse_args()

    log("=" * 60)
    log("SELF-EVOLVING LOOP START")
    log("=" * 60)

    if args.step in (0, 1):
        step1_scrape_odds()
    if args.step in (0, 2):
        step2_predict()
    if args.step in (0, 3) and not args.skip_serve:
        step3_serve()
    if args.step in (0, 4):
        step4_reconcile()
    if args.step in (0, 5):
        step5_learn()

    log("=" * 60)
    log("SELF-EVOLVING LOOP END")
    log("=" * 60)


if __name__ == "__main__":
    main()
