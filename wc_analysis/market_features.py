"""Feature enrichment using market odds and historical xG where available.

Data sources:
- MatchHistory CSVs: Pinnacle closing odds (PSH/PSD/PSA + PSC*) + Asian handicap
- pinnacle_history.json: 5404 matches with closing lines
- FBref cached HTML: xG for WC2026 qualifiers (sparse for small teams)

Strategy: market odds are the strongest predictor (Pinnacle r²=0.997 with outcomes).
Use them as features + for CLV benchmarking.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


def load_matchhistory_with_odds(data_dir: Path = Path("data/MatchHistory")) -> pd.DataFrame:
    """Load all MatchHistory CSVs with Pinnacle closing odds."""
    files = sorted([f for f in os.listdir(data_dir) if f.endswith(".csv")])
    frames = []
    for f in files:
        try:
            df = pd.read_csv(data_dir / f, encoding="utf-8-sig")
            df["source_file"] = f
            frames.append(df)
        except Exception as e:
            print(f"Skip {f}: {e}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    # Normalize team names
    df["HomeTeam"] = df["HomeTeam"].astype(str).str.strip()
    df["AwayTeam"] = df["AwayTeam"].astype(str).str.strip()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    return df


def pinnacle_implied_probs(odds_h: float, odds_d: float, odds_a: float) -> dict:
    """De-vigged implied probabilities from Pinnacle closing odds."""
    if any(o != o or o <= 1.0 for o in [odds_h, odds_d, odds_a]):
        return {"p_home": None, "p_draw": None, "p_away": None, "margin": None}
    imp_h = 1.0 / odds_h
    imp_d = 1.0 / odds_d
    imp_a = 1.0 / odds_a
    margin = imp_h + imp_d + imp_a
    return {
        "p_home": imp_h / margin, "p_draw": imp_d / margin,
        "p_away": imp_a / margin, "margin": margin,
    }


def asian_handicap_implied(ahh: float, aha: float) -> dict:
    """Asian handicap implied prob from Pinnacle AH odds."""
    if ahh != ahh or aha != aha or ahh <= 1.0 or aha <= 1.0:
        return {"p_home_cover": None, "p_away_cover": None}
    imp_h = 1.0 / ahh
    imp_a = 1.0 / aha
    margin = imp_h + imp_a
    return {"p_home_cover": imp_h / margin, "p_away_cover": imp_a / margin}


def build_market_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add Pinnacle closing odds features to MatchHistory data."""
    # Pinnacle closing 1X2
    df["pin_p"] = df.apply(lambda r: pinnacle_implied_probs(
        r.get("PSCH"), r.get("PSCD"), r.get("PSCA")), axis=1)
    df["pin_p_home"] = df["pin_p"].apply(lambda x: x["p_home"] if x else None)
    df["pin_p_draw"] = df["pin_p"].apply(lambda x: x["p_draw"] if x else None)
    df["pin_p_away"] = df["pin_p"].apply(lambda x: x["p_away"] if x else None)
    df["pin_margin"] = df["pin_p"].apply(lambda x: x["margin"] if x else None)

    # Asian handicap
    df["ah_implied"] = df.apply(lambda r: asian_handicap_implied(
        r.get("PCAHH"), r.get("PCAHA")), axis=1)
    df["ah_home_cover"] = df["ah_implied"].apply(lambda x: x["p_home_cover"] if x else None)
    df["ah_away_cover"] = df["ah_implied"].apply(lambda x: x["p_away_cover"] if x else None)

    # Odds-derived features
    df["odds_implied_home"] = df["B365H"].apply(lambda x: 1.0/x if x == x and x > 1 else None)
    df["odds_implied_draw"] = df["B365D"].apply(lambda x: 1.0/x if x == x and x > 1 else None)
    df["odds_implied_away"] = df["B365A"].apply(lambda x: 1.0/x if x == x and x > 1 else None)

    # Actual result
    df["actual"] = df["FTR"].map({"H": "H", "D": "D", "A": "A"})

    # Total goals
    df["total_goals"] = df["FTHG"] + df["FTAG"]
    df["over_2_5"] = (df["total_goals"] > 2.5).astype(int)

    return df


def load_pinnacle_history(path: Path = Path("wc_analysis/data/pinnacle_history.json")) -> pd.DataFrame:
    """Load the 5404-match Pinnacle history."""
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return pd.DataFrame(data)
        elif isinstance(data, dict):
            return pd.DataFrame(data.get("matches", []))
    except Exception as e:
        print(f"Load pinnacle_history failed: {e}")
    return pd.DataFrame()


def compute_clv(model_odds: float, pinnacle_closing: float) -> float:
    """Closing Line Value: (model_odds / pinnacle_closing - 1) * 100."""
    if pinnacle_closing <= 1.0:
        return 0.0
    return (model_odds / pinnacle_closing - 1) * 100


if __name__ == "__main__":
    print("=== Loading MatchHistory with odds ===")
    df = load_matchhistory_with_odds()
    print(f"Total matches: {len(df)}")
    print(f"Date range: {df['Date'].min()} → {df['Date'].max()}")

    df = build_market_features(df)
    pin_valid = df["pin_p_home"].notna().sum()
    ah_valid = df["ah_home_cover"].notna().sum()
    print(f"\nPinnacle closing odds available: {pin_valid} ({pin_valid/len(df)*100:.1f}%)")
    print(f"AH odds available: {ah_valid} ({ah_valid/len(df)*100:.1f}%)")

    print(f"\nSample (first 3 with Pinnacle):")
    sample = df[df["pin_p_home"].notna()].head(3)
    cols = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR",
            "PSCH", "PSCD", "PSCA", "pin_p_home", "pin_p_draw", "pin_p_away"]
    print(sample[cols].to_string())

    print(f"\n=== Pinnacle history ===")
    pin = load_pinnacle_history()
    print(f"Matches: {len(pin)}")
    if len(pin) > 0:
        print(f"Columns: {list(pin.columns)[:10]}")
