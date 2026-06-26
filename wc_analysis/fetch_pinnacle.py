#!/usr/bin/env python3
"""从 Football-Data.co.uk 拉取 Pinnacle 历史收盘赔率作为市场真实概率基准。

数据源: football-data.co.uk CSV (含 Pinnacle PSH/PSD/PSA 列)
Fallback: Bet365 B365H/B365D/B365A

功能:
  1. 抓取英超(E0)、西甲(SP1)、德甲(D1)、意甲(I1)、法甲(F1) 最近几个赛季
  2. 提取比赛日期、主客队、比分、Pinnacle/Bet365 收盘赔率
  3. 对赔率去水(1/odds 归一化) 得到市场隐含概率
  4. 计算 CLV(收盘线价值) — 需要开盘+收盘两组赔率,仅有收盘时标记 clv=null
  5. 输出 JSON 到 wc_analysis/data/pinnacle_history.json
  6. 输出统计摘要到 stdout

CLV 公式说明:
  CLV = (closing_prob / opening_prob) - 1
  如果你在开盘价 implied_prob_open 买入,收盘时 implied_prob_close 变化了,
  CLV > 0 表示你的头寸获得了正向线移(市场认同你的方向)。
  当仅有收盘赔率时,无法计算 CLV,置为 null。

用法:
    python wc_analysis/fetch_pinnacle.py
    python wc_analysis/fetch_pinnacle.py --seasons 2324 2223 2122
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# 联赛配置: soccerdata league name -> Football-Data.co.uk code
LEAGUES = {
    "ENG-Premier League": "E0",
    "ESP-La Liga": "SP1",
    "GER-Bundesliga": "D1",
    "ITA-Serie A": "I1",
    "FRA-Ligue 1": "F1",
}

# 默认赛季(最近 3 个完整赛季)
DEFAULT_SEASONS = ["2324", "2223", "2122"]


def devig(odds: dict[str, float]) -> dict[str, float]:
    """对一组十进制赔率去水,返回归一化隐含概率。

    odds: {"h": 1.53, "d": 3.83, "a": 4.65}
    返回: {"h": 0.62, "d": 0.25, "a": 0.13} (概率之和=1)
    """
    inv = {k: 1.0 / v for k, v in odds.items() if v and v > 1.0}
    total = sum(inv.values())
    if total == 0:
        return {}
    return {k: round(v / total, 4) for k, v in inv.items()}


def compute_overround(odds: dict[str, float]) -> float:
    """计算 overround (margin)。

    Overround = sum(1/odds_i) - 1
    例如: 1/1.5 + 1/3.8 + 1/4.5 = 0.667+0.263+0.222 = 1.152 → overround=15.2%
    """
    inv_sum = sum(1.0 / v for v in odds.values() if v and v > 1.0)
    return round(inv_sum - 1.0, 4) if inv_sum > 0 else 0.0


def compute_clv(
    open_odds: Optional[dict[str, float]],
    close_odds: dict[str, float],
) -> Optional[dict[str, float]]:
    """计算 CLV(收盘线价值)。

    CLV_outcome = (close_implied_prob / open_implied_prob) - 1
    正值表示如果你在开盘买入该方向,到收盘时获得了正向线移。

    如果没有开盘赔率,返回 None。
    """
    if not open_odds:
        return None
    open_prob = devig(open_odds)
    close_prob = devig(close_odds)
    if not open_prob or not close_prob:
        return None
    clv = {}
    for k in close_prob:
        if k in open_prob and open_prob[k] > 0:
            clv[k] = round(close_prob[k] / open_prob[k] - 1.0, 4)
    return clv if clv else None


def _direct_download_csv(league_code: str, season: str) -> Optional[pd.DataFrame]:
    """直接用 urllib 下载 CSV (绕过 tls_requests 的 503 问题)。"""
    import io
    import ssl
    import urllib.request

    url = f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # football-data.co.uk 的 SSL 配置有时不兼容,需要宽松模式
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [WARN] 直接下载失败 {url}: {e}", file=sys.stderr)
        return None

    # 解码: 2024-25+ 用 UTF-8-SIG, 之前用 latin-1
    encoding = "utf-8-sig" if int(season) >= 2425 else "latin-1"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    df = pd.read_csv(io.StringIO(text), on_bad_lines="warn")

    # 缓存到本地以备后续使用
    cache_dir = Path(__file__).resolve().parent.parent / "data" / "MatchHistory"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{league_code}_{season}.csv"
    cache_path.write_bytes(raw)

    return df


def fetch_league_season(league: str, season: str) -> Optional[pd.DataFrame]:
    """抓取单个联赛单个赛季数据。

    优先用 soccerdata.MatchHistory (利用缓存),
    如果失败则 fallback 到直接 urllib 下载。
    """
    league_code = LEAGUES.get(league, "")

    # 方法1: 尝试 soccerdata (会利用本地缓存)
    try:
        import soccerdata as sd
        mh = sd.MatchHistory(league, season, no_cache=False)
        df = mh.read_games()
        return df
    except Exception:
        pass

    # 方法2: 直接 urllib 下载
    if league_code:
        df = _direct_download_csv(league_code, season)
        if df is not None:
            return df

    print(f"  [WARN] {league} {season}: 所有下载方式均失败", file=sys.stderr)
    return None


def extract_match_records(df: pd.DataFrame, league_name: str) -> list[dict]:
    """从 DataFrame 提取比赛记录。

    赔率列优先级:
    - 收盘: PSCH/PSCD/PSCA (Pinnacle Closing) > PSH/PSD/PSA > B365H/B365D/B365A
    - 开盘: PSH/PSD/PSA (当 PSCH 存在时, PSH 即为开盘)

    CLV 需要同时有开盘和收盘:
    - 如果有 PSCH + PSH: close=PSCH, open=PSH → 可算 CLV
    - 如果只有 PSH (无 PSCH): close=PSH, open=无 → CLV=null
    """
    records = []
    cols = set(df.columns)

    # 确定收盘/开盘赔率列
    has_pin_close = all(c in cols for c in ("PSCH", "PSCD", "PSCA"))
    has_pin_open = all(c in cols for c in ("PSH", "PSD", "PSA"))
    has_b365 = all(c in cols for c in ("B365H", "B365D", "B365A"))

    if has_pin_close:
        # PSCH 是收盘, PSH 是开盘
        close_cols = ("PSCH", "PSCD", "PSCA")
        open_cols = ("PSH", "PSD", "PSA") if has_pin_open else None
        source = "pinnacle"
    elif has_pin_open:
        # 只有 PSH (可能既是开盘也是唯一数据), 当作收盘, 无开盘
        close_cols = ("PSH", "PSD", "PSA")
        open_cols = None
        source = "pinnacle"
    elif has_b365:
        close_cols = ("B365H", "B365D", "B365A")
        open_cols = None
        source = "bet365"
    else:
        print(f"  [WARN] 无可用赔率列, 跳过", file=sys.stderr)
        return []

    for idx, row in df.iterrows():
        # 基本信息 (兼容 soccerdata 重命名后 和 原始 CSV 列名)
        home = row.get("home_team") or row.get("HomeTeam", "")
        away = row.get("away_team") or row.get("AwayTeam", "")
        date_val = row.get("date") if "date" in row.index else row.get("Date")
        fthg = row.get("FTHG")
        ftag = row.get("FTAG")
        ftr = row.get("FTR", "")

        if pd.isna(fthg) or pd.isna(ftag):
            continue

        # 格式化日期
        if pd.notna(date_val):
            if isinstance(date_val, pd.Timestamp):
                date_str = date_val.strftime("%Y-%m-%d")
            else:
                # 尝试解析常见日期格式 (dd/mm/yyyy 或 yyyy-mm-dd)
                try:
                    dt = pd.to_datetime(str(date_val), dayfirst=True)
                    date_str = dt.strftime("%Y-%m-%d")
                except Exception:
                    date_str = str(date_val)[:10]
        else:
            date_str = ""

        # 收盘赔率
        close_h = row.get(close_cols[0])
        close_d = row.get(close_cols[1])
        close_a = row.get(close_cols[2])

        if pd.isna(close_h) or pd.isna(close_d) or pd.isna(close_a):
            continue
        if close_h <= 1.0 or close_d <= 1.0 or close_a <= 1.0:
            continue

        close_odds = {"h": float(close_h), "d": float(close_d), "a": float(close_a)}
        pin_prob = devig(close_odds)
        overround = compute_overround(close_odds)

        # 开盘赔率 (如果有)
        open_odds_dict = None
        if open_cols:
            oh = row.get(open_cols[0])
            od = row.get(open_cols[1])
            oa = row.get(open_cols[2])
            if pd.notna(oh) and pd.notna(od) and pd.notna(oa):
                if oh > 1.0 and od > 1.0 and oa > 1.0:
                    open_odds_dict = {"h": float(oh), "d": float(od), "a": float(oa)}

        clv = compute_clv(open_odds_dict, close_odds)

        record = {
            "date": date_str,
            "league": league_name,
            "home": str(home),
            "away": str(away),
            "fthg": int(fthg),
            "ftag": int(ftag),
            "result": str(ftr) if ftr in ("H", "D", "A") else "",
            "odds_source": source,
            "close_odds": close_odds,
            "pin_prob": pin_prob,
            "overround": overround,
            "clv": clv,
        }
        records.append(record)

    return records


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取 Pinnacle 历史收盘赔率")
    ap.add_argument(
        "--seasons", nargs="*", default=DEFAULT_SEASONS,
        help="赛季列表, 格式如 2324 2223 (默认最近3赛季)"
    )
    ap.add_argument(
        "--leagues", nargs="*", default=None,
        help="联赛列表 (默认全部5大联赛)"
    )
    ap.add_argument(
        "--output", default=str(DATA_DIR / "pinnacle_history.json"),
        help="输出 JSON 文件路径"
    )
    args = ap.parse_args()

    leagues = LEAGUES
    if args.leagues:
        leagues = {k: v for k, v in LEAGUES.items() if v in args.leagues or k in args.leagues}

    all_records: list[dict] = []
    stats = {
        "total_matches": 0,
        "by_league": {},
        "by_season": {},
        "overrounds": [],
        "pinnacle_count": 0,
        "bet365_fallback_count": 0,
    }

    for league_name, league_code in leagues.items():
        print(f"\n>>> {league_name} ({league_code})")
        league_total = 0

        for season in args.seasons:
            print(f"  赛季 {season}...", end=" ")
            df = fetch_league_season(league_name, season)
            if df is None:
                print("SKIP")
                continue

            records = extract_match_records(df, league_name)
            print(f"{len(records)} 场")

            for r in records:
                r["season"] = season
                if r["odds_source"] == "pinnacle":
                    stats["pinnacle_count"] += 1
                else:
                    stats["bet365_fallback_count"] += 1
                stats["overrounds"].append(r["overround"])

            all_records.extend(records)
            league_total += len(records)

            # by_season 统计
            stats["by_season"].setdefault(season, 0)
            stats["by_season"][season] += len(records)

        stats["by_league"][league_name] = league_total

    stats["total_matches"] = len(all_records)

    # 统计摘要
    avg_overround = (
        round(sum(stats["overrounds"]) / len(stats["overrounds"]) * 100, 2)
        if stats["overrounds"] else 0
    )

    summary = {
        "total_matches": stats["total_matches"],
        "avg_overround_pct": avg_overround,
        "pinnacle_count": stats["pinnacle_count"],
        "bet365_fallback_count": stats["bet365_fallback_count"],
        "by_league": stats["by_league"],
        "by_season": stats["by_season"],
    }

    # 输出 JSON
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(all_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 输出摘要
    summary_path = output_path.with_name("pinnacle_summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{'='*60}")
    print(f"总场次: {stats['total_matches']}")
    print(f"Pinnacle 赔率: {stats['pinnacle_count']} 场")
    print(f"Bet365 fallback: {stats['bet365_fallback_count']} 场")
    print(f"平均 overround: {avg_overround}%")
    print(f"\n各联赛覆盖:")
    for lg, cnt in stats["by_league"].items():
        print(f"  {lg}: {cnt} 场")
    print(f"\n各赛季覆盖:")
    for s, cnt in stats["by_season"].items():
        print(f"  {s}: {cnt} 场")
    print(f"\n数据已保存: {output_path}")
    print(f"摘要已保存: {summary_path}")


if __name__ == "__main__":
    main()
