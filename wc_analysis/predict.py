#!/usr/bin/env python3
"""
足球比赛预测引擎 v2 — 完整管线

数据层:
  1. 体彩竞彩盘口 (sporttery API)
  2. 国家队 Elo (eloratings.net, 正确解析主客Elo)
  3. 天气 (open-meteo forecast, 场馆坐标自动查)
  4. 伤病 (手动JSON录入, 支持热加载)

模型层:
  Dixon-Coles: Elo锚定双泊松 + τ低比分校正
  情境微调: 天气(高温/雨) + 伤病(关键球员缺阵) → λ调整

校准层:
  对数池融合: 先验(模型) × 后验(市场) → edge

输出:
  data/predictions.json + site/index.html (带刷新按钮)

用法:
  .venv/bin/python wc_analysis/predict.py          # 一次性运行
  .venv/bin/python wc_analysis/predict.py --serve   # 启动本地服务+自动刷新
"""
from __future__ import annotations
import json, math, urllib.request, time, sys, os
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading

import numpy as np

# ═══════════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════════
DATA_DIR = Path(__file__).parent / "data"
SITE_DIR = Path(__file__).parent / "site"
DATA_DIR.mkdir(exist_ok=True)
SITE_DIR.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SPORTTERY_URL = ("https://webapi.sporttery.cn/gateway/jc/football/"
                 "getMatchCalculatorV1.qry?poolCode=hhad,had,crs,ttg,hafu")
ELO_CACHE = DATA_DIR / "elo_cache"
ELO_CACHE.mkdir(exist_ok=True)
INJURIES_FILE = DATA_DIR / "injuries.json"
COHESION_FILE = DATA_DIR / "cohesion.json"
CORNERS_FILE = DATA_DIR / "corners.json"

RHO = -0.20  # 交叉验证最优(2286场, 2020-2026)
AVG_GOALS = 2.50  # 交叉验证最优
HOME_ADV = 0.40  # 世界杯中立场仍有~40Elo主场效应; 联赛可设更高

# 自进化参数覆盖(harness.py 诊断后自动写入)
_PARAMS_OVERRIDE = DATA_DIR / "params_override.json"
if _PARAMS_OVERRIDE.exists():
    _po = __import__("json").loads(_PARAMS_OVERRIDE.read_text(encoding="utf-8"))
    RHO = _po.get("rho", RHO)
    AVG_GOALS = _po.get("avg_goals", AVG_GOALS)
    HOME_ADV = _po.get("home_adv", HOME_ADV)

# 中文队名 → eloratings 文件名 + 2字母代码
TEAM_DB = {
    "荷兰": ("Netherlands", "NL"), "瑞典": ("Sweden", "SE"),
    "德国": ("Germany", "DE"), "科特迪瓦": ("Ivory_Coast", "CI"),
    "厄瓜多尔": ("Ecuador", "EC"), "库拉索": ("Curacao", "CW"),
    "突尼斯": ("Tunisia", "TN"), "日本": ("Japan", "JP"),
    "西班牙": ("Spain", "ES"), "沙特阿拉伯": ("Saudi_Arabia", "SA"),
    "比利时": ("Belgium", "BE"), "伊朗": ("IR_Iran", "IR"),
    "乌拉圭": ("Uruguay", "UY"), "佛得角": ("Cape_Verde", "CV"),
    "新西兰": ("New_Zealand", "NZ"), "埃及": ("Egypt", "EG"),
    "阿根廷": ("Argentina", "AR"), "奥地利": ("Austria", "AT"),
    "法国": ("France", "FR"), "伊拉克": ("Iraq", "IQ"),
    "挪威": ("Norway", "NO"), "塞内加尔": ("Senegal", "SN"),
    "约旦": ("Jordan", "JO"), "阿尔及利亚": ("Algeria", "DZ"),
    "英格兰": ("England", "EN"), "克罗地亚": ("Croatia", "HR"),
    "葡萄牙": ("Portugal", "PT"), "哥伦比亚": ("Colombia", "CO"),
    "巴西": ("Brazil", "BR"), "摩洛哥": ("Morocco", "MA"),
    "加纳": ("Ghana", "GH"), "巴拿马": ("Panama", "PA"),
    "韩国": ("South_Korea", "KR"), "墨西哥": ("Mexico", "MX"),
    "美国": ("United_States", "US"), "加拿大": ("Canada", "CA"),
    "澳大利亚": ("Australia", "AU"), "瑞士": ("Switzerland", "CH"),
    "乌兹别克斯坦": ("Uzbekistan", "UZ"), "刚果(金)": ("DR_Congo", "CD"),
    "南非": ("South_Africa", "ZA"), "卡塔尔": ("Qatar", "QA"),
    "捷克": ("Czechia", "CZ"), "波黑": ("Bosnia_and_Herzegovina", "BA"),
    "海地": ("Haiti", "HT"), "苏格兰": ("Scotland", "SQ"),
    "土耳其": ("Turkey", "TR"), "巴拉圭": ("Paraguay", "PY"),
    "委内瑞拉": ("Venezuela", "VE"), "秘鲁": ("Peru", "PE"),
    "智利": ("Chile", "CL"), "玻利维亚": ("Bolivia", "BO"),
}

# ═══════════════════════════════════════════════════════════════════
# 1. 体彩赔率
# ═══════════════════════════════════════════════════════════════════

def fetch_sporttery() -> list[dict]:
    # 体彩为中国境内站, 海外服务器可能访问失败 -> 抓取成功则缓存, 失败则回退上次缓存
    # 海外VPS会被WAF拦截, 优先读取由4090 relay 推送的最新缓存
    parsed_cache = DATA_DIR / "odds_parsed.json"
    fresh_cache = DATA_DIR / "odds_parsed_fresh.json"
    relay_source = None  # 记录数据来源
    
    # 优先读取 4090 relay 推送的"最新"缓存 (由 cron 每30分钟刷新)
    if fresh_cache.exists():
        age = time.time() - fresh_cache.stat().st_mtime
        if age < 3600:  # 1小时内的数据视为新鲜
            relay_source = f"relay_4090({int(age/60)}分钟前)"
            return json.loads(fresh_cache.read_text(encoding="utf-8"))
    
    # 尝试直接抓取 (从中国IP可成功)
    req = urllib.request.Request(SPORTTERY_URL, headers={
        "User-Agent": UA, "Referer": "https://static.sporttery.cn/",
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "sec-ch-ua": '"Chromium";v="120"',
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = json.loads(r.read())
    except Exception as e:
        if parsed_cache.exists():
            age = time.time() - parsed_cache.stat().st_mtime
            print(f"  ⚠ 体彩抓取失败({e}), 回退缓存 {parsed_cache.name} ({int(age/60)}分钟前)")
            return json.loads(parsed_cache.read_text(encoding="utf-8"))
        print(f"  ⚠ 体彩抓取失败({e}), 无缓存可用")
        return []
    matches = []
    for day in raw.get("value", {}).get("matchInfoList", []):
        for m in day.get("subMatchList", []):
            rec = {
                "home": m.get("homeTeamAllName", ""),
                "away": m.get("awayTeamAllName", ""),
                "home_en": m.get("homeTeamAbbEnName", ""),
                "away_en": m.get("awayTeamAbbEnName", ""),
                "date": m.get("matchDate", ""),
                "time": m.get("matchTime", ""),
                "league": m.get("leagueAbbName", ""),
                "num": m.get("matchNumStr", ""),
            }
            had = m.get("had") or {}
            if had.get("h"):
                odds = {"h": float(had["h"]), "d": float(had["d"]), "a": float(had["a"])}
                rec["had_odds"] = odds
                rec["had_prob"] = _devig(odds)
            hhad = m.get("hhad") or {}
            if hhad.get("h"):
                rec["hhad_line"] = hhad.get("goalLineValue", "")
                odds = {"h": float(hhad["h"]), "d": float(hhad["d"]), "a": float(hhad["a"])}
                rec["hhad_odds"] = odds
                rec["hhad_prob"] = _devig(odds)
            ttg = m.get("ttg") or {}
            if ttg.get("s0"):
                rec["ttg_odds"] = {str(i): float(ttg[f"s{i}"]) for i in range(8) if ttg.get(f"s{i}")}
            matches.append(rec)
    if matches:
        parsed_cache.write_text(
            json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    return matches


def _devig(odds: dict) -> dict:
    inv = {k: 1.0/v for k, v in odds.items() if v > 0}
    s = sum(inv.values())
    return {k: round(v/s, 5) for k, v in inv.items()} if s else {}


# ═══════════════════════════════════════════════════════════════════
# 2. Elo 评分 (修正: 根据队伍代码判断主客)
# ═══════════════════════════════════════════════════════════════════

def get_elo(team_cn: str) -> float | None:
    entry = TEAM_DB.get(team_cn)
    if not entry:
        return None
    fname, code = entry
    cache = ELO_CACHE / f"{fname}.tsv"
    if not cache.exists() or (time.time() - cache.stat().st_mtime > 86400):
        url = f"https://www.eloratings.net/{quote(fname)}.tsv"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            data = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
            cache.write_text(data, encoding="utf-8")
        except Exception:
            if not cache.exists():
                return None
    text = cache.read_text(encoding="utf-8")
    lines = [l for l in text.strip().split("\n") if l.count("\t") >= 10]
    if not lines:
        return None
    last = lines[-1].split("\t")
    try:
        home_code = last[3]
        home_elo = float(last[10].replace("−", "-").replace("−", "-"))
        away_elo = float(last[11].replace("−", "-").replace("−", "-"))
    except (IndexError, ValueError):
        return None
    # 正确判断: 该队在最后一场是主还是客
    if home_code == code:
        return home_elo
    else:
        return away_elo


# ═══════════════════════════════════════════════════════════════════
# 3. Dixon-Coles 模型
# ═══════════════════════════════════════════════════════════════════

def dc_tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    elif x == 1 and y == 0:
        return 1.0 + mu * rho
    elif x == 0 and y == 1:
        return 1.0 + lam * rho
    elif x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def score_matrix(lam_h: float, lam_a: float, rho: float = RHO, n: int = 8) -> np.ndarray:
    mat = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            p = poisson_pmf(i, lam_h) * poisson_pmf(j, lam_a) * dc_tau(i, j, lam_h, lam_a, rho)
            mat[i, j] = max(p, 0)
    mat /= mat.sum()
    return mat


def elo_to_lambdas(elo_h: float, elo_a: float) -> tuple[float, float]:
    dr = elo_h - elo_a + HOME_ADV * 100
    we = 1.0 / (10 ** (-dr / 400) + 1)
    we = max(0.05, min(0.95, we))
    ratio = we / (1 - we)
    lam_a = AVG_GOALS / (1 + ratio)
    lam_h = AVG_GOALS - lam_a
    return max(lam_h, 0.25), max(lam_a, 0.25)


_xg_cache = None
def _load_xg_profiles() -> dict:
    global _xg_cache
    if _xg_cache is not None:
        return _xg_cache
    xg_file = DATA_DIR / "xg_profiles.json"
    if xg_file.exists():
        _xg_cache = json.loads(xg_file.read_text(encoding="utf-8"))
    else:
        _xg_cache = {}
    return _xg_cache


_draw_model = None
def _predict_draw_prob(elo_h: float, elo_a: float, home_cn: str = None, away_cn: str = None) -> float | None:
    """用训练好的逻辑回归预测平局概率(v2: 8特征含风格+交锋)。"""
    global _draw_model
    if _draw_model is None:
        dm_file = DATA_DIR / "draw_model.json"
        if not dm_file.exists():
            return None
        _draw_model = json.loads(dm_file.read_text(encoding="utf-8"))
    m = _draw_model
    n_feats = len(m["w"])

    elo_diff = abs(elo_h - elo_a)
    avg_elo = (elo_h + elo_a) / 2

    if n_feats == 3:
        # v1: 3特征
        feats = np.array([elo_diff, avg_elo, (elo_h - elo_a)**2])
    else:
        # v2: 8特征 — 需要球队风格数据
        h_draw_rate, a_draw_rate = 0.25, 0.25
        low_score_tendency, avg_conceded, h2h_draw = 0.40, 1.2, 0.25

        if home_cn and away_cn:
            h_entry = TEAM_DB.get(home_cn)
            a_entry = TEAM_DB.get(away_cn)
            if h_entry and a_entry:
                h_code, a_code = h_entry[1], a_entry[1]
                h_stats = _get_team_style(home_cn)
                a_stats = _get_team_style(away_cn)
                if h_stats:
                    h_draw_rate = h_stats["draw_rate"]
                if a_stats:
                    a_draw_rate = a_stats["draw_rate"]
                if h_stats and a_stats:
                    low_score_tendency = (h_stats["low_score_rate"] + a_stats["low_score_rate"]) / 2
                    avg_conceded = (h_stats["avg_conceded"] + a_stats["avg_conceded"]) / 2
                # 交锋平局率
                h2h_draw = _get_h2h_draw_rate(home_cn, away_cn)

        feats = np.array([elo_diff, avg_elo, elo_diff**2,
                          h_draw_rate, a_draw_rate,
                          low_score_tendency, avg_conceded, h2h_draw])

    x_norm = (feats - np.array(m["mu"])) / np.array(m["sigma"])
    z = float(x_norm @ np.array(m["w"]) + m["b"])
    return 1.0 / (1.0 + math.exp(-max(-500, min(500, z))))


_style_cache = {}
def _get_team_style(team_cn: str) -> dict | None:
    if team_cn in _style_cache:
        return _style_cache[team_cn]
    entry = TEAM_DB.get(team_cn)
    if not entry:
        return None
    fname, code = entry
    tsv = ELO_CACHE / f"{fname}.tsv"
    if not tsv.exists():
        return None
    recent = []
    for line in tsv.read_text(encoding="utf-8").strip().split("\n")[-25:]:
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        try:
            hc, ac = parts[3], parts[4]
            hs, as_ = int(parts[5]), int(parts[6])
        except (ValueError, IndexError):
            continue
        if hc == code:
            recent.append({"scored": hs, "conceded": as_})
        elif ac == code:
            recent.append({"scored": as_, "conceded": hs})
    if len(recent) < 5:
        return None
    result = {
        "draw_rate": sum(1 for m in recent if m["scored"] == m["conceded"]) / len(recent),
        "low_score_rate": sum(1 for m in recent if m["scored"] + m["conceded"] <= 2) / len(recent),
        "avg_conceded": sum(m["conceded"] for m in recent) / len(recent),
        "attack": sum(m["scored"] for m in recent) / len(recent),
        "tempo": sum(m["scored"] + m["conceded"] for m in recent) / len(recent),
        "low_block": sum(1 for m in recent if m["conceded"] <= 1) / len(recent),
    }
    _style_cache[team_cn] = result
    return result


def _get_h2h_draw_rate(home_cn: str, away_cn: str) -> float:
    h_entry = TEAM_DB.get(home_cn)
    a_entry = TEAM_DB.get(away_cn)
    if not h_entry or not a_entry:
        return 0.25
    h_code, a_code = h_entry[1], a_entry[1]
    fname = h_entry[0]
    tsv = ELO_CACHE / f"{fname}.tsv"
    if not tsv.exists():
        return 0.25
    h2h = []
    for line in tsv.read_text(encoding="utf-8").strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        hc, ac = parts[3], parts[4]
        if (hc == h_code and ac == a_code) or (hc == a_code and ac == h_code):
            try:
                h2h.append(int(parts[5]) == int(parts[6]))
            except (ValueError, IndexError):
                pass
    if not h2h:
        return 0.25
    return sum(h2h) / len(h2h)


def _get_cohesion_factor(team_cn: str) -> float:
    """综合磨合度因子: 自动量化 + 手动录入。返回 λ 乘数。"""
    factor = 1.0

    # 手动定性因子(优先级最高,人工判断)
    if COHESION_FILE.exists():
        coh = json.loads(COHESION_FILE.read_text(encoding="utf-8"))
        if team_cn in coh:
            return coh[team_cn].get("lambda_factor", 1.0)

    # 自动量化: 基于近期比赛数和一致性
    entry = TEAM_DB.get(team_cn)
    if not entry:
        return 1.0
    fname, code = entry
    tsv = ELO_CACHE / f"{fname}.tsv"
    if not tsv.exists():
        return 1.0

    recent_gd = []
    n_recent = 0
    for line in tsv.read_text(encoding="utf-8").strip().split("\n")[-30:]:
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        try:
            y = int(parts[0])
            if y < 2023:
                continue
            hc, ac = parts[3], parts[4]
            hs, as_ = int(parts[5]), int(parts[6])
        except (ValueError, IndexError):
            continue
        if hc == code:
            recent_gd.append(hs - as_)
        elif ac == code:
            recent_gd.append(as_ - hs)
        n_recent += 1

    if n_recent < 10:
        factor *= 0.95  # 比赛少=磨合不足

    if recent_gd:
        consistency = float(np.std(recent_gd))
        if consistency > 2.5:
            factor *= 0.96  # 表现极不稳定

    return round(factor, 3)


def weighted_goals_rate(team_cn: str, days_back: int = 365) -> tuple[float, float] | None:
    """
    Dixon-Coles 时间衰减加权进球率。
    从 elo_cache TSV 读取该队最近 N 天的比赛,
    用指数衰减 weight = exp(-xi * days_ago) 计算加权场均进球和失球。
    TSV 字段: year month day home away hs as type x change home_elo away_elo
    返回 (加权场均进球, 加权场均失球),若数据不足返回 None。
    """
    XI = 0.0065  # 半衰期约107天
    entry = TEAM_DB.get(team_cn)
    if not entry:
        return None
    fname, code = entry
    cache = ELO_CACHE / f"{fname}.tsv"
    if not cache.exists():
        return None
    text = cache.read_text(encoding="utf-8")
    lines = [l for l in text.strip().split("\n") if l.count("\t") >= 10]
    if not lines:
        return None

    today = datetime.now()
    weighted_scored = 0.0
    weighted_conceded = 0.0
    total_weight = 0.0

    for line in lines:
        parts = line.split("\t")
        try:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            home_code = parts[3]
            away_code = parts[4]
            home_goals = int(parts[5])
            away_goals = int(parts[6])
        except (IndexError, ValueError):
            continue

        try:
            match_date = datetime(year, month, day)
        except ValueError:
            continue

        days_ago = (today - match_date).days
        if days_ago < 0 or days_ago > days_back:
            continue

        weight = math.exp(-XI * days_ago)

        if home_code == code:
            scored = home_goals
            conceded = away_goals
        elif away_code == code:
            scored = away_goals
            conceded = home_goals
        else:
            continue

        weighted_scored += scored * weight
        weighted_conceded += conceded * weight
        total_weight += weight

    if total_weight < 0.5:  # 数据不足
        return None

    return (weighted_scored / total_weight, weighted_conceded / total_weight)


_motivation_cache = None
def _load_motivation() -> dict:
    """加载战意系数 (实时积分/出线情况 → 期望进球调整系数)。"""
    global _motivation_cache
    if _motivation_cache is not None:
        return _motivation_cache
    f = DATA_DIR / "standings.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            _motivation_cache = d.get("motivation", {})
        except Exception:
            _motivation_cache = {}
    else:
        _motivation_cache = {}
    return _motivation_cache


def _get_team_motivation(team_en: str) -> tuple[float, str]:
    """根据球队英文名取战意系数和状态。返回 (motivation_factor, status_label)。"""
    if not team_en:
        return 1.0, "unknown"
    m = _load_motivation()
    info = m.get(team_en)
    if not info:
        return 1.0, "unknown"
    factor = float(info.get("motivation", 1.0))
    status = info.get("status", "fighting")
    label_map = {
        "qualified_top2": "已出线(轮换)",
        "near_qualified": "接近出线",
        "fighting_3rd": "争最好第3",
        "fighting": "正常",
        "must_win": "必拼",
        "eliminated": "已淘汰",
        "unknown": "未知",
    }
    return factor, label_map.get(status, status)


def predict_match(elo_h: float, elo_a: float, adj_h: float = 1.0, adj_a: float = 1.0,
                   home_cn: str | None = None, away_cn: str | None = None,
                   handicap_line: str | None = None,
                   team_en_h: str | None = None, team_en_a: str | None = None) -> dict:
    """
    核心预测函数 — 架构v2: 让球盘为主输出。

    主输出: hc_prior (让球盘模型概率) → 用于校准和显示
    辅助输出: prior (常规胜平负) → 仅供参考

    新增: 战意系数 (motivation factor) - 实时积分/出线情况调整期望进球。
    """
    # 战意系数 (实时积分 → 调整 λ)
    mot_h, status_h = _get_team_motivation(team_en_h or home_cn or "")
    mot_a, status_a = _get_team_motivation(team_en_a or away_cn or "")
    lam_h, lam_a = elo_to_lambdas(elo_h, elo_a)
    lam_h *= adj_h * mot_h
    lam_a *= adj_a * mot_a

    # Dixon-Coles 时间衰减加权融合 (60% Elo推导, 25% 历史加权进球率)
    # + xG 档案微调(如果有)
    xg_profiles = _load_xg_profiles()

    if home_cn:
        wgr_h = weighted_goals_rate(home_cn)
        if wgr_h is not None:
            hist_lam_h = wgr_h[0]
            lam_h = 0.60 * lam_h + 0.25 * hist_lam_h
            # xG 档案进攻质量微调
            if home_cn in xg_profiles and xg_profiles[home_cn].get("attack_xg90"):
                xg_factor = xg_profiles[home_cn]["attack_xg90"] / 0.35  # 0.35=联赛平均npxG/90
                xg_factor = max(0.7, min(1.4, xg_factor))  # 封顶避免极端
                lam_h = 0.85 * lam_h + 0.15 * (lam_h * xg_factor)
        else:
            lam_h = 0.70 * lam_h + 0.30 * lam_h  # 无历史数据不变
    if away_cn:
        wgr_a = weighted_goals_rate(away_cn)
        if wgr_a is not None:
            hist_lam_a = wgr_a[0]
            lam_a = 0.60 * lam_a + 0.25 * hist_lam_a
            if away_cn in xg_profiles and xg_profiles[away_cn].get("attack_xg90"):
                xg_factor = xg_profiles[away_cn]["attack_xg90"] / 0.35
                xg_factor = max(0.7, min(1.4, xg_factor))
                lam_a = 0.85 * lam_a + 0.15 * (lam_a * xg_factor)
        else:
            lam_a = 0.70 * lam_a + 0.30 * lam_a

    lam_h = max(lam_h, 0.25)
    lam_a = max(lam_a, 0.25)

    mat = score_matrix(lam_h, lam_a)
    n = mat.shape[0]

    # 常规胜平负 (辅助输出,仅供参考)
    p_h = sum(mat[i, j] for i in range(n) for j in range(n) if i > j)
    p_d = sum(mat[i, j] for i in range(n) for j in range(n) if i == j)
    p_a = sum(mat[i, j] for i in range(n) for j in range(n) if i < j)

    # 平局校正: 用训练好的逻辑回归模型修正 DC 的平局概率
    draw_model_p = _predict_draw_prob(elo_h, elo_a, home_cn, away_cn)
    if draw_model_p is not None:
        # 融合权重: Elo差大时信DC多,Elo差小时信平局模型多
        elo_gap = abs(elo_h - elo_a)
        # 平局模型权重: 差<100时0.5, 差>400时0.1, 线性插值
        draw_weight = max(0.10, min(0.50, 0.50 - 0.40 * (elo_gap - 100) / 300))
        p_d_corrected = (1 - draw_weight) * p_d + draw_weight * draw_model_p
        # 重新分配概率(从 H/A 中按比例扣减)
        delta = p_d_corrected - p_d
        if p_h + p_a > 0:
            ratio_h = p_h / (p_h + p_a)
            p_h -= delta * ratio_h
            p_a -= delta * (1 - ratio_h)
            p_d = p_d_corrected
        # 确保非负
        p_h = max(p_h, 0.02)
        p_a = max(p_a, 0.02)
        p_d = max(p_d, 0.05)
        s = p_h + p_d + p_a
        p_h, p_d, p_a = p_h/s, p_d/s, p_a/s

    # ═══ 主输出: 让球盘概率 (根据体彩实际让球线) ═══
    hc_prior = None
    if handicap_line:
        try:
            hline = float(handicap_line)
            # hline 是主队让球数,如 -1.00 表示主队让1球
            # 主胜条件: home_goals + hline > away_goals (即 diff > -hline)
            # 平局条件: home_goals + hline == away_goals (即 diff == -hline)
            # 主负条件: home_goals + hline < away_goals (即 diff < -hline)
            threshold = -hline  # 主队需要赢的净胜球数
            hc_h = 0.0
            hc_d = 0.0
            hc_a = 0.0
            for i in range(n):
                for j in range(n):
                    diff = i - j
                    if abs(threshold - round(threshold)) < 0.01:
                        # 整数盘口 (如 -1, -2, +1)
                        thr_int = int(round(threshold))
                        if diff > thr_int:
                            hc_h += mat[i, j]
                        elif diff == thr_int:
                            hc_d += mat[i, j]
                        else:
                            hc_a += mat[i, j]
                    else:
                        # 半球盘口 (如 -0.5, -1.5, +0.5)
                        if diff > threshold:
                            hc_h += mat[i, j]
                        else:
                            hc_a += mat[i, j]
            hc_prior = {"h": round(hc_h, 4), "d": round(hc_d, 4), "a": round(hc_a, 4),
                        "line": handicap_line}
        except (ValueError, TypeError):
            pass

    # fallback: 如果没有让球线,用固定 -1 让球
    if hc_prior is None:
        hc_h = sum(mat[i, j] for i in range(n) for j in range(n) if i - j > 1)
        hc_d = sum(mat[i, j] for i in range(n) for j in range(n) if i - j == 1)
        hc_a = sum(mat[i, j] for i in range(n) for j in range(n) if i - j < 1)
        hc_prior = {"h": round(hc_h, 4), "d": round(hc_d, 4), "a": round(hc_a, 4),
                    "line": "-1"}

    # 总进球
    ttg = {}
    for g in range(n):
        ttg[str(g)] = float(sum(mat[i, g-i] for i in range(n) if 0 <= g-i < n))
    # 热门比分
    scores = [(f"{i}-{j}", float(mat[i, j])) for i in range(6) for j in range(6)]
    scores.sort(key=lambda x: -x[1])

    result = {
        "lam_h": round(lam_h, 3), "lam_a": round(lam_a, 3),
        # 战意系数 (实时积分)
        "motivation_h": round(mot_h, 3),
        "motivation_a": round(mot_a, 3),
        "status_h": status_h,
        "status_a": status_a,
        # 主输出: 让球盘先验
        "hc_prior": hc_prior,
        # 辅助输出: 常规胜平负先验(仅供参考)
        "prior": {"h": round(p_h, 4), "d": round(p_d, 4), "a": round(p_a, 4)},
        "ttg": {k: round(v, 4) for k, v in ttg.items()},
        "top_scores": scores[:6],
    }
    return result


# ═══════════════════════════════════════════════════════════════════
# 4. 情境微调 (天气 + 伤病)
# ═══════════════════════════════════════════════════════════════════

def get_corner_boost(team: str) -> float:
    """
    获取角球/定位球能力加成。返回 λ 加成 0-0.15。

    优先级:
    1. corners.json 手动数据(如果存在)
    2. 用 Elo 等级估算(高Elo队伍角球能力强)

    Args:
        team: 中文队名

    Returns:
        λ 加成(0-0.15),直接乘到 λ 上(如 λ × (1 + boost))
    """
    # 1. 优先读 corners.json
    if CORNERS_FILE.exists():
        try:
            corners_data = json.loads(CORNERS_FILE.read_text(encoding="utf-8"))
            if team in corners_data:
                entry = corners_data[team]
                cpg = entry.get("corners_per_game", 0)  # 场均角球数
                ctg = entry.get("corners_to_goals", 0)  # 角球转化率

                # 综合评分: 场均角球数反映控制力,转化率反映效率
                # 欧洲顶级队伍: cpg ~6-7, ctg ~0.10-0.12
                # 归一化: cpg/7 × 0.5 + ctg/0.12 × 0.5, 上限 0.15
                score = (min(cpg / 7.0, 1.0) * 0.5 + min(ctg / 0.12, 1.0) * 0.5) * 0.15
                return round(score, 3)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # 2. fallback: 用 Elo 估算
    elo = get_elo(team)
    if elo is None:
        return 0.0

    # Elo -> 角球能力映射
    # 假设: Elo 2000+ → 0.12, Elo 1800 → 0.06, Elo 1500 → 0.0
    # 线性映射: boost = max(0, (elo - 1500) / 500 * 0.12)
    if elo >= 2000:
        boost = 0.12
    elif elo >= 1500:
        boost = (elo - 1500) / 500 * 0.12
    else:
        boost = 0.0

    return round(min(boost, 0.15), 3)


def get_adjustments(home: str, away: str) -> tuple[float, float, list[str]]:
    """返回 (adj_h, adj_a, notes)。adj 是 λ 的乘数(1.0=不变)。"""
    adj_h, adj_a = 1.0, 1.0
    notes = []

    # 角球/定位球能力加成
    corner_boost_h = get_corner_boost(home)
    corner_boost_a = get_corner_boost(away)
    if corner_boost_h > 0.02:
        adj_h *= (1.0 + corner_boost_h)
        notes.append(f"{home}定位球能力强(λ×{1+corner_boost_h:.3f})")
    if corner_boost_a > 0.02:
        adj_a *= (1.0 + corner_boost_a)
        notes.append(f"{away}定位球能力强(λ×{1+corner_boost_a:.3f})")

    # 风格相克调整
    h_style = _get_team_style(home)
    a_style = _get_team_style(away)
    if h_style and a_style:
        # 进攻型 vs 防守型: 进攻方λ打折(被克制), 防守方λ不变
        h_is_attacker = h_style["attack"] > 1.8 and h_style["tempo"] > 2.8
        a_is_attacker = a_style["attack"] > 1.8 and a_style["tempo"] > 2.8
        h_is_defensive = a_style["low_block"] > 0.65 and a_style["attack"] < 1.6
        a_is_defensive = h_style["low_block"] > 0.65 and h_style["attack"] < 1.6

        if h_is_attacker and h_is_defensive:
            adj_h *= 0.88
            notes.append(f"风格相克: {home}进攻被{away}防反克制(λ×0.88)")
        elif a_is_attacker and a_is_defensive:
            adj_a *= 0.88
            notes.append(f"风格相克: {away}进攻被{home}防反克制(λ×0.88)")

    # 磨合度/经验因子 (自动量化 + 手动定性)
    for team, adj_key in [(home, "adj_h"), (away, "adj_a")]:
        cohesion_factor = _get_cohesion_factor(team)
        if cohesion_factor != 1.0:
            if adj_key == "adj_h":
                adj_h *= cohesion_factor
            else:
                adj_a *= cohesion_factor
            if cohesion_factor < 0.95:
                notes.append(f"{team}磨合度低(λ×{cohesion_factor:.2f})")

    # 伤病 (从 injuries.json 读取)
    if INJURIES_FILE.exists():
        inj = json.loads(INJURIES_FILE.read_text(encoding="utf-8"))
        for team, adj_key in [(home, "adj_h"), (away, "adj_a")]:
            if team in inj:
                factor = inj[team].get("lambda_factor", 1.0)
                if adj_key == "adj_h":
                    adj_h *= factor
                else:
                    adj_a *= factor
                if factor != 1.0:
                    notes.append(f"{team}: {inj[team].get('reason','伤停')} (λ×{factor:.2f})")

    # 天气 (高温>33°C 或 湿度>85% 降总进球)
    weather_file = DATA_DIR / "weather.json"
    if weather_file.exists():
        w = json.loads(weather_file.read_text(encoding="utf-8"))
        key = f"{home}vs{away}"
        if key in w and "temp_c" in w[key]:
            max_temp = max(w[key]["temp_c"])
            avg_hum = sum(w[key]["humidity"]) / len(w[key]["humidity"])
            if max_temp > 33:
                factor = 0.92
                adj_h *= factor; adj_a *= factor
                notes.append(f"高温{max_temp:.0f}°C (λ×{factor})")
            elif avg_hum > 85:
                factor = 0.95
                adj_h *= factor; adj_a *= factor
                notes.append(f"高湿{avg_hum:.0f}% (λ×{factor})")
    return adj_h, adj_a, notes


# ═══════════════════════════════════════════════════════════════════
# 5. 校准
# ═══════════════════════════════════════════════════════════════════

def hhad_to_had(hhad_prob: dict, handicap_line: float, score_matrix: np.ndarray) -> dict:
    """
    从让球盘概率(hhad) + 让球线 + 比分矩阵 → 反推等效的常规胜平负(had)概率。

    原理: 让球盘隐含了市场对比分分布的预期。通过比分矩阵对让球线分区积分,
    可以反推出市场认为的常规胜平负概率分布。

    Args:
        hhad_prob: 让球盘去水概率 {"h": 主让胜, "d": 平, "a": 客让胜}
        handicap_line: 让球线(负=主队让,如-2.0表示主让2球; 可能是str需转换)
        score_matrix: Dixon-Coles 比分概率矩阵 [i主进球, j客进球]

    Returns:
        等效 had 概率 {"h": 主胜, "d": 平, "a": 客胜}
    """
    try:
        hc_line = float(handicap_line)
    except (ValueError, TypeError):
        # 无法解析让球线,返回均匀分布
        return {"h": 0.33, "d": 0.33, "a": 0.34}

    n = score_matrix.shape[0]
    threshold = -hc_line  # 转成主队视角净胜球阈值(如让-2 → 需净胜>2)

    # 从比分矩阵提取让球盘区域的总概率
    hc_h_total = 0.0  # 让球主胜区域
    hc_d_total = 0.0  # 让球平局区域
    hc_a_total = 0.0  # 让球客胜区域

    for i in range(n):
        for j in range(n):
            diff = i - j
            if abs(threshold - round(threshold)) < 0.01:
                # 整数盘口
                thr_int = int(round(threshold))
                if diff > thr_int:
                    hc_h_total += score_matrix[i, j]
                elif diff == thr_int:
                    hc_d_total += score_matrix[i, j]
                else:
                    hc_a_total += score_matrix[i, j]
            else:
                # 半球盘口(无平局)
                if diff > threshold:
                    hc_h_total += score_matrix[i, j]
                else:
                    hc_a_total += score_matrix[i, j]

    # 归一化模型的让球盘概率分布
    hc_model = {"h": hc_h_total, "d": hc_d_total, "a": hc_a_total}
    hc_sum = sum(hc_model.values())
    if hc_sum > 0:
        hc_model = {k: v/hc_sum for k, v in hc_model.items()}

    # 市场的 hhad 认为比分分布偏离模型。按市场/模型的比值调整比分矩阵
    # 简化假设: 市场 hhad 反映了对各让球区域的概率调整
    # 用市场 hhad / 模型 hhad 作为权重,重新分配比分矩阵到常规胜平负
    adj_matrix = score_matrix.copy()

    # 对让球主胜区域的比分按 市场hhad_h/模型hhad_h 缩放
    for i in range(n):
        for j in range(n):
            diff = i - j
            if abs(threshold - round(threshold)) < 0.01:
                thr_int = int(round(threshold))
                if diff > thr_int and hc_model["h"] > 0:
                    adj_matrix[i, j] *= (hhad_prob["h"] / hc_model["h"])
                elif diff == thr_int and hc_model["d"] > 0:
                    adj_matrix[i, j] *= (hhad_prob["d"] / hc_model["d"])
                elif diff < thr_int and hc_model["a"] > 0:
                    adj_matrix[i, j] *= (hhad_prob["a"] / hc_model["a"])
            else:
                if diff > threshold and hc_model["h"] > 0:
                    adj_matrix[i, j] *= (hhad_prob["h"] / hc_model["h"])
                elif diff <= threshold and hc_model["a"] > 0:
                    adj_matrix[i, j] *= (hhad_prob["a"] / hc_model["a"])

    # 从调整后的比分矩阵计算常规 had
    had_h = sum(adj_matrix[i, j] for i in range(n) for j in range(n) if i > j)
    had_d = sum(adj_matrix[i, j] for i in range(n) for j in range(n) if i == j)
    had_a = sum(adj_matrix[i, j] for i in range(n) for j in range(n) if i < j)

    had_sum = had_h + had_d + had_a
    if had_sum > 0:
        return {"h": had_h/had_sum, "d": had_d/had_sum, "a": had_a/had_sum}
    else:
        # fallback 到模型常规 had
        return {"h": 0.33, "d": 0.33, "a": 0.34}


def calibrate(prior: dict, market: dict | None, w: float = 0.6, is_handicap: bool = False) -> dict:
    """
    对数池校准: 先验(模型) × 后验(市场) → 校准概率。

    Args:
        prior: 模型先验概率 {"h","d","a"}
        market: 市场去水概率 (可选)
        w: 市场权重 (默认0.6 = 市场60% + 模型40%)
        is_handicap: 是否为让球盘校准 (影响市场权重)

    Returns:
        校准后的概率分布
    """
    if not market:
        return {**prior, "src": "model_only"}

    # 让球盘: 市场更精准,提高市场权重到0.70
    if is_handicap:
        w = 0.70

    post = {}
    for k in ("h", "d", "a"):
        p = max(prior.get(k, 0.33), 0.01)
        m = max(market.get(k, 0.33), 0.01)
        post[k] = math.exp((1 - w) * math.log(p) + w * math.log(m))
    s = sum(post.values())
    return {k: round(v/s, 4) for k, v in post.items()} | {"src": "calibrated"}


def edge(post: dict, market: dict | None) -> dict | None:
    if not market:
        return None
    return {k: round(post.get(k, 0) - market.get(k, 0), 4) for k in ("h", "d", "a")}


def calibrate_n(prior: dict, market: dict | None, w: float = 0.5) -> dict:
    """通用 n-outcome 校准: 对数池融合先验(模型) × 后验(市场) → 校准概率。
    用于 TTG (8 outcomes), HAFU (9 outcomes) 等多玩法市场。
    """
    if not market:
        return {**prior, "src": "model_only"}
    keys = set(prior.keys()) & set(market.keys())
    if not keys:
        return {**prior, "src": "model_only"}
    post = {}
    for k in keys:
        p = max(prior.get(k, 0.01), 0.005)
        m = max(market.get(k, 0.01), 0.005)
        # 几何平均 (log-pool) with floor
        post[k] = math.exp((1 - w) * math.log(p) + w * math.log(m))
    s = sum(post.values())
    if s <= 0:
        return {**prior, "src": "model_only"}
    return {k: round(v/s, 4) for k, v in post.items()} | {"src": "calibrated"}


def kelly_fraction(model_prob: float, odds: float) -> float:
    """凯利公式: f* = (bp - q) / b, 其中 b=赔率-1, p=模型概率, q=1-p。
    返回 0~1 的最佳下注比例,加 0.25 系数做半凯利更稳健。"""
    if odds <= 1.0 or model_prob <= 0:
        return 0.0
    b = odds - 1.0
    q = 1.0 - model_prob
    f = (b * model_prob - q) / b
    return max(0.0, f * 0.25)  # 半凯利


def compute_recommendations(rec: dict, pred: dict) -> list[dict]:
    """对每场比赛,扫描所有市场找出有 edge 的投注项。
    返回按 edge 降序排列的推荐列表。"""
    out = []
    m = rec  # rec 中含有市场赔率
    p = pred  # pred 中含有模型概率

    def _add(market_name, outcome, model_p, odds, mkt_p, prob_label):
        if model_p is None or odds is None or odds <= 1.0:
            return
        edge_v = model_p - mkt_p
        if edge_v < 0.03:  # 3% edge 门槛
            return
        kelly = kelly_fraction(model_p, odds)
        if kelly < 0.005:  # 凯利<0.5% 不下注
            return
        # 期望值: 模型概率 × 赔率 - 1
        ev = model_p * odds - 1.0
        out.append({
            "market": market_name,
            "outcome": outcome,
            "label": prob_label,
            "model_p": round(model_p, 4),
            "mkt_p": round(mkt_p, 4),
            "odds": odds,
            "edge": round(edge_v, 4),
            "ev": round(ev, 4),
            "kelly": round(kelly, 4),
        })

    # 1. HAD 胜平负
    if m.get("had_odds") and p.get("prior") and m.get("had_prob"):
        had_o = m["had_odds"]
        had_mkt = m["had_prob"]
        had_model = p["prior"]
        _add("胜平负", "h", had_model["h"], had_o["h"], had_mkt["h"], "主胜")
        _add("胜平负", "d", had_model["d"], had_o["d"], had_mkt["d"], "平局")
        _add("胜平负", "a", had_model["a"], had_o["a"], had_mkt["a"], "客胜")

    # 2. HHAD 让球胜平负
    if m.get("hhad_odds") and p.get("hc_prior") and m.get("hhad_prob"):
        hhad_o = m["hhad_odds"]
        hhad_mkt = m["hhad_prob"]
        hc_model = p["hc_prior"]
        _add(f"让球{m.get('hhad_line','')}", "h", hc_model["h"], hhad_o["h"], hhad_mkt["h"], "主让胜")
        _add(f"让球{m.get('hhad_line','')}", "d", hc_model["d"], hhad_o["d"], hhad_mkt["d"], "平(让球)")
        _add(f"让球{m.get('hhad_line','')}", "a", hc_model["a"], hhad_o["a"], hhad_mkt["a"], "客让胜")

    # 3. TTG 总进球 (0/1/2/3/4/5/6/7+)
    if m.get("ttg_odds") and p.get("ttg") and m.get("ttg_prob"):
        ttg_o = m["ttg_odds"]
        ttg_mkt = m["ttg_prob"]
        ttg_model = p["ttg"]
        for g in range(len(ttg_o)):
            k = str(g)
            if k in ttg_o and k in ttg_mkt and k in ttg_model:
                label = f"{g}球" if g < 7 else "7+球"
                _add("总进球", k, ttg_model[k], ttg_o[k], ttg_mkt[k], label)

    out.sort(key=lambda x: -x["edge"])
    return out


# ═══════════════════════════════════════════════════════════════════
# 6. HTML 生成 (带刷新按钮 + 自动重载)
# ═══════════════════════════════════════════════════════════════════

def _mot_color(motivation: float) -> str:
    """战意系数 → 颜色: 1.0=正常, <0.95=灰(轮换/淘汰), >1.05=绿(必拼)"""
    if motivation >= 1.05:
        return "#4ade80"  # green
    if motivation <= 0.90:
        return "#8a8178"  # gray (rotation / eliminated)
    return "#c4bcb2"  # normal text


def render_html(predictions: list[dict]) -> str:
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 数据来源/新鲜度
    fresh = DATA_DIR / "odds_parsed_fresh.json"
    if fresh.exists():
        age_sec = time.time() - fresh.stat().st_mtime
        if age_sec < 3600:
            data_source = f"4090 relay ({int(age_sec/60)}分钟前)"
        else:
            data_source = f"4090 relay ({int(age_sec/3600)}小时前)"
    else:
        cache = DATA_DIR / "odds_parsed.json"
        if cache.exists():
            age_sec = time.time() - cache.stat().st_mtime
            data_source = f"本地缓存 ({int(age_sec/60)}分钟前)"
        else:
            data_source = "未知"
    cards_html = []
    for p in predictions:
        # ═══ 主显示: 让球盘后验概率 ═══
        ph = p["hhad_posterior"]["h"]
        pd_ = p["hhad_posterior"]["d"]
        pa = p["hhad_posterior"]["a"]
        handicap_line = p.get("handicap_line", "-1")
        # 概率条比例(百分比宽度)
        bar_h = f"{ph*100:.1f}"
        bar_d = f"{pd_*100:.1f}"
        bar_a = f"{pa*100:.1f}"

        # 模型置信度: max(posterior) 越高越确信
        confidence = max(ph, pd_, pa)
        conf_label = "高确信" if confidence > 0.65 else ("中等" if confidence > 0.45 else "开放")
        conf_color = "#1a7f37" if confidence > 0.65 else ("#9a6700" if confidence > 0.45 else "#656d76")

        # Edge 信号 (让球盘)
        edge_signal = ""
        if p.get("hhad_edge"):
            e = p["hhad_edge"]
            best_k = max(e, key=lambda k: e[k])
            best_v = e[best_k]
            if best_v > 0.03:
                _elabels = {"h": "主让胜", "d": "平局", "a": "客让胜"}
                label = _elabels[best_k]
                edge_signal = f'<span class="value-badge">{label} +{best_v:.1%}</span>'

        # 市场概率对比 (让球盘)
        mkt_row = ""
        if p.get("hhad_market"):
            m = p["hhad_market"]
            mkt_row = f'''<tr><td class="row-label">市场</td>
            <td class="num">{m["h"]:.0%}</td><td class="num">{m["d"]:.0%}</td><td class="num">{m["a"]:.0%}</td></tr>'''

        # 常规盘胜率 (小字显示,仅供参考)
        had_ref = ""
        if p.get("had_posterior"):
            hp = p["had_posterior"]
            had_ref = f'<div class="had-ref">常规盘参考: 主{hp["h"]:.0%} / 平{hp["d"]:.0%} / 客{hp["a"]:.0%}</div>'

        # 热门比分
        scores = p.get("top_scores", [])[:5]
        scores_chips = "".join(f'<span class="score-chip"><b>{s}</b> {prob:.0%}</span>' for s, prob in scores)

        # 注释
        notes_html = ""
        if p.get("notes"):
            notes_html = f'<div class="insight">{p["notes"]}</div>'
        if p.get("odds_movement"):
            notes_html += f'<div class="insight movement">{p["odds_movement"]}</div>'

        # 让球线显示
        try:
            hc_line_display = f"让{float(handicap_line):+.1f}球" if handicap_line else "让-1球"
        except (ValueError, TypeError):
            hc_line_display = f"让{handicap_line}"

        # ═══ 多玩法数据准备 ═══
        # 1. 常规盘 (HAD) - 仅在开盘时显示
        had_section = ""
        if p.get("had_posterior") and p.get("had_odds"):
            hp = p["had_posterior"]
            ho = p["had_odds"]
            hm = p.get("had_market") or {}
            had_section = f'''
    <div class="market-section">
      <div class="market-title">胜平负 (HAD)</div>
      <table>
        <thead><tr><th></th><th>主胜</th><th>平局</th><th>客胜</th></tr></thead>
        <tbody>
          <tr><td class="row-label">模型</td>
          <td class="num">{p["prior"]["h"]:.0%}</td><td class="num">{p["prior"]["d"]:.0%}</td><td class="num">{p["prior"]["a"]:.0%}</td></tr>
          <tr><td class="row-label">市场</td>
          <td class="num">{hm.get("h",0):.0%}</td><td class="num">{hm.get("d",0):.0%}</td><td class="num">{hm.get("a",0):.0%}</td></tr>
          <tr class="posterior-row"><td class="row-label">后验</td>
          <td class="num"><b>{hp["h"]:.0%}</b></td><td class="num"><b>{hp["d"]:.0%}</b></td><td class="num"><b>{hp["a"]:.0%}</b></td></tr>
          <tr><td class="row-label">赔率</td>
          <td class="num">{ho["h"]:.2f}</td><td class="num">{ho["d"]:.2f}</td><td class="num">{ho["a"]:.2f}</td></tr>
        </tbody>
      </table>
    </div>'''

        # 2. 总进球 (TTG) - 显示模型概率 vs 市场概率
        ttg_section = ""
        if p.get("ttg_odds") and p.get("ttg"):
            ttg_o = p["ttg_odds"]
            ttg_mkt = p.get("ttg_market") or {}
            ttg_model = p["ttg"]
            ttg_post = p.get("ttg_posterior") or {}
            # 找出最大概率的进球数
            best_g = max(ttg_mkt.keys(), key=lambda k: ttg_mkt.get(k, 0)) if ttg_mkt else "0"
            best_market_p = ttg_mkt.get(best_g, 0)
            best_model_p = ttg_model.get(best_g, 0)
            best_odds = ttg_o.get(best_g, 0)
            best_post = ttg_post.get(best_g, 0) if ttg_post else 0
            # 渲染 0-3 球 + 4+球
            ttg_cells_model = "".join(
                f'<td class="num">{ttg_model.get(str(g), 0):.0%}</td>'
                for g in range(4))
            ttg_cells_market = "".join(
                f'<td class="num">{ttg_mkt.get(str(g), 0):.0%}</td>'
                for g in range(4))
            ttg_cells_post = "".join(
                f'<td class="num"><b>{ttg_post.get(str(g), 0):.0%}</b></td>'
                for g in range(4))
            ttg_cells_odds = "".join(
                f'<td class="num">{ttg_o.get(str(g), 0):.2f}</td>'
                for g in range(4))
            # 4+球合并
            p4plus_m = sum(ttg_model.get(str(g), 0) for g in range(4, 8))
            p4plus_k = sum(ttg_mkt.get(str(g), 0) for g in range(4, 8))
            p4plus_post = sum(ttg_post.get(str(g), 0) for g in range(4, 8)) if ttg_post else 0
            o4plus_inv = sum(1.0/ttg_o.get(str(g), 999) for g in range(4, 8) if ttg_o.get(str(g)))
            o4plus = 1.0 / o4plus_inv if o4plus_inv > 0 else 0
            ttg_section = f'''
    <div class="market-section">
      <div class="market-title">总进球 (TTG) · 市场最可能: <b>{best_g}球</b> ({best_market_p:.0%})</div>
      <table>
        <thead><tr><th></th><th>0球</th><th>1球</th><th>2球</th><th>3球</th><th>4+球</th></tr></thead>
        <tbody>
          <tr><td class="row-label">模型</td>
          {ttg_cells_model}<td class="num">{p4plus_m:.0%}</td></tr>
          <tr><td class="row-label">市场</td>
          {ttg_cells_market}<td class="num">{p4plus_k:.0%}</td></tr>
          <tr class="posterior-row"><td class="row-label">后验</td>
          {ttg_cells_post}<td class="num"><b>{p4plus_post:.0%}</b></td></tr>
          <tr><td class="row-label">赔率</td>
          {ttg_cells_odds}<td class="num">{o4plus:.2f}</td></tr>
        </tbody>
      </table>
    </div>'''

        # 3. 体彩购买建议
        recs = p.get("recommendations") or []
        rec_html = ""
        if recs:
            rec_chips = ""
            for r in recs[:3]:  # 最多3个推荐
                kelly_pct = r["kelly"] * 100
                ev_pct = r["ev"] * 100
                # 颜色: 期望值越高越绿
                if r["ev"] > 0.15:
                    color = "#4ade80"
                elif r["ev"] > 0.05:
                    color = "#fbbf24"
                else:
                    color = "#60a5fa"
                rec_chips += f'''<div class="rec-chip" style="border-color:{color}">
              <div class="rec-market">{r["market"]} · <b>{r["label"]}</b></div>
              <div class="rec-odds">赔率 <b>{r["odds"]:.2f}</b> · 模型 {r["model_p"]:.0%} · 市场 {r["mkt_p"]:.0%}</div>
              <div class="rec-edge" style="color:{color}">edge +{r["edge"]:.1%} · EV {ev_pct:+.1f}% · 凯利 {kelly_pct:.1f}%</div>
            </div>'''
            rec_html = f'''
    <div class="rec-section">
      <div class="rec-title">体彩购买建议 (Top {min(3, len(recs))} of {len(recs)})</div>
      <div class="rec-list">{rec_chips}</div>
    </div>'''

        # 主让球盘 (HHAD) - 总是显示
        card = f'''<article class="match">
  <header>
    <div class="matchup">
      <span class="team home">{p["home"]}</span>
      <span class="vs">vs</span>
      <span class="team away">{p["away"]}</span>
    </div>
    <div class="meta-row">
      <time>{p["date"]} {p["time"][:5]}</time>
      <span class="league-tag">{p.get("league","")}</span>
      <span class="hc-tag">{hc_line_display}</span>
      <span class="conf-tag" style="color:{conf_color}">{conf_label}</span>
      <span class="mot-tag" title="主队战意: {p.get("status_h","")} | λ×{p.get("motivation_h",1.0):.2f}" style="color:{_mot_color(p.get("motivation_h",1.0))}">主 {p.get("status_h","-")} {p.get("motivation_h",1.0):.2f}</span>
      <span class="mot-tag" title="客队战意: {p.get("status_a","")} | λ×{p.get("motivation_a",1.0):.2f}" style="color:{_mot_color(p.get("motivation_a",1.0))}">客 {p.get("status_a","-")} {p.get("motivation_a",1.0):.2f}</span>
      {edge_signal}
    </div>
  </header>

  <div class="prob-visual">
    <div class="bar-container">
      <div class="bar bar-h" style="width:{bar_h}%"><span>{ph:.0%}</span></div>
      <div class="bar bar-d" style="width:{bar_d}%"><span>{pd_:.0%}</span></div>
      <div class="bar bar-a" style="width:{bar_a}%"><span>{pa:.0%}</span></div>
    </div>
    <div class="bar-labels"><span>主让胜</span><span>平局</span><span>客让胜</span></div>
  </div>

  <div class="data-grid">
    <div class="market-section primary">
      <div class="market-title">让球盘 (HHAD) {hc_line_display}</div>
      <table>
        <thead><tr><th></th><th>主让胜</th><th>平局</th><th>客让胜</th></tr></thead>
        <tbody>
          <tr><td class="row-label">模型</td>
          <td class="num">{p["hc_prior"]["h"]:.0%}</td><td class="num">{p["hc_prior"]["d"]:.0%}</td><td class="num">{p["hc_prior"]["a"]:.0%}</td></tr>
          {mkt_row}
          <tr class="posterior-row"><td class="row-label">后验</td>
          <td class="num"><b>{ph:.0%}</b></td><td class="num"><b>{pd_:.0%}</b></td><td class="num"><b>{pa:.0%}</b></td></tr>
        </tbody>
      </table>
    </div>
    {had_section}
    {ttg_section}
    <div class="params">
      <span>Elo {p["elo_h"]} / {p["elo_a"]}</span>
      <span>λ {p["lam_h"]:.2f} / {p["lam_a"]:.2f}</span>
    </div>
  </div>
  {rec_html}
  <div class="chips-row">
    {scores_chips}
  </div>
  {notes_html}
</article>'''
        cards_html.append(card)

    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Match Forecast</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #0f0d0c;
  --surface: rgba(28, 25, 23, 0.78);
  --border: #332e2a;
  --text-1: #f5f1ed;
  --text-2: #c4bcb2;
  --text-3: #8a8178;
  --accent: #e8783a;
  --accent-soft: #2e1f14;
  --green: #4ade80;
  --green-bg: #132a1c;
  --amber: #fbbf24;
  --amber-bg: #2a2010;
  --blue: #60a5fa;
  --blue-bg: #0f1f3a;
  --red: #f87171;
  --red-bg: #2a1010;
  --radius: 8px;
  --mono: 'JetBrains Mono', monospace;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: 'Inter', -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text-1);
  line-height: 1.5;
  padding: 40px 20px;
  max-width: 720px;
  margin: 0 auto;
  position: relative;
  z-index: 1;
}}
body::before {{
  content: '';
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: url('messi.png') center/cover no-repeat fixed;
  opacity: 0.55;
  z-index: -2;
  pointer-events: none;
}}
body::after {{
  content: '';
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: linear-gradient(180deg, rgba(15,13,12,0.55) 0%, rgba(15,13,12,0.78) 60%, rgba(15,13,12,0.9) 100%);
  z-index: -1;
  pointer-events: none;
}}
header.page-header {{
  margin-bottom: 40px;
  padding-bottom: 24px;
  border-bottom: 1px solid var(--border);
}}
header.page-header h1 {{
  font-size: 1.1em;
  font-weight: 600;
  letter-spacing: -0.02em;
  margin-bottom: 4px;
}}
header.page-header .tagline {{
  font-size: .8em;
  color: var(--text-3);
}}
.controls {{
  display: flex;
  gap: 8px;
  margin-bottom: 32px;
}}
.controls button {{
  font-family: inherit;
  font-size: .78em;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text-2);
  cursor: pointer;
  transition: border-color .15s;
}}
.controls button:hover {{ border-color: var(--accent); color: var(--accent); }}
.status-msg {{
  font-size: .75em;
  color: var(--text-3);
  transition: opacity .3s;
}}
.status-msg.success {{ color: var(--green); }}
.status-msg.error {{ color: var(--red); }}
.status-msg.loading {{ color: var(--amber); }}
@keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.5 }} }}
.status-msg.loading {{ animation: pulse 1.2s infinite; }}

.match {{
  background: var(--surface);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px;
  margin-bottom: 16px;
  transition: border-color .2s;
}}
.match:hover {{ border-color: var(--text-3); }}

.match header {{
  margin-bottom: 16px;
}}
.matchup {{
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 6px;
}}
.team {{ font-size: 1.15em; font-weight: 600; letter-spacing: -0.01em; }}
.vs {{ font-size: .75em; color: var(--text-3); font-weight: 400; }}
.meta-row {{
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: .75em;
  color: var(--text-3);
}}
.league-tag {{
  background: var(--accent-soft);
  color: var(--accent);
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 500;
  font-size: .85em;
}}
.hc-tag {{
  background: var(--blue-bg);
  color: var(--blue);
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 500;
  font-size: .85em;
}}
.conf-tag {{ font-weight: 500; }}
.mot-tag {{
  font-size: .7em;
  font-family: var(--mono);
  padding: 2px 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--bg);
  cursor: help;
}}
.value-badge {{
  background: var(--green-bg);
  color: var(--green);
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
  font-size: .9em;
}}

.prob-visual {{ margin-bottom: 16px; }}
.bar-container {{
  display: flex;
  height: 32px;
  border-radius: 6px;
  overflow: hidden;
  gap: 2px;
}}
.bar {{
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--mono);
  font-size: .75em;
  font-weight: 500;
  transition: width .4s ease;
  min-width: 30px;
}}
.bar span {{ opacity: .9; }}
.bar-h {{ background: var(--green-bg); color: var(--green); }}
.bar-d {{ background: var(--amber-bg); color: var(--amber); }}
.bar-a {{ background: var(--blue-bg); color: var(--blue); }}
.bar-labels {{
  display: flex;
  justify-content: space-between;
  font-size: .65em;
  color: var(--text-3);
  margin-top: 4px;
  padding: 0 4px;
}}

.data-grid {{ margin-bottom: 12px; }}
.data-grid table {{
  width: 100%;
  border-collapse: collapse;
  font-size: .8em;
}}
.data-grid th {{
  font-weight: 500;
  color: var(--text-3);
  text-align: right;
  padding: 4px 8px;
  font-size: .85em;
}}
.data-grid th:first-child {{ text-align: left; }}
.data-grid td {{ padding: 4px 8px; }}
.data-grid .row-label {{ color: var(--text-3); font-size: .85em; }}
.data-grid .num {{ text-align: right; font-family: var(--mono); font-size: .85em; }}
.posterior-row td {{ border-top: 1px solid var(--border); }}
.params {{
  display: flex;
  gap: 16px;
  margin-top: 8px;
  font-size: .7em;
  color: var(--text-3);
  font-family: var(--mono);
}}
.had-ref {{
  font-size: .7em;
  color: var(--text-3);
  margin-top: 4px;
  font-style: italic;
}}
.market-section {{
  margin-bottom: 12px;
  padding: 8px 0;
  border-bottom: 1px dashed var(--border);
}}
.market-section:last-of-type {{
  border-bottom: none;
}}
.market-section.primary {{
  background: var(--accent-soft);
  margin: 0 -16px 12px;
  padding: 10px 16px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
}}
.market-title {{
  font-size: .72em;
  color: var(--text-2);
  margin-bottom: 6px;
  font-weight: 500;
  letter-spacing: 0.02em;
}}
.market-section.primary .market-title {{
  color: var(--accent);
  font-size: .8em;
}}
.rec-section {{
  background: linear-gradient(135deg, rgba(232,120,58,0.10), rgba(232,120,58,0.04));
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  padding: 10px 12px;
  margin: 12px 0;
}}
.rec-title {{
  font-size: .75em;
  color: var(--accent);
  font-weight: 600;
  margin-bottom: 8px;
  letter-spacing: 0.04em;
}}
.rec-list {{
  display: flex;
  flex-direction: column;
  gap: 6px;
}}
.rec-chip {{
  background: rgba(15,13,12,0.4);
  border-left: 3px solid;
  border-radius: 4px;
  padding: 6px 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}}
.rec-market {{
  font-size: .78em;
  color: var(--text-1);
  font-weight: 500;
}}
.rec-odds {{
  font-size: .7em;
  color: var(--text-2);
  font-family: var(--mono);
}}
.rec-edge {{
  font-size: .7em;
  font-weight: 600;
  font-family: var(--mono);
}}
.no-rec {{
  font-size: .7em;
  color: var(--text-3);
  font-style: italic;
  padding: 4px 0;
}}

.chips-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}}
.score-chip {{
  background: var(--bg);
  border: 1px solid var(--border);
  padding: 3px 8px;
  border-radius: 4px;
  font-size: .75em;
  font-family: var(--mono);
}}
.score-chip b {{ font-weight: 600; }}
.detail-chip {{
  background: var(--accent-soft);
  color: var(--accent);
  padding: 3px 8px;
  border-radius: 4px;
  font-size: .72em;
  font-weight: 500;
}}

.insight {{
  margin-top: 10px;
  padding: 8px 12px;
  background: var(--amber-bg);
  border-left: 3px solid var(--amber);
  border-radius: 0 var(--radius) var(--radius) 0;
  font-size: .75em;
  color: var(--amber);
}}
.insight.movement {{
  background: var(--red-bg);
  border-color: var(--red);
  color: var(--red);
}}

footer.page-footer {{
  margin-top: 40px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
  font-size: .7em;
  color: var(--text-3);
  line-height: 1.7;
}}
@media (max-width: 500px) {{
  body {{ padding: 20px 12px; }}
  .match {{ padding: 16px; }}
  .matchup {{ flex-wrap: wrap; gap: 6px; }}
}}
</style>
</head><body>
<header class="page-header">
  <h1>让球盘预测 (Asian Handicap Forecast)</h1>
  <p class="tagline">Dixon-Coles + 体彩让球盘校准 · {gen_time} · 数据: {data_source}</p>
</header>

<div class="controls">
  <button onclick="location.reload()">刷新</button>
  <button id="fetchBtn" onclick="doRefresh()">重新抓取</button>
  <a href="/data/standings.json" target="_blank" class="link-btn" style="font-size:.75em;color:var(--text-2);text-decoration:none;padding:6px 10px;border:1px solid var(--border);border-radius:var(--radius);">实时积分</a>
  <a href="/data/groups_2026.json" target="_blank" class="link-btn" style="font-size:.75em;color:var(--text-2);text-decoration:none;padding:6px 10px;border:1px solid var(--border);border-radius:var(--radius);">小组分组</a>
  <span id="status" class="status-msg"></span>
</div>

{"".join(cards_html)}

<footer class="page-footer">
  Elo锚定双泊松 · Dixon-Coles τ (ρ={RHO}) · 平局逻辑回归(8特征) · 风格相克 · 磨合度 · xG档案 · 角球能力<br>
  让球盘对数池校准 (市场70% + 模型30%) · 数据: sporttery.cn · eloratings.net · open-meteo<br>
  仅供研究参考，不构成投注建议
</footer>
<script>
  setTimeout(()=>location.reload(), 60000);
  function doRefresh() {{
    const btn = document.getElementById('fetchBtn');
    const st = document.getElementById('status');
    btn.disabled = true;
    btn.style.opacity = '0.5';
    st.textContent = '正在抓取数据...';
    st.className = 'status-msg loading';
    const t0 = Date.now();
    fetch('api/refresh')
      .then(r => {{
        if (!r.ok) throw new Error(r.status);
        return r.json();
      }})
      .then(() => {{
        const sec = ((Date.now()-t0)/1000).toFixed(1);
        st.textContent = `完成 (${{sec}}s)，3秒后刷新页面...`;
        st.className = 'status-msg success';
        setTimeout(()=>location.reload(), 3000);
      }})
      .catch(e => {{
        st.textContent = '失败: 需启动 --serve 模式';
        st.className = 'status-msg error';
        btn.disabled = false;
        btn.style.opacity = '1';
      }});
  }}
</script>
</body></html>'''


# ═══════════════════════════════════════════════════════════════════
# 7. 主逻辑 + serve 模式
# ═══════════════════════════════════════════════════════════════════

def run_pipeline() -> list[dict]:
    print(f"[{datetime.now():%H:%M:%S}] 抓取体彩盘口...")
    matches = fetch_sporttery()
    print(f"  {len(matches)} 场在售")

    # 盘口变动追踪: 读取上次预测结果
    prev_predictions = {}
    pred_file = DATA_DIR / "predictions.json"
    if pred_file.exists():
        try:
            prev_data = json.loads(pred_file.read_text(encoding="utf-8"))
            for p in prev_data:
                key = f"{p['home']}vs{p['away']}_{p.get('date','')}"
                prev_predictions[key] = p
        except (json.JSONDecodeError, KeyError):
            pass

    predictions = []
    for m in matches:
        elo_h = get_elo(m["home"])
        elo_a = get_elo(m["away"])
        notes_parts = []
        if elo_h is None:
            elo_h = 1500.0
            notes_parts.append(f"{m['home']}Elo未知")
        if elo_a is None:
            elo_a = 1500.0
            notes_parts.append(f"{m['away']}Elo未知")
        adj_h, adj_a, adj_notes = get_adjustments(m["home"], m["away"])
        notes_parts.extend(adj_notes)

        # 盘口变动检测 (针对让球盘)
        match_key = f"{m['home']}vs{m['away']}_{m.get('date','')}"
        odds_movement = None
        if match_key in prev_predictions and m.get("hhad_odds"):
            prev_hhad = prev_predictions[match_key].get("hhad_odds")
            curr_hhad = m["hhad_odds"]
            if prev_hhad:
                diff_h = curr_hhad["h"] - prev_hhad["h"]
                if abs(diff_h) > 0.05:
                    direction = "↓" if diff_h < 0 else "↑"
                    reason = "资金看好主让胜" if diff_h < 0 else "资金看衰主让胜"
                    odds_movement = f"让球主胜赔 {prev_hhad['h']:.2f}→{curr_hhad['h']:.2f} {direction}({reason})"
                    notes_parts.append(odds_movement)

        # 传入中文队名用于历史加权进球率, 传入让球线, 传入英文队名用于战意
        handicap_line = m.get("hhad_line") or None
        pred = predict_match(elo_h, elo_a, adj_h, adj_a,
                             home_cn=m["home"], away_cn=m["away"],
                             handicap_line=handicap_line,
                             team_en_h=m.get("home_en"), team_en_a=m.get("away_en"))

        # ═══ 架构v2: 让球盘为主校准目标 ═══
        # 主输出: hhad_prob (让球盘去水概率)
        # 辅助: had_prob (常规盘去水概率,仅供参考)

        hhad_market = m.get("hhad_prob")  # 让球盘市场概率
        had_market = m.get("had_prob")    # 常规盘市场概率(辅助)
        ttg_market = m.get("ttg_prob")    # 总进球市场概率

        # 校准让球盘 (主输出)
        hhad_post = calibrate(pred["hc_prior"], hhad_market, is_handicap=True)

        # 校准常规盘 (辅助输出,仅供参考)
        had_post = calibrate(pred["prior"], had_market, is_handicap=False)

        # 校准总进球 (TTG) - 用通用 n-outcome 校准
        ttg_post = calibrate_n(pred["ttg"], ttg_market, w=0.5) if ttg_market else None

        # Edge 计算 (让球盘)
        hhad_edge = edge(hhad_post, hhad_market)
        had_edge = edge(had_post, had_market)

        # 如果没有让球盘市场,从常规盘推导(fallback)
        if not hhad_market and had_market and handicap_line:
            score_mat = score_matrix(pred["lam_h"], pred["lam_a"])
            hhad_market = hhad_to_had(had_market, handicap_line, score_mat)
            notes_parts.append(f"常规盘转让球盘({handicap_line}): 主让{hhad_market['h']*100:.0f}%")

        rec = {
            "home": m["home"], "away": m["away"],
            "date": m["date"], "time": m["time"],
            "league": m.get("league", ""), "num": m.get("num", ""),
            "elo_h": round(elo_h), "elo_a": round(elo_a), "elo_diff": elo_h - elo_a,
            "lam_h": pred["lam_h"], "lam_a": pred["lam_a"],
            # 战意系数 (实时积分/出线情况)
            "motivation_h": pred.get("motivation_h", 1.0),
            "motivation_a": pred.get("motivation_a", 1.0),
            "status_h": pred.get("status_h", "unknown"),
            "status_a": pred.get("status_a", "unknown"),

            # ═══ 主输出: 让球盘 ═══
            "handicap_line": handicap_line or "-1",
            "hc_prior": pred["hc_prior"],          # 让球盘模型先验
            "hhad_market": hhad_market,            # 让球盘市场概率
            "hhad_posterior": hhad_post,           # 让球盘校准后验(主要显示)
            "hhad_edge": hhad_edge,                # 让球盘edge信号

            # ═══ 辅助输出: 常规盘 (仅供参考) ═══
            "prior": pred["prior"],                # 常规胜平负先验
            "had_market": had_market,              # 常规盘市场概率
            "had_posterior": had_post,             # 常规盘后验(仅参考)
            "had_edge": had_edge,                  # 常规盘edge信号

            # ═══ 总进球 (TTG) ═══
            "ttg": pred["ttg"],                    # 模型总进球分布
            "ttg_market": ttg_market,              # 市场总进球概率
            "ttg_posterior": ttg_post,             # 校准后验

            # ═══ 多玩法赔率 (HAD/HHAD/TTG/HAFU/CRS) ═══
            "hhad_odds": m.get("hhad_odds"),
            "had_odds": m.get("had_odds"),
            "ttg_odds": m.get("ttg_odds"),
            "hafu_odds": m.get("hafu_odds"),
            "hafu_prob": m.get("hafu_prob"),
            "crs_odds": m.get("crs_odds"),

            "top_scores": pred["top_scores"],
            "odds_movement": odds_movement,
            "notes": "; ".join(notes_parts) if notes_parts else None,
        }
        # 体彩购买建议 (跨多玩法扫描)
        rec["recommendations"] = compute_recommendations(m, pred)
        predictions.append(rec)

    (DATA_DIR / "predictions.json").write_text(
        json.dumps(predictions, ensure_ascii=False, indent=2), encoding="utf-8")
    # 追加到历史预测日志(供 backtest.py 对比真实结果用)
    _append_prediction_log(predictions)
    html = render_html(predictions)
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"  ✅ {len(predictions)} 场预测 → site/index.html")
    return predictions


def _append_prediction_log(predictions: list[dict]):
    """把本次预测追加到 prediction_history.json,供 backtest 匹配真实结果。"""
    hist_file = DATA_DIR / "prediction_history.json"
    history = json.loads(hist_file.read_text(encoding="utf-8")) if hist_file.exists() else []
    existing_keys = {h.get("key") for h in history}
    ts = datetime.now().isoformat()
    for p in predictions:
        key = f"{p['date']}_{p['home']}_{p['away']}"
        if key in existing_keys:
            continue
        history.append({
            "key": key,
            "date": p["date"],
            "home": p["home"],
            "away": p["away"],
            # ═══ 主输出: 让球盘预测 ═══
            "handicap_line": p.get("handicap_line", "-1"),
            "hc_posterior": p["hhad_posterior"],  # 让球盘校准后验
            "hc_prior": p["hc_prior"],            # 让球盘模型先验
            "hhad_market": p.get("hhad_market"),  # 让球盘市场概率
            # ═══ 辅助: 常规盘预测(仅供参考) ═══
            "had_posterior": p["had_posterior"],  # 常规盘后验
            "had_prior": p["prior"],              # 常规盘先验
            "had_market": p.get("had_market"),    # 常规盘市场概率
            "elo_diff": p.get("elo_diff", 0),
            "predicted_at": ts,
        })
    hist_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


class ThreadedHTTPServer(HTTPServer):
    """Multi-threaded HTTP server to handle concurrent requests."""
    daemon_threads = True

    def process_request(self, request, client_address):
        import threading
        t = threading.Thread(target=self._handle_request, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle_request(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        finally:
            self.shutdown_request(request)


class RefreshHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/api/refresh":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            run_pipeline()
            self.wfile.write(b'{"ok":true}')
        elif self.path.startswith("/data/"):
            # Serve from wc_analysis/data directory
            rel = self.path[len("/data/"):]
            target = DATA_DIR / rel
            if target.is_file():
                self.send_response(200)
                if target.suffix == ".json":
                    self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(target.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error": "not found"}')
        elif self.path == "/api/top3":
            # Generate fresh top-3 predictions using TopPredictor (run in thread to avoid blocking)
            def _run_top3():
                try:
                    from generate_top3 import generate_top3_predictions
                    return generate_top3_predictions()
                except Exception as e:
                    return e
            import threading
            result = [None]
            def _worker():
                result[0] = _run_top3()
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=90)  # wait up to 90s
            if t.is_alive():
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"status":"running","msg":"top3 generation in progress, check /data/top3_predictions.json"}')
            elif isinstance(result[0], Exception):
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(result[0])}).encode("utf-8"))
            else:
                preds = result[0]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "n": len(preds)},
                                           ensure_ascii=False).encode("utf-8"))
        else:
            super().do_GET()

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        # POST also handled for /api/refresh
        if self.path == "/api/refresh":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            run_pipeline()
            self.wfile.write(b'{"ok":true}')
        elif self.path == "/api/retrain":
            # Trigger model weight retraining (step5_learn)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                from self_evolving_loop import step5_learn
                step5_learn()
                self.wfile.write(b'{"ok":true,"retrained":true}')
            except Exception as e:
                self.wfile.write(json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def _auto_refresh_loop(interval: int = 600):
    """后台定时刷新: 每 interval 秒重跑一次 pipeline, 保持页面数据新鲜.
    Also runs daily retrain (fusion weights + reconciliation)."""
    import datetime
    last_retrain_date = None
    while True:
        time.sleep(interval)
        try:
            run_pipeline()
            # 每日北京时间 14 点触发权重重训
            now = datetime.datetime.now()
            if now.hour == 14 and (last_retrain_date is None or last_retrain_date != now.date()):
                print(f"[{now:%Y-%m-%d %H:%M:%S}] 每日权重重训触发...")
                try:
                    from self_evolving_loop import step5_learn
                    step5_learn()
                    last_retrain_date = now.date()
                    print(f"  ✅ 权重重训完成")
                except Exception as e:
                    print(f"  ⚠ 权重重训失败: {e}")
        except Exception as e:
            print(f"  ⚠ 自动刷新失败: {e}")


def main():
    run_pipeline()
    if "--serve" in sys.argv:
        port = 8026
        print(f"\n🌐 http://localhost:{port}")
        print("   '重新抓取'按钮 = 实时刷新 | 后台每10分钟自动刷新 | Ctrl+C 停止")
        threading.Thread(target=_auto_refresh_loop, daemon=True).start()
        HTTPServer(("0.0.0.0", port), RefreshHandler)  # for type check
        ThreadedHTTPServer(("0.0.0.0", port), RefreshHandler).serve_forever()
    else:
        print(f"\n  打开: file://{(SITE_DIR / 'index.html').resolve()}")
        print("  加 --serve 启动本地服务(支持实时刷新按钮)")


if __name__ == "__main__":
    main()