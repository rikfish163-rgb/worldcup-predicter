"""Elo rating system computed from scratch with strict as_of_date cutoff.

Prevents leakage by only using matches before the cutoff date.
Based on eloratings.net formula with K-factor tiers per tournament type.

Usage:
    from wc_analysis.elo_model import EloModel
    model = EloModel()
    model.fit(matches_df)  # chronologically forward-pass
    rating = model.get_rating("Brazil", as_of_date="2022-11-20")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class EloConfig:
    home_adv: int = 100
    divisor: int = 400
    base_k: int = 20
    initial_rating: float = 1200.0
    wc_finals_k: int = 60
    continental_finals_k: int = 50
    wc_qualifiers_k: int = 40
    other_tournament_k: int = 30
    friendly_k: int = 20


def goal_diff_multiplier(goal_diff: int) -> float:
    """G multiplier from eloratings.net."""
    gd = abs(goal_diff)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    if gd == 3:
        return 1.75
    return 1.75 + (gd - 3) / 8.0


def classify_tournament(tournament: str) -> str:
    """Classify tournament name into K-factor tier."""
    t = tournament.lower()
    if "fifa world cup" in t and "qualif" not in t:
        return "wc_finals"
    if "world cup" in t and "qualif" not in t:
        return "wc_finals"
    if any(x in t for x in ["euro", "copa america", "african cup", "afcon", "asian cup", "nations league", "gold cup"]):
        if "qualif" not in t:
            return "continental_finals"
    if "qualif" in t and "world cup" in t:
        return "wc_qualifiers"
    if "friendly" in t:
        return "friendly"
    return "other"


def k_factor(tournament: str, config: EloConfig) -> int:
    tier = classify_tournament(tournament)
    return {
        "wc_finals": config.wc_finals_k,
        "continental_finals": config.continental_finals_k,
        "wc_qualifiers": config.wc_qualifiers_k,
        "friendly": config.friendly_k,
        "other": config.other_tournament_k,
    }[tier]


class EloModel:
    """Elo ratings computed from match history with as_of_date cutoff."""

    def __init__(self, config: Optional[EloConfig] = None):
        self.config = config or EloConfig()
        self.ratings: dict[str, float] = {}
        self.history: list[dict] = []

    def _get_or_init(self, team: str) -> float:
        if team not in self.ratings:
            self.ratings[team] = self.config.initial_rating
        return self.ratings[team]

    def _expected(self, rating_a: float, rating_b: float, home_adv: int, is_neutral: bool) -> float:
        dr = rating_a - rating_b
        if not is_neutral:
            dr += home_adv
        return 1.0 / (1.0 + 10.0 ** (-dr / self.config.divisor))

    def update_match(self, home: str, away: str, hs: int, as_: int, tournament: str, neutral: bool) -> None:
        r_h = self._get_or_init(home)
        r_a = self._get_or_init(away)
        we_h = self._expected(r_h, r_a, self.config.home_adv, neutral)
        we_a = 1.0 - we_h

        if hs > as_:
            w_h, w_a = 1.0, 0.0
        elif hs < as_:
            w_h, w_a = 0.0, 1.0
        else:
            w_h, w_a = 0.5, 0.5

        k = k_factor(tournament, self.config)
        g = goal_diff_multiplier(hs - as_)
        delta_h = k * g * (w_h - we_h)
        delta_a = k * g * (w_a - we_a)

        self.ratings[home] = r_h + delta_h
        self.ratings[away] = r_a + delta_a
        self.history.append({
            "home": home, "away": away, "hs": hs, "as": as_,
            "tournament": tournament, "neutral": neutral,
            "r_h_before": r_h, "r_a_before": r_a,
            "r_h_after": self.ratings[home], "r_a_after": self.ratings[away],
            "k": k, "g": g, "we_h": we_h,
        })

    def fit(self, matches_df: pd.DataFrame, date_col: str = "date",
            home_col: str = "home_team", away_col: str = "away_team",
            hs_col: str = "home_score", as_col: str = "away_score",
            tournament_col: str = "tournament", neutral_col: str = "neutral") -> None:
        """Forward-pass through all matches chronologically."""
        df = matches_df.sort_values(date_col).reset_index(drop=True)
        df = df.dropna(subset=[hs_col, as_col])
        df = df.drop_duplicates(subset=[date_col, home_col, away_col])
        df = df[(df[hs_col] >= 0) & (df[as_col] >= 0) & (df[hs_col] <= 30) & (df[as_col] <= 30)]
        for _, row in df.iterrows():
            self.update_match(
                row[home_col], row[away_col],
                int(row[hs_col]), int(row[as_col]),
                row[tournament_col], bool(row[neutral_col]),
            )

    def get_rating(self, team: str, as_of_date: Optional[str] = None,
                   matches_df: Optional[pd.DataFrame] = None) -> float:
        """Get rating at a point in time. If as_of_date given, recompute from scratch
        using only matches before that date (requires matches_df)."""
        if as_of_date is None:
            return self._get_or_init(team)
        if matches_df is None:
            raise ValueError("matches_df required when as_of_date is specified")
        temp = EloModel(self.config)
        subset = matches_df[matches_df["date"] < as_of_date]
        temp.fit(subset)
        return temp._get_or_init(team)

    def get_ratings_at(self, as_of_date: str, matches_df: pd.DataFrame) -> dict[str, float]:
        """Get all ratings at a point in time (recompute from scratch)."""
        temp = EloModel(self.config)
        subset = matches_df[matches_df["date"] < as_of_date]
        temp.fit(subset)
        return dict(temp.ratings)

    def predict(self, home: str, away: str, neutral: bool = False,
                as_of_date: Optional[str] = None,
                matches_df: Optional[pd.DataFrame] = None) -> dict:
        """Return P(home win), P(draw), P(away win) from Elo."""
        if as_of_date and matches_df is not None:
            r_h = self.get_rating(home, as_of_date, matches_df)
            r_a = self.get_rating(away, as_of_date, matches_df)
        else:
            r_h = self._get_or_init(home)
            r_a = self._get_or_init(away)
        we_h = self._expected(r_h, r_a, self.config.home_adv, neutral)
        we_a = 1.0 - we_h
        dr = r_h - r_a + (0 if neutral else self.config.home_adv)
        p_draw = 0.26 * math.exp(-(dr / 300.0) ** 2)
        p_home = we_h - p_draw / 2
        p_away = we_a - p_draw / 2
        total = p_home + p_draw + p_away
        return {"p_home": p_home / total, "p_draw": p_draw / total, "p_away": p_away / total,
                "r_home": r_h, "r_away": r_a, "elo_diff": r_h - r_a}
