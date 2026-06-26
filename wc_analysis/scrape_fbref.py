#!/usr/bin/env python3
"""
FBref 数据爬取 - 使用 Playwright 绕过 Cloudflare

数据目标:
1. xG/npxG per 90
2. 近5场战绩
3. 角球数据

反爬对策:
- Playwright 真实浏览器指纹
- 随机延迟
- 代理轮换(可选)
"""
from __future__ import annotations
import json, time, random, re
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

DATA_DIR = Path(__file__).parent / "data"
PROXY = "http://127.0.0.1:7890"  # 本地代理

# 世界杯48队 FBref squad ID (需要手动查找并填充)
FBREF_SQUADS = {
    "荷兰": "19538871",
    "德国": "c2a9b341",
    "英格兰": "fd962109",
    "法国": "9d02c100",
    "西班牙": "9c9f7cdb",
    "巴西": "2b84cea8",
    "阿根廷": "49e8ebf6",
    "葡萄牙": "03c57e2b",
    "日本": "09f4dc93",
    # 更多队伍需要从 FBref 手动查找 squad ID...
}

def scrape_team_stats(team_cn: str, squad_id: str, playwright) -> dict | None:
    """
    爬取单支队伍的统计数据

    Args:
        team_cn: 中文队名
        squad_id: FBref squad ID
        playwright: Playwright实例

    Returns:
        {"npxG_per_90": float, "form": [...], "corners_per_game": float}
    """
    url = f"https://fbref.com/en/squads/{squad_id}/2025-2026/{team_cn.replace(' ','-')}-Stats"

    browser = playwright.chromium.launch(
        headless=True,
        proxy={"server": PROXY} if PROXY else None
    )

    # 伪造指纹
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0"
        }
    )

    page = context.new_page()

    try:
        print(f"  [抓取] {team_cn} ({squad_id})...")
        page.goto(url, timeout=30000, wait_until="domcontentloaded")

        # 等待Cloudflare检查完成(如果有)
        time.sleep(random.uniform(2, 4))

        # 检查是否被Cloudflare拦截
        if "checking your browser" in page.content().lower() or "just a moment" in page.content().lower():
            print(f"  ⚠️ {team_cn}: Cloudflare拦截,等待10秒...")
            time.sleep(10)
            page.reload()
            time.sleep(3)

        content = page.content()

        # 提取 npxG/90 (从 Standard Stats 表格)
        npxg_match = re.search(r'<td[^>]*data-stat="npxg_per_90"[^>]*>([0-9.]+)</td>', content)
        npxg_per_90 = float(npxg_match.group(1)) if npxg_match else None

        # 提取角球数据(从 Possession 表格)
        corners_match = re.search(r'<td[^>]*data-stat="corner_kicks"[^>]*>([0-9.]+)</td>', content)
        corners = float(corners_match.group(1)) if corners_match else None

        # 提取近期战绩(需要进入Fixtures页面,这里简化为None)
        form = None  # TODO: 需要额外请求 /fixtures 页面

        result = {
            "npxG_per_90": npxg_per_90,
            "corners_per_game": corners,
            "form": form,
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        print(f"  ✓ {team_cn}: npxG/90={npxg_per_90}, 角球={corners}")
        return result

    except PlaywrightTimeout:
        print(f"  ✗ {team_cn}: 超时")
        return None
    except Exception as e:
        print(f"  ✗ {team_cn}: {e}")
        return None
    finally:
        context.close()
        browser.close()


def main():
    print("🔍 FBref 数据爬取 (Playwright + 伪造指纹)")
    print(f"代理: {PROXY or '直连'}")
    print(f"目标: {len(FBREF_SQUADS)} 支队伍\n")

    results = {}

    with sync_playwright() as p:
        for i, (team_cn, squad_id) in enumerate(FBREF_SQUADS.items(), 1):
            print(f"[{i}/{len(FBREF_SQUADS)}]", end=" ")

            stats = scrape_team_stats(team_cn, squad_id, p)
            if stats:
                results[team_cn] = stats

            # 随机延迟避免rate limit
            if i < len(FBREF_SQUADS):
                delay = random.uniform(3, 6)
                print(f"  (等待 {delay:.1f}s...)")
                time.sleep(delay)

    # 保存
    output_file = DATA_DIR / "fbref_xg_profiles.json"
    output_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ 完成! {len(results)}/{len(FBREF_SQUADS)} 支队伍成功")
    print(f"保存至: {output_file}")


if __name__ == "__main__":
    main()
