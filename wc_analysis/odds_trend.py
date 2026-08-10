#!/usr/bin/env python3
"""
概率趋势记录 — 独立于 prediction_history.json(那个是"每场比赛一条快照,供回测
配对最终结果", 语义不能动, 否则会破坏 evolve_groupstage.py 的自进化回测)。

这里记录的是"同一场未来比赛, 随时间推进的多个概率快照", 供前端画近12小时
趋势图, 回答"模型预测这段时间到底变没变"这个问题。

设计(与 news_factors.py 同样的时效性管理思路):
  - 每次 run_pipeline() 结束后追加一个时间点(主输出 hhad_posterior 的 h/d/a)
  - 同一场比赛的采样点按 MIN_INTERVAL_MIN 去重(避免8分钟一次的cron把文件撑爆)
  - 超过 RETENTION_HOURS 的老点自动裁掉; 比赛已开始(日期早于今天)的整条序列删除
  - 输出 wc_analysis/data/odds_trend.json: {match_key: {meta..., "points": [...]}}
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
TREND_FILE = DATA_DIR / "odds_trend.json"

RETENTION_HOURS = 12       # 前端展示"近12小时", 存储也只留这么多, 避免无限增长
MIN_INTERVAL_MIN = 12      # 同一场比赛两个采样点之间至少隔这么久(防止8分钟cron把文件撑爆)


def _load() -> dict:
    if TREND_FILE.exists():
        try:
            return json.loads(TREND_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save(data: dict) -> None:
    TREND_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def prune(data: dict) -> dict:
    """裁剪: 比赛已开始的整条序列删除; 序列内超过 RETENTION_HOURS 的老点删除。"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    cutoff = now - timedelta(hours=RETENTION_HOURS)
    pruned = {}
    for key, block in data.items():
        if block.get("date", "") < today:
            continue  # 比赛已开赛/结束, 整条序列丢弃
        pts = [p for p in block.get("points", []) if p["t"] >= cutoff.isoformat()]
        if pts:
            block["points"] = pts
            pruned[key] = block
    return pruned


def record_snapshot(predictions: list[dict]) -> None:
    """
    run_pipeline() 每次生成新预测后调用一次。
    只对"让球盘后验"(hhad_posterior, 主输出)打点, 这是页面上实际展示的概率。
    """
    data = _load()
    data = prune(data)
    now_iso = datetime.now().isoformat(timespec="seconds")

    for p in predictions:
        post = p.get("hhad_posterior")
        if not post:
            continue
        match_key = f"{p['date']}_{p['home']}_{p['away']}"
        block = data.setdefault(match_key, {
            "date": p["date"], "home": p["home"], "away": p["away"],
            "handicap_line": p.get("handicap_line"), "points": [],
        })
        pts = block["points"]
        # 去重: 距上一个点不足 MIN_INTERVAL_MIN 分钟则跳过(避免文件被高频cron撑爆)
        if pts:
            last_t = datetime.fromisoformat(pts[-1]["t"])
            if (datetime.now() - last_t) < timedelta(minutes=MIN_INTERVAL_MIN):
                continue
        pts.append({
            "t": now_iso,
            "h": round(post.get("h", 0), 4),
            "d": round(post.get("d", 0), 4),
            "a": round(post.get("a", 0), 4),
        })

    _save(data)


def get_trend(home_cn: str, away_cn: str, match_date: str) -> list[dict]:
    """只读接口: 供 predict.py 渲染时取某场比赛的近12小时趋势点, 不做网络请求。"""
    data = _load()
    match_key = f"{match_date}_{home_cn}_{away_cn}"
    block = data.get(match_key)
    return block.get("points", []) if block else []


if __name__ == "__main__":
    # 自测: 用当前 predictions.json 打一个快照点
    pred_file = DATA_DIR / "predictions.json"
    if pred_file.exists():
        preds = json.loads(pred_file.read_text(encoding="utf-8"))
        record_snapshot(preds)
        data = _load()
        print(f"记录 {len(data)} 场比赛的趋势序列")
        for k, b in data.items():
            print(f"  {k}: {len(b['points'])} 个采样点")
