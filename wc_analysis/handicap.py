"""Asian handicap probability derivation from score-line matrix.

Fixes the quarter-ball bug identified by @oracle.
Standard approach: model raw goals (Poisson/Dixon-Coles) → derive AH probabilities.

Supports all 4 line types:
- Integer (-1.0, 0.0, +1.0): full push on exact margin
- Half-ball (-0.5, +0.5): no push
- Quarter-ball (-0.75, -0.25, +0.25, +0.75): stake split between adjacent lines
"""

from __future__ import annotations

import numpy as np


def prob_cover(score_matrix: np.ndarray, line: float) -> dict:
    """Compute Asian handicap cover probability for a given line.

    Args:
        score_matrix: 2D array P(home=i, away=j), must sum to ~1.0
        line: handicap line (negative = home favored, e.g., -1.0 means home -1)

    Returns:
        {"p_win": float, "p_push": float, "p_loss": float}
        where p_win is full win, p_push is half-refund, p_loss is full loss.
        For quarter-ball, p_push is the half-push component from the integer half.
    """
    n = score_matrix.shape[0]
    rows, cols = np.indices((n, n))
    diff = rows - cols  # home_goals - away_goals

    if _is_integer(line):
        p_win = float(score_matrix[diff > line].sum())
        p_push = float(score_matrix[diff == line].sum())
        p_loss = float(score_matrix[diff < line].sum())
        return {"p_win": p_win, "p_push": p_push, "p_loss": p_loss}

    elif _is_half(line):
        p_win = float(score_matrix[diff > line].sum())
        p_loss = float(score_matrix[diff < line].sum())
        return {"p_win": p_win, "p_push": 0.0, "p_loss": p_loss}

    else:
        # Quarter-ball: split between two adjacent half-lines
        lo = np.floor(line * 2) / 2  # e.g., -0.75 → -1.0
        hi = np.ceil(line * 2) / 2   # e.g., -0.75 → -0.5
        result_lo = prob_cover(score_matrix, lo)
        result_hi = prob_cover(score_matrix, hi)
        return {
            "p_win": 0.5 * result_lo["p_win"] + 0.5 * result_hi["p_win"],
            "p_push": 0.5 * result_lo["p_push"] + 0.5 * result_hi["p_push"],
            "p_loss": 0.5 * result_lo["p_loss"] + 0.5 * result_hi["p_loss"],
        }


def _is_integer(x: float) -> bool:
    return abs(x - round(x)) < 1e-9


def _is_half(x: float) -> bool:
    return abs(x * 2 - round(x * 2)) < 1e-9 and not _is_integer(x)


def expected_value(prob: dict, odds: float) -> float:
    """EV for 1-unit stake: (p_win * (odds-1)) + (p_push * 0) + (p_loss * -1)."""
    return prob["p_win"] * (odds - 1) + prob["p_loss"] * (-1)


def implied_prob_from_odds(odds: float) -> float:
    """Convert decimal odds to implied probability (raw, not de-vigged)."""
    return 1.0 / odds if odds > 0 else 0.0


def handicap_to_1x2(score_matrix: np.ndarray, line: float) -> dict:
    """Convert AH probabilities to 1X2-style (win/push/loss) for display."""
    p = prob_cover(score_matrix, line)
    return {
        "home_cover": p["p_win"] + 0.5 * p["p_push"],
        "push": p["p_push"],
        "away_cover": p["p_loss"] + 0.5 * p["p_push"],
    }


def derive_all_handicaps(score_matrix: np.ndarray, lines: list[float] | None = None) -> dict:
    """Derive AH probabilities for multiple lines at once."""
    if lines is None:
        lines = [-2.0, -1.75, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25,
                 0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
    return {line: prob_cover(score_matrix, line) for line in lines}
