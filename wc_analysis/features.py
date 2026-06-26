"""Simplified feature engineering for World Cup prediction.

Per @oracle: keep 6 features, compute on-the-fly (no parquet).
All features use strict as_of_date cutoff to prevent leakage.

Features:
1. elo_diff — Elo rating difference (home - away)
2. form_5 — last 5 matches points-per-game differential
3. h2h_draw_rate — head-to-head draw rate (last 10 meetings)
4. home_neutral — 1=home, 0=neutral
5. tournament_stage — 0=friendly, 1=qualifier, 2=group, 3=knockout
6. injury_factor — from existing injuries.json (1.0=healthy, <1.0=degraded)
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from wc_analysis.elo_model import EloModel


def _load_injuries(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / "injuries.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _tournament_stage(tournament: str) -> int:
    t = tournament.lower()
    if "friendly" in t:
        return 0
    if "qualif" in t:
        return 1
    if "world cup" in t and "qualif" not in t:
        return 3  # World Cup finals
    if any(x in t for x in ["euro", "copa america", "afcon", "asian cup", "gold cup"]):
        if "qualif" not in t:
            return 3
    if "nations league" in t:
        return 2
    return 1


def _form_ppg(team: str, matches: pd.DataFrame, as_of_date: str, n: int = 5) -> float:
    """Points-per-game in last N matches before as_of_date."""
    past = matches[
        (matches["date"] < as_of_date) &
        ((matches["home_team"] == team) | (matches["away_team"] == team))
    ].sort_values("date", ascending=False).head(n)

    if len(past) == 0:
        return 1.0  # neutral — no data

    pts = 0
    for _, m in past.iterrows():
        hs, as_ = int(m["home_score"]), int(m["away_score"])
        if m["home_team"] == team:
            if hs > as_:
                pts += 3
            elif hs == as_:
                pts += 1
        else:
            if as_ > hs:
                pts += 3
            elif as_ == hs:
                pts += 1
    return pts / len(past)


def _h2h_draw_rate(home: str, away: str, matches: pd.DataFrame, as_of_date: str,
                  n: int = 10) -> float:
    """Draw rate in last N head-to-head meetings before as_of_date."""
    past = matches[
        (matches["date"] < as_of_date) &
        (
            ((matches["home_team"] == home) & (matches["away_team"] == away)) |
            ((matches["home_team"] == away) & (matches["away_team"] == home))
        )
    ].sort_values("date", ascending=False).head(n)

    if len(past) == 0:
        return 0.26  # global average draw rate
    draws = sum(1 for _, m in past.iterrows() if m["home_score"] == m["away_score"])
    return draws / len(past)


def compute_features(home: str, away: str, neutral: bool, tournament: str,
                     as_of_date: str, matches_df: pd.DataFrame,
                     elo_model: EloModel, data_dir: Path | None = None) -> dict:
    """Compute all 6 features for a single match.

    Args:
        as_of_date: ISO date string — features only use data before this date
        matches_df: full match history (for form, H2H)
        elo_model: pre-fitted EloModel (ratings already computed)
        data_dir: path to wc_analysis/data/ for injuries.json

    Returns:
        dict with all feature values + derived prediction inputs
    """
    r_h = elo_model._get_or_init(home)
    r_a = elo_model._get_or_init(away)

    injuries = _load_injuries(data_dir) if data_dir else {}
    home_injury = injuries.get(home, {}).get("lambda_factor", 1.0)
    away_injury = injuries.get(away, {}).get("lambda_factor", 1.0)

    return {
        "elo_diff": r_h - r_a,
        "elo_home": r_h,
        "elo_away": r_a,
        "form_home": _form_ppg(home, matches_df, as_of_date, n=5),
        "form_away": _form_ppg(away, matches_df, as_of_date, n=5),
        "form_diff": _form_ppg(home, matches_df, as_of_date, n=5) - _form_ppg(away, matches_df, as_of_date, n=5),
        "h2h_draw_rate": _h2h_draw_rate(home, away, matches_df, as_of_date, n=10),
        "home_neutral": 0 if neutral else 1,
        "tournament_stage": _tournament_stage(tournament),
        "injury_home": home_injury,
        "injury_away": away_injury,
    }


def build_feature_matrix(matches_df: pd.DataFrame, start_year: int = 1990,
                         data_dir: Path | None = None) -> pd.DataFrame:
    """Build feature matrix for all matches (for backtesting).

    Uses walk-forward: Elo is fitted incrementally, features computed
    with strict as_of_date cutoff.
    """
    df = matches_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"].dt.year >= start_year].sort_values("date").reset_index(drop=True)
    df = df.dropna(subset=["home_score", "away_score"])
    df = df.drop_duplicates(subset=["date", "home_team", "away_team"])

    elo = EloModel()
    # Pre-fit Elo on all data before start_year for meaningful initial ratings
    pre = matches_df[matches_df["date"].dt.year < start_year]
    if len(pre) > 0:
        elo.fit(pre)
    features_list = []

    for _, match in df.iterrows():
        as_of = str(match["date"].date())
        feat = compute_features(
            match["home_team"], match["away_team"],
            bool(match["neutral"]), match["tournament"],
            as_of, matches_df, elo, data_dir  # pass FULL dataset for form/H2H
        )
        feat["date"] = as_of
        feat["home"] = match["home_team"]
        feat["away"] = match["away_team"]
        feat["home_score"] = int(match["home_score"])
        feat["away_score"] = int(match["away_score"])
        feat["actual"] = "H" if match["home_score"] > match["away_score"] else (
                         "D" if match["home_score"] == match["away_score"] else "A")
        features_list.append(feat)

        # Update Elo AFTER computing features (no leakage)
        elo.update_match(match["home_team"], match["away_team"],
                         int(match["home_score"]), int(match["away_score"]),
                         match["tournament"], bool(match["neutral"]))

    return pd.DataFrame(features_list)
