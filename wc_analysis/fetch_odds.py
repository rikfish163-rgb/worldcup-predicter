#!/usr/bin/env python3
"""抓取中国体彩(sporttery)竞彩足球赔率,并转成去水后的隐含概率。

数据来源: webapi.sporttery.cn 竞彩足球 getMatchCalculatorV1 接口。
该接口返回所有在售竞彩场次的赔率(胜平负 had / 让球胜平负 hhad /
比分 crs / 总进球 ttg / 半全场 hafu)。

用法:
    python fetch_odds.py                 # 抓全部在售, 存 data/odds_raw.json
    python fetch_odds.py --match 荷兰     # 只看含"荷兰"的场次

设计要点:
  - 必须伪装 UA + Referer, 否则 403。
  - had 字段可能为空 {}(只开了让球盘), 需容错。
  - 赔率 -> 概率: p_i = 1/odds_i, 再除以 sum(p) 归一化(去除博彩公司抽水/margin)。
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

API_URL = "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?poolCode=hhad,had,crs,ttg,hafu"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.sporttery.cn/",
    "Accept": "application/json, text/plain, */*",
}


def fetch_raw() -> dict:
    """拉取原始赔率 JSON。"""
    req = urllib.request.Request(API_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def devig(odds: dict[str, float]) -> dict[str, float]:
    """把一组十进制赔率去水, 返回归一化隐含概率。

    odds: {"h": 1.53, "d": 3.83, "a": 4.65}
    """
    inv = {k: 1.0 / v for k, v in odds.items() if v and v > 0}
    total = sum(inv.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in inv.items()}


def parse_match(sub: dict) -> dict:
    """从一个 subMatch 提取我们关心的赔率块 + 隐含概率。"""
    home = sub.get("homeTeamAllName", "")
    away = sub.get("awayTeamAllName", "")

    out: dict = {
        "match": f"{home} vs {away}",
        "home": home,
        "away": away,
        "home_en": sub.get("homeTeamAbbEnName", ""),
        "away_en": sub.get("awayTeamAbbEnName", ""),
        "home_rank": sub.get("homeRank", ""),
        "away_rank": sub.get("awayRank", ""),
        "match_date": sub.get("matchDate", ""),
        "match_time": sub.get("matchTime", ""),
        "match_num": sub.get("matchNumStr", ""),
    }

    # 胜平负 (had): 不含让球
    had = sub.get("had") or {}
    if had.get("h"):
        odds = {"home": float(had["h"]), "draw": float(had["d"]), "away": float(had["a"])}
        out["had"] = {"odds": odds, "prob": devig(odds)}

    # 让球胜平负 (hhad): goalLine 为让球数
    hhad = sub.get("hhad") or {}
    if hhad.get("h"):
        odds = {"home": float(hhad["h"]), "draw": float(hhad["d"]), "away": float(hhad["a"])}
        out["hhad"] = {
            "handicap": hhad.get("goalLineValue", ""),
            "odds": odds,
            "prob": devig(odds),
        }

    # 总进球 (ttg): s0..s7+ (s7 表示 7+)
    ttg = sub.get("ttg") or {}
    if ttg.get("s0"):
        odds = {f"{i}": float(ttg[f"s{i}"]) for i in range(8) if ttg.get(f"s{i}")}
        out["ttg"] = {"odds": odds, "prob": devig(odds)}

    # 比分 (crs): 取最热门几个(赔率最低)
    crs = sub.get("crs") or {}
    crs_odds = {}
    for k, v in crs.items():
        if k.startswith("s") and "s" in k[1:] and not k.endswith("f"):
            # 形如 s01s02 -> 1-2; s00s00 -> 0-0
            try:
                parts = k[1:].split("s")
                h, a = int(parts[0]), int(parts[1])
                crs_odds[f"{h}-{a}"] = float(v)
            except (ValueError, IndexError):
                continue
    if crs_odds:
        top = dict(sorted(crs_odds.items(), key=lambda x: x[1])[:8])
        out["crs_top"] = top

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", help="只显示队名含该关键词的场次")
    ap.add_argument("--save", action="store_true", help="保存原始 JSON")
    args = ap.parse_args()

    raw = fetch_raw()
    (DATA_DIR / "odds_raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    results = []
    for info in raw.get("value", {}).get("matchInfoList", []):
        for sub in info.get("subMatchList", []):
            m = parse_match(sub)
            if args.match and args.match not in m["match"]:
                continue
            results.append(m)

    (DATA_DIR / "odds_parsed.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for m in results:
        print(f"\n{'='*60}")
        print(f"[{m['match_num']}] {m['match']}  ({m['match_date']} {m['match_time']})")
        if "had" in m:
            p = m["had"]["prob"]
            print(f"  胜平负(去水): 主 {p['home']:.1%} / 平 {p['draw']:.1%} / 客 {p['away']:.1%}")
        if "hhad" in m:
            p = m["hhad"]["prob"]
            print(f"  让球[{m['hhad']['handicap']}](去水): 主 {p['home']:.1%} / 平 {p['draw']:.1%} / 客 {p['away']:.1%}")
        if "ttg" in m:
            best = max(m["ttg"]["prob"].items(), key=lambda x: x[1])
            print(f"  最可能总进球数: {best[0]} 球 ({best[1]:.1%})")
        if "crs_top" in m:
            top3 = list(m["crs_top"].items())[:3]
            print(f"  热门比分: {', '.join(f'{s}@{o}' for s, o in top3)}")

    print(f"\n共 {len(results)} 场, 已存 data/odds_parsed.json")


if __name__ == "__main__":
    main()
