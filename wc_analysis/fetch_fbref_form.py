#!/usr/bin/env python3
"""
本届世界杯首轮真实表现数据 (数据源: FBref via soccerdata)

read_schedule 给比分; read_team_match_stats(stat_type='schedule') 给每队
逐场的 xG / xGA / 控球(possession) 等高级指标 —— 用于判断"比分是否虚高"。

注: read_team_match_stats 会抓本届全部参赛队(命中缓存后很快)。
本脚本只筛出我们关心的 8 队, 输出每队首轮的 真实 vs 期望 表现。
"""
import json
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# FBref 中的队名 (注意重音/拼写)
TEAMS_FBREF = {
    "Netherlands": "荷兰", "Sweden": "瑞典",
    "Germany": "德国", "Côte d'Ivoire": "科特迪瓦",
    "Ecuador": "厄瓜多尔", "Curaçao": "库拉索",
    "Tunisia": "突尼斯", "Japan": "日本",
}


def main():
    import soccerdata as sd

    fb = sd.FBref("INT-World Cup", 2026)

    # 当前赛季默认会重新下载并撞 CAPTCHA; force_cache=True 强制用已缓存的页面
    sched = fb.read_schedule(force_cache=True)
    print("赛程已读, 共", len(sched), "场")

    # 每队逐场 schedule 统计 (含 xG), 同样走缓存
    ts = fb.read_team_match_stats(stat_type="schedule", force_cache=True).reset_index()
    print("team_match_stats 列:", list(ts.columns))

    # 找出 xG 相关列
    xg_cols = [c for c in ts.columns if "xg" in c.lower() or c.lower() in ("poss", "possession")]
    print("识别到的高级指标列:", xg_cols)

    out = {}
    for fbname, cn in TEAMS_FBREF.items():
        rows = ts[ts["team"].astype(str).str.contains(fbname.split()[0], na=False, regex=False)]
        if rows.empty:
            # 尝试精确匹配
            rows = ts[ts["team"] == fbname]
        recs = []
        for _, r in rows.iterrows():
            rec = {c: (None if pd.isna(r[c]) else (float(r[c]) if isinstance(r[c], (int, float)) else str(r[c])))
                   for c in ts.columns if c in
                   (["date", "opponent", "venue", "result", "gf", "ga", "poss"] + xg_cols)}
            recs.append(rec)
        out[cn] = recs
        if recs:
            print(f"\n{cn} ({fbname}): {len(recs)} 场")
            for rec in recs:
                print("   ", rec)

    (DATA_DIR / "fbref_form.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n已存 data/fbref_form.json")


if __name__ == "__main__":
    main()
