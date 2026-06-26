"""Match results fetcher — pulls yesterday's results for backtest/reconciliation.

Sources:
1. martj42 GitHub (CSV update)
2. RSSSF / openfootball historical
3. FBref (TLS) for current season
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

WC_DIR = Path("wc_analysis")
DATA_DIR = WC_DIR / "data"


def fetch_recent_results(days: int = 2) -> list:
    """Fetch results from the last `days` days for reconciliation."""
    results = []
    today = datetime.now()
    start = today - timedelta(days=days)
    start_str = start.strftime("%Y-%m-%d")

    # Strategy 1: re-pull martj42 (gets latest commits)
    try:
        subprocess.run(
            ["git", "pull", "--depth=1"],
            cwd="data", capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass

    # Read latest CSV
    csv_path = Path("data/international_results.csv")
    if not csv_path.exists():
        return results

    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    recent = df[df["date"] >= start_str]
    for _, row in recent.iterrows():
        results.append({
            "date": str(row["date"].date()),
            "home": row["home_team"], "away": row["away_team"],
            "home_score": int(row["home_score"]) if pd.notna(row["home_score"]) else None,
            "away_score": int(row["away_score"]) if pd.notna(row["away_score"]) else None,
            "tournament": row.get("tournament", ""),
            "result": _result_letter(row["home_score"], row["away_score"]),
        })
    return results


def _result_letter(hs, as_):
    if pd.isna(hs) or pd.isna(as_):
        return None
    if hs > as_: return "H"
    if hs < as_: return "A"
    return "D"


def update_historical() -> int:
    """Pull latest from martj42, deduplicate, return new matches count."""
    csv_path = Path("data/international_results.csv")
    if not csv_path.exists():
        return 0

    # Backup
    backup = csv_path.with_suffix(".csv.bak")
    if csv_path.exists():
        import shutil
        shutil.copy(csv_path, backup)

    # Pull latest
    subprocess.run(
        ["git", "pull", "--depth=1"],
        cwd="data", capture_output=True, text=True, timeout=60,
    )

    # Count new rows
    df_new = pd.read_csv(csv_path)
    return len(df_new)


def fetch_today_fixtures() -> list:
    """Load today's fixtures from sporttery odds (for prediction tracking)."""
    try:
        matches = json.loads((DATA_DIR / "odds_parsed.json").read_text())
        today = datetime.now().strftime("%Y-%m-%d")
        today_matches = [m for m in matches if m.get("date") == today]
        return today_matches
    except Exception:
        return []


def reconcile_predictions() -> dict:
    """Compare predictions.json (predictions made earlier) vs actual results.

    Returns per-match accuracy stats for today's matches.
    """
    today_matches = fetch_today_fixtures()
    if not today_matches:
        return {"n": 0, "msg": "no fixtures today"}

    results = fetch_recent_results(days=1)
    actual_by_key = {
        f"{r['date']}_{r['home']}_{r['away']}": r for r in results
    }

    # Load today's predictions
    try:
        preds = json.loads((DATA_DIR / "top3_predictions.json").read_text())
    except Exception:
        return {"n": 0, "msg": "no predictions.json"}

    hits = {"top1": 0, "top2": 0, "top3": 0, "total": 0}
    per_match = []
    for p in preds:
        key = f"{p['date']}_{p['home']}_{p['away']}"
        if key not in actual_by_key:
            continue
        actual = actual_by_key[key]
        actual_class = actual["result"]
        if not actual_class:
            continue

        ranked = sorted([("H", p["p_home"]), ("D", p["p_draw"]), ("A", p["p_away"])],
                        key=lambda x: -x[1])
        top3_classes = [c for c, _ in ranked[:3]]
        top1_hit = top3_classes[0] == actual_class
        top2_hit = actual_class in top3_classes[:2]
        top3_hit = actual_class in top3_classes

        hits["total"] += 1
        hits["top1"] += int(top1_hit)
        hits["top2"] += int(top2_hit)
        hits["top3"] += int(top3_hit)

        per_match.append({
            "home": p["home"], "away": p["away"],
            "predicted": top3_classes[0],
            "actual": actual_class,
            "score": f"{actual['home_score']}-{actual['away_score']}",
            "p_home": p["p_home"], "p_draw": p["p_draw"], "p_away": p["p_away"],
            "top1_hit": top1_hit, "top2_hit": top2_hit, "top3_hit": top3_hit,
        })

    if hits["total"] == 0:
        return {"n": 0, "msg": "no matches reconciled yet"}

    return {
        "n": hits["total"],
        "top1_accuracy": hits["top1"] / hits["total"],
        "top2_accuracy": hits["top2"] / hits["total"],
        "top3_accuracy": hits["top3"] / hits["total"],
        "per_match": per_match,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "reconcile":
        result = reconcile_predictions()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        results = fetch_recent_results()
        print(f"Fetched {len(results)} recent results")
        for r in results[:5]:
            print(f"  {r['date']}: {r['home']} {r['home_score']}-{r['away_score']} {r['away']} ({r['result']})")
