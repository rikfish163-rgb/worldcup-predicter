#!/usr/bin/env python3
"""整合分析: 市场赔率(sporttery) × 实力模型(Elo) × 近期状态(FBref)。

三源数据:
  data/odds_parsed.json  —— 体彩去水后的市场隐含概率(市场共识)
  data/elo_model.json    —— eloratings 全史 Elo + Elo 胜平负模型(长期实力)
  data/fbref_form.json   —— FBref 本届及近期逐场战绩/控球(当前状态)

核心方法 —— 市场 vs 模型校准:
  - 市场概率 = 博彩公司+大众资金的共识, 信息最全(含伤停/阵容/临场)。
  - Elo 概率 = 纯实力基线, 不含临场信息。
  - 二者差异 (edge = model - market) 指出"模型认为被低估/高估"的方向。
    edge 显著为正 → 模型看好程度高于市场, 潜在价值方向(value)。
    但: 市场通常更准, 大的 edge 往往意味着"模型漏掉了信息"(伤停等),
    需结合 FBref 近期状态人工研判, 而非盲目相信任一方。

输出: 控制台报告 + data/report.json
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def load(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def recent_form(matches: list[dict], n: int = 6) -> dict:
    """从 FBref 逐场记录里取最近 n 场已结束比赛, 汇总战绩。"""
    played = [m for m in matches if m.get("result") in ("W", "D", "L")]
    last = played[-n:]
    w = sum(1 for m in last if m["result"] == "W")
    d = sum(1 for m in last if m["result"] == "D")
    loss = sum(1 for m in last if m["result"] == "L")
    poss_vals = [m["Poss"] for m in last if m.get("Poss")]
    avg_poss = round(sum(poss_vals) / len(poss_vals), 1) if poss_vals else None
    return {
        "n": len(last),
        "w": w, "d": d, "l": loss,
        "pts_per_game": round((w * 3 + d) / len(last), 2) if last else 0,
        "avg_poss": avg_poss,
        "seq": "".join(m["result"] for m in last),
    }


def first_round(matches: list[dict]) -> dict | None:
    """本届世界杯首轮 (2026-06-11 ~ 2026-06-17) 那一场。"""
    for m in matches:
        d = str(m.get("date", ""))
        if "2026-06-1" in d and m.get("venue") == "Neutral" and m.get("result"):
            return m
    return None


def fmt_pct(p):
    return f"{p:.1%}" if isinstance(p, (int, float)) else "—"


def main():
    odds = load("odds_parsed.json")
    elo = load("elo_model.json")
    form = load("fbref_form.json")

    odds_by_match = {o["match"]: o for o in odds}

    # 我们关心的四场 (主队名来自 elo fixtures)
    report = []
    for fx in elo["fixtures"]:
        home, away = fx["home"], fx["away"]
        match_key = f"{home} vs {away}"
        o = odds_by_match.get(match_key)

        rec = {
            "match": match_key,
            "elo": {"home": fx["elo_home"], "away": fx["elo_away"], "diff": fx["model"]["dr"]},
            "model_prob": {k: fx["model"][k] for k in ("home", "draw", "away")},
            "market_prob": None,
            "handicap": None,
            "form": {
                "home": recent_form(form.get(home, [])),
                "away": recent_form(form.get(away, [])),
            },
            "first_round": {
                "home": first_round(form.get(home, [])),
                "away": first_round(form.get(away, [])),
            },
            "ttg_market": None,
            "crs_top": None,
        }
        if o:
            if "had" in o:
                rec["market_prob"] = o["had"]["prob"]
            if "hhad" in o:
                rec["handicap"] = {
                    "line": o["hhad"]["handicap"],
                    "prob": o["hhad"]["prob"],
                }
            if "ttg" in o:
                best = max(o["ttg"]["prob"].items(), key=lambda x: x[1])
                rec["ttg_market"] = {"most_likely": best[0], "prob": best[1],
                                     "dist": o["ttg"]["prob"]}
            rec["crs_top"] = o.get("crs_top")

        # 校准: edge = model - market (仅当有标准胜平负盘)
        if rec["market_prob"]:
            rec["edge"] = {
                k: rec["model_prob"][k] - rec["market_prob"][k]
                for k in ("home", "draw", "away")
            }
        report.append(rec)

    (DATA_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- 控制台报告 ----
    for r in report:
        print("\n" + "=" * 64)
        print(f"  {r['match']}")
        e = r["elo"]
        print(f"  Elo: {e['home']:.0f} vs {e['away']:.0f}  (差 {e['diff']:+.0f})")

        mp = r["model_prob"]
        print(f"  [实力模型] 主 {fmt_pct(mp['home'])} / 平 {fmt_pct(mp['draw'])} / 客 {fmt_pct(mp['away'])}")
        if r["market_prob"]:
            kp = r["market_prob"]
            print(f"  [市场共识] 主 {fmt_pct(kp['home'])} / 平 {fmt_pct(kp['draw'])} / 客 {fmt_pct(kp['away'])}")
            ed = r["edge"]
            print(f"  [校准 edge] 主 {ed['home']:+.1%} / 平 {ed['draw']:+.1%} / 客 {ed['away']:+.1%}")
        else:
            print("  [市场共识] 体彩未开标准胜平负盘(实力悬殊)")
        if r["handicap"]:
            h = r["handicap"]
            hp = h["prob"]
            print(f"  [让球 {h['line']}] 主 {fmt_pct(hp['home'])} / 平 {fmt_pct(hp['draw'])} / 客 {fmt_pct(hp['away'])}")
        if r["ttg_market"]:
            t = r["ttg_market"]
            print(f"  [总进球] 最可能 {t['most_likely']} 球 ({fmt_pct(t['prob'])})")
        if r["crs_top"]:
            top3 = list(r["crs_top"].items())[:3]
            print(f"  [热门比分] {', '.join(f'{s}@{o}' for s, o in top3)}")

        fh, fa = r["form"]["home"], r["form"]["away"]
        print(f"  [近期 {fh['n']}场] {r['match'].split(' vs ')[0]}: {fh['seq']} "
              f"({fh['pts_per_game']}分/场, 控球{fh['avg_poss']})")
        print(f"  [近期 {fa['n']}场] {r['match'].split(' vs ')[1]}: {fa['seq']} "
              f"({fa['pts_per_game']}分/场, 控球{fa['avg_poss']})")
        frh, fra = r["first_round"]["home"], r["first_round"]["away"]
        if frh:
            print(f"  [首轮] {r['match'].split(' vs ')[0]}: {frh['result']} vs {frh['opponent']} (控球{frh.get('Poss')})")
        if fra:
            print(f"  [首轮] {r['match'].split(' vs ')[1]}: {fra['result']} vs {fra['opponent']} (控球{fra.get('Poss')})")

    print(f"\n已存 {DATA_DIR / 'report.json'}")


if __name__ == "__main__":
    main()
