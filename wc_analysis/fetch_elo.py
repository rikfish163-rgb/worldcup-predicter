#!/usr/bin/env python3
"""
国家队 Elo 抓取与建模 (数据源: eloratings.net)

库自带的 ClubElo 只含俱乐部, 不适用国家队。eloratings.net 提供
331 支国家队的完整历史 Elo (每场比赛一条记录), 是国家队实力的权威源。

数据文件:
  - https://www.eloratings.net/{Team_Name}.tsv  -> 该队完整历史每场记录
  - https://www.eloratings.net/World.tsv         -> 当前全球排名快照

每场记录字段 (TSV, 无表头):
  year, month, day, home_code, away_code, home_score, away_score,
  type, ?, elo_change, home_elo_after, away_elo_after, ...

我们主要用最后已知的 elo 值作为"当前实力", 并保留历史用于趋势。

胜平负概率模型 (国际通用 Elo 公式):
  dr = elo_home - elo_away + HFA           # HFA: 主场优势, 世界杯中立场取 0
  We = 1 / (10^(-dr/400) + 1)              # 主队期望胜率(含半平)
  再用经验公式拆分出平局概率。
"""
import io
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR = DATA_DIR / "elo_cache"
CACHE_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 队名 -> eloratings.net 文件名 (实测确认)
TEAM_FILE = {
    "荷兰": "Netherlands", "瑞典": "Sweden",
    "德国": "Germany", "科特迪瓦": "Ivory_Coast",
    "厄瓜多尔": "Ecuador", "库拉索": "Curacao",
    "突尼斯": "Tunisia", "日本": "Japan",
}

# 国际足联两位代码 -> 中文 (用于解析对手, 仅覆盖需要的队)
CODE_CN = {
    "NL": "荷兰", "SE": "瑞典", "DE": "德国", "CI": "科特迪瓦",
    "EC": "厄瓜多尔", "CW": "库拉索", "TN": "突尼斯", "JP": "日本",
}


def _fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_team_history(team_cn: str, max_age_h: int = 24) -> pd.DataFrame:
    """抓取单队完整历史 Elo, 带本地缓存。"""
    fname = TEAM_FILE[team_cn]
    cache = CACHE_DIR / f"{fname}.tsv"
    if cache.is_file() and (time.time() - cache.stat().st_mtime) < max_age_h * 3600:
        text = cache.read_text(encoding="utf-8")
    else:
        url = f"https://www.eloratings.net/{quote(fname)}.tsv"
        text = _fetch(url)
        cache.write_text(text, encoding="utf-8")

    cols = ["year", "month", "day", "home", "away", "hs", "as_",
            "type", "x", "change", "home_elo", "away_elo", "c1", "c2", "c3", "c4"]
    rows = []
    for line in text.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        rows.append(parts[:16] + [None] * (16 - len(parts[:16])))
    df = pd.DataFrame(rows, columns=cols)
    for c in ["year", "month", "day", "hs", "as_"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ["home_elo", "away_elo"]:
        df[c] = pd.to_numeric(df[c].astype(str).str.replace("−", "-"), errors="coerce")
    df["date"] = pd.to_datetime(
        dict(year=df.year, month=df.month, day=df.day), errors="coerce"
    )
    # 该队在每场中的 Elo (home 或 away 视角)
    code = [k for k, v in CODE_CN.items() if v == team_cn]
    code = code[0] if code else None
    return df.dropna(subset=["date"]).reset_index(drop=True)


def current_elo(team_cn: str) -> dict:
    """返回该队最近一场后的 Elo 及近期信息。"""
    df = fetch_team_history(team_cn)
    code = [k for k, v in CODE_CN.items() if v == team_cn][0]
    last = df.iloc[-1]
    # 最后一场该队的 elo
    if last["home"] == code:
        elo = last["home_elo"]
    elif last["away"] == code:
        elo = last["away_elo"]
    else:
        # 取最后非空
        elo = df["home_elo"].dropna().iloc[-1]
    # 近 10 场战绩
    recent = df.tail(10)
    return {
        "team": team_cn,
        "elo": round(float(elo), 1),
        "last_date": str(last["date"].date()),
        "history_from": str(df["date"].min().date()),
        "n_matches": len(df),
        "peak_elo": round(float(pd.concat([df["home_elo"], df["away_elo"]]).max()), 1),
    }


def win_draw_loss(elo_h: float, elo_a: float, hfa: float = 0.0) -> dict:
    """
    Elo -> 胜平负概率。
    World Cup 小组赛多为中立场, hfa 默认 0。
    We = 主队"期望分"(胜=1,平=0.5)。平局概率用经验公式估计:
      draw ≈ (2*sqrt(p_h_raw*p_a_raw)) 风格, 这里采用常见近似:
      draw_base 随实力差缩小。
    """
    dr = elo_h - elo_a + hfa
    we = 1.0 / (10 ** (-dr / 400) + 1)  # 主队期望分
    # 平局概率经验模型: 实力越接近平局越高, 峰值约 0.28
    # draw = 0.28 * exp(-(dr/200)^2 ... ) 采用简化高斯
    import math
    draw = 0.30 * math.exp(-((dr / 250.0) ** 2))
    # 由期望分反推胜负: we = p_h + 0.5*draw  ->  p_h = we - 0.5*draw
    p_h = we - 0.5 * draw
    p_a = 1 - we - 0.5 * draw
    # 防御性裁剪
    p_h, p_a = max(p_h, 0.01), max(p_a, 0.01)
    s = p_h + p_a + draw
    return {"home": p_h / s, "draw": draw / s, "away": p_a / s, "dr": dr}


# 四场对阵 (主, 客) — 以 sporttery/FBref 主客一致
FIXTURES = [
    ("荷兰", "瑞典"),
    ("德国", "科特迪瓦"),
    ("厄瓜多尔", "库拉索"),
    ("突尼斯", "日本"),
]


def main():
    elos = {}
    for t in TEAM_FILE:
        try:
            elos[t] = current_elo(t)
            print(f"  {t:6s} Elo={elos[t]['elo']:7.1f}  "
                  f"峰值={elos[t]['peak_elo']:7.1f}  "
                  f"({elos[t]['n_matches']}场, 始于{elos[t]['history_from']})")
        except Exception as e:
            print(f"  {t}: ERR {type(e).__name__} {e}")

    print("\n=== Elo 模型胜平负 (中立场) ===")
    out = {"elos": elos, "fixtures": []}
    for h, a in FIXTURES:
        if h in elos and a in elos:
            p = win_draw_loss(elos[h]["elo"], elos[a]["elo"])
            print(f"  {h} vs {a}: 主{p['home']*100:.1f}% / 平{p['draw']*100:.1f}% / "
                  f"客{p['away']*100:.1f}%  (Elo差 {p['dr']:+.0f})")
            out["fixtures"].append({
                "home": h, "away": a,
                "elo_home": elos[h]["elo"], "elo_away": elos[a]["elo"],
                "model": p,
            })
    (DATA_DIR / "elo_model.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已存 {DATA_DIR / 'elo_model.json'}")


if __name__ == "__main__":
    main()
