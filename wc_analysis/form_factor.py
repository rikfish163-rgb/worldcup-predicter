#!/usr/bin/env python3
"""
近期状态特征增强 - 综合状态调整因子

当前基础实现: 最近5场 Elo 趋势
增强维度:
1. 进球波动性 (标准差大 = 不稳定)
2. 连胜/连败 streak 检测
3. Elo 变化加速度 (momentum)
4. 控球率稳定性

返回综合调整因子: 0.88-1.12 (±12% 最大调整幅度)
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TypedDict

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"


class FormFactor(TypedDict):
    """状态因子结构"""
    trend: float  # Elo 趋势斜率 (-1 ~ +1 归一化)
    volatility: float  # 进球波动 (0 ~ 1, 越大越不稳定)
    streak: int  # 连胜(+)/连败(-) 场次
    momentum: float  # Elo 加速度 (-1 ~ +1 归一化)
    possession_stability: float  # 控球率稳定性 (0 ~ 1)
    adjustment_factor: float  # 最终调整因子 (0.88 ~ 1.12)
    components: dict  # 各维度贡献详情


def load_elo_history(team_cn: str) -> pd.DataFrame:
    """加载该队完整 Elo 历史。"""
    from fetch_elo import TEAM_FILE, fetch_team_history

    if team_cn not in TEAM_FILE:
        raise ValueError(f"Team {team_cn} not in TEAM_FILE mapping")

    return fetch_team_history(team_cn, max_age_h=168)  # 一周缓存


def load_fbref_form(team_cn: str) -> list[dict]:
    """加载 FBref 近期战绩。"""
    data = json.loads((DATA_DIR / "fbref_form.json").read_text(encoding="utf-8"))
    return data.get(team_cn, [])


def _safe_std(values: list[float]) -> float:
    """安全计算标准差 (空列表返回0)"""
    if len(values) < 2:
        return 0.0
    return float(pd.Series(values).std())


def _normalize(value: float, min_val: float, max_val: float) -> float:
    """归一化到 [-1, 1] 或 [0, 1]"""
    if max_val == min_val:
        return 0.0
    return (value - min_val) / (max_val - min_val)


def get_elo_trend_and_momentum(team_cn: str, n: int = 5) -> tuple[float, float]:
    """
    计算最近 n 场 Elo 趋势和加速度。

    趋势: 线性回归斜率 (归一化到 -1 ~ +1, 每场±50分为满幅)
    加速度: 二阶差分 (最近3场斜率 - 前3场斜率)

    Returns:
        (trend_normalized, momentum_normalized)
    """
    try:
        df = load_elo_history(team_cn)
        # Import CODE_CN from fetch_elo module
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent))
        from fetch_elo import CODE_CN

        code = [k for k, v in CODE_CN.items() if v == team_cn][0]

        # 提取该队每场的 Elo
        elos = []
        for _, row in df.tail(n * 2).iterrows():  # 取2n场用于计算加速度
            if row["home"] == code:
                elos.append(row["home_elo"])
            elif row["away"] == code:
                elos.append(row["away_elo"])

        if len(elos) < n:
            return 0.0, 0.0

        # 1. 趋势: 最近 n 场线性回归
        recent_n = elos[-n:]
        x = list(range(len(recent_n)))
        mean_x = sum(x) / len(x)
        mean_y = sum(recent_n) / len(recent_n)

        numerator = sum((x[i] - mean_x) * (recent_n[i] - mean_y) for i in range(len(x)))
        denominator = sum((x[i] - mean_x) ** 2 for i in range(len(x)))

        slope = numerator / denominator if denominator != 0 else 0
        trend = max(-1.0, min(1.0, slope / 50.0))  # ±50分/场为满幅

        # 2. 加速度: 前后段斜率差
        if len(elos) >= n + 3:
            first_half = elos[-(n+3):-n]
            second_half = elos[-n:]

            def calc_slope(vals):
                x = list(range(len(vals)))
                mx = sum(x) / len(x)
                my = sum(vals) / len(vals)
                num = sum((x[i] - mx) * (vals[i] - my) for i in range(len(x)))
                den = sum((x[i] - mx) ** 2 for i in range(len(x)))
                return num / den if den != 0 else 0

            slope1 = calc_slope(first_half)
            slope2 = calc_slope(second_half)
            accel = slope2 - slope1
            momentum = max(-1.0, min(1.0, accel / 30.0))  # ±30分为满幅
        else:
            momentum = 0.0

        return trend, momentum

    except Exception:
        return 0.0, 0.0


def get_goals_volatility(team_cn: str, n: int = 5) -> float:
    """
    计算进球波动性 (标准差)。

    Returns:
        volatility: 0 ~ 1 (归一化, 标准差 > 2.0 视为高波动)
    """
    form = load_fbref_form(team_cn)
    played = [m for m in form if m.get("result") in ("W", "D", "L")]

    if len(played) < n:
        return 0.5  # 样本不足返回中性值

    recent = played[-n:]
    goals = [m.get("gf", 0) for m in recent if "gf" in m]

    if not goals:
        return 0.5

    std = _safe_std(goals)
    # 标准差 0 ~ 2.0 映射到 0 ~ 1
    volatility = min(1.0, std / 2.0)

    return volatility


def get_streak(team_cn: str) -> int:
    """
    检测连胜/连败 streak。

    Returns:
        streak: 正数=连胜场次, 负数=连败场次, 0=无连续
    """
    form = load_fbref_form(team_cn)
    played = [m for m in form if m.get("result") in ("W", "D", "L")]

    if not played:
        return 0

    # 从最近向前统计
    streak = 0
    last_result = played[-1]["result"]

    if last_result == "D":
        return 0

    target = last_result
    for match in reversed(played):
        if match["result"] == target:
            streak += 1
        else:
            break

    return streak if target == "W" else -streak


def get_possession_stability(team_cn: str, n: int = 5) -> float:
    """
    计算控球率稳定性 (标准差小 = 稳定)。

    Returns:
        stability: 0 ~ 1 (1 = 极稳定, 标准差 < 5%)
    """
    form = load_fbref_form(team_cn)
    played = [m for m in form if m.get("result") in ("W", "D", "L")]

    if len(played) < n:
        return 0.5

    recent = played[-n:]
    poss_values = [m.get("Poss", m.get("poss")) for m in recent]
    poss_values = [p for p in poss_values if p is not None]

    if len(poss_values) < 3:
        return 0.5

    std = _safe_std(poss_values)
    # 标准差 15% 为高波动, 5% 以下为稳定
    stability = max(0.0, 1.0 - std / 15.0)

    return stability


def calculate_adjustment_factor(
    trend: float,
    momentum: float,
    volatility: float,
    streak: int,
    possession_stability: float,
) -> tuple[float, dict]:
    """
    综合各维度计算调整因子。

    权重分配:
    - Elo 趋势: 30%
    - Elo 加速度: 20%
    - Streak: 25%
    - 进球波动: 15% (反向, 越稳定越好)
    - 控球稳定: 10%

    Returns:
        (adjustment_factor, components_dict)
        调整范围: 0.88 ~ 1.12 (±12%)
    """
    # 趋势贡献: -1~+1 -> -0.036~+0.036 (±3.6%)
    trend_contrib = trend * 0.036

    # 加速度贡献: -1~+1 -> -0.024~+0.024 (±2.4%)
    momentum_contrib = momentum * 0.024

    # Streak 贡献: ±5场为满幅 -> -0.03~+0.03 (±3%)
    streak_normalized = max(-1.0, min(1.0, streak / 5.0))
    streak_contrib = streak_normalized * 0.030

    # 波动贡献 (反向): 0~1 -> -0.018~0 (高波动减分)
    volatility_contrib = -volatility * 0.018

    # 控球稳定贡献: 0~1 -> 0~+0.012 (高稳定加分)
    stability_contrib = possession_stability * 0.012

    # 综合
    total_adjustment = (
        trend_contrib +
        momentum_contrib +
        streak_contrib +
        volatility_contrib +
        stability_contrib
    )

    # 映射到 0.88 ~ 1.12
    factor = 1.0 + total_adjustment
    factor = max(0.88, min(1.12, factor))

    components = {
        "trend": round(trend_contrib, 4),
        "momentum": round(momentum_contrib, 4),
        "streak": round(streak_contrib, 4),
        "volatility": round(volatility_contrib, 4),
        "stability": round(stability_contrib, 4),
        "total": round(total_adjustment, 4),
    }

    return factor, components


def get_form_factor(team: str) -> FormFactor:
    """
    主入口: 获取球队综合状态因子。

    Args:
        team: 中文队名 (如 "荷兰")

    Returns:
        FormFactor 字典, 包含各维度指标和最终调整因子
    """
    # 1. Elo 趋势和加速度
    trend, momentum = get_elo_trend_and_momentum(team, n=5)

    # 2. 进球波动
    volatility = get_goals_volatility(team, n=5)

    # 3. Streak
    streak = get_streak(team)

    # 4. 控球稳定性
    possession_stability = get_possession_stability(team, n=5)

    # 5. 计算综合调整因子
    adjustment_factor, components = calculate_adjustment_factor(
        trend, momentum, volatility, streak, possession_stability
    )

    return FormFactor(
        trend=round(trend, 3),
        volatility=round(volatility, 3),
        streak=streak,
        momentum=round(momentum, 3),
        possession_stability=round(possession_stability, 3),
        adjustment_factor=round(adjustment_factor, 4),
        components=components,
    )


def main():
    """测试: 计算所有队伍的状态因子。"""
    from fetch_elo import TEAM_FILE

    print("=" * 80)
    print("近期状态因子 (Form Factor) 分析")
    print("=" * 80)

    results = {}
    for team in TEAM_FILE.keys():
        try:
            factor = get_form_factor(team)
            results[team] = factor

            print(f"\n【{team}】")
            print(f"  Elo趋势: {factor['trend']:+.3f}  加速度: {factor['momentum']:+.3f}")
            print(f"  Streak: {factor['streak']:+d}场  进球波动: {factor['volatility']:.3f}")
            print(f"  控球稳定: {factor['possession_stability']:.3f}")
            print(f"  → 调整因子: {factor['adjustment_factor']:.4f}  "
                  f"({(factor['adjustment_factor']-1)*100:+.2f}%)")
            print(f"  贡献明细: {factor['components']}")

        except Exception as e:
            print(f"\n【{team}】ERR: {e}")

    # 保存结果
    output_path = DATA_DIR / "form_factors.json"
    output_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n已存储至 {output_path}")


if __name__ == "__main__":
    main()
