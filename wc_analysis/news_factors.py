#!/usr/bin/env python3
"""
场外因素新闻抓取 — 赛前一手信息聚合(反思案例: 墨西哥vs厄瓜多尔赛前,
球迷夜间在酒店外骚扰厄瓜多尔球队+厄瓜多尔发布会诉苦疲劳+9万人主场声浪,
这些赛前12-48h媒体已报道的"软信息", 模型完全没捕捉到, 导致低估墨西哥胜率)。

设计原则:
  - 不做精确概率量化(样本/因果链太弱, 强行量化=伪科学)。
  - 只做: 抓取 -> 关键词分类 -> 时效性过滤 -> 面板 notice 提醒, 让人自行判断。
  - 时效性管理: 每条新闻打时间戳, 比赛开始后 or 超过 VALID_HOURS 直接从库中清除,
    避免"过期新闻"污染下一轮预测, 也避免 json 无限增长。

数据源: Google News RSS (免key, 直连+代理均可达, 实测无地域限制)。
"""
from __future__ import annotations
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
NEWS_FILE = DATA_DIR / "news_factors.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 新闻抓回来只保留最近 N 小时内发布的(时效性核心: 太旧的赛前传闻已无意义)
VALID_HOURS = 60
# 每支队每场比赛最多展示几条 notice(避免刷屏)
MAX_NOTICES_PER_TEAM = 2

# 场外因素关键词分类. 覆盖反思案例的三类 + 常见赛前扰动因素.
# 命中多个类目的新闻按下列顺序取第一个(最具体的排前面)。
CATEGORIES: list[tuple[str, list[str], str]] = [
    ("fan_disturbance", [
        "hotel noise", "horn", "fireworks outside", "sleepless night", "keep awake",
        "kept awake", "hotel incident", "noise complaint", "blast horns", "disturb",
    ], "⚠️ 球迷骚扰/扰眠"),
    ("fatigue_travel", [
        "fatigue", "tired", "jet lag", "flight delay", "travel delay", "long journey",
        "exhausted", "sleep deprivation", "rest issue", "travel issue",
    ], "😴 疲劳/舟车劳顿"),
    ("crowd_pressure", [
        "sold out", "capacity crowd", "packed stadium", "hostile crowd", "deafening",
        "home support", "roar of the crowd", "90,000", "fans pack",
    ], "📢 主场声浪/满座压制"),
    ("injury_news", [
        "injury doubt", "ruled out", "fitness concern", "injury scare", "will miss",
        "doubtful for", "knee injury", "hamstring", "muscle injury",
    ], "🩹 伤停新闻(赛前确认)"),
    ("controversy", [
        "fifa complaint", "formal complaint", "protest", "dispute", "sanction threat",
        "investigation", "disciplinary",
    ], "🚩 争议/申诉"),
    ("weather_delay", [
        "storm delay", "match delayed", "weather delay", "postponed", "heavy rain",
    ], "🌩️ 天气/比赛延期"),
]


def _fetch_rss(query: str, timeout: int = 15) -> list[dict]:
    """打 Google News RSS, 返回该查询下的原始条目列表(含标题/时间/来源/链接)。"""
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            xml_bytes = r.read()
    except Exception as e:
        print(f"  ⚠ RSS请求失败({query}): {e}")
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    items = []
    for it in root.findall(".//item"):
        title_el = it.find("title")
        pub_el = it.find("pubDate")
        link_el = it.find("link")
        source_el = it.find("source")
        if title_el is None or title_el.text is None:
            continue
        try:
            pub_dt = parsedate_to_datetime(pub_el.text) if pub_el is not None and pub_el.text else None
        except (ValueError, TypeError):
            pub_dt = None
        items.append({
            "title": title_el.text,
            "pub": pub_dt.isoformat() if pub_dt else None,
            "link": link_el.text if link_el is not None else None,
            "source": source_el.text if source_el is not None else None,
        })
    return items


def _classify(title: str) -> tuple[str, str] | None:
    """标题命中哪个场外因素类目。返回 (category_key, label) 或 None(无关新闻)。"""
    t = title.lower()
    for key, keywords, label in CATEGORIES:
        if any(kw in t for kw in keywords):
            return key, label
    return None


def fetch_team_factors(team_en: str, opponent_en: str, match_date: str) -> list[dict]:
    """
    抓某队在这场比赛前的场外因素新闻。

    两路检索合并:
    1. "{队名} {对手} World Cup 2026" — 精确锁定这场比赛的报道
    2. "{队名} press conference World Cup" — 补赛前发布会/教练表态类新闻
       (窄检索1常被赛后战报淹没, 发布会诉苦/疲劳表态是反思案例的核心线索)
    """
    raw = _fetch_rss(f'"{team_en}" "{opponent_en}" World Cup 2026')
    raw += _fetch_rss(f'"{team_en}" press conference travel fatigue World Cup')

    # 队名匹配: 只取标题中确实提到本队的新闻(第二路检索是宽泛的"发布会/疲劳",
    # Google News 会连带返回其他队的同类新闻, 必须过滤掉, 否则会把
    # "厄瓜多尔教练谈疲劳"错配到"英格兰vs刚果金"这种无关比赛下, 造成虚假信息)。
    team_first_word = team_en.split()[0].lower()  # "Ivory Coast" -> "ivory"

    now = datetime.now(timezone.utc)
    found: dict[str, dict] = {}  # category_key -> 最新一条
    for it in raw:
        if team_first_word not in it["title"].lower():
            continue  # 标题未提及本队, 视为不相关(即使命中场外因素关键词)
        cat = _classify(it["title"])
        if not cat:
            continue
        key, label = cat
        if it["pub"]:
            try:
                pub_dt = datetime.fromisoformat(it["pub"])
                age_h = (now - pub_dt).total_seconds() / 3600
                if age_h > VALID_HOURS or age_h < 0:
                    continue  # 时效性过滤: 太旧的赛前传闻不展示
            except ValueError:
                continue
        else:
            continue
        # 同类目只留最新一条
        if key not in found or (it["pub"] and it["pub"] > found[key]["pub"]):
            found[key] = {
                "category": key,
                "label": label,
                "title": it["title"],
                "pub": it["pub"],
                "source": it["source"],
                "link": it["link"],
            }

    # 按类目在 CATEGORIES 中的顺序排(反思案例验证过的高信号类目优先),
    # 同类目内取最新一条(found 已保证), 而非纯发布时间倒序(会让噪声类目挤掉信号类目)。
    cat_order = {key: i for i, (key, _, _) in enumerate(CATEGORIES)}
    result = list(found.values())
    result.sort(key=lambda x: cat_order.get(x["category"], 99))
    return result[:MAX_NOTICES_PER_TEAM]


def load_news() -> dict:
    if NEWS_FILE.exists():
        try:
            return json.loads(NEWS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_news(data: dict) -> None:
    NEWS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_expired(news: dict) -> dict:
    """
    清理过期条目, 避免 json 无限增长:
    - 比赛已开始(match_date 早于当前) -> 整场比赛的新闻块删除
    - 单条新闻超过 VALID_HOURS -> 从该场比赛的列表中剔除
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    pruned = {}
    for match_key, block in news.items():
        match_date = block.get("date", "")
        if match_date and match_date < today:
            continue  # 比赛已结束, 整块丢弃
        for side in ("home", "away"):
            items = block.get(side, [])
            fresh = []
            for it in items:
                pub = it.get("pub")
                if not pub:
                    continue
                try:
                    age_h = (now - datetime.fromisoformat(pub)).total_seconds() / 3600
                    if 0 <= age_h <= VALID_HOURS:
                        fresh.append(it)
                except ValueError:
                    continue
            block[side] = fresh
        if block.get("home") or block.get("away"):
            pruned[match_key] = block
    return pruned


def update_match_factors(home_cn: str, away_cn: str, home_en: str, away_en: str,
                         match_date: str) -> dict:
    """
    为一场比赛抓取双方场外因素, 合并进 news_factors.json, 并清理全库过期数据。

    返回该场比赛的 {"home": [...], "away": [...]}, 供 predict.py 直接消费。
    """
    news = load_news()
    news = prune_expired(news)

    match_key = f"{match_date}_{home_cn}_{away_cn}"
    old_block = news.get(match_key) or {}

    # (审计修复2026-07-02: 此前home/away两次抓取共用一个try/except, 任一路
    # 抛异常都会把已经成功抓到的另一路也清空为[]; 且写回时无条件整体替换
    # news[match_key], 就算这次两路都空也会覆盖掉上一次可能已经抓到的非空
    # 旧数据。改为分别try/except(一路失败不连累另一路), 且某一路为空时
    # 保留旧数据里对应那一路的非空值, 而不是用空值覆盖。)
    try:
        home_items = fetch_team_factors(home_en, away_en, match_date)
    except Exception as e:
        print(f"  ⚠ 场外因素抓取失败({home_cn}): {e}")
        home_items = []
    time.sleep(1.0)  # 温和限速
    try:
        away_items = fetch_team_factors(away_en, home_en, match_date)
    except Exception as e:
        print(f"  ⚠ 场外因素抓取失败({away_cn}): {e}")
        away_items = []

    if not home_items and old_block.get("home"):
        home_items = old_block["home"]
    if not away_items and old_block.get("away"):
        away_items = old_block["away"]

    block = {"date": match_date, "home": home_items, "away": away_items,
             "fetched_at": datetime.now(timezone.utc).isoformat()}
    news[match_key] = block
    save_news(news)
    return block


if __name__ == "__main__":
    # 自测: 复现反思案例(墨西哥 vs 厄瓜多尔)
    r = update_match_factors("墨西哥", "厄瓜多尔", "Mexico", "Ecuador", "2026-07-01")
    print(json.dumps(r, ensure_ascii=False, indent=2))
