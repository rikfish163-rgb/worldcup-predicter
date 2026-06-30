#!/usr/bin/env python3
"""自进化诊断 + 调参: 把后验预测与真实小组赛结果配对,
算系统性偏差(平局/强队/主场/校准), 据此进化参数并写入 params_override.json.

核心 bug 修复: 预测历史用中文队名 key, 真实结果用英文全称.
本脚本通过 predict.py 的 TEAM_DB 做中文<->英文桥接, 并对英文做模糊归一化
(Czechia/Czech Republic, South_Korea/South Korea, Curaçao/Curacao,
 Ivory_Coast/Ivory Coast, DR_Congo/DR Congo 等), 使交集不再为 0.

诊断维度:
  1. 平局偏差   : 模型平均平局概率 vs 实际平局率
  2. 强队偏差   : |Elo差|>150 时强队预测胜率 vs 实际胜率
  3. 主场偏差   : 主胜预测概率 vs 实际主胜率 (世界杯名义中立场)
  4. 整体校准   : Brier / LogLoss (model prior 与 calibrated posterior 各算一次)

调参逻辑 (在现有最优参数附近做有界微调):
  - 平局被低估 -> 增大 |RHO| (更负, Dixon-Coles 提升低比分/平局质量)
  - 平局被高估 -> 减小 |RHO|
  - 强队被高估 -> 提示提高市场权重 (并轻微下调 HOME_ADV / 收敛 AVG_GOALS)
  - 主胜被系统高/低估 -> 调整 HOME_ADV
"""
import json
import math
import unicodedata
from datetime import date
from pathlib import Path

from predict import TEAM_DB, DATA_DIR

HISTORY = DATA_DIR / "prediction_history.json"
RESULTS = DATA_DIR / "wc_results.json"
PARAMS_OVERRIDE = DATA_DIR / "params_override.json"

# 进化基线 = predict.py 的原始交叉验证最优值 (硬编码, 不读 override).
# 关键: 必须用原始基线而非已生效的 override 值, 否则每次运行会在上次结果上
# 再次进化, 导致参数被反复叠加推向极端 (幂等性保证).
BASE_RHO = -0.20        # 交叉验证最优 (2286场, 2020-2026)
BASE_AVG_GOALS = 2.50   # 交叉验证最优
BASE_HOME_ADV = 0.40    # 世界杯中立场基线

# 调参安全边界 (避免单次小样本把参数推到极端)
RHO_BOUNDS = (-0.35, -0.05)
AVG_GOALS_BOUNDS = (2.30, 2.75)
HOME_ADV_BOUNDS = (0.10, 0.60)

STRONG_ELO_GAP = 150.0   # |Elo差| 超过此值视为"有明显强弱"
LOW_CONFIDENCE_N = 10    # 样本 < 此值则标注置信度低


def normalize_en(name: str) -> str:
    """英文队名归一: 去重音/下划线->空格/小写/去掉 republic 等冗余词."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))  # Curaçao -> Curacao
    s = s.replace("_", " ").lower().strip()
    # 同义改写: 让 Czechia <-> Czech Republic, DR Congo <-> Congo 等对齐
    s = s.replace("czech republic", "czech")
    s = s.replace("czechia", "czech")
    s = s.replace("dr congo", "congo")
    s = s.replace("democratic republic of the congo", "congo")
    s = " ".join(s.split())
    return s


# 中文 -> 归一化英文
CN_TO_NORM = {cn: normalize_en(en) for cn, (en, code) in TEAM_DB.items()}


def cn_to_norm(team_cn: str) -> str | None:
    return CN_TO_NORM.get(team_cn)


def result_outcome(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "h"
    if home_goals < away_goals:
        return "a"
    return "d"


def load_posteriors() -> list[dict]:
    data = json.loads(Path(HISTORY).read_text(encoding="utf-8"))
    return [x for x in data if isinstance(x, dict) and "posterior" in x]


def load_results() -> list[dict]:
    data = json.loads(Path(RESULTS).read_text(encoding="utf-8"))
    return data["results"] if isinstance(data, dict) else data


def build_result_index(results: list[dict]) -> dict:
    """以无序队伍对(归一化英文 frozenset)为 key, 存 (home_norm, outcome, raw)."""
    idx = {}
    for m in results:
        hn = normalize_en(m["home"])
        an = normalize_en(m["away"])
        key = frozenset((hn, an))
        outcome = result_outcome(m["home_goals"], m["away_goals"])
        idx.setdefault(key, []).append({
            "home_norm": hn,
            "away_norm": an,
            "outcome": outcome,
            "raw": m,
        })
    return idx


def brier(posterior: dict, actual: str) -> float:
    classes = ["h", "d", "a"]
    return sum((posterior.get(c, 0.0) - (1.0 if c == actual else 0.0)) ** 2
               for c in classes)


def _norm3(probs: dict) -> dict:
    """取 h/d/a 三键并归一(防止 src 字段或浮点漂移影响)。"""
    p = {k: max(float(probs.get(k, 0.0)), 0.0) for k in ("h", "d", "a")}
    s = sum(p.values())
    if s <= 0:
        return {"h": 1 / 3, "d": 1 / 3, "a": 1 / 3}
    return {k: v / s for k, v in p.items()}


def logloss(probs: dict, actual: str) -> float:
    p = max(min(probs.get(actual, 0.0), 1 - 1e-12), 1e-12)
    return -math.log(p)


def build_matched() -> tuple[list[dict], list[tuple]]:
    """把 24 条后验预测与 72 条真实小组赛结果配对。
    每条记录保留 model prior / calibrated posterior / market / elo_diff,
    并把所有概率翻转到"预测主队"视角后再翻到真实主队视角,
    保证 actual 与 probs 同一参照系。
    """
    posteriors = load_posteriors()
    results = load_results()
    ridx = build_result_index(results)

    matched: list[dict] = []
    unmatched_pred: list[tuple] = []

    for p in posteriors:
        home_cn, away_cn = p["home"], p["away"]
        hn, an = cn_to_norm(home_cn), cn_to_norm(away_cn)
        if hn is None or an is None:
            unmatched_pred.append((p["date"], home_cn, away_cn, "not_in_TEAM_DB"))
            continue
        key = frozenset((hn, an))
        candidates = ridx.get(key)
        if not candidates:
            unmatched_pred.append((p["date"], home_cn, away_cn, "no_result"))
            continue

        cand = min(candidates, key=lambda c: abs((_d(c["raw"]["date"]) - _d(p["date"]))))

        actual_home_persp = cand["outcome"]
        flip = cand["home_norm"] != hn  # 真实主队与预测主队相反
        if flip:
            actual = {"h": "a", "a": "h", "d": "d"}[actual_home_persp]
        else:
            actual = actual_home_persp

        post = _norm3(p["posterior"])
        prior = _norm3(p.get("prior", post))
        market = _norm3(p["market"]) if p.get("market") else None
        # elo_diff 历史里是"预测主队 - 预测客队"; 与 prior/post 同视角, 无需翻转
        elo_diff = float(p.get("elo_diff", 0.0))

        pred_pick = max(post, key=post.get)
        matched.append({
            "date_pred": p["date"],
            "date_real": cand["raw"]["date"],
            "home_cn": home_cn,
            "away_cn": away_cn,
            "real": f'{cand["raw"]["home"]} {cand["raw"]["home_goals"]}-'
                    f'{cand["raw"]["away_goals"]} {cand["raw"]["away"]}',
            "prior": prior,
            "posterior": post,
            "market": market,
            "elo_diff": elo_diff,
            "pred_pick": pred_pick,
            "actual": actual,
            "hit": pred_pick == actual,
            "brier_post": round(brier(post, actual), 4),
            "brier_prior": round(brier(prior, actual), 4),
        })

    return matched, unmatched_pred


def diagnose(matched: list[dict]) -> dict:
    """计算系统性偏差。所有概率/结果均以'预测主队'视角对齐。"""
    n = len(matched)
    d: dict = {"n": n}
    if n == 0:
        return d

    # ---- 1. 平局偏差 (用 model prior, 因为 RHO/HOME_ADV 直接影响 prior) ----
    pred_draw_prior = sum(m["prior"]["d"] for m in matched) / n
    pred_draw_post = sum(m["posterior"]["d"] for m in matched) / n
    actual_draw = sum(1 for m in matched if m["actual"] == "d") / n
    d["draw"] = {
        "pred_prior": pred_draw_prior,
        "pred_post": pred_draw_post,
        "actual": actual_draw,
        "bias_prior": pred_draw_prior - actual_draw,  # <0 => 模型低估平局
    }

    # ---- 2. 强队偏差 (|Elo差|>150) ----
    strong = [m for m in matched if abs(m["elo_diff"]) >= STRONG_ELO_GAP]
    if strong:
        # "强队"= elo_diff 符号那侧. 强队赢 = (diff>0 且 actual=h) 或 (diff<0 且 actual=a)
        def strong_pred_p(m):
            return m["prior"]["h"] if m["elo_diff"] > 0 else m["prior"]["a"]

        def strong_won(m):
            return ((m["elo_diff"] > 0 and m["actual"] == "h") or
                    (m["elo_diff"] < 0 and m["actual"] == "a"))

        pred_strong = sum(strong_pred_p(m) for m in strong) / len(strong)
        actual_strong = sum(1 for m in strong if strong_won(m)) / len(strong)
        d["strong"] = {
            "n": len(strong),
            "pred_winrate": pred_strong,
            "actual_winrate": actual_strong,
            "bias": pred_strong - actual_strong,  # >0 => 强队被高估
        }
    else:
        d["strong"] = {"n": 0}

    # ---- 3. 主场偏差 (名义主队, 世界杯中立场) ----
    pred_home = sum(m["prior"]["h"] for m in matched) / n
    actual_home = sum(1 for m in matched if m["actual"] == "h") / n
    d["home"] = {
        "pred_winrate": pred_home,
        "actual_winrate": actual_home,
        "bias": pred_home - actual_home,  # >0 => 主胜被高估
    }

    # ---- 4. 整体校准: Brier / LogLoss (prior 与 posterior 各一) ----
    d["calib"] = {
        "brier_prior": sum(m["brier_prior"] for m in matched) / n,
        "brier_post": sum(m["brier_post"] for m in matched) / n,
        "logloss_prior": sum(logloss(m["prior"], m["actual"]) for m in matched) / n,
        "logloss_post": sum(logloss(m["posterior"], m["actual"]) for m in matched) / n,
        "hit_rate": sum(1 for m in matched if m["hit"]) / n,
    }
    return d


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def evolve_params(diag: dict) -> dict:
    """根据偏差给出有界参数调整。返回新参数 + 决策日志。"""
    decisions: list[str] = []
    new_rho = BASE_RHO
    new_avg = BASE_AVG_GOALS
    new_home = BASE_HOME_ADV
    market_weight_hint = "保持 (让球盘 0.70 / 常规盘 0.60)"

    n = diag.get("n", 0)
    low_conf = n < LOW_CONFIDENCE_N

    # --- 平局偏差 -> RHO ---
    draw = diag.get("draw", {})
    dbias = draw.get("bias_prior", 0.0)  # <0 模型低估平局
    # 缩放: 每 1% 低估 -> |RHO| 增 ~0.004 (有界, 小样本时半步)
    step_scale = 0.40 if low_conf else 0.80
    drho = _clamp(-dbias, -0.10, 0.10) * step_scale  # 低估(dbias<0)-> drho>0 -> rho 更负
    # rho 更负 = |rho| 更大. rho_new = rho_base - drho
    new_rho = _clamp(BASE_RHO - drho, *RHO_BOUNDS)
    if abs(dbias) < 0.03:
        decisions.append(f"平局偏差 {dbias:+.1%} 在阈值内, RHO 基本不动")
    elif dbias < 0:
        decisions.append(f"平局被低估 {dbias:+.1%} -> 增大|RHO|: {BASE_RHO:.3f}->{new_rho:.3f}")
    else:
        decisions.append(f"平局被高估 {dbias:+.1%} -> 减小|RHO|: {BASE_RHO:.3f}->{new_rho:.3f}")

    # --- 强队偏差 -> 市场权重提示 (+ 轻微收敛 AVG_GOALS) ---
    strong = diag.get("strong", {})
    if strong.get("n", 0) >= 3:
        sbias = strong["bias"]  # >0 强队被高估
        if sbias > 0.05:
            market_weight_hint = "提高市场权重 (让球盘 0.70->0.75, 常规盘 0.60->0.65)"
            decisions.append(
                f"强队被高估 {sbias:+.1%} (n={strong['n']}) -> 建议提高市场权重, "
                f"模型过度自信强队")
        elif sbias < -0.05:
            decisions.append(
                f"强队被低估 {sbias:+.1%} (n={strong['n']}) -> 模型对强队偏保守, 市场权重可微降")
        else:
            decisions.append(f"强队偏差 {sbias:+.1%} (n={strong['n']}) 可接受, 市场权重不变")
    else:
        decisions.append(f"强队样本不足 (n={strong.get('n',0)}<3), 跳过强队调参")

    # --- 主场偏差 -> HOME_ADV ---
    home = diag.get("home", {})
    hbias = home.get("bias", 0.0)  # >0 主胜被高估
    # 每 1% 高估 -> HOME_ADV 降 ~0.6 (有界). 世界杯中立场, 高估主胜应削主场效应.
    dhome = _clamp(hbias, -0.15, 0.15) * (30.0 if low_conf else 60.0) / 100.0
    new_home = _clamp(BASE_HOME_ADV - dhome, *HOME_ADV_BOUNDS)
    if abs(hbias) < 0.04:
        decisions.append(f"主场偏差 {hbias:+.1%} 在阈值内, HOME_ADV 基本不动")
    elif hbias > 0:
        decisions.append(f"主胜被高估 {hbias:+.1%} -> 降 HOME_ADV: {BASE_HOME_ADV:.3f}->{new_home:.3f}")
    else:
        decisions.append(f"主胜被低估 {hbias:+.1%} -> 升 HOME_ADV: {BASE_HOME_ADV:.3f}->{new_home:.3f}")

    # AVG_GOALS: 仅当 prior Brier 明显差且强队被高估时轻微下调(降低极端比分自信)
    calib = diag.get("calib", {})
    if strong.get("n", 0) >= 3 and strong.get("bias", 0) > 0.08 and calib.get("brier_prior", 0) > 0.55:
        new_avg = _clamp(BASE_AVG_GOALS - (0.03 if low_conf else 0.06), *AVG_GOALS_BOUNDS)
        decisions.append(f"模型整体过度自信 -> 轻微下调 AVG_GOALS: {BASE_AVG_GOALS:.2f}->{new_avg:.2f}")
    else:
        decisions.append(f"AVG_GOALS 维持 {BASE_AVG_GOALS:.2f}")

    return {
        "rho": round(new_rho, 4),
        "avg_goals": round(new_avg, 4),
        "home_adv": round(new_home, 4),
        "_decisions": decisions,
        "_market_weight_hint": market_weight_hint,
        "_low_confidence": low_conf,
        "_n": n,
    }


def write_override(params: dict, hit_rate: float | None = None,
                   brier_post: float | None = None) -> None:
    payload = {
        "rho": params["rho"],
        "avg_goals": params["avg_goals"],
        "home_adv": params["home_adv"],
        "evolved_by": "evolve_groupstage.py",
        "evolved_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "sample_n": params["_n"],
        "low_confidence": params["_low_confidence"],
        "market_weight_hint": params["_market_weight_hint"],
        "decisions": params["_decisions"],
    }
    if hit_rate is not None:
        payload["hit_rate"] = round(hit_rate, 4)
    if brier_post is not None:
        payload["brier_post"] = round(brier_post, 4)
    PARAMS_OVERRIDE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _pct(x):
    return f"{x*100:.1f}%"


# 自动进化最小写入样本门控: 低于此场次只诊断不写 override, 避免小样本漂移
MIN_WRITE_N = 12


def run_evolution(write: bool = True, min_write_n: int = MIN_WRITE_N) -> dict:
    """无副作用(可选写)的自进化入口, 供 predict.py auto_refresh_loop 每日调用.

    流程: 配对预测↔真实结果 → 诊断系统性偏差 → 有界调参 → (样本足够才)写 params_override.
    幂等: evolve_params 基于硬编码原始基线 BASE_*, 不读已有 override, 反复调用不漂移.

    返回 dict: {n, matched, diagnosis, evolved_params, written(bool), reason}
    """
    matched, unmatched = build_matched()
    n = len(matched)
    if n == 0:
        return {"n": 0, "written": False, "reason": "无可诊断样本"}

    diag = diagnose(matched)
    evolved = evolve_params(diag)

    written = False
    reason = ""
    if not write:
        reason = "write=False, 仅诊断"
    elif n < min_write_n:
        reason = f"样本 {n} < {min_write_n}, 仅诊断不写 override (防小样本漂移)"
    else:
        write_override(evolved, hit_rate=diag["calib"]["hit_rate"],
                       brier_post=diag["calib"]["brier_post"])
        written = True
        reason = f"样本 {n} >= {min_write_n}, 已写 {PARAMS_OVERRIDE.name}"

    return {
        "n": n,
        "hit_rate": diag["calib"]["hit_rate"],
        "brier_post": diag["calib"]["brier_post"],
        "diagnosis": diag,
        "evolved_params": {k: v for k, v in evolved.items() if not k.startswith("_")},
        "decisions": evolved["_decisions"],
        "written": written,
        "reason": reason,
    }


def main() -> None:
    matched, unmatched = build_matched()
    n = len(matched)

    # ---- 配对明细 ----
    print(f"成功匹配小组赛场次: {n} / 后验预测 {len(load_posteriors())} 条")
    if n < LOW_CONFIDENCE_N:
        print(f"⚠ 样本 {n} < {LOW_CONFIDENCE_N}, 诊断置信度低, 调参取半步并设安全边界")
    lbl = {"h": "主胜", "d": "平", "a": "客胜"}
    if n:
        print("-" * 100)
        for m in matched:
            mark = "✓" if m["hit"] else "✗"
            pp = m["posterior"]
            print(f"{mark} {m['home_cn']} vs {m['away_cn']:<8} | 真实: {m['real']:<32} "
                  f"| 后验 h{pp['h']:.2f}/d{pp['d']:.2f}/a{pp['a']:.2f} "
                  f"预测={lbl[m['pred_pick']]} 实际={lbl[m['actual']]} "
                  f"Brier={m['brier_post']:.3f}")

    if n == 0:
        print("无可诊断样本, 退出")
        return

    diag = diagnose(matched)

    # ---- 诊断报告 ----
    print("=" * 100)
    print("【系统性偏差诊断】")
    dr = diag["draw"]
    print(f"  1. 平局偏差 : 模型先验平均平局率 {_pct(dr['pred_prior'])} | "
          f"后验 {_pct(dr['pred_post'])} | 实际平局率 {_pct(dr['actual'])} "
          f"=> 偏差 {dr['bias_prior']*100:+.1f}pt "
          f"({'低估' if dr['bias_prior']<0 else '高估'})")

    st = diag["strong"]
    if st.get("n", 0):
        print(f"  2. 强队偏差 : |Elo差|>{int(STRONG_ELO_GAP)} 共 {st['n']} 场 | "
              f"预测强队胜率 {_pct(st['pred_winrate'])} | 实际 {_pct(st['actual_winrate'])} "
              f"=> 偏差 {st['bias']*100:+.1f}pt "
              f"({'高估' if st['bias']>0 else '低估'})")
    else:
        print(f"  2. 强队偏差 : |Elo差|>{int(STRONG_ELO_GAP)} 场次 0, 无法评估")

    hm = diag["home"]
    print(f"  3. 主场偏差 : 预测主胜率 {_pct(hm['pred_winrate'])} | 实际主胜率 {_pct(hm['actual_winrate'])} "
          f"=> 偏差 {hm['bias']*100:+.1f}pt "
          f"({'高估' if hm['bias']>0 else '低估'}) [世界杯名义中立场]")

    cb = diag["calib"]
    print(f"  4. 整体校准 : 命中率 {_pct(cb['hit_rate'])} | "
          f"Brier(prior) {cb['brier_prior']:.4f} / Brier(post) {cb['brier_post']:.4f} | "
          f"LogLoss(prior) {cb['logloss_prior']:.4f} / LogLoss(post) {cb['logloss_post']:.4f}")
    print(f"     参照: 三选一均匀分布 Brier=0.667, LogLoss=1.099")

    # ---- 调参决策 ----
    evolved = evolve_params(diag)
    print("=" * 100)
    print("【调参决策】")
    for d in evolved["_decisions"]:
        print(f"  - {d}")
    print(f"  - 市场权重建议: {evolved['_market_weight_hint']}")

    # ---- 新旧参数对比 + 写入 ----
    print("=" * 100)
    print("【新旧参数对比】")
    rows = [
        ("RHO", BASE_RHO, evolved["rho"]),
        ("AVG_GOALS", BASE_AVG_GOALS, evolved["avg_goals"]),
        ("HOME_ADV", BASE_HOME_ADV, evolved["home_adv"]),
    ]
    print(f"  {'参数':<12}{'旧值':>10}{'新值':>10}{'变化':>12}")
    for name, old, new in rows:
        delta = new - old
        flag = "" if abs(delta) < 1e-9 else ("↑" if delta > 0 else "↓")
        print(f"  {name:<12}{old:>10.4f}{new:>10.4f}{delta:>+11.4f}{flag}")

    write_override(evolved)
    print("-" * 100)
    print(f"✅ 进化后参数已写入: {PARAMS_OVERRIDE}")
    print(f"   (predict.py 启动时自动加载 params_override.json 覆盖默认参数)")

    # ---- 未匹配统计 ----
    skipped = [u for u in unmatched if u[3] == "not_in_TEAM_DB"]
    noresult = [u for u in unmatched if u[3] == "no_result"]
    if skipped:
        print("-" * 100)
        print(f"跳过(非WC/不在TEAM_DB) {len(skipped)} 条: " +
              ", ".join(f"{u[1]}-{u[2]}" for u in skipped))
    if noresult:
        print(f"无对应真实结果 {len(noresult)} 条: " +
              ", ".join(f"{u[1]}-{u[2]}" for u in noresult))


def _d(s: str):
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d)


if __name__ == "__main__":
    main()
