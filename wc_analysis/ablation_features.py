#!/usr/bin/env python3
"""消融回测: 在 18 场已匹配真实小组赛上, 对每个候选 SofaScore 特征
(门将活跃度 gk_saves / 纪律 red_cards / 休息天数 rest_days) 做 on/off 对比。

方法论 (诚实, 可复现):
  · 复用 predict.py 的真实预测管线 (elo_to_lambdas + Dixon-Coles + 历史加权 + xG + 平局LR模型)
  · baseline = 当前线上模型 (已含 def_xga90)。每个候选特征作为额外的 λ 乘子注入 get_adjustments 的同款机制。
  · 18 场的 elo / market / 真实结果 全部取自 prediction_history.json + wc_results.json (与 evolve_groupstage 完全同源)
  · 校准: 常规盘 w=0.6 (与线上 had 校准一致), 与铁律"盘口已吸收公开信息"对齐
  · 评分: Brier(post) / Brier(prior) / 命中率(smart argmax) / LogLoss(post)

诚实声明:
  · SofaScore 特征是 2026-06-30 的快照 (last_n=20 / rest_days=距采集时刻)。
    18 场比赛发生在 06-23~06-27。
    - red/xGA/gk_saves 是近20场赛季聚合, 含极少量赛后场次 → 轻微前视, 仅作方向性检验, 已标注。
    - rest_days 快照 (如日本0.4/秘鲁21.0) 是"距 06-30 的天数", 对 06-23 的历史场次反因果,
      无法在此样本上做有效验证 → 单独按 538 设计意图构造"对齐版"测试或直接判数据不足。
"""
import json
import math
import unicodedata
from datetime import date
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))

import predict as P
from predict import (TEAM_DB, DATA_DIR, get_elo, predict_match, calibrate,
                     get_adjustments, _load_sofascore, score_matrix)

HISTORY = DATA_DIR / "prediction_history.json"
RESULTS = DATA_DIR / "wc_results.json"

# ── 队名归一 (复用 evolve_groupstage 的逻辑) ──
def normalize_en(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("_", " ").lower().strip()
    s = s.replace("czech republic", "czech").replace("czechia", "czech")
    s = s.replace("dr congo", "congo").replace("democratic republic of the congo", "congo")
    return " ".join(s.split())

CN_TO_NORM = {cn: normalize_en(en) for cn, (en, code) in TEAM_DB.items()}

def result_outcome(h, a):
    return "h" if h > a else ("a" if h < a else "d")

def _d(s):
    y, m, dd = (int(x) for x in s.split("-"))
    return date(y, m, dd)

def build_result_index(results):
    idx = {}
    for m in results:
        hn, an = normalize_en(m["home"]), normalize_en(m["away"])
        idx.setdefault(frozenset((hn, an)), []).append({
            "home_norm": hn, "outcome": result_outcome(m["home_goals"], m["away_goals"]),
            "raw": m})
    return idx

def _norm3(p):
    q = {k: max(float(p.get(k, 0.0)), 0.0) for k in ("h", "d", "a")}
    s = sum(q.values())
    return {k: v/s for k, v in q.items()} if s > 0 else {"h": 1/3, "d": 1/3, "a": 1/3}

def brier(p, actual):
    return sum((p.get(c, 0.0) - (1.0 if c == actual else 0.0))**2 for c in ("h", "d", "a"))

def logloss(p, actual):
    return -math.log(max(min(p.get(actual, 0.0), 1-1e-12), 1e-12))

def smart_pick(p):
    if p["d"] >= 0.25 and abs(p["h"] - p["a"]) <= 0.12:
        return "d"
    return max(("h", "d", "a"), key=lambda k: p[k])

# ── 候选特征因子 ──
SOFA = _load_sofascore()

def gk_saves_factor(team_cn):
    """门将活跃度(扑救数)代理。注意: 高扑救数 = 对手射门多 = 防守压力大,
    并非门将"好"; 但也反映门将参与度。中性基准 = 全队中位数, 封顶±15%。
    作为'对手进攻 λ'的乘子方向待定 → 这里测两种符号都做。返回 (factor_as_def, matches)。"""
    prof = SOFA.get(team_cn)
    if not isinstance(prof, dict):
        return 1.0, 0
    m = prof.get("gk_saves_matches", 0)
    saves = prof.get("gk_saves_pg")
    if m < 5 or not saves:
        return 1.0, m
    return saves, m

def red_factor(team_cn):
    """纪律: 近N场场均红牌。红牌→少打一人→本队进攻 λ 下降。
    设计: 每 0.1 红牌/场 → λ×(1-0.1*k)。返回 (red_pg, matches)。"""
    prof = SOFA.get(team_cn)
    if not isinstance(prof, dict):
        return None, 0
    m = prof.get("discipline_matches", 0)
    if m < 5:
        return None, m
    return prof.get("red_cards_pg"), m

def rest_days(team_cn):
    prof = SOFA.get(team_cn)
    if not isinstance(prof, dict):
        return None
    return prof.get("rest_days")


def base_prior(home_cn, away_cn, elo_h, elo_a, with_sofa_xga=True):
    """重算常规盘 prior。with_sofa_xga 控制是否保留已上线的 def_xga90。
    其余 adj (角球/磨合/伤病/天气) 走 get_adjustments 真实逻辑。"""
    adj_h, adj_a, _ = get_adjustments(home_cn, away_cn)
    if not with_sofa_xga:
        # 抵消 def_xga90 的影响: 重新算一遍不含 xga 的 adj
        sda = P._sofa_defense_factor(away_cn)
        sdh = P._sofa_defense_factor(home_cn)
        if abs(sda - 1.0) > 0.01:
            adj_h /= sda
        if abs(sdh - 1.0) > 0.01:
            adj_a /= sdh
    pred = predict_match(elo_h, elo_a, adj_h, adj_a, home_cn=home_cn, away_cn=away_cn)
    return _norm3(pred["prior"]), adj_h, adj_a


def apply_factor(adj_h, adj_a, home_cn, away_cn, elo_h, elo_a, fh=1.0, fa=1.0):
    """在已有 adj 上额外乘特征因子 fh(home λ)/fa(away λ), 重算 prior。"""
    pred = predict_match(elo_h, elo_a, adj_h * fh, adj_a * fa,
                         home_cn=home_cn, away_cn=away_cn)
    return _norm3(pred["prior"])


def load_matched():
    hist = json.loads(HISTORY.read_text(encoding="utf-8"))
    posts = [x for x in hist if isinstance(x, dict) and "posterior" in x]
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    results = results["results"] if isinstance(results, dict) else results
    ridx = build_result_index(results)
    matched = []
    for p in posts:
        hn, an = CN_TO_NORM.get(p["home"]), CN_TO_NORM.get(p["away"])
        if hn is None or an is None:
            continue
        cands = ridx.get(frozenset((hn, an)))
        if not cands:
            continue
        cand = min(cands, key=lambda c: abs(_d(c["raw"]["date"]) - _d(p["date"])))
        outcome = cand["outcome"]
        flip = cand["home_norm"] != hn
        actual = {"h": "a", "a": "h", "d": "d"}[outcome] if flip else outcome
        eh, ea = get_elo(p["home"]), get_elo(p["away"])
        if eh is None or ea is None:
            continue
        matched.append({
            "home": p["home"], "away": p["away"], "actual": actual,
            "market": _norm3(p["market"]) if p.get("market") else None,
            "elo_h": eh, "elo_a": ea, "elo_diff": p.get("elo_diff", eh-ea),
        })
    return matched


def score_arm(matched, prior_fn, w=0.6):
    """prior_fn(m) -> prior dict. 校准 + 评分."""
    n = len(matched)
    bp = bpost = ll = hit = 0.0
    for m in matched:
        prior = prior_fn(m)
        post = _norm3(calibrate(prior, m["market"], w=w)) if m["market"] else prior
        a = m["actual"]
        bp += brier(prior, a)
        bpost += brier(post, a)
        ll += logloss(post, a)
        hit += 1 if smart_pick(post) == a else 0
    return {"n": n, "brier_prior": bp/n, "brier_post": bpost/n,
            "logloss_post": ll/n, "hit": hit/n}


def main():
    matched = load_matched()
    n = len(matched)
    print(f"消融样本: {n} 场已匹配真实小组赛 (与 evolve_groupstage 同源)\n")

    # 预先缓存每场的 base adj (含线上 xga) 与 elo
    for m in matched:
        prior, adj_h, adj_a = base_prior(m["home"], m["away"], m["elo_h"], m["elo_a"], with_sofa_xga=True)
        m["_adj_h"], m["_adj_a"] = adj_h, adj_a

    def baseline_prior(m):
        return apply_factor(m["_adj_h"], m["_adj_a"], m["home"], m["away"], m["elo_h"], m["elo_a"])

    base = score_arm(matched, baseline_prior)
    print("="*92)
    print(f"BASELINE (线上模型, 含 def_xga90):")
    print(f"  命中 {base['hit']*100:.1f}%  Brier(prior) {base['brier_prior']:.4f}  "
          f"Brier(post) {base['brier_post']:.4f}  LogLoss(post) {base['logloss_post']:.4f}")
    print("="*92)

    # ─── 候选1: 纪律 red_cards (红牌→本队进攻λ下降) ───
    # 测多档强度 k: λ_factor = 1 - k * red_pg  (red_pg 已是场均, 量级 0~0.31)
    print("\n【候选1: 纪律 red_cards_pg → 进攻 λ 折损】")
    cov = sum(1 for m in matched if red_factor(m["home"])[0] is not None and red_factor(m["away"])[0] is not None)
    print(f"  双方均有≥5场纪律样本的场次: {cov}/{n}")
    for k in (0.5, 1.0, 2.0):
        def red_prior(m, k=k):
            rh, _ = red_factor(m["home"]); ra, _ = red_factor(m["away"])
            fh = max(0.85, 1 - k*rh) if rh is not None else 1.0
            fa = max(0.85, 1 - k*ra) if ra is not None else 1.0
            return apply_factor(m["_adj_h"], m["_adj_a"], m["home"], m["away"],
                                m["elo_h"], m["elo_a"], fh, fa)
        r = score_arm(matched, red_prior)
        db = r["brier_post"]-base["brier_post"]; dh=r["hit"]-base["hit"]
        print(f"  k={k:<4} 命中 {r['hit']*100:.1f}% ({dh*100:+.1f}pt) | "
              f"Brier(post) {r['brier_post']:.4f} ({db:+.4f}) | LogLoss {r['logloss_post']:.4f}")

    # ─── 候选2: 门将扑救活跃度 gk_saves_pg ───
    print("\n【候选2: 门将 gk_saves_pg (扑救活跃度代理)】")
    gk_cov = sum(1 for m in matched if gk_saves_factor(m["home"])[1]>=5 and gk_saves_factor(m["away"])[1]>=5)
    print(f"  双方均有≥5场扑救样本: {gk_cov}/{n}  (gk_goals_prevented 门控0/52 → 已弃, 此为活跃度代理)")
    # 高扑救可能=门将强(防守好→压低对手λ) 或 =防线漏(对手射门多). 测两种符号方向.
    league_saves = 2.5  # 大致中位
    for sign, label in ((-1, "高扑救=门将强→压低对手λ"), (1, "高扑救=防线被压→放大对手λ")):
        def gk_prior(m, sign=sign):
            sh, mh = gk_saves_factor(m["home"]); sa, ma = gk_saves_factor(m["away"])
            # home 门将影响 away 进攻 λ; away 门将影响 home 进攻 λ
            fa = 1.0; fh = 1.0
            if mh >= 5:
                dev = (sh-league_saves)/league_saves
                fa = max(0.85, min(1.15, 1 + sign*0.10*dev))  # home GK -> away atk
            if ma >= 5:
                dev = (sa-league_saves)/league_saves
                fh = max(0.85, min(1.15, 1 + sign*0.10*dev))  # away GK -> home atk
            return apply_factor(m["_adj_h"], m["_adj_a"], m["home"], m["away"],
                                m["elo_h"], m["elo_a"], fh, fa)
        r = score_arm(matched, gk_prior)
        db=r["brier_post"]-base["brier_post"]; dh=r["hit"]-base["hit"]
        print(f"  {label:<24} 命中 {r['hit']*100:.1f}% ({dh*100:+.1f}pt) | "
              f"Brier(post) {r['brier_post']:.4f} ({db:+.4f}) | LogLoss {r['logloss_post']:.4f}")

    # ─── 候选3: 休息天数 rest_days ───
    print("\n【候选3: 休息天数 rest_days (538 泊松参数)】")
    rd_cov = sum(1 for m in matched if rest_days(m["home"]) is not None and rest_days(m["away"]) is not None)
    print(f"  双方均有 rest_days: {rd_cov}/{n}")
    print("  ⚠ 诚实声明: rest_days 是'距 2026-06-30 采集时刻'的快照, 18 场比赛在 06-23~06-27,")
    print("     对历史场次反因果(如某队赛后停赛20天, 快照=20天但赛前其实只休3天)。")
    print("     在此样本上做的是'快照值 vs 结果'的伪相关检验, 不是真实赛前休息。仅供证伪, 不作采纳依据。")
    # 538设计: 休息少→疲劳→λ略降。每多1天休息(基准3天)→λ微调
    REST_BASE = 3.3
    for k in (0.01, 0.02):
        def rest_prior(m, k=k):
            rh = rest_days(m["home"]); ra = rest_days(m["away"])
            fh = max(0.90, min(1.10, 1 + k*(rh-REST_BASE))) if rh is not None else 1.0
            fa = max(0.90, min(1.10, 1 + k*(ra-REST_BASE))) if ra is not None else 1.0
            return apply_factor(m["_adj_h"], m["_adj_a"], m["home"], m["away"],
                                m["elo_h"], m["elo_a"], fh, fa)
        r = score_arm(matched, rest_prior)
        db=r["brier_post"]-base["brier_post"]; dh=r["hit"]-base["hit"]
        print(f"  k={k:<5} 命中 {r['hit']*100:.1f}% ({dh*100:+.1f}pt) | "
              f"Brier(post) {r['brier_post']:.4f} ({db:+.4f}) | LogLoss {r['logloss_post']:.4f}")


if __name__ == "__main__":
    main()
