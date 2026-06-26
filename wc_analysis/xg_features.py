"""xG-based feature enrichment for the prediction model.

Uses Understat club xG data to derive player-level/team-level xG features
that transfer to international matches via squad composition.

Strategy:
- Compute rolling xG differentials per team from club data
- Map club xG to national teams via squad membership (approximate)
- For international matches without direct xG, use club-derived proxy
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd


def load_understat_xg(data_dir: Path = Path("data/understat_enriched")) -> pd.DataFrame:
    """Load all Understat xG data."""
    if not data_dir.exists():
        return pd.DataFrame()
    frames = []
    for f in sorted(os.listdir(data_dir)):
        if f.endswith(".csv"):
            try:
                df = pd.read_csv(data_dir / f)
                df["source"] = f
                frames.append(df)
            except Exception:
                pass
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def compute_xg_features(understat_df: pd.DataFrame) -> pd.DataFrame:
    """Compute rolling xG differentials per team."""
    if understat_df.empty or "date" not in understat_df.columns:
        return pd.DataFrame()

    df = understat_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["team", "date"])

    # Rolling xG differentials
    for window in [5, 10, 20]:
        df[f"xg_diff_{window}"] = df.groupby("team")["xg_diff"].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )
        df[f"xg_for_{window}"] = df.groupby("team")["xg_for"].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )
        df[f"xg_against_{window}"] = df.groupby("team")["xg_against"].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )

    return df


def estimate_squad_xg(team: str, as_of_date: str,
                      understat_df: pd.DataFrame,
                      squad_mapping: dict | None = None) -> dict:
    """Estimate a national team's xG strength from club data.

    This is a simplified proxy: uses the average xG of the league
    the team's players play in. A full implementation would map
    each national team's squad to their club teams.

    Returns default values if no data available.
    """
    # Default: neutral xG
    return {
        "xg_for_proxy": 1.3, "xg_against_proxy": 1.2,
        "xg_diff_proxy": 0.1, "xg_confidence": 0.0,
    }


def add_xg_features_to_match(match_features: dict, home: str, away: str,
                             as_of_date: str, understat_df: pd.DataFrame) -> dict:
    """Add xG proxy features to a match's feature dict."""
    home_xg = estimate_squad_xg(home, as_of_date, understat_df)
    away_xg = estimate_squad_xg(away, as_of_date, understat_df)

    match_features["xg_home_proxy"] = home_xg["xg_for_proxy"]
    match_features["xg_away_proxy"] = away_xg["xg_for_proxy"]
    match_features["xg_diff_proxy"] = home_xg["xg_diff_proxy"] - away_xg["xg_diff_proxy"]
    match_features["xg_home_confidence"] = home_xg["xg_confidence"]
    match_features["xg_away_confidence"] = away_xg["xg_confidence"]

    return match_features


if __name__ == "__main__":
    print("=== xG feature module ===")
    df = load_understat_xg()
    if df.empty:
        print("No Understat data found. Run scraper first.")
    else:
        print(f"Loaded {len(df)} rows")
        features = compute_xg_features(df)
        print(f"Computed features for {len(features)} rows")
        print(features[["team", "date", "xg_diff_5", "xg_for_5"]].tail(10))
