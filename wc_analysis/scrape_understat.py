#!/usr/bin/env python3
"""
Understat xG 数据抓取 - 无反爬,直接JSON API

数据: 每支队伍的 xG/xGA/npxG/npxGA per game
"""
from __future__ import annotations
import json, re, time
from pathlib import Path
import urllib.request

DATA_DIR = Path(__file__).parent / "data"

# 队名映射(Understat用的是英文全名)
UNDERSTAT_TEAMS = {
    "荷兰": "Netherlands",
    "德国": "Germany",
    "英格兰": "England",
    "法国": "France",
    "西班牙": "Spain",
    "巴西": "Brazil",
    "阿根廷": "Argentina",
    "葡萄牙": "Portugal",
    "日本": "Japan",
    "比利时": "Belgium",
    "意大利": "Italy",
    # 更多队伍...
}

def fetch_team_xg(team_en: str) -> dict | None:
    """
    从 Understat 抓取国家队 xG 数据

    注意: Understat 主要是联赛数据,国家队数据可能不全
    这里先尝试,如果404说明该队无数据
    """
    # Understat 国家队数据在 /team/{team}/2025 下(如果有的话)
    # 实际上Understat主要是俱乐部联赛数据,国家队数据很少
    # 这里改为抓取该国主要球员所在联赛的数据聚合

    # 简化方案: 直接返回None,说明Understat不适合国家队数据
    return None


def fetch_league_xg(league: str, season: str = "2025") -> dict:
    """
    抓取联赛整体 xG 数据

    Understat 支持的联赛: EPL, La_liga, Bundesliga, Serie_A, Ligue_1, RFPL
    """
    url = f"https://understat.com/league/{league}/{season}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")

        # Understat 把数据嵌在 <script> 标签的 JSON 里
        # 查找 teamsData = JSON.parse('...')
        match = re.search(r"teamsData\s*=\s*JSON\.parse\('([^']+)'\)", html)
        if not match:
            return {}

        # 解码JSON字符串(需要unescape)
        json_str = match.group(1).encode().decode('unicode_escape')
        data = json.loads(json_str)

        return data

    except Exception as e:
        print(f"  ✗ {league}: {e}")
        return {}


def main():
    print("🔍 Understat xG 数据抓取")
    print("注意: Understat 主要是联赛数据,不直接支持国家队\n")

    # 测试抓取英超数据(示例)
    print("测试: 英超 2024-25 赛季")
    epl_data = fetch_league_xg("EPL", "2024")

    if epl_data:
        print(f"✅ 抓到 {len(epl_data)} 支球队数据")
        # 显示一支队伍的数据结构
        sample = list(epl_data.values())[0]
        print(f"\n示例数据结构:")
        print(json.dumps(sample, indent=2)[:300])

        # 保存
        output = DATA_DIR / "understat_epl_2024.json"
        output.write_text(json.dumps(epl_data, indent=2), encoding="utf-8")
        print(f"\n保存至: {output}")
    else:
        print("❌ 抓取失败")

    print("\n结论: Understat 不适合国家队xG数据(主要是联赛)")
    print("建议: FBref (需Playwright) 或 手动维护 xg_profiles.json")


if __name__ == "__main__":
    main()
