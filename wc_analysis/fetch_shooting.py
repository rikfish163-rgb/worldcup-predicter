#!/usr/bin/env python3
"""抓取 8 队 shooting matchlog (含射门数 Sh/SoT), 用作 xG 代理的原料。

FBref 国家队 shooting 不含 Expected/xG 列, 只有基础射门。
本脚本抓 Sh/SoT/G_per_Sh 等, 落盘供 xG 代理模型使用。
当前赛季默认强制下载, 这里先抓未缓存的队。
"""
import json
import warnings
from pathlib import Path
import pandas as pd

warnings.filterwarnings("ignore")
DATA_DIR = Path(__file__).parent / "data"

TEAMS = {
    "Netherlands": "荷兰", "Sweden": "瑞典",
    "Germany": "德国", "Côte d'Ivoire": "科特迪瓦",
    "Ecuador": "厄瓜多尔", "Curaçao": "库拉索",
    "Tunisia": "突尼斯", "Japan": "日本",
}

def main():
    import soccerdata as sd
    fb = sd.FBref("INT-World Cup", 2026)
    # force_cache 优先用缓存, 没有的现抓
    try:
        ts = fb.read_team_match_stats(stat_type="shooting", force_cache=True).reset_index()
    except Exception as e:
        print("force_cache 读取失败, 尝试在线抓:", repr(e)[:120])
        ts = fb.read_team_match_stats(stat_type="shooting").reset_index()
    ts.columns = ['|'.join([str(x) for x in c if x]).strip('|') if isinstance(c, tuple) else str(c) for c in ts.columns]
    print("列:", list(ts.columns))
    out = {}
    for fbname, cn in TEAMS.items():
        rows = ts[ts["team"].astype(str) == fbname]
        recs = []
        for _, r in rows.iterrows():
            d = {}
            for col in ts.columns:
                v = r[col]
                if pd.isna(v):
                    v = None
                elif hasattr(v, 'item'):
                    v = v.item()
                elif isinstance(v, pd.Timestamp):
                    v = str(v.date())
                d[col] = v
            recs.append(d)
        out[cn] = recs
        print(f"{cn}: {len(recs)} 场")
    (DATA_DIR / "shooting.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("已存 shooting.json")

if __name__ == "__main__":
    main()
