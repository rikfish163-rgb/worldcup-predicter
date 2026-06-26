#!/usr/bin/env python3
"""
FBref 爬虫 - Playwright + Stealth 绕过 Cloudflare

反反爬技术:
1. playwright-stealth 隐藏自动化特征
2. 真实浏览器指纹
3. 人类行为模拟(随机延迟、滚动)
4. 代理支持
"""
from __future__ import annotations
import json, time, random, re
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

DATA_DIR = Path(__file__).parent.parent / "data"
PROXY = "http://127.0.0.1:7890"  # 可选代理

# 世界杯48队 FBref squad ID mapping (手动维护)
FBREF_SQUADS = {
    "阿根廷": ("Argentina", "49e8ebf6"),
    "澳大利亚": ("Australia", "18d3d2d6"),
    "奥地利": ("Austria", "bdd06ff6"),
    "比利时": ("Belgium", "86445459"),
    "巴西": ("Brazil", "2b84cea8"),
    "加拿大": ("Canada", "04a9b7eb"),
    "智利": ("Chile", "b1e2878f"),
    "哥伦比亚": ("Colombia", "61fe4ad4"),
    "克罗地亚": ("Croatia", "98bc90f6"),
    "丹麦": ("Denmark", "28d8090f"),
    "厄瓜多尔": ("Ecuador", "df9bf3bd"),
    "英格兰": ("England", "fd962109"),
    "法国": ("France", "9d02c100"),
    "德国": ("Germany", "c2a9b341"),
    "加纳": ("Ghana", "e7db3e44"),
    "意大利": ("Italy", "c1cf4c6d"),
    "日本": ("Japan", "09f4dc93"),
    "墨西哥": ("Mexico", "22a0dcdd"),
    "摩洛哥": ("Morocco", "2b89d613"),
    "荷兰": ("Netherlands", "19538871"),
    "巴拿马": ("Panama", "20db5d9b"),
    "巴拉圭": ("Paraguay", "d1b4a058"),
    "秘鲁": ("Peru", "2326d2a4"),
    "波兰": ("Poland", "3d50b0c8"),
    "葡萄牙": ("Portugal", "03c57e2b"),
    "韩国": ("South Korea", "4b664e60"),
    "塞内加尔": ("Senegal", "b7bb5506"),
    "塞尔维亚": ("Serbia", "53139b8e"),
    "西班牙": ("Spain", "9c9f7cdb"),
    "瑞典": ("Sweden", "14a2e2d3"),
    "瑞士": ("Switzerland", "7b9e12dd"),
    "乌拉圭": ("Uruguay", "2e4f5ff2"),
    "美国": ("United States", "9f635c82"),
    "威尔士": ("Wales", "3d7f6b40"),
}


def install_stealth():
    """检查并安装 playwright-stealth"""
    import sys
    try:
        import playwright_stealth
        return True
    except ImportError:
        print("⚠️  playwright-stealth 未安装,尝试安装...")
        import subprocess
        subprocess.run([
            sys.executable, "-m", "pip", "install", "playwright-stealth", "-q"
        ], check=True)
        return True


def scrape_team(team_cn: str, team_en: str, squad_id: str, page) -> dict | None:
    """
    爬取单支队伍数据

    Returns:
        {
            "npxG_per_90": float,
            "xG_per_90": float,
            "corners_per_game": float,
            "goals_per_game": float,
            "form": [...],  # 近5场战绩
            "scraped_at": str
        }
    """
    url = f"https://fbref.com/en/squads/{squad_id}/{team_en}-Stats"

    try:
        print(f"  访问: {url}")

        # 访问页面
        page.goto(url, timeout=45000, wait_until="domcontentloaded")

        # 随机延迟模拟人类
        time.sleep(random.uniform(2, 4))

        # 随机滚动(模拟真实用户)
        page.evaluate(f"window.scrollTo(0, {random.randint(300, 800)})")
        time.sleep(random.uniform(0.5, 1.5))

        # 检查 Cloudflare 拦截
        content = page.content()
        if any(kw in content.lower() for kw in ["checking your browser", "just a moment", "cloudflare"]):
            print(f"  ⚠️  Cloudflare 检测,等待...")
            time.sleep(15)
            page.reload(timeout=30000)
            time.sleep(5)
            content = page.content()

        # 提取数据 (使用 page.locator 更稳定)
        result = {"scraped_at": time.strftime("%Y-%m-%d %H:%M:%S")}

        # npxG/90
        try:
            npxg_elem = page.locator('td[data-stat="npxg_per_90"]').first
            result["npxG_per_90"] = float(npxg_elem.inner_text())
        except:
            result["npxG_per_90"] = None

        # xG/90
        try:
            xg_elem = page.locator('td[data-stat="xg_per_90"]').first
            result["xG_per_90"] = float(xg_elem.inner_text())
        except:
            result["xG_per_90"] = None

        # 进球/90
        try:
            goals_elem = page.locator('td[data-stat="goals_per_90"]').first
            result["goals_per_game"] = float(goals_elem.inner_text())
        except:
            result["goals_per_game"] = None

        # 角球数(从 Possession 表格)
        try:
            corners_elem = page.locator('td[data-stat="corner_kicks"]').first
            corners_total = float(corners_elem.inner_text())
            matches_elem = page.locator('td[data-stat="games"]').first
            matches = float(matches_elem.inner_text())
            result["corners_per_game"] = round(corners_total / matches, 2) if matches > 0 else None
        except:
            result["corners_per_game"] = None

        # 打印成功提取的数据
        print(f"  ✓ {team_cn}: xG={result.get('xG_per_90')}, npxG={result.get('npxG_per_90')}, 角球={result.get('corners_per_game')}")
        return result

    except PlaywrightTimeout:
        print(f"  ✗ {team_cn}: 超时")
        return None
    except Exception as e:
        print(f"  ✗ {team_cn}: {e}")
        return None


def main():
    import sys

    # 安装 stealth
    if not install_stealth():
        print("❌ 无法安装 playwright-stealth")
        return

    from playwright_stealth import stealth_sync

    print("🔍 FBref 数据爬取 (Playwright + Stealth)")
    print(f"代理: {PROXY if PROXY else '直连'}")
    print(f"目标: {len(FBREF_SQUADS)} 支队伍\n")

    results = {}

    with sync_playwright() as p:
        # 启动浏览器
        browser = p.chromium.launch(
            headless=True,  # 无头模式
            proxy={"server": PROXY} if PROXY else None,
            args=[
                '--disable-blink-features=AutomationControlled',  # 隐藏自动化标识
                '--no-sandbox',
            ]
        )

        # 创建上下文(伪造指纹)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            permissions=[],  # 不授予任何权限
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "DNT": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            }
        )

        page = context.new_page()

        # 应用 stealth
        stealth_sync(page)

        # 遍历队伍
        for i, (team_cn, (team_en, squad_id)) in enumerate(FBREF_SQUADS.items(), 1):
            print(f"\n[{i}/{len(FBREF_SQUADS)}] {team_cn}")

            stats = scrape_team(team_cn, team_en, squad_id, page)
            if stats:
                results[team_cn] = stats

            # 随机延迟(避免被封)
            if i < len(FBREF_SQUADS):
                delay = random.uniform(4, 8)
                print(f"  (等待 {delay:.1f}s...)")
                time.sleep(delay)

        context.close()
        browser.close()

    # 保存
    output_file = DATA_DIR / "fbref_xg_profiles.json"
    output_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ 完成! {len(results)}/{len(FBREF_SQUADS)} 支队伍成功")
    print(f"保存至: {output_file}")

    # 统计
    valid_xg = sum(1 for r in results.values() if r.get("npxG_per_90"))
    valid_corners = sum(1 for r in results.values() if r.get("corners_per_game"))
    print(f"\n数据完整性:")
    print(f"  xG数据: {valid_xg}/{len(results)}")
    print(f"  角球数据: {valid_corners}/{len(results)}")


if __name__ == "__main__":
    main()
