#!/usr/bin/env python3
"""
场外因素新闻定时抓取 — 独立任务, 与 predict.py 主流程解耦。

为什么独立: predict.py 每8分钟跑一次(cron), 新闻不需要这么高频抓取
(Google News 索引更新是小时级), 且新闻抓取涉及2次RSS请求/队×2队/场,
塞进主流程会拖慢每次预测生成。这里单独定时(建议每小时一次)。

用法: .venv/bin/python wc_analysis/fetch_news.py
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from news_factors import update_match_factors, prune_expired, load_news, save_news
from predict import DATA_DIR, TEAM_DB


def _team_en(team_cn: str) -> str:
    """中文队名 -> 新闻检索用标准英文名(TEAM_DB下划线转空格, 如 Ivory_Coast -> Ivory Coast)。
    体彩接口的 home_en/away_en 是缩写(如 ENG), 不适合新闻检索, 故用 TEAM_DB 而非它。"""
    entry = TEAM_DB.get(team_cn)
    if entry:
        return entry[0].replace("_", " ")
    return team_cn


def main():
    pred_file = DATA_DIR / "predictions.json"
    if not pred_file.exists():
        print("无 predictions.json, 先跑一次 predict.py")
        return

    predictions = json.loads(pred_file.read_text(encoding="utf-8"))
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 为 {len(predictions)} 场比赛抓取场外因素新闻...")

    # 先清理全库过期数据(比赛已结束/新闻超过时效)
    news = load_news()
    before = len(news)
    news = prune_expired(news)
    save_news(news)
    if before != len(news):
        print(f"  🧹 清理过期比赛块: {before} -> {len(news)}")

    ok, fail = 0, 0
    for p in predictions:
        home_cn, away_cn = p["home"], p["away"]
        home_en = _team_en(home_cn)
        away_en = _team_en(away_cn)
        date = p.get("date", "")
        try:
            block = update_match_factors(home_cn, away_cn, home_en, away_en, date)
            n = len(block.get("home", [])) + len(block.get("away", []))
            if n:
                print(f"  ✓ {home_cn} vs {away_cn}: {n} 条 notice")
            ok += 1
        except Exception as e:
            print(f"  ✗ {home_cn} vs {away_cn}: {e}")
            fail += 1
        time.sleep(1.5)  # 温和限速, 避免被 Google News 限流

    print(f"完成: {ok} 场成功, {fail} 场失败")


if __name__ == "__main__":
    main()
