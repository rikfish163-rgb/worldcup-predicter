#!/usr/bin/env python3
"""
自进化 Harness — 预测系统的在线学习闭环

核心思路:
  1. 每场比赛结束后,对比"预测概率" vs "真实结果"
  2. 累积计算 Brier/LogLoss,追踪模型校准度随时间的变化
  3. 诊断系统性偏差:平局被低估?强队被高估?某些特征导致的错误?
  4. 自动调整参数(ρ、市场融合权重、时间衰减ξ、xG影响幅度)

闭环:
  predict.py 生成预测 → 比赛结束 → harness.py 收集结果 →
  诊断偏差 → 写入 data/params_override.json → predict.py 下次运行读取最新参数

用法:
  .venv/bin/python wc_analysis/harness.py                # 运行一次: 收集结果+诊断+调参
  .venv/bin/python wc_analysis/harness.py --status       # 查看当前模型状态
  .venv/bin/python wc_analysis/harness.py --reset        # 重置学习历史
"""
from __future__ import annotations
import json, math
from pathlib import Path
from datetime import datetime

import numpy as np

DATA_DIR = Path(__file__).parent / "data"
HISTORY_FILE = DATA_DIR / "prediction_history.json"
PARAMS_FILE = DATA_DIR / "params_override.json"
DIAGNOSTICS_FILE = DATA_DIR / "diagnostics.json"

# 默认参数(predict.py 的初始值)
DEFAULT_PARAMS = {
    "rho": -0.13,
    "avg_goals": 2.55,
    "home_adv": 0.0,
    "market_weight": 0.60,
    "xi_decay": 0.0065,
    "xg_blend": 0.15,
    "elo_blend": 0.60,
    "hist_blend": 0.25,
}

# 参数调整边界(不允许无限漂移)
PARAM_BOUNDS = {
    "rho": (-0.25, 0.0),
    "avg_goals": (2.0, 3.2),
    "market_weight": (0.3, 0.8),
    "xi_decay": (0.003, 0.012),
    "xg_blend": (0.0, 0.30),
}


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def save_history(history: list[dict]):
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def load_params() -> dict:
    if PARAMS_FILE.exists():
        override = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
        return {**DEFAULT_PARAMS, **override}
    return DEFAULT_PARAMS.copy()


def save_params(params: dict):
    PARAMS_FILE.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")


def brier(prob: dict, actual: str) -> float:
    return sum((prob.get(k, 0.33) - (1.0 if k == actual else 0.0))**2 for k in ("h", "d", "a"))


def log_loss(prob: dict, actual: str) -> float:
    p = max(prob.get(actual, 0.33), 0.001)
    return -math.log(p)


def collect_results() -> list[dict]:
    """从 FBref 缓存和 ESPN 收集已完赛比赛的真实结果,与历史预测匹配。"""
    history = load_history()
    existing_keys = {h["key"] for h in history}

    # 从 predictions.json 历史版本收集预测
    # (每次 predict.py 运行都会覆盖,所以我们需要在比赛结束前保存)
    # 策略: 从 eloratings TSV 获取最近比赛结果
    new_results = []

    # 从所有队伍的 Elo TSV 提取最近已完赛比赛
    elo_cache = DATA_DIR / "elo_cache"
    if not elo_cache.exists():
        return new_results

    # 导入 TEAM_DB
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from predict import TEAM_DB

    code_to_cn = {v[1]: k for k, v in TEAM_DB.items()}
    seen_matches = set()

    for team_cn, (fname, code) in TEAM_DB.items():
        tsv = elo_cache / f"{fname}.tsv"
        if not tsv.exists():
            continue
        for line in tsv.read_text(encoding="utf-8").strip().split("\n")[-20:]:
            parts = line.split("\t")
            if len(parts) < 12:
                continue
            try:
                y, m_val, d_val = int(parts[0]), int(parts[1]), int(parts[2])
                if y < 2026:
                    continue
                home_code, away_code = parts[3], parts[4]
                hs, as_ = int(parts[5]), int(parts[6])
            except (ValueError, IndexError):
                continue

            key = f"{y}-{m_val:02d}-{d_val:02d}_{home_code}_{away_code}"
            if key in seen_matches or key in existing_keys:
                continue
            seen_matches.add(key)

            if hs > as_:
                result = "h"
            elif hs == as_:
                result = "d"
            else:
                result = "a"

            home_cn = code_to_cn.get(home_code, home_code)
            away_cn = code_to_cn.get(away_code, away_code)

            new_results.append({
                "key": key,
                "date": f"{y}-{m_val:02d}-{d_val:02d}",
                "home": home_cn,
                "away": away_cn,
                "score": f"{hs}-{as_}",
                "result": result,
                "collected_at": datetime.now().isoformat(),
            })

    return new_results


def diagnose(history: list[dict]) -> dict:
    """诊断系统性偏差。"""
    if len(history) < 5:
        return {"status": "数据不足", "n": len(history)}

    # 只分析有预测概率的记录
    scored = [h for h in history if h.get("posterior") and h.get("result")]
    if len(scored) < 5:
        return {"status": "有概率的记录不足", "n": len(scored)}

    briers = [brier(h["posterior"], h["result"]) for h in scored]
    lls = [log_loss(h["posterior"], h["result"]) for h in scored]

    # 校准分析: 按预测概率分桶,看实际命中率
    # 简化版: 看"模型认为>60%的事件"实际命中率
    high_conf = [h for h in scored if max(h["posterior"].values()) > 0.6]
    high_conf_hits = sum(1 for h in high_conf if h["result"] == max(h["posterior"], key=h["posterior"].get))

    # 平局偏差: 模型预测平局概率 vs 实际平局率
    pred_draw_avg = np.mean([h["posterior"].get("d", 0.25) for h in scored])
    actual_draw_rate = sum(1 for h in scored if h["result"] == "d") / len(scored)
    draw_bias = actual_draw_rate - pred_draw_avg

    # 强队偏差: Elo差>200时,强队胜率 vs 预测
    strong_games = [h for h in scored if abs(h.get("elo_diff", 0)) > 200]
    if strong_games:
        pred_strong_win = np.mean([
            h["posterior"]["h"] if h.get("elo_diff", 0) > 0 else h["posterior"]["a"]
            for h in strong_games
        ])
        actual_strong_win = sum(
            1 for h in strong_games
            if (h.get("elo_diff", 0) > 0 and h["result"] == "h") or
               (h.get("elo_diff", 0) < 0 and h["result"] == "a")
        ) / len(strong_games)
        strong_bias = actual_strong_win - pred_strong_win
    else:
        strong_bias = 0.0

    return {
        "status": "ok",
        "n_scored": len(scored),
        "avg_brier": round(float(np.mean(briers)), 4),
        "avg_logloss": round(float(np.mean(lls)), 4),
        "high_conf_accuracy": f"{high_conf_hits}/{len(high_conf)}" if high_conf else "N/A",
        "draw_bias": round(draw_bias, 3),  # 正=平局被低估
        "strong_team_bias": round(strong_bias, 3),  # 正=强队被高估
        "recommendations": [],
    }


def evolve_params(diagnostics: dict, current_params: dict) -> dict:
    """基于诊断结果自动微调参数。保守策略:每次只小幅调整。"""
    new_params = current_params.copy()
    recs = []

    # 平局被低估 → 增大 |ρ| (加强低比分相关)
    if diagnostics.get("draw_bias", 0) > 0.04:
        old = new_params["rho"]
        new_params["rho"] = max(PARAM_BOUNDS["rho"][0], old - 0.01)
        recs.append(f"平局被低估(bias={diagnostics['draw_bias']:+.3f}): ρ {old:.3f}→{new_params['rho']:.3f}")

    # 平局被高估 → 减小 |ρ|
    elif diagnostics.get("draw_bias", 0) < -0.04:
        old = new_params["rho"]
        new_params["rho"] = min(PARAM_BOUNDS["rho"][1], old + 0.01)
        recs.append(f"平局被高估(bias={diagnostics['draw_bias']:+.3f}): ρ {old:.3f}→{new_params['rho']:.3f}")

    # 强队被高估 → 减小 avg_goals(少进球=更多平局/冷门)或增大 market_weight
    if diagnostics.get("strong_team_bias", 0) < -0.05:
        old = new_params["market_weight"]
        new_params["market_weight"] = min(PARAM_BOUNDS["market_weight"][1], old + 0.03)
        recs.append(f"强队被高估(bias={diagnostics['strong_team_bias']:+.3f}): 市场权重 {old:.2f}→{new_params['market_weight']:.2f}")

    # 强队被低估 → 减小 market_weight (更信模型)
    elif diagnostics.get("strong_team_bias", 0) > 0.05:
        old = new_params["market_weight"]
        new_params["market_weight"] = max(PARAM_BOUNDS["market_weight"][0], old - 0.03)
        recs.append(f"强队被低估(bias={diagnostics['strong_team_bias']:+.3f}): 市场权重 {old:.2f}→{new_params['market_weight']:.2f}")

    diagnostics["recommendations"] = recs
    return new_params


def main():
    import sys

    if "--reset" in sys.argv:
        for f in [HISTORY_FILE, PARAMS_FILE, DIAGNOSTICS_FILE]:
            if f.exists():
                f.unlink()
        print("✅ 学习历史已重置")
        return

    params = load_params()

    if "--status" in sys.argv:
        history = load_history()
        diag = diagnose(history)
        print(f"📊 模型状态")
        print(f"   历史记录: {len(history)} 场")
        print(f"   当前参数: {json.dumps(params, ensure_ascii=False)}")
        if diag.get("avg_brier"):
            print(f"   Brier: {diag['avg_brier']}")
            print(f"   平局偏差: {diag['draw_bias']:+.3f} ({'被低估' if diag['draw_bias']>0 else '被高估'})")
            print(f"   强队偏差: {diag['strong_team_bias']:+.3f}")
        return

    # 主流程: 收集 → 诊断 → 调参
    print("🔄 自进化 Harness 运行中...")
    print()

    # 1. 收集新结果
    new_results = collect_results()
    history = load_history()

    if new_results:
        print(f"📥 收集到 {len(new_results)} 场新结果")
        for r in new_results[:5]:
            print(f"   {r['date']} {r['home']} vs {r['away']}: {r['score']} ({r['result']})")
        if len(new_results) > 5:
            print(f"   ... 及其他 {len(new_results)-5} 场")
        history.extend(new_results)
        save_history(history)
    else:
        print("📥 无新结果(Elo缓存未更新或已全部收录)")

    print(f"\n📊 总历史: {len(history)} 场")

    # 2. 诊断
    diag = diagnose(history)
    if diag["status"] == "ok":
        print(f"   Brier Score: {diag['avg_brier']}")
        print(f"   平局偏差: {diag['draw_bias']:+.3f} ({'⚠️ 被低估' if diag['draw_bias']>0.03 else '✅ OK' if abs(diag['draw_bias'])<0.03 else '⚠️ 被高估'})")
        print(f"   强队偏差: {diag['strong_team_bias']:+.3f} ({'⚠️ 强队实际更强' if diag['strong_team_bias']>0.04 else '✅ OK' if abs(diag['strong_team_bias'])<0.04 else '⚠️ 冷门多于预期'})")

        # 3. 自动调参
        new_params = evolve_params(diag, params)
        if new_params != params:
            print(f"\n🧬 参数进化:")
            for rec in diag.get("recommendations", []):
                print(f"   → {rec}")
            save_params(new_params)
            print(f"   已写入 {PARAMS_FILE.name}")
        else:
            print(f"\n✅ 参数无需调整(偏差在容忍范围内)")
    else:
        print(f"   {diag['status']} (需≥5场有概率的记录才能诊断)")

    # 保存诊断
    (DIAGNOSTICS_FILE).write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 诊断结果存入 {DIAGNOSTICS_FILE.name}")


if __name__ == "__main__":
    main()
