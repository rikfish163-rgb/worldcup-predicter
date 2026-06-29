#!/usr/bin/env python3
"""
SofaScore 爬虫客户端 — seleniumbase undetected chromedriver 绕过 Cloudflare.

实测验证(2026-06-30): 直接 driver.get(api_url) + uc 模式可绕过 Cloudflare 403,
拿到全维度足球数据(统计/阵容/评分/xG/h2h/胜率投票)。

数据维度(单场比赛 statistics 端点):
  Match overview: 控球率/xG/绝佳机会/总射门/扑救/角球/犯规/传球/抢断/任意球
  Shots: 总射门/射正/中柱/射偏/被封堵/禁区内外射门
  Attack: 错失绝佳机会/禁区触球/越位
  Passes: 准确传球/掷界外球/前场推进/长传/传中
  Duels: 对抗/失球权/地面对抗/空中对抗/过人
  Defending: 抢断/拦截/解围/回收
  Goalkeeping: 扑救/扑救失球预防/球门球

用法:
  with SofaScoreClient() as c:
      live = c.live_events()
      stats = c.event_statistics(event_id)
"""
from __future__ import annotations
import json
import time
import random
from pathlib import Path

API = "https://api.sofascore.com/api/v1/"
DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_DIR = DATA_DIR / "sofascore_cache"


class SofaScoreClient:
    def __init__(self, headless: bool = True, rate_limit: float = 1.2):
        self.headless = headless
        self.rate_limit = rate_limit
        self._driver = None

    def __enter__(self):
        from seleniumbase import Driver
        self._driver = Driver(uc=True, headless=self.headless)
        return self

    def __exit__(self, *a):
        if self._driver:
            self._driver.quit()

    def _get(self, path: str, retries: int = 3) -> dict | None:
        """打 SofaScore API, 返回 JSON dict 或 None."""
        url = API + path
        for i in range(retries):
            try:
                self._driver.uc_open_with_reconnect(url, reconnect_time=3)
                time.sleep(self.rate_limit + random.random() * 0.8)
                body = self._driver.find_element("tag name", "body").text
                if body.strip().startswith("{"):
                    return json.loads(body)
                # 非JSON = 可能被挑战, 重试
                time.sleep(2 * (i + 1))
            except Exception:
                time.sleep(2 * (i + 1))
        return None

    # ─── 赛事发现 ───
    def live_events(self) -> list[dict]:
        d = self._get("sport/football/events/live")
        return d.get("events", []) if d else []

    def scheduled_events(self, date: str) -> list[dict]:
        """date 格式 YYYY-MM-DD."""
        d = self._get(f"sport/football/scheduled-events/{date}")
        return d.get("events", []) if d else []

    def team_events(self, team_id: int, page: int = 0) -> list[dict]:
        """球队近期已完赛比赛(用于近况/状态)."""
        d = self._get(f"team/{team_id}/events/last/{page}")
        return d.get("events", []) if d else []

    # ─── 单场全维度 ───
    def event(self, event_id: int) -> dict | None:
        d = self._get(f"event/{event_id}")
        return d.get("event") if d else None

    def event_statistics(self, event_id: int) -> dict | None:
        """7大类40+统计指标(控球/xG/射门/对抗/防守...)."""
        return self._get(f"event/{event_id}/statistics")

    def event_lineups(self, event_id: int) -> dict | None:
        """阵容+阵型+球员评分."""
        return self._get(f"event/{event_id}/lineups")

    def event_h2h(self, event_id: int) -> dict | None:
        """两队历史交锋."""
        return self._get(f"event/{event_id}/h2h")

    def event_votes(self, event_id: int) -> dict | None:
        """大众胜率投票(市场情绪)."""
        return self._get(f"event/{event_id}/votes")

    def event_pregame_form(self, event_id: int) -> dict | None:
        """赛前两队近期战绩 form."""
        return self._get(f"event/{event_id}/pregame-form")


# ─── 统计提取工具 ───
def parse_statistics(stats_json: dict) -> dict:
    """把 statistics API 的嵌套结构拍平成 {指标: (home值, away值)}."""
    out = {}
    if not stats_json or not stats_json.get("statistics"):
        return out
    period = stats_json["statistics"][0]  # ALL
    for group in period.get("groups", []):
        for item in group.get("statisticsItems", []):
            name = item.get("name")
            out[name] = {
                "home": item.get("homeValue", item.get("home")),
                "away": item.get("awayValue", item.get("away")),
                "group": group.get("groupName"),
            }
    return out


if __name__ == "__main__":
    # 自测
    with SofaScoreClient() as c:
        live = c.live_events()
        print(f"直播 {len(live)} 场")
        if live:
            ev = live[0]
            print(f"  {ev['homeTeam']['name']} vs {ev['awayTeam']['name']}")
            stats = c.event_statistics(ev["id"])
            flat = parse_statistics(stats)
            print(f"  统计指标 {len(flat)} 项: {list(flat.keys())[:8]}")
