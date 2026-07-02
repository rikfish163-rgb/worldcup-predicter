"""Comprehensive match prediction with weighted factors (参考系统量化).

Weights per @reference:
- FIFA积分差: 35%  (主队 - 客队 FIFA 积分)
- 排名差:    15%  (FIFA 排名差)
- 状态:      15%  (内部因素 40% + 心理压力 25% + 对手策略 20% + 其他 15%)
- 教练:      10%  (教练档位 A/B/C/D)
- 20年战绩:   10%  (时间衰减: 1年内 70-85%, ..., 20年+ <5%)
- 足球强洲:   5%  (大洲胜平负加权)
- 比赛环境:   5%  (2026 世界杯地点影响)
- 黑马/爆冷:  5%  (偶然因素)
- 心理压力:   动态放大 P(draw)·Dmax·(1-I^Δ)^k

Final dynamic draw: P(draw) = base_draw * (1 - 实力差^2)^k
"""

from __future__ import annotations

import json
from math import exp, factorial
from pathlib import Path

import numpy as np
import pandas as pd

from wc_analysis.elo_model import EloModel
from wc_analysis.draw_correction import DrawCorrection


# FIFA积分差 vs 1X2 概率（主队视角，5 档查表）
FIFA_POINT_TABLE = {
    (0, 50):    {"win": 0.55, "draw": 0.30, "loss": 0.45},
    (50, 100):  {"win": 0.60, "draw": 0.20, "loss": 0.40},
    (100, 200): {"win": 0.70, "draw": 0.15, "loss": 0.30},
    (200, 300): {"win": 0.80, "draw": 0.10, "loss": 0.20},
    (300, 9999):{"win": 0.90, "draw": 0.10, "loss": 0.10},
}

# FIFA 排名差 vs 1X2 概率
RANK_DIFF_TABLE = {
    (0, 5):    {"win": 0.48, "draw": 0.30, "loss": 0.22},
    (5, 15):   {"win": 0.52, "draw": 0.28, "loss": 0.20},
    (15, 30):  {"win": 0.58, "draw": 0.26, "loss": 0.16},
    (30, 50):  {"win": 0.65, "draw": 0.23, "loss": 0.12},
    (50, 80):  {"win": 0.72, "draw": 0.20, "loss": 0.08},
    (80, 120): {"win": 0.78, "draw": 0.17, "loss": 0.05},
    (120, 9999):{"win": 0.83, "draw": 0.14, "loss": 0.03},
}

# 大洲 vs 大洲（加权胜率，权益后）
CONTINENTAL_TABLE = {
    ("UEFA", "UEFA"): 0.50,
    ("UEFA", "CONMEBOL"): 0.55,
    ("UEFA", "CONCACAF"): 0.75,
    ("UEFA", "CAF"): 0.70,
    ("UEFA", "AFC"): 0.75,
    ("CONMEBOL", "UEFA"): 0.45,
    ("CONMEBOL", "CONMEBOL"): 0.50,
    ("CONMEBOL", "CONCACAF"): 0.70,
    ("CONMEBOL", "CAF"): 0.65,
    ("CONMEBOL", "AFC"): 0.70,
    ("CONCACAF", "UEFA"): 0.25,
    ("CONCACAF", "CONMEBOL"): 0.30,
    ("CONCACAF", "CONCACAF"): 0.50,
    ("CONCACAF", "CAF"): 0.45,
    ("CONCACAF", "AFC"): 0.50,
    ("CAF", "UEFA"): 0.30,
    ("CAF", "CONMEBOL"): 0.35,
    ("CAF", "CONCACAF"): 0.55,
    ("CAF", "CAF"): 0.50,
    ("CAF", "AFC"): 0.55,
    ("AFC", "UEFA"): 0.25,
    ("AFC", "CONMEBOL"): 0.30,
    ("AFC", "CONCACAF"): 0.50,
    ("AFC", "CAF"): 0.45,
    ("AFC", "AFC"): 0.50,
}

# 地理加成（100% 加权后）
GEO_ADVANTAGE = {
    ("home", "neutral"): 0.12,
    ("neutral", "neutral"): 0.0,
    ("neutral", "home"): -0.12,
    ("away", "neutral"): -0.08,
    ("neutral", "away"): 0.08,
}


class ComprehensiveMatchPredictor:
    """参考系统的加权预测器。"""

    def __init__(self, fifa_points: dict | None = None,
                 fifa_ranks: dict | None = None,
                 confederations: dict | None = None,
                 elo_model: EloModel | None = None,
                 draw_correction: DrawCorrection | None = None,
                 config: dict | None = None):
        # Static data
        self.fifa_points = fifa_points or {}
        self.fifa_ranks = fifa_ranks or {}
        self.confederations = confederations or {}
        self.elo = elo_model or EloModel()
        self.dc = draw_correction

        # Weight config
        self.weights = config or {
            "fifa_points": 0.35, "rank_diff": 0.15, "status": 0.15,
            "coach": 0.10, "h2h_20y": 0.10, "continent": 0.05,
            "environment": 0.05, "dark_horse": 0.05,
        }
        # Status sub-weights
        self.status_weights = {
            "internal": 0.40, "psychology": 0.25,
            "opponent_strategy": 0.20, "other": 0.15,
        }
        # Internal sub-weights
        self.internal_weights = {
            "locker_room": 0.40, "age": 0.20, "injuries": 0.30, "logistics": 0.10,
        }
        # Coach tier (A=1, B=0.5, C=0, D=-0.5)
        self.coach_tier_value = {"A": 0.12, "B": 0.06, "C": 0.0, "D": -0.05}
        # Coach 20-year record tiers
        self.coach_history = [
            (1, 0.30),   # 1 year
            (2, 0.25),
            (3, 0.20),
            (5, 0.15),
            (8, 0.10),
            (10, 0.07),
            (15, 0.05),
            (20, 0.02),
            (999, 0.01),
        ]
        # Squad tier bonus (1st tier +12% etc.)
        self.squad_tier = {
            1: 0.12, 2: 0.08, 3: 0.05, 4: 0.02, 5: -0.02, 6: -0.05,
        }
        # Status factors for 5 categories (default grade D)
        self.status_default = "D"
        self.status_grades = {
            "A": 0.10, "B": 0.05, "C": 0.0, "D": -0.03, "E": -0.08,
        }
        # Draw amplification
        self.draw_k = 1.2  # 衰减速度
        self.draw_dmax = 0.35  # 最大平局率

    def _lookup_fifa_points(self, diff: int) -> dict:
        for (lo, hi), p in FIFA_POINT_TABLE.items():
            if lo <= abs(diff) < hi:
                if diff < 0:
                    return {"win": p["loss"], "draw": p["draw"], "loss": p["win"]}
                return p
        return {"win": 0.50, "draw": 0.25, "loss": 0.25}

    def _lookup_rank_diff(self, diff: int) -> dict:
        for (lo, hi), p in RANK_DIFF_TABLE.items():
            if lo <= abs(diff) < hi:
                if diff < 0:
                    return {"win": p["loss"], "draw": p["draw"], "loss": p["win"]}
                return p
        return {"win": 0.50, "draw": 0.25, "loss": 0.25}

    def _coach_bonus(self, coach_data: dict) -> float:
        """Coach tier (12%) + history record (8%)."""
        if not coach_data:
            return 0.0
        tier = self.coach_tier_value.get(coach_data.get("tier", "C"), 0.0)
        years_known = coach_data.get("years_known", 0)
        history_bonus = 0.0
        for threshold, val in self.coach_history:
            if years_known < threshold:
                history_bonus = val
                break
        else:
            history_bonus = 0.01
        return tier + history_bonus * 0.3

    def _status_score(self, status_data: dict) -> float:
        """Status factors 15%: internal + psychology + opponent + other."""
        if not status_data:
            return 0.0
        s = 0.0
        # Internal 40%
        internal = status_data.get("internal", {})
        for cat, w in self.internal_weights.items():
            grade = internal.get(cat, self.status_default)
            s += self.status_grades[grade] * w * self.status_weights["internal"]
        # Psychology 25%
        psych = status_data.get("psychology", self.status_default)
        s += self.status_grades[psych] * self.status_weights["psychology"]
        # Opponent 20%
        opp = status_data.get("opponent_strategy", self.status_default)
        s += self.status_grades[opp] * self.status_weights["opponent_strategy"]
        # Other 15%
        oth = status_data.get("other", self.status_default)
        s += self.status_grades[oth] * self.status_weights["other"]
        return s

    def _h2h_bonus(self, h2h_data: dict) -> float:
        """20-year record 10% (already in self.coach_history logic via H2H)."""
        if not h2h_data:
            return 0.0
        # h2h_data: {"years_known": X, "win_rate": 0.55}
        win_rate = h2h_data.get("win_rate", 0.5)
        years = h2h_data.get("years_known", 0)
        # Time decay
        decay = max(0.05, 0.85 - 0.1 * (years - 1))
        return (win_rate - 0.5) * decay

    def _continent_bonus(self, home_conf: str, away_conf: str) -> float:
        """Continental matchup 5%."""
        if not home_conf or not away_conf:
            return 0.0
        base = CONTINENTAL_TABLE.get((home_conf, away_conf), 0.5)
        return (base - 0.5) * 2  # 转换为正负贡献

    def _environment_bonus(self, host: str | None, home: str, venue: str) -> float:
        """Environment 5%: host nation, climate, altitude."""
        if not host:
            return 0.0
        if host == home:
            return 0.10
        if host == venue:
            return 0.05
        return 0.0

    def _dark_horse_factor(self, home_form_recent: float, away_form_recent: float) -> float:
        """Dark horse / upset 5%."""
        if home_form_recent > away_form_recent + 0.3:
            return 0.05
        if away_form_recent > home_form_recent + 0.3:
            return -0.05
        return 0.0

    def _draw_amplification(self, p_draw: float, strength_diff: float) -> float:
        """Dynamic draw amplification: P(draw) * Dmax * (1 - I^Δ)^k"""
        amplification = self.draw_dmax * (1 - abs(strength_diff) ** 2) ** self.draw_k
        return p_draw + (amplification - p_draw) * 0.5

    def predict(self, home: str, away: str, neutral: bool = True,
                status_home: dict | None = None, status_away: dict | None = None,
                coach_home: dict | None = None, coach_away: dict | None = None,
                h2h: dict | None = None,
                home_form_recent: float = 0.5, away_form_recent: float = 0.5,
                host: str | None = None, venue: str | None = None) -> dict:
        """Comprehensive prediction with all weighted factors."""

        # 1. FIFA积分差
        home_pts = self.fifa_points.get(home, 1500)
        away_pts = self.fifa_points.get(away, 1500)
        fifa_diff = home_pts - away_pts
        fifa_probs = self._lookup_fifa_points(fifa_diff)

        # 2. FIFA 排名差
        home_rank = self.fifa_ranks.get(home, 50)
        away_rank = self.fifa_ranks.get(away, 50)
        rank_diff = away_rank - home_rank  # 排名越低越好
        rank_probs = self._lookup_rank_diff(rank_diff)

        # 3. Status 15%
        home_status = self._status_score(status_home or {})
        away_status = self._status_score(status_away or {})
        status_bonus = (home_status - away_status) * self.weights["status"]

        # 4. Coach 10%
        home_coach = self._coach_bonus(coach_home or {})
        away_coach = self._coach_bonus(coach_away or {})
        coach_bonus = (home_coach - away_coach) * self.weights["coach"]

        # 5. 20-year record 10%
        h2h_bonus = self._h2h_bonus(h2h or {}) * self.weights["h2h_20y"]

        # 6. Continent 5%
        home_conf = self.confederations.get(home, "")
        away_conf = self.confederations.get(away, "")
        continent_bonus = self._continent_bonus(home_conf, away_conf) * self.weights["continent"]

        # 7. Environment 5%
        env_bonus = self._environment_bonus(host, home, venue or "") * self.weights["environment"]

        # 8. Dark horse 5%
        dark_horse = self._dark_horse_factor(home_form_recent, away_form_recent) * self.weights["dark_horse"]

        # Combine: weighted base probabilities from FIFA + rank
        base_home = (fifa_probs["win"] * 0.6 + rank_probs["win"] * 0.4)
        base_draw = (fifa_probs["draw"] * 0.6 + rank_probs["draw"] * 0.4)
        base_away = (fifa_probs["loss"] * 0.6 + rank_probs["loss"] * 0.4)

        # Apply bonuses (as probability shifts)
        total_bonus_home = (status_bonus + coach_bonus + h2h_bonus +
                            continent_bonus + env_bonus + dark_horse)
        # Split total_bonus between H/A, reduce D proportionally
        p_home = base_home + total_bonus_home
        p_away = base_away - total_bonus_home
        p_draw = base_draw * 0.5  # reduce base draw

        # Draw amplification (动态平局放大)
        strength_diff = (fifa_diff / 200) + (rank_diff / 50) + total_bonus_home
        strength_diff = float(max(-1.0, min(1.0, strength_diff)))
        p_draw = float(self._draw_amplification(p_draw, strength_diff))

        # Normalize
        total = p_home + p_draw + p_away
        p_home, p_draw, p_away = p_home/total, p_draw/total, p_away/total

        return {
            "home": home, "away": away,
            "fifa_points_home": home_pts, "fifa_points_away": away_pts,
            "fifa_diff": fifa_diff, "fifa_rank_diff": rank_diff,
            "p_home": round(p_home, 3), "p_draw": round(p_draw, 3), "p_away": round(p_away, 3),
            "components": {
                "base": {"home": base_home, "draw": base_draw, "away": base_away},
                "status_bonus": status_bonus, "coach_bonus": coach_bonus,
                "h2h_bonus": h2h_bonus, "continent_bonus": continent_bonus,
                "env_bonus": env_bonus, "dark_horse": dark_horse,
                "strength_diff": strength_diff,
            },
        }
