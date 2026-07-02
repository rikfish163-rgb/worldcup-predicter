#!/usr/bin/env python3
"""
回测验证: 用历史比赛数据验证 Dixon-Coles 模型 vs 纯 Elo vs 市场基线。

指标:
  - Brier Score (越低越好) + Log-Loss (越低越好)
  - Handicap Accuracy (让球盘3结果命中率, 越高越好)

数据: eloratings.net TSV (全史进球) — 取最近 N 场国际比赛做样本

用法: .venv/bin/python wc_analysis/backtest.py
"""
from __future__ import annotations
import math, json
from pathlib import Path
import numpy as np

# 复用 predict.py 的核心函数
import sys
sys.path.insert(0, str(Path(__file__).parent))
from predict import (
    score_matrix, elo_to_lambdas, dc_tau, poisson_pmf,
    RHO, AVG_GOALS, ELO_CACHE
)

DATA_DIR = Path(__file__).parent / "data"


def load_recent_matches(team_file: str, n: int = 50) -> list[dict]:
    """从一队的 TSV 加载最近 n 场有比分的比赛。"""
    cache = ELO_CACHE / f"{team_file}.tsv"
    if not cache.exists():
        return []
    text = cache.read_text(encoding="utf-8")
    matches = []
    for line in text.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        try:
            hs = int(parts[5])
            as_ = int(parts[6])
            home_elo = float(parts[10].replace("−", "-"))
            away_elo = float(parts[11].replace("−", "-"))
        except (ValueError, IndexError):
            continue
        matches.append({
            "home_elo": home_elo, "away_elo": away_elo,
            "hs": hs, "as": as_,
            "result": "h" if hs > as_ else ("d" if hs == as_ else "a"),
        })
    return matches[-n:]


def brier_score(prob: dict, actual: str) -> float:
    """Brier score for a single prediction. Lower = better."""
    bs = 0.0
    for k in ("h", "d", "a"):
        target = 1.0 if k == actual else 0.0
        bs += (prob.get(k, 0.33) - target) ** 2
    return bs


def log_loss_single(prob: dict, actual: str) -> float:
    """Log loss for a single prediction."""
    p = max(prob.get(actual, 0.33), 0.001)
    return -math.log(p)


def elo_simple_prob(elo_h: float, elo_a: float) -> dict:
    """简版 Elo 概率(无 Dixon-Coles 校正,作为基线)。"""
    dr = elo_h - elo_a
    we = 1.0 / (10 ** (-dr / 400) + 1)
    draw = 0.26 * math.exp(-((dr / 300) ** 2))
    p_h = we - 0.5 * draw
    p_a = 1 - we - 0.5 * draw
    p_h = max(p_h, 0.02); p_a = max(p_a, 0.02)
    s = p_h + p_a + draw
    return {"h": p_h/s, "d": draw/s, "a": p_a/s}


def dc_prob(elo_h: float, elo_a: float) -> dict:
    """Dixon-Coles 模型概率(我们的模型)。"""
    lam_h, lam_a = elo_to_lambdas(elo_h, elo_a)
    mat = score_matrix(lam_h, lam_a)
    n = mat.shape[0]
    p_h = sum(mat[i, j] for i in range(n) for j in range(n) if i > j)
    p_d = sum(mat[i, j] for i in range(n) for j in range(n) if i == j)
    p_a = sum(mat[i, j] for i in range(n) for j in range(n) if i < j)
    return {"h": float(p_h), "d": float(p_d), "a": float(p_a), "mat": mat}


def handicap_prob(score_mat: np.ndarray, handicap_line: float) -> dict:
    """
    从比分矩阵计算让球盘概率。

    Args:
        score_mat: Dixon-Coles 比分矩阵
        handicap_line: 让球线 (负=主让, 如-1表示主让1球)

    Returns:
        {"h": 主让胜概率, "d": 平局概率, "a": 客让胜概率}
    """
    n = score_mat.shape[0]
    threshold = -handicap_line  # 主队需赢的净胜球数
    hc_h = 0.0
    hc_d = 0.0
    hc_a = 0.0

    for i in range(n):
        for j in range(n):
            diff = i - j
            if abs(threshold - round(threshold)) < 0.01:
                # 整数盘口
                thr_int = int(round(threshold))
                if diff > thr_int:
                    hc_h += score_mat[i, j]
                elif diff == thr_int:
                    hc_d += score_mat[i, j]
                else:
                    hc_a += score_mat[i, j]
            else:
                # 半球盘口(无平局)
                if diff > threshold:
                    hc_h += score_mat[i, j]
                else:
                    hc_a += score_mat[i, j]

    return {"h": float(hc_h), "d": float(hc_d), "a": float(hc_a)}


def handicap_result(hs: int, as_: int, handicap_line: float) -> str:
    """
    根据实际比分和让球线计算让球盘结果。

    Returns:
        "h" = 主让胜, "d" = 平局, "a" = 客让胜
    """
    adjusted_diff = hs - as_ + handicap_line  # 调整后的净胜球
    threshold = -handicap_line

    if abs(threshold - round(threshold)) < 0.01:
        # 整数盘口
        if adjusted_diff > 0:
            return "h"
        elif adjusted_diff == 0:
            return "d"
        else:
            return "a"
    else:
        # 半球盘口(无平局)
        if adjusted_diff > 0:
            return "h"
        else:
            return "a"


def main():
    teams = ["Germany", "Japan", "Netherlands", "Sweden", "France",
             "Argentina", "Brazil", "Spain", "England"]

    all_matches = []
    seen = set()
    for t in teams:
        for m in load_recent_matches(t, n=80):
            key = (m["home_elo"], m["away_elo"], m["hs"], m["as"])
            if key not in seen:
                seen.add(key)
                all_matches.append(m)

    print(f"回测样本: {len(all_matches)} 场国际比赛")
    print()

    # ═══ 常规胜平负回测 ═══
    print("=" * 50)
    print("常规胜平负 (HAD) 回测")
    print("=" * 50)
    results_had = {"elo_simple": [], "dixon_coles": []}
    for m in all_matches:
        elo_p = elo_simple_prob(m["home_elo"], m["away_elo"])
        dc_p = dc_prob(m["home_elo"], m["away_elo"])
        actual = m["result"]

        results_had["elo_simple"].append((brier_score(elo_p, actual), log_loss_single(elo_p, actual)))
        results_had["dixon_coles"].append((brier_score(dc_p, actual), log_loss_single(dc_p, actual)))

    print(f"{'模型':<15} {'Brier↓':>8} {'LogLoss↓':>10}")
    print("-" * 35)
    for name, scores in results_had.items():
        avg_brier = np.mean([s[0] for s in scores])
        avg_ll = np.mean([s[1] for s in scores])
        print(f"{name:<15} {avg_brier:>8.4f} {avg_ll:>10.4f}")

    dc_brier = np.mean([s[0] for s in results_had["dixon_coles"]])
    elo_brier = np.mean([s[0] for s in results_had["elo_simple"]])
    diff = elo_brier - dc_brier
    print(f"\nDixon-Coles vs 纯Elo: Brier 改善 {diff:+.4f} ({'✅ 更好' if diff > 0 else '⚠️ 更差'})")

    # ═══ 让球盘回测 (固定-1让球) ═══
    print("\n" + "=" * 50)
    print("让球盘 (HHAD -1) 回测")
    print("=" * 50)

    handicap_line = -1.0  # 主队让1球
    results_hhad = {"elo_simple": [], "dixon_coles": []}
    accuracy_hhad = {"elo_simple": 0, "dixon_coles": 0}

    for m in all_matches:
        # 计算让球盘实际结果
        hc_actual = handicap_result(m["hs"], m["as"], handicap_line)

        # Elo Simple 让球盘预测
        elo_p_had = elo_simple_prob(m["home_elo"], m["away_elo"])
        elo_mat = score_matrix(*elo_to_lambdas(m["home_elo"], m["away_elo"]))
        elo_hc_p = handicap_prob(elo_mat, handicap_line)

        # Dixon-Coles 让球盘预测
        dc_p_full = dc_prob(m["home_elo"], m["away_elo"])
        dc_hc_p = handicap_prob(dc_p_full["mat"], handicap_line)

        # Brier / LogLoss
        results_hhad["elo_simple"].append((brier_score(elo_hc_p, hc_actual), log_loss_single(elo_hc_p, hc_actual)))
        results_hhad["dixon_coles"].append((brier_score(dc_hc_p, hc_actual), log_loss_single(dc_hc_p, hc_actual)))

        # 命中率 (预测最高概率 == 实际结果)
        elo_pred = max(elo_hc_p, key=elo_hc_p.get)
        dc_pred = max(dc_hc_p, key=dc_hc_p.get)
        if elo_pred == hc_actual:
            accuracy_hhad["elo_simple"] += 1
        if dc_pred == hc_actual:
            accuracy_hhad["dixon_coles"] += 1

    print(f"{'模型':<15} {'Brier↓':>8} {'LogLoss↓':>10} {'命中率↑':>8}")
    print("-" * 45)
    for name, scores in results_hhad.items():
        avg_brier = np.mean([s[0] for s in scores])
        avg_ll = np.mean([s[1] for s in scores])
        acc = accuracy_hhad[name] / len(all_matches)
        print(f"{name:<15} {avg_brier:>8.4f} {avg_ll:>10.4f} {acc:>8.1%}")

    dc_hc_brier = np.mean([s[0] for s in results_hhad["dixon_coles"]])
    elo_hc_brier = np.mean([s[0] for s in results_hhad["elo_simple"]])
    diff_hc = elo_hc_brier - dc_hc_brier
    print(f"\nDixon-Coles vs 纯Elo: Brier 改善 {diff_hc:+.4f} ({'✅ 更好' if diff_hc > 0 else '⚠️ 更差'})")

    dc_acc = accuracy_hhad["dixon_coles"] / len(all_matches)
    elo_acc = accuracy_hhad["elo_simple"] / len(all_matches)
    print(f"命中率: Dixon-Coles {dc_acc:.1%} vs Elo Simple {elo_acc:.1%} (差{(dc_acc-elo_acc)*100:+.1f}%)")

    out = {
        "n_matches": len(all_matches),
        # 常规盘
        "had": {
            "elo_simple_brier": round(float(np.mean([s[0] for s in results_had["elo_simple"]])), 5),
            "dixon_coles_brier": round(float(np.mean([s[0] for s in results_had["dixon_coles"]])), 5),
            "improvement": round(float(diff), 5),
        },
        # 让球盘
        "hhad": {
            "handicap_line": handicap_line,
            "elo_simple_brier": round(float(np.mean([s[0] for s in results_hhad["elo_simple"]])), 5),
            "dixon_coles_brier": round(float(np.mean([s[0] for s in results_hhad["dixon_coles"]])), 5),
            "improvement": round(float(diff_hc), 5),
            "elo_accuracy": round(float(elo_acc), 4),
            "dixon_coles_accuracy": round(float(dc_acc), 4),
        }
    }
    (DATA_DIR / "backtest.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n结果已存 data/backtest.json")


if __name__ == "__main__":
    main()
