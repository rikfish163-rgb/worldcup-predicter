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
import html, json, math, urllib.request, time, sys, os
from pathlib import Path
from datetime import datetime, timedelta
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
NEWS_FILE = DATA_DIR / "news_factors.json"
CORNERS_FILE = DATA_DIR / "corners.json"
SOFASCORE_FILE = DATA_DIR / "sofascore_features.json"

RHO = -0.20  # 交叉验证最优(2286场, 2020-2026); 会被params_override.json覆盖
AVG_GOALS = 2.50  # 交叉验证最优; 会被params_override.json覆盖
HOME_ADV = 0.40  # 世界杯中立场仍有~40Elo主场效应; 联赛可设更高; 会被params_override.json覆盖

# 自进化参数覆盖(evolve_groupstage.py 诊断后自动写入 params_override.json)
_PARAMS_OVERRIDE = DATA_DIR / "params_override.json"


def _reload_params_override() -> None:
    """重新读取 params_override.json 覆盖 RHO/AVG_GOALS/HOME_ADV。

    (审计修复2026-07-02: 此前这段逻辑只在模块import时执行一次, 写入全局变量后
    再无任何地方重新读取。--serve长驻进程一旦启动, RHO/AVG_GOALS/HOME_ADV就
    永久停留在启动那一刻读到的值——不管_auto_refresh_loop里evolve_groupstage
    之后又诊断出多少次新参数并写入磁盘, 只要进程不重启, 内存里用的值就与磁盘
    上的params_override.json静默漂移。现在把加载逻辑独立成函数, run_pipeline()
    每次运行前都会调用它, 保证每一轮预测用的都是当前磁盘上最新的参数。)
    """
    global RHO, AVG_GOALS, HOME_ADV
    if not _PARAMS_OVERRIDE.exists():
        return
    try:
        _po = json.loads(_PARAMS_OVERRIDE.read_text(encoding="utf-8"))
        RHO = _po.get("rho", RHO)
        AVG_GOALS = _po.get("avg_goals", AVG_GOALS)
        HOME_ADV = _po.get("home_adv", HOME_ADV)
    except (json.JSONDecodeError, OSError):
        pass  # 读取失败保留当前值, 不让坏文件打断启动/刷新


_reload_params_override()

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


def score_matrix(lam_h: float, lam_a: float, rho: float | None = None, n: int = 8) -> np.ndarray:
    # rho默认值不能写成"= RHO"(会在模块加载时把当时的RHO值固定绑死进函数签名,
    # 之后_reload_params_override()更新全局RHO对已绑定的默认值毫无作用)。
    # 两处调用方(predict_match/run_pipeline)都不显式传rho, 全靠这个默认值,
    # 改成None+运行时读取全局变量, 才能让热重载真正影响到这里。
    if rho is None:
        rho = RHO
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
_xg_cache_mtime = 0
def _load_xg_profiles() -> dict:
    """加载xG档案。
    (审计修复2026-07-02: 此前"读一次永久缓存", --serve长驻进程永远看不到
    xg_profiles.json后续更新, 与_load_motivation()已有的mtime校验模式不一致。
    改统一成同款: 检查文件mtime, 变化才重新读。)"""
    global _xg_cache, _xg_cache_mtime
    xg_file = DATA_DIR / "xg_profiles.json"
    if not xg_file.exists():
        _xg_cache = {}
        return _xg_cache
    mtime = xg_file.stat().st_mtime
    if _xg_cache is not None and mtime == _xg_cache_mtime:
        return _xg_cache
    try:
        _xg_cache = json.loads(xg_file.read_text(encoding="utf-8"))
        _xg_cache_mtime = mtime
    except (json.JSONDecodeError, OSError):
        _xg_cache = {}
    return _xg_cache


def _xg_factor(xg_profiles: dict, team_cn: str) -> float:
    """xG 进攻质量乘子(审计修复)。
    返回 1.0(中性, 不改变 λ)当: 无档案 / 无 attack_xg90 / players_found<3(样本不可靠,
    如刚果金1人、卡塔尔0人)。这样缺失/不可靠队与有效队走同一尺度路径, 消除原"缺失队跳过
    整块、有效队被cap钳到-4.5%"的非对称偏差。有效时按 attack_xg90/0.35 缩放并封顶[0.7,1.4]。
    """
    prof = xg_profiles.get(team_cn)
    if not prof:
        return 1.0
    axg = prof.get("attack_xg90")
    if not axg or prof.get("players_found", 0) < 3:
        return 1.0
    return max(0.7, min(1.4, axg / 0.35))  # 0.35=联赛平均npxG/90(硬编码基准, 来源待补)


# ─── SofaScore 防守质量特征 (def_xga90) ───
# 价值验证裁决: 在 40+ SofaScore 候选指标中, 唯一与现有信号正交且经实采验证可落盘的是
# 近N场场均"被预期进球"(xGA/90)。理由:
#   · Elo 是整体净实力, 不区分攻/防; Understat 档案只有进攻端 attack_xg90 → 防守维空缺。
#   · 真实 xGA 比"被进球数"更稳(去运气), 比体彩盘口更细颗粒(盘口不拆攻防)。
# 集成方式 = 镜像已验证的 _xg_factor: 作为对手进攻 λ 的乘子, 防守好(低xGA)→ 压低对手 λ。
# 数据诚实门控: matches_found < SOFA_MIN_MATCHES 即中性(1.0), 与缺数据队走同一尺度路径,
# 杜绝"有数据队被调、无数据队跳过"的非对称偏差(corners/xG 审计同款教训)。
SOFA_MIN_MATCHES = 5      # 少于5场样本不可靠 → 中性
# 基准 = 实采 46 支过门队 def_xga90 的中位数(2026-06-30 采集, n=46, median=1.10)。
# 用中位数而非拍脑袋值: 让因子在真实分布中心对称, 强防守队<1、弱防守队>1, 无系统性偏移。
SOFA_LEAGUE_XGA = 1.10
_sofa_cache = None
_sofa_cache_mtime = 0


def _load_sofascore() -> dict:
    """加载SofaScore防守特征, 同_load_xg_profiles()的mtime校验修复。"""
    global _sofa_cache, _sofa_cache_mtime
    if not SOFASCORE_FILE.exists():
        _sofa_cache = {}
        return _sofa_cache
    mtime = SOFASCORE_FILE.stat().st_mtime
    if _sofa_cache is not None and mtime == _sofa_cache_mtime:
        return _sofa_cache
    try:
        _sofa_cache = json.loads(SOFASCORE_FILE.read_text(encoding="utf-8"))
        _sofa_cache_mtime = mtime
    except (json.JSONDecodeError, OSError):
        _sofa_cache = {}
    return _sofa_cache


def _sofa_defense_factor(team_cn: str) -> float:
    """对手进攻 λ 的防守乘子。返回 1.0(中性) 当: 无数据 / matches_found<SOFA_MIN_MATCHES /
    无 def_xga90。有效时 = def_xga90 / 基准, 封顶 [0.80, 1.20](防小样本极值, ±20%上限)。
    >1 = 该队防守差(被xG高) → 放大对手进攻; <1 = 防守好 → 压低对手进攻。
    """
    sofa = _load_sofascore()
    prof = sofa.get(team_cn)
    if not isinstance(prof, dict):
        return 1.0
    if prof.get("matches_found", 0) < SOFA_MIN_MATCHES:
        return 1.0
    xga = prof.get("def_xga90")
    if not xga or xga <= 0:
        return 1.0
    return max(0.80, min(1.20, xga / SOFA_LEAGUE_XGA))


_draw_model = None
_draw_model_mtime = 0
def _predict_draw_prob(elo_h: float, elo_a: float, home_cn: str = None, away_cn: str = None) -> float | None:
    """用训练好的逻辑回归预测平局概率(v2: 8特征含风格+交锋)。
    同_load_xg_profiles()的mtime校验修复: 此前"读一次永久缓存", --serve长驻
    进程永远看不到draw_model.json后续被重新训练产出的新权重。"""
    global _draw_model, _draw_model_mtime
    dm_file = DATA_DIR / "draw_model.json"
    if not dm_file.exists():
        return None
    mtime = dm_file.stat().st_mtime
    if _draw_model is None or mtime != _draw_model_mtime:
        try:
            _draw_model = json.loads(dm_file.read_text(encoding="utf-8"))
            _draw_model_mtime = mtime
        except (json.JSONDecodeError, OSError):
            if _draw_model is None:
                return None  # 从未成功加载过, 没有旧缓存可回退
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


_style_cache = {}  # team_cn -> (tsv_mtime, result)
def _get_team_style(team_cn: str) -> dict | None:
    """(审计修复2026-07-02: 此前按team_cn缓存后永不失效, 但源头tsv文件
    (ELO_CACHE/{team}.tsv)会被get_elo()每24h重新抓取更新, --serve长驻进程
    缓存命中后就再也不会用新抓的比赛数据重算球队风格特征。改成对比tsv文件
    mtime, 变了才重新计算, 跟其余缓存(_xg_cache等)统一到同一套模式。)"""
    entry = TEAM_DB.get(team_cn)
    if not entry:
        return None
    fname, code = entry
    tsv = ELO_CACHE / f"{fname}.tsv"
    if not tsv.exists():
        return None
    mtime = tsv.stat().st_mtime
    cached = _style_cache.get(team_cn)
    if cached is not None and cached[0] == mtime:
        return cached[1]
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
    _style_cache[team_cn] = (mtime, result)
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


def _get_cohesion_factor(team_cn: str, knockout: bool = False) -> float:
    """综合磨合度因子: 自动量化 + 手动录入。返回 λ 乘数。

    (审计修复) 淘汰赛阶段(knockout=True)跳过 first_world_cup=true 的手动惩罚:
    这些队已踢满3场小组赛, "首次世界杯/集训不足/未磨合"前提已被证伪, 继续惩罚等于
    对刚证明前提错误的球队双重扣分。改为穿透到下方自动量化逻辑(随新赛果自适应)。
    """
    factor = 1.0

    # 手动定性因子(优先级最高,人工判断)
    if COHESION_FILE.exists():
        try:
            coh = json.loads(COHESION_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            coh = {}  # 文件损坏/并发写入截断时不让整条pipeline崩溃, 退化为跳过手动因子
        if team_cn in coh:
            entry = coh[team_cn]
            # 淘汰赛阶段忽略"首次世界杯"前提的磨合惩罚, 穿透到自动量化
            if not (knockout and entry.get("first_world_cup")):
                return entry.get("lambda_factor", 1.0)

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


# 小组赛专有战意标签 — 这些状态在淘汰赛(单场淘汰人人必拼)毫无意义,
# 一旦阶段进入淘汰赛必须忽略, 否则会用过时的小组赛系数污染 λ。
#
# (修复2026-07-02: 白名单曾只列4个标签(qualified_top2/fighting_3rd/eliminated/
#  near_qualified), 但 standings.json 里实际出现过 fighting_top2/must_win_3rd/
#  fighting_3rd_top8 三个未被列入的标签(经查是历史上手动/脚本写入, standings.py
#  当前版本反而不产出这几个) —— 白名单漏了它们, 一旦_is_knockout_stage()因数据
#  不同步误判False, 这三个漏网标签会穿透到面板显示, 如"fighting_top2 1.05"。
#  这是双重防御的第二层(第一层是_is_knockout_stage硬判定), 必须覆盖 standings.json
#  历史上出现过的全部小组赛状态, 不能只跟着 standings.py 当前代码的4个硬编码值。)
_GROUP_STAGE_STATUSES = {
    "qualified_top2", "fighting_3rd", "eliminated", "near_qualified",
    "fighting_top2", "must_win_3rd", "fighting_3rd_top8",
}


def _is_knockout_stage() -> bool:
    """判定当前是否已进入淘汰赛阶段(硬保护)。
    依据: (1) standings.json 显式写了 stage; (2) wc_results 完成场次 >= 72(48队×3÷2,
    小组赛全部打完); (3) 当前 predictions.json 已生成淘汰赛对阵。任一成立即判定 knockout。
    淘汰赛阶段下小组赛战意逻辑(出线/已淘汰/已晋级轮换)全部失效。"""
    # (1) standings.json 显式 stage
    f = DATA_DIR / "standings.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if str(d.get("stage", "")).lower() == "knockout":
                return True
        except Exception:
            pass
    # (2) 小组赛 72 场全部完成
    rf = DATA_DIR / "wc_results.json"
    if rf.exists():
        try:
            r = json.loads(rf.read_text(encoding="utf-8"))
            results = r.get("results", r) if isinstance(r, dict) else r
            if isinstance(results, list) and len(results) >= 72:
                return True
        except Exception:
            pass
    return False


def _injuries_is_fresh() -> bool:
    """injuries.json 是否仍然时效新鲜(审计修复)。
    手动 injuries.json 无 as_of 字段, 以文件 mtime 对比 standings.json(随赛果更新)的
    mtime: 若伤情文件早于最新赛果同步, 视为过时停用, 避免旧伤情污染 λ。"""
    inj = INJURIES_FILE
    st = DATA_DIR / "standings.json"
    if not inj.exists():
        return False
    if not st.exists():
        return True  # 无参照, 保守保留
    try:
        return inj.stat().st_mtime >= st.stat().st_mtime
    except OSError:
        return True


_motivation_cache = None
_motivation_mtime = 0
def _load_motivation() -> dict:
    """加载战意系数 (实时积分/出线情况 → 期望进球调整系数)。
    每次检查文件 mtime, 如果 standings.json 更新了就重新加载。"""
    global _motivation_cache, _motivation_mtime
    f = DATA_DIR / "standings.json"
    if not f.exists():
        _motivation_cache = {}
        return _motivation_cache
    mtime = f.stat().st_mtime
    if _motivation_cache is not None and mtime == _motivation_mtime:
        return _motivation_cache  # 缓存有效
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        _motivation_cache = d.get("motivation", {})
        _motivation_mtime = mtime
    except Exception:
        _motivation_cache = {}
    return _motivation_cache


def _get_team_motivation(team_en: str) -> tuple[float, str]:
    """根据球队英文名取战意系数和状态。返回 (motivation_factor, status_label)。

    (审计修复) 淘汰赛阶段硬保护: 小组赛排名战意(出线/已淘汰/已晋级轮换)在单场淘汰
    赛制下无意义, 且 standings.json 仍挂着过时的小组赛状态。淘汰赛阶段一律返回 1.0,
    彻底关闭小组赛战意逻辑, 杜绝残留数据污染 λ。
    """
    if not team_en:
        return 1.0, "unknown"
    if _is_knockout_stage():
        return 1.0, "knockout"
    m = _load_motivation()
    info = m.get(team_en)
    if not info:
        return 1.0, "unknown"
    status = info.get("status", "fighting")
    # 防御: 即便 key 命中, 若是小组赛专有标签也强制中性(双保险, 阶段判定之外再兜底)
    if status in _GROUP_STAGE_STATUSES:
        return 1.0, "knockout"
    factor = float(info.get("motivation", 1.0))
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
            # (审计修复 BUG-1) Elo/历史权重归一: 原 0.60+0.25=0.85 会静默把 λ 缩小15%,
            # 且 xG 块乘的是已缩水值无法补回。除以 0.85 复原尺度, 权重和=1.0。
            lam_h = (0.60 * lam_h + 0.25 * hist_lam_h) / 0.85
            # xG 档案进攻质量微调
            # (审计修复 BUG-2 + 门控) 仅当样本足够(players_found>=3)且有 attack_xg90 时启用;
            # 否则中性(xg_factor=1.0, 不改变 λ), 使缺失队与有效队走同一尺度, 消除非对称偏差。
            xg_factor = _xg_factor(xg_profiles, home_cn)
            lam_h = 0.85 * lam_h + 0.15 * (lam_h * xg_factor)
    if away_cn:
        wgr_a = weighted_goals_rate(away_cn)
        if wgr_a is not None:
            hist_lam_a = wgr_a[0]
            lam_a = (0.60 * lam_a + 0.25 * hist_lam_a) / 0.85
            xg_factor = _xg_factor(xg_profiles, away_cn)
            lam_a = 0.85 * lam_a + 0.15 * (lam_a * xg_factor)

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

    # 总进球: 0..n-2球各自"恰好N球", 最后一档(n-1, 通常是"7")是市场约定的
    # "N+球"开口档, 而非"恰好N球"。
    # (审计修复2026-07-02: 此前最后一档只算"恰好7球", 且score_matrix用n=8
    # 截断(每队最多算到7球), i+j>=8的所有组合(如4-4/4-5等)被完全丢弃, 既没
    # 算进模型自己的分布也没暴露出来, 导致9场生产预测的ttg字典求和实测都<1
    # (缺失0.46%~2.0%), 不是合法概率分布; 且市场盘口的这一档本身就是"N球或
    # 以上"开口档, 用"恰好N球"的口径去跟市场devig概率算edge, 会系统性压低
    # 这一档的edge(exact-N的概率天然小于N+的概率)。改为把所有i+j>=n-1的格子
    # 折进最后一档, 使模型分布严格归一且与市场同口径。)
    ttg = {}
    for g in range(n - 1):
        ttg[str(g)] = float(sum(mat[i, g-i] for i in range(n) if 0 <= g-i < n))
    ttg[str(n - 1)] = float(sum(mat[i, j] for i in range(n) for j in range(n) if i + j >= n - 1))
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
    # 仅对 corners.json 中有真实定位球数据的队伍加成。
    # (审计修复) 删除原 Elo fallback: 用整体实力Elo冒充定位球能力是编造特征
    # (强队≠定位球强队, 二者无统计关联), 且会给90%无数据队伍最高15%无依据加成。
    # 无 corners.json 数据 → 返回 0.0 (不加成)。
    if CORNERS_FILE.exists():
        try:
            corners_data = json.loads(CORNERS_FILE.read_text(encoding="utf-8"))
            if team in corners_data:
                entry = corners_data[team]
                cpg = entry.get("corners_per_game", 0)  # 场均角球数
                ctg = entry.get("corners_to_goals", 0)  # 角球转化率(口径存疑, 见 corners.json)

                # 综合评分: 场均角球数反映控制力,转化率反映效率。
                # 归一化基准用固定值(cpg/8, ctg/0.15)而非写死天花板队,
                # 避免数据集中最强的队被人为顶到 min 上限(原 cpg/7、ctg/0.12 让德国撞顶)。
                score = (min(cpg / 8.0, 1.0) * 0.5 + min(ctg / 0.15, 1.0) * 0.5) * 0.15
                return round(score, 3)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    return 0.0


def get_news_notices(home_cn: str, away_cn: str, match_date: str) -> list[dict]:
    """
    只读场外因素新闻(不触发网络抓取, 抓取由独立的 fetch_news.py 定时任务完成).

    不做概率量化, 仅作为面板 notice 提醒, 让人自行判断影响。
    """
    if not NEWS_FILE.exists():
        return []
    try:
        news = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    match_key = f"{match_date}_{home_cn}_{away_cn}"
    block = news.get(match_key)
    if not block:
        return []
    out = []
    for side, side_cn in (("home", home_cn), ("away", away_cn)):
        for item in block.get(side, []):
            out.append({**item, "team": side_cn})
    return out


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

    # SofaScore 防守质量 (def_xga90) — 对手防守好/差 → 压低/放大本队进攻 λ。
    # 价值验证唯一裁定可集成的正交特征(Elo不分攻防、Understat仅进攻端)。
    # 跨向乘子: away 的防守因子作用于 home 的 adj_h, 反之亦然。
    # 数据诚实: 任一方无数据/样本<5 → 因子=1.0 中性, 不惩罚不加成。
    sofa_def_h = _sofa_defense_factor(home)  # home 防守 → 影响 away 进攻
    sofa_def_a = _sofa_defense_factor(away)  # away 防守 → 影响 home 进攻
    if abs(sofa_def_a - 1.0) > 0.01:
        adj_h *= sofa_def_a
        verb = "差" if sofa_def_a > 1.0 else "强"
        notes.append(f"{away}近况防守{verb}[实采xGA](影响{home}进攻 λ×{sofa_def_a:.2f})")
    if abs(sofa_def_h - 1.0) > 0.01:
        adj_a *= sofa_def_h
        verb = "差" if sofa_def_h > 1.0 else "强"
        notes.append(f"{home}近况防守{verb}[实采xGA](影响{away}进攻 λ×{sofa_def_h:.2f})")

    # 风格相克调整 — (审计修复) 已禁用。
    # 该特征无任何真实战术数据: _get_team_style 仅用近25场比分结果反推"进攻型/防守型",
    # 混入友谊赛/各洲预选, 指标反映赛程强度而非风格; 分类逻辑自相矛盾(强攻击队被判为防守队);
    # 0.88 系数与 1.8/2.8/0.65 阈值均为无回测依据的拍脑袋值。带来噪声而非信号, 故移除。
    # 如需恢复: 必须先用 backtest_v2 消融实验标定系数, 并改用真实 xG/控球/压迫数据。

    # 磨合度/经验因子 (自动量化 + 手动定性)
    # (审计修复) 淘汰赛阶段跳过"首次世界杯未磨合"等小组赛前提的手动惩罚:
    # 被罚队已踢满3场小组赛, "未磨合"前提被证伪; 让程序穿透到自动量化逻辑。
    knockout = _is_knockout_stage()
    for team, adj_key in [(home, "adj_h"), (away, "adj_a")]:
        cohesion_factor = _get_cohesion_factor(team, knockout=knockout)
        if cohesion_factor != 1.0:
            if adj_key == "adj_h":
                adj_h *= cohesion_factor
            else:
                adj_a *= cohesion_factor
            if cohesion_factor < 0.95:
                notes.append(f"{team}磨合度低(λ×{cohesion_factor:.2f})")

    # 伤病 (从 injuries.json 读取)
    # (审计修复) 时效门控: injuries.json 为手动维护且无 as_of 字段。若其 mtime 早于
    # standings.json(最新赛果同步时间), 视为过时数据并停用 — 防止小组赛结束前的旧伤情
    # (如"德容存疑"但其实已康复首发)继续以最高优先级污染 λ。
    if INJURIES_FILE.exists() and _injuries_is_fresh():
        try:
            inj = json.loads(INJURIES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            inj = {}
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
    # (审计修复2026-07-02: 此前这里裸读取, 全文件唯一没有try/except保护的数据源
    # ——corners.json/cohesion.json/sofascore都已有保护。get_adjustments被run_pipeline
    # 对每场比赛调用一次, 且调用链上无外层try/except, weather.json一旦被并发写入
    # 截断或写入非法JSON, 会直接抛出未捕获异常中止整条run_pipeline, predictions.json
    # 不再更新, 而/api/refresh在此崩溃前已发送200, 客户端会误判"刷新成功"。)
    weather_file = DATA_DIR / "weather.json"
    if weather_file.exists():
        try:
            w = json.loads(weather_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            w = {}
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


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """原子写: 先写临时文件再os.replace()替换, 避免读者读到写入中途的半成品。

    (审计修复2026-07-02: predictions.json/prediction_history.json/index.html
    此前都是裸write_text(先truncate再写入新内容), 对于几万行的大json文件写入
    有一定耗时。_pipeline_lock已经解决了"两个run_pipeline()互相踩写"的竞态,
    但读者(如/data/predictions.json的HTTP请求, 或本地另一进程直接读文件)在
    这个写入窗口期读, 仍可能读到被截断的不完整内容——这是读写竞态, 锁解决
    不了(锁只保护写者之间, 不会让读者等待)。os.replace()在同一文件系统内
    是原子操作, 读者只会看到"旧完整版本"或"新完整版本", 不会看到中间状态。)
    """
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════════
# 6. HTML 生成 (带刷新按钮 + 自动重载)
# ═══════════════════════════════════════════════════════════════════

def _pct3(*probs: float) -> list[int]:
    """
    把 n 个概率(和应为1.0)转成整数百分比, 保证总和恒等于100。

    (修复2026-07-02: 用户报告"胜平负加起来概率大于1"。根因不是概率计算错误
    ——排查确认 hc_prior/hhad_posterior/prior/had_posterior 等底层字段sum全部
    精确=1.0000, 是渲染层用 {x:.0%} 对h/d/a各自独立四舍五入导致的: 例如
    0.7852/0.1578/0.0570 独立round成79/16/6, 相加=101%。原始数字都对,
    只是分别取整时产生的误差没有互相抵消。

    用最大余数法(Largest Remainder Method)修复: 先都向下取整, 算出总共
    "亏欠"的百分点数, 按小数部分从大到小补给对应的项, 数学上保证 sum(结果)
    恒等于100, 且每一项的调整幅度不超过1个百分点(视觉上几乎不可察觉)。
    """
    n = len(probs)
    scaled = [p * 100 for p in probs]
    floors = [int(s) for s in scaled]
    remainder = 100 - sum(floors)
    # 按小数部分从大到小排序, 把亏欠的百分点分给最"该四舍五入向上"的项
    order = sorted(range(n), key=lambda i: scaled[i] - floors[i], reverse=True)
    result = floors[:]
    for i in range(remainder):
        result[order[i % n]] += 1
    return result


def _mot_color(motivation: float) -> str:
    """战意系数 → 颜色: 1.0=正常, <0.95=灰(轮换/淘汰), >1.05=绿(必拼)"""
    if motivation >= 1.05:
        return "#4ade80"  # green
    if motivation <= 0.90:
        return "#8a8178"  # gray (rotation / eliminated)
    return "#c4bcb2"  # normal text


_TREND_W, _TREND_H = 560, 96
_TREND_PAD_L, _TREND_PAD_R = 4, 4


def _render_trend_chart(points: list[dict]) -> str:
    """
    生成近12h概率趋势的可拖动SVG图。

    x轴固定跨度12小时(不是"最早点到最晚点"), 这样比赛刚上架只有1-2个点时
    图表右侧留白, 不会把稀疏数据拉伸成误导性的满幅曲线; 数据点随时间推进
    自然从右侧长出来。

    拖动交互由JS完成(见render_html的<script>): 鼠标/触摸移动时找最近的点,
    移动竖线光标+更新读数框, 不重新请求数据(纯前端插值, 零成本)。
    """
    import json as _json
    now = datetime.now()
    window_start = now - timedelta(hours=12)

    def x_of(t_iso: str) -> float:
        t = datetime.fromisoformat(t_iso)
        frac = (t - window_start).total_seconds() / (12 * 3600)
        frac = max(0.0, min(1.0, frac))
        return _TREND_PAD_L + frac * (_TREND_W - _TREND_PAD_L - _TREND_PAD_R)

    # Y轴自适应缩放: 让球盘融合权重70%给市场, 市场半天不开新盘时概率常常
    # 只微幅漂移(<2%), 若Y轴固定按0-100%整幅画, 0.5%的变化只占96px高的
    # 不到1px, 肉眼看就是一条死直线 —— 这才是"全是水平线"的真实原因,
    # 不是没记录到变化, 是变化被画图的固定量程压没了。
    # 修复: 按当前窗口内 h/d/a 三条线的真实min/max定Y轴范围, 并强制保留
    # 至少 MIN_SPAN 的可视幅度(避免真正持平时反而因除零/过度放大出现抖动假象)。
    all_vals = [pt[k] for pt in points for k in ("h", "d", "a")]
    v_min, v_max = min(all_vals), max(all_vals)
    MIN_SPAN = 0.06  # 至少按6个百分点的幅度画, 小于这个视觉上已算"持平"
    span = max(v_max - v_min, MIN_SPAN)
    mid = (v_max + v_min) / 2
    y_lo = max(0.0, mid - span / 2)
    y_hi = min(1.0, y_lo + span)
    if y_hi - y_lo < span:  # 撞到0或1的边界, 反向补回来
        y_lo = max(0.0, y_hi - span)

    def y_of(p: float) -> float:
        frac = (p - y_lo) / (y_hi - y_lo) if y_hi > y_lo else 0.5
        frac = max(0.0, min(1.0, frac))
        return _TREND_H - 10 - frac * (_TREND_H - 20)  # 10px上下留白(给范围标注留空间)

    series = {"h": [], "d": [], "a": []}
    for pt in points:
        x = x_of(pt["t"])
        for k in ("h", "d", "a"):
            series[k].append((x, y_of(pt[k])))

    # 卡通配色: 高饱和荧光色, 在深色背景上要"跳出来"而不是融进去
    colors = {"h": "#3ddc84", "d": "#ffd23d", "a": "#4fc3ff"}
    glow_ids = {"h": "glowH", "d": "glowD", "a": "glowA"}

    def _pts_str(k):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in series[k])

    # 面积填充(渐变到透明), 让曲线不再是"细线飘在黑洞里", 而是有体积感的色带
    def _area_path(k):
        pts = series[k]
        top = " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        return (f"M {pts[0][0]:.1f},{_TREND_H} L {top} "
                f"L {pts[-1][0]:.1f},{_TREND_H} Z")

    areas = "".join(
        f'<path d="{_area_path(k)}" fill="url(#fill{k.upper()})" class="trend-area trend-area-{k}"/>'
        for k in ("h", "d", "a")
    )
    # 曲线本体: 加发光滤镜+粗描边, 卡通描边质感(先画深色描边垫底,再叠亮色主线)
    outlines = "".join(
        f'<polyline points="{_pts_str(k)}" fill="none" stroke="#1a1512" '
        f'stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.5"/>'
        for k in ("h", "d", "a")
    )
    polylines = "".join(
        f'<polyline points="{_pts_str(k)}" fill="none" stroke="{colors[k]}" '
        f'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" '
        f'filter="url(#{glow_ids[k]})" class="trend-line trend-{k}"/>'
        for k in ("h", "d", "a")
    )
    # 数据点圆点: 白心+彩色描边的"糖豆"造型, 最后一点加呼吸动画表示"实时"
    def _dot(k, cx, cy, live=False):
        cls = "trend-dot trend-dot-live" if live else "trend-dot"
        r = 6 if live else 4.5
        return (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{colors[k]}" '
                f'stroke="#fff" stroke-width="1.6" class="{cls}"/>')
    all_dots = "".join(
        _dot(k, *series[k][-1], live=True) for k in ("h", "d", "a")
    )
    points_json = _json.dumps([
        {"t": pt["t"], "h": pt["h"], "d": pt["d"], "a": pt["a"], "x": round(x_of(pt["t"]), 1)}
        for pt in points
    ], ensure_ascii=False)

    defs = "".join(
        f'''<linearGradient id="fill{k.upper()}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="{colors[k]}" stop-opacity="0.45"/>
          <stop offset="100%" stop-color="{colors[k]}" stop-opacity="0"/>
        </linearGradient>
        <filter id="{glow_ids[k]}" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.2" result="blur"/>
          <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>'''
        for k in ("h", "d", "a")
    )

    # Y轴范围提示: 缩放后曲线看起来波动很大, 必须标注真实量程, 否则会
    # 把"其实只变了0.5%"误读成"剧烈波动"——这跟修复"看不出变化"同等重要。
    zoom_note = (f"纵轴 {y_lo*100:.0f}%~{y_hi*100:.0f}%" if span > MIN_SPAN + 1e-6
                 else "纵轴 0%~100%(变化<6pt, 已按最小量程显示)")

    return f'''<div class="trend-wrap">
    <div class="trend-hdr"><span>✨ 近12h让球盘概率走势</span><span class="trend-hint">👆 拖动查看历史</span></div>
    <svg class="trend-svg" viewBox="0 0 {_TREND_W} {_TREND_H}" preserveAspectRatio="none"
         data-points='{points_json}'>
      <defs>{defs}</defs>
      {areas}
      {outlines}
      {polylines}
      {all_dots}
      <line class="trend-cursor" x1="0" y1="0" x2="0" y2="{_TREND_H}" style="display:none"/>
      <circle class="trend-cursor-dot" r="4" style="display:none"/>
      <text x="{_TREND_W - 6}" y="12" text-anchor="end" class="trend-zoom-note">{zoom_note}</text>
    </svg>
    <div class="trend-readout"></div>
  </div>'''


def render_html(predictions: list[dict]) -> str:
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        from odds_trend import get_trend
    except Exception:
        get_trend = lambda *a, **k: []
    # 记分牌数据: 当前进化参数 + 最近回测命中率 + 版本号(predictions.json mtime, 供前端轮询)
    _pf = DATA_DIR / "predictions.json"
    page_version = int(_pf.stat().st_mtime) if _pf.exists() else 0
    _hit_pct = ""
    if _PARAMS_OVERRIDE.exists():
        try:
            _ov = __import__("json").loads(_PARAMS_OVERRIDE.read_text(encoding="utf-8"))
            if _ov.get("hit_rate") is not None:
                _hit_pct = f"{_ov['hit_rate']*100:.0f}%"
        except Exception:
            pass
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
    for _card_i, p in enumerate(predictions):
        # ═══ 主显示: 让球盘后验概率 ═══
        ph = p["hhad_posterior"]["h"]
        pd_ = p["hhad_posterior"]["d"]
        pa = p["hhad_posterior"]["a"]
        handicap_line = p.get("handicap_line", "-1")
        # 概率条比例(百分比宽度, 用原始浮点数, 不受下面整数归一影响)
        bar_h = f"{ph*100:.1f}"
        bar_d = f"{pd_*100:.1f}"
        bar_a = f"{pa*100:.1f}"
        # 整数百分比文字显示: 用最大余数法保证三者相加恒=100(见_pct3注释)
        pct_h, pct_d, pct_a = _pct3(ph, pd_, pa)
        hc_prior_pct = _pct3(p["hc_prior"]["h"], p["hc_prior"]["d"], p["hc_prior"]["a"])

        # 近12h概率趋势图: 取历史采样点 + 追加当前值(确保曲线延伸到"现在"),
        # 少于2个点(比赛刚上架, 还没积累趋势)则不渲染图表。
        _trend_pts = get_trend(p["home"], p["away"], p.get("date", ""))
        _now_pt = {"t": datetime.now().isoformat(timespec="seconds"),
                   "h": round(ph, 4), "d": round(pd_, 4), "a": round(pa, 4)}
        if not _trend_pts or _trend_pts[-1]["t"] != _now_pt["t"]:
            _trend_pts = _trend_pts + [_now_pt]
        if len(_trend_pts) >= 2:
            trend_html = _render_trend_chart(_trend_pts)
        else:
            trend_html = '<div class="trend-empty">趋势积累中(每次刷新记一个点, 12小时后可看变化曲线)</div>'

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
            m_pct = _pct3(m.get("h", 0), m.get("d", 0), m.get("a", 0))
            mkt_row = f'''<tr><td class="row-label">市场</td>
            <td class="num">{m_pct[0]}%</td><td class="num">{m_pct[1]}%</td><td class="num">{m_pct[2]}%</td></tr>'''

        # 常规盘胜率 (小字显示,仅供参考)
        had_ref = ""
        if p.get("had_posterior"):
            hp = p["had_posterior"]
            _hr_pct = _pct3(hp["h"], hp["d"], hp["a"])
            had_ref = f'<div class="had-ref">常规盘参考: 主{_hr_pct[0]}% / 平{_hr_pct[1]}% / 客{_hr_pct[2]}%</div>'

        # 热门比分
        scores = p.get("top_scores", [])[:5]
        scores_chips = "".join(f'<span class="score-chip"><b>{s}</b> {prob:.0%}</span>' for s, prob in scores)

        # 注释
        notes_html = ""
        if p.get("notes"):
            notes_html = f'<div class="insight">{p["notes"]}</div>'
        if p.get("odds_movement"):
            notes_html += f'<div class="insight movement">{p["odds_movement"]}</div>'

        # 场外因素新闻 notice(不量化概率, 仅提醒人工判断)
        # (审计修复2026-07-02: title/source/link来自Google News RSS抓取的外部内容,
        # 此前直接拼接进HTML, 未做html.escape——link还被直接用在href=属性里, 存在
        # 潜在XSS风险面(纵深防御: 即便Google News本身可信, 抓取链路上任何环节被
        # 污染都会直接注入到面板)。全部字段渲染前统一转义。)
        news_html = ""
        for n in p.get("news_notices", []) or []:
            title = html.escape(n.get("title", ""))
            src = html.escape(n.get("source", ""))
            link = html.escape(n.get("link", "#"), quote=True)
            label = html.escape(n.get("label", ""))
            team = html.escape(n.get("team", ""))
            news_html += (
                f'<div class="news-notice">'
                f'<span class="news-tag">{label}</span>'
                f'<span class="news-team">{team}</span>'
                f'<a href="{link}" target="_blank" rel="noopener" class="news-title">{title}</a>'
                f'<span class="news-src">{src}</span>'
                f'</div>'
            )

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
            prior_pct = _pct3(p["prior"]["h"], p["prior"]["d"], p["prior"]["a"])
            hm_pct = _pct3(hm.get("h", 0), hm.get("d", 0), hm.get("a", 0))
            hp_pct = _pct3(hp["h"], hp["d"], hp["a"])
            had_section = f'''
    <div class="market-section">
      <div class="market-title">胜平负 (HAD)</div>
      <table>
        <thead><tr><th></th><th>主胜</th><th>平局</th><th>客胜</th></tr></thead>
        <tbody>
          <tr><td class="row-label">模型</td>
          <td class="num">{prior_pct[0]}%</td><td class="num">{prior_pct[1]}%</td><td class="num">{prior_pct[2]}%</td></tr>
          <tr><td class="row-label">市场</td>
          <td class="num">{hm_pct[0]}%</td><td class="num">{hm_pct[1]}%</td><td class="num">{hm_pct[2]}%</td></tr>
          <tr class="posterior-row"><td class="row-label">后验</td>
          <td class="num"><b>{hp_pct[0]}%</b></td><td class="num"><b>{hp_pct[1]}%</b></td><td class="num"><b>{hp_pct[2]}%</b></td></tr>
          <tr><td class="row-label">赔率</td>
          <td class="num">{ho["h"]:.2f}</td><td class="num">{ho["d"]:.2f}</td><td class="num">{ho["a"]:.2f}</td></tr>
        </tbody>
      </table>
    </div>'''

        # 2. 总进球 (TTG) - 显示模型概率 vs 市场概率
        # (审计修复2026-07-02: 此前只检查ttg_odds/ttg是否存在, 没检查ttg_market/
        # ttg_posterior——市场没开对应去水概率时这两者是空字典/None, ttg_mkt.get(...,0)
        # 全部返回0, 渲染出一排"市场0% 0% 0% 0% 0%"的表格, 看起来像"模型认为这些
        # 都不可能"而不是"这项数据缺失"(9场生产预测实测全部踩中, 因为市场概率来自
        # devig计算, 某些盘口没开全)。分别判断市场/后验是否真的有数据, 没有就显示
        # "暂无市场数据"文案而不是伪造的0%; 同时5档(0/1/2/3/4+)统一用_pct3()最大
        # 余数法保证显示总和=100%, 之前这里跟修复前的胜平负一样是各自独立.0%取整。)
        ttg_section = ""
        if p.get("ttg_odds") and p.get("ttg"):
            ttg_o = p["ttg_odds"]
            ttg_mkt = p.get("ttg_market") or {}
            ttg_model = p["ttg"]
            ttg_post = p.get("ttg_posterior") or {}
            has_market = bool(ttg_mkt)
            has_post = bool(ttg_post)

            def _bucket5(d: dict) -> list[float]:
                """把0/1/2/3/4+这5档从ttg字典里取出(4+ = 4..7档相加)。"""
                b = [d.get(str(g), 0.0) for g in range(4)]
                b.append(sum(d.get(str(g), 0.0) for g in range(4, 8)))
                return b

            model_vals = _bucket5(ttg_model)
            model_pct = _pct3(*model_vals)
            if has_market:
                market_vals = _bucket5(ttg_mkt)
                market_pct = _pct3(*market_vals)
                best_g = max(ttg_mkt.keys(), key=lambda k: ttg_mkt.get(k, 0))
                best_market_p = ttg_mkt.get(best_g, 0)
                title_suffix = f" · 市场最可能: <b>{best_g}球</b> ({best_market_p:.0%})"
                market_row = "".join(f'<td class="num">{x}%</td>' for x in market_pct)
            else:
                title_suffix = " · 市场未开盘"
                market_row = '<td class="num" colspan="5">暂无市场数据</td>'
            if has_post:
                post_vals = _bucket5(ttg_post)
                post_pct = _pct3(*post_vals)
                post_row = "".join(f'<td class="num"><b>{x}%</b></td>' for x in post_pct)
            else:
                post_row = '<td class="num" colspan="5">暂无市场数据, 后验退化为模型值(见上)</td>'
            ttg_cells_model = "".join(f'<td class="num">{x}%</td>' for x in model_pct)
            ttg_cells_odds = "".join(
                f'<td class="num">{ttg_o.get(str(g), 0):.2f}</td>'
                for g in range(4))
            o4plus_inv = sum(1.0/ttg_o.get(str(g), 999) for g in range(4, 8) if ttg_o.get(str(g)))
            o4plus = 1.0 / o4plus_inv if o4plus_inv > 0 else 0
            ttg_section = f'''
    <div class="market-section">
      <div class="market-title">总进球 (TTG){title_suffix}</div>
      <table>
        <thead><tr><th></th><th>0球</th><th>1球</th><th>2球</th><th>3球</th><th>4+球</th></tr></thead>
        <tbody>
          <tr><td class="row-label">模型</td>
          {ttg_cells_model}</tr>
          <tr><td class="row-label">市场</td>
          {market_row}</tr>
          <tr class="posterior-row"><td class="row-label">后验</td>
          {post_row}</tr>
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
        card = f'''<article class="match" style="--i:{_card_i}">
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
      <div class="bar bar-h" style="--w:{bar_h}%"><span>{pct_h}%</span></div>
      <div class="bar bar-d" style="--w:{bar_d}%"><span>{pct_d}%</span></div>
      <div class="bar bar-a" style="--w:{bar_a}%"><span>{pct_a}%</span></div>
    </div>
    <div class="bar-labels"><span>主让胜</span><span>平局</span><span>客让胜</span></div>
  </div>
  {trend_html}

  <div class="data-grid">
    <div class="market-section primary">
      <div class="market-title">让球盘 (HHAD) {hc_line_display}</div>
      <table>
        <thead><tr><th></th><th>主让胜</th><th>平局</th><th>客让胜</th></tr></thead>
        <tbody>
          <tr><td class="row-label">模型</td>
          <td class="num">{hc_prior_pct[0]}%</td><td class="num">{hc_prior_pct[1]}%</td><td class="num">{hc_prior_pct[2]}%</td></tr>
          {mkt_row}
          <tr class="posterior-row"><td class="row-label">后验</td>
          <td class="num"><b>{pct_h}%</b></td><td class="num"><b>{pct_d}%</b></td><td class="num"><b>{pct_a}%</b></td></tr>
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
  {news_html}
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
  padding: 28px 36px 60px;
  max-width: 1680px;
  margin: 0 auto;
  position: relative;
  z-index: 1;
}}
@keyframes cardIn {{ from {{ opacity:0; transform:translateY(18px) }} to {{ opacity:1; transform:none }} }}
@keyframes barGrow {{ from {{ width:0 }} to {{ width:var(--w) }} }}
@keyframes dotPulse {{ 0%,100% {{ opacity:1; box-shadow:0 0 0 0 var(--green) }} 50% {{ opacity:.55; box-shadow:0 0 0 5px transparent }} }}
@keyframes toastIn {{ from {{ opacity:0; transform:translate(-50%,-16px) }} to {{ opacity:1; transform:translate(-50%,0) }} }}
.match-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 20px;
  align-items: start;
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
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 28px;
  padding: 18px 22px;
  background: var(--surface);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--border);
  border-radius: 14px;
}}
header.page-header .brand {{
  display: flex;
  align-items: baseline;
  gap: 12px;
}}
header.page-header h1 {{
  font-size: 1.35em;
  font-weight: 700;
  letter-spacing: -0.03em;
}}
header.page-header .tagline {{
  font-size: .72em;
  color: var(--text-3);
}}
.scoreboard {{
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}}
.sb-badge {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--mono);
  font-size: .72em;
  font-weight: 500;
  padding: 5px 11px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg);
  color: var(--text-2);
  white-space: nowrap;
}}
.sb-badge.live {{ color: var(--green); border-color: var(--green); }}
.sb-badge.live .dot {{
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--green);
  animation: dotPulse 1.6s ease-in-out infinite;
}}
.sb-badge.evo {{ color: var(--accent); border-color: var(--accent); }}
.sb-badge.hit {{ color: var(--green); }}
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
  border-radius: 14px;
  padding: 24px;
  transition: transform .25s cubic-bezier(.4,0,.2,1), box-shadow .25s, border-color .2s;
  animation: cardIn .5s ease both;
  animation-delay: calc(var(--i, 0) * 0.06s);
}}
.match:hover {{
  transform: translateY(-4px);
  box-shadow: 0 12px 36px rgba(0,0,0,.45);
  border-color: var(--accent);
}}

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
  font-weight: 600;
  width: var(--w);
  min-width: 32px;
  animation: barGrow .85s cubic-bezier(.4,0,.2,1) both;
  animation-delay: calc(var(--i, 0) * 0.06s + 0.25s);
}}
.bar span {{ opacity: .95; }}
.bar-h {{ background: var(--green-bg); color: var(--green); box-shadow: inset 0 0 12px -4px var(--green); }}
.bar-d {{ background: var(--amber-bg); color: var(--amber); box-shadow: inset 0 0 12px -4px var(--amber); }}
.bar-a {{ background: var(--blue-bg); color: var(--blue); box-shadow: inset 0 0 12px -4px var(--blue); }}
.bar-labels {{
  display: flex;
  justify-content: space-between;
  font-size: .65em;
  color: var(--text-3);
  margin-top: 4px;
  padding: 0 4px;
}}

.trend-wrap {{ margin: 14px 0 16px; }}
.trend-hdr {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: .72em;
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 6px;
}}
.trend-hint {{
  opacity: .85;
  font-weight: 500;
  color: var(--accent);
  animation: trendHintBounce 1.8s ease-in-out infinite;
}}
@keyframes trendHintBounce {{ 0%,100% {{ transform: translateX(0); }} 50% {{ transform: translateX(3px); }} }}
.trend-svg {{
  width: 100%;
  height: 88px;
  display: block;
  cursor: crosshair;
  touch-action: none;
  background:
    radial-gradient(circle at 15% 20%, rgba(61,220,132,.10), transparent 55%),
    radial-gradient(circle at 85% 75%, rgba(79,195,255,.10), transparent 55%),
    linear-gradient(180deg, #23201c 0%, #17140f 100%);
  border: 1.5px solid #3d3630;
  border-radius: 12px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.04), 0 3px 10px rgba(0,0,0,.35);
}}
.trend-line {{ opacity: 1; }}
.trend-area {{ opacity: .9; }}
.trend-dot {{ filter: drop-shadow(0 0 3px rgba(0,0,0,.5)); }}
.trend-dot-live {{ animation: trendPulse 1.4s ease-in-out infinite; }}
@keyframes trendPulse {{
  0%,100% {{ r: 5.5; opacity: 1; }}
  50% {{ r: 7.5; opacity: .75; }}
}}
.trend-cursor {{ stroke: #ffd23d; stroke-width: 1.5; stroke-dasharray: 4 4; opacity: .85; }}
.trend-cursor-dot {{ stroke: #fff; stroke-width: 2; filter: drop-shadow(0 0 5px rgba(255,210,61,.8)); }}
.trend-zoom-note {{ font-size: 8.5px; fill: var(--text-3); font-family: var(--mono); opacity: .8; }}
.trend-readout {{
  font-family: var(--mono);
  font-size: .76em;
  font-weight: 600;
  color: var(--text-2);
  min-height: 1.5em;
  margin-top: 6px;
  padding: 5px 10px;
  background: #1c1917;
  border: 1px solid #3d3630;
  border-radius: 8px;
  display: flex;
  gap: 12px;
  align-items: center;
}}
.trend-readout .r-h {{ color: #3ddc84; text-shadow: 0 0 8px rgba(61,220,132,.5); }}
.trend-readout .r-d {{ color: #ffd23d; text-shadow: 0 0 8px rgba(255,210,61,.5); }}
.trend-readout .r-a {{ color: #4fc3ff; text-shadow: 0 0 8px rgba(79,195,255,.5); }}
.trend-readout .r-t {{ color: var(--text-3); font-weight: 500; }}
.trend-empty {{
  font-size: .72em;
  color: var(--text-3);
  padding: 14px 10px;
  text-align: center;
  font-style: italic;
  background: linear-gradient(180deg, #23201c 0%, #17140f 100%);
  border: 1.5px dashed #3d3630;
  border-radius: 12px;
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
.news-notice {{
  margin-top: 8px;
  padding: 7px 12px;
  background: var(--blue-bg);
  border-left: 3px solid var(--blue);
  border-radius: 0 var(--radius) var(--radius) 0;
  font-size: .72em;
  display: flex;
  align-items: baseline;
  gap: 7px;
  flex-wrap: wrap;
}}
.news-tag {{ font-weight: 600; color: var(--blue); white-space: nowrap; }}
.news-team {{ color: var(--text-3); font-family: var(--mono); font-size: .9em; white-space: nowrap; }}
.news-title {{ color: var(--text-2); text-decoration: none; flex: 1; min-width: 160px; }}
.news-title:hover {{ color: var(--accent); text-decoration: underline; }}
.news-src {{ color: var(--text-3); font-size: .85em; white-space: nowrap; }}

footer.page-footer {{
  margin-top: 40px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
  font-size: .7em;
  color: var(--text-3);
  line-height: 1.7;
}}
#toast {{
  position: fixed;
  top: 20px; left: 50%;
  transform: translate(-50%, 0);
  z-index: 100;
  background: var(--green);
  color: #08120b;
  font-weight: 600;
  font-size: .82em;
  padding: 10px 20px;
  border-radius: 999px;
  box-shadow: 0 6px 24px rgba(0,0,0,.4);
  animation: toastIn .4s ease both;
  display: none;
}}
.fading {{ transition: opacity .5s ease; opacity: 0 !important; }}
@media (max-width: 920px) {{
  .match-grid {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 680px) {{
  body {{ padding: 16px 12px 40px; }}
  header.page-header {{ padding: 14px 16px; }}
  header.page-header h1 {{ font-size: 1.15em; }}
  .match {{ padding: 16px; }}
  .matchup {{ flex-wrap: wrap; gap: 6px; }}
  .scoreboard {{ gap: 6px; }}
  .sb-badge {{ font-size: .66em; padding: 4px 9px; }}
}}
@media (prefers-reduced-motion: reduce) {{
  *, .match, .bar, .sb-badge .dot {{ animation: none !important; transition: none !important; }}
  .bar {{ width: var(--w); }}
}}
</style>
</head><body>
<div id="toast">⟳ 盘口已更新</div>
<header class="page-header">
  <div class="brand">
    <h1>⚽ 世界杯让球盘预测</h1>
    <span class="tagline">Dixon-Coles · {gen_time}</span>
  </div>
  <div class="scoreboard">
    <span class="sb-badge live"><span class="dot"></span>LIVE</span>
    <span class="sb-badge evo" title="自进化的Dixon-Coles相关系数">进化 ρ={RHO}</span>
    {f'<span class="sb-badge hit" title="最近小组赛回测命中率">命中 {_hit_pct}</span>' if _hit_pct else ''}
    <span class="sb-badge" title="数据来源/新鲜度">{data_source}</span>
  </div>
</header>

<div class="controls">
  <button onclick="location.reload()">刷新</button>
  <button id="fetchBtn" onclick="doRefresh()">重新抓取</button>
  <a href="/data/standings.json" target="_blank" class="link-btn" style="font-size:.75em;color:var(--text-2);text-decoration:none;padding:6px 10px;border:1px solid var(--border);border-radius:var(--radius);">实时积分</a>
  <a href="/data/groups_2026.json" target="_blank" class="link-btn" style="font-size:.75em;color:var(--text-2);text-decoration:none;padding:6px 10px;border:1px solid var(--border);border-radius:var(--radius);">小组分组</a>
  <span id="status" class="status-msg"></span>
</div>

<div class="match-grid">
{"".join(cards_html)}
</div>

<footer class="page-footer">
  Elo锚定双泊松 · Dixon-Coles τ (ρ={RHO}) · 平局逻辑回归(8特征) · 磨合度 · xG档案 · 定位球(仅限有真实角球数据的队)<br>
  让球盘对数池校准 (市场70% + 模型30%) · 数据: sporttery.cn · eloratings.net · open-meteo<br>
  仅供研究参考，不构成投注建议
</footer>
<script>
  // 当前页面数据版本(predictions.json mtime). 轮询此值, 变化即有新盘口数据。
  window.__VER__ = {page_version};
  function smoothReload() {{
    document.body.classList.add('fading');
    setTimeout(() => location.reload(), 520);
  }}
  function showToast(msg) {{
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.style.display = 'block';
  }}
  // 自动轮询: 每60s问后端版本号, 变化则提示+平滑刷新。静态托管(无api)则静默。
  setInterval(() => {{
    fetch('api/version', {{cache: 'no-store'}})
      .then(r => r.ok ? r.json() : null)
      .then(d => {{
        if (d && d.version && d.version !== window.__VER__) {{
          showToast('⟳ 盘口已更新，正在刷新…');
          setTimeout(smoothReload, 1400);
        }}
      }})
      .catch(() => {{}});
  }}, 60000);
  // 手动"重新抓取": 触发后端实时重算
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
        st.textContent = `完成 (${{sec}}s)，刷新中…`;
        st.className = 'status-msg success';
        setTimeout(smoothReload, 800);
      }})
      .catch(e => {{
        st.textContent = '失败: 需启动 --serve 模式';
        st.className = 'status-msg error';
        btn.disabled = false;
        btn.style.opacity = '1';
      }});
  }}

  // ── 趋势图拖动查看(纯前端插值, 数据已随HTML内嵌, 拖动不发请求) ──
  function initTrendCharts() {{
    document.querySelectorAll('.trend-svg').forEach(svg => {{
      let pts;
      try {{ pts = JSON.parse(svg.dataset.points || '[]'); }} catch (e) {{ return; }}
      if (!pts.length) return;
      const cursor = svg.querySelector('.trend-cursor');
      const cursorDot = svg.querySelector('.trend-cursor-dot');
      const readout = svg.parentElement.querySelector('.trend-readout');
      const vbW = 560, vbH = 96;
      function yOf(p) {{ return vbH - 8 - p * (vbH - 16); }}

      function fmtTime(iso) {{
        const d = new Date(iso);
        return d.toLocaleString('zh-CN', {{month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}});
      }}

      function updateAt(clientX) {{
        const rect = svg.getBoundingClientRect();
        const relX = (clientX - rect.left) / rect.width * vbW;
        // 找最近的两个点做线性插值, 让拖动手感连续而不是跳格
        let lo = pts[0], hi = pts[pts.length - 1];
        for (let i = 0; i < pts.length - 1; i++) {{
          if (pts[i].x <= relX && pts[i+1].x >= relX) {{ lo = pts[i]; hi = pts[i+1]; break; }}
        }}
        const span = hi.x - lo.x;
        const frac = span > 0 ? Math.max(0, Math.min(1, (relX - lo.x) / span)) : 0;
        const h = lo.h + (hi.h - lo.h) * frac;
        const d = lo.d + (hi.d - lo.d) * frac;
        const a = lo.a + (hi.a - lo.a) * frac;
        const t = frac < 0.5 ? lo.t : hi.t;

        cursor.setAttribute('x1', relX); cursor.setAttribute('x2', relX);
        cursor.style.display = 'block';
        cursorDot.setAttribute('cx', relX);
        cursorDot.setAttribute('cy', yOf(h));
        cursorDot.setAttribute('fill', '#ffd23d');
        cursorDot.style.display = 'block';
        readout.innerHTML =
          `<span class="r-t">${{fmtTime(t)}}</span>` +
          `<span class="r-h">主${{(h*100).toFixed(0)}}%</span>` +
          `<span class="r-d">平${{(d*100).toFixed(0)}}%</span>` +
          `<span class="r-a">客${{(a*100).toFixed(0)}}%</span>`;
      }}

      function clear() {{
        cursor.style.display = 'none';
        cursorDot.style.display = 'none';
        const last = pts[pts.length - 1];
        readout.innerHTML =
          `<span class="r-t">最新 ${{fmtTime(last.t)}}</span>` +
          `<span class="r-h">主${{(last.h*100).toFixed(0)}}%</span>` +
          `<span class="r-d">平${{(last.d*100).toFixed(0)}}%</span>` +
          `<span class="r-a">客${{(last.a*100).toFixed(0)}}%</span>`;
      }}

      svg.addEventListener('mousemove', e => updateAt(e.clientX));
      svg.addEventListener('mouseleave', clear);
      svg.addEventListener('touchstart', e => {{ updateAt(e.touches[0].clientX); }}, {{passive: true}});
      svg.addEventListener('touchmove', e => {{ updateAt(e.touches[0].clientX); e.preventDefault(); }}, {{passive: false}});
      svg.addEventListener('touchend', clear);
      clear();  // 初始显示"最新"读数
    }});
  }}
  initTrendCharts();
</script>
</body></html>'''


# ═══════════════════════════════════════════════════════════════════
# 7. 主逻辑 + serve 模式
# ═══════════════════════════════════════════════════════════════════

def run_pipeline() -> list[dict]:
    _reload_params_override()  # 每轮都读一次磁盘, 让evolve_groupstage进化出的新参数真正生效
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

        # 场外因素新闻(只读缓存, 不量化概率, 面板notice提醒)
        news_notices = get_news_notices(m["home"], m["away"], m.get("date", ""))

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
            "news_notices": news_notices,
        }
        # 体彩购买建议 (跨多玩法扫描)
        # (审计修复2026-07-02: 此前直接传pred(predict_match的原始返回值), 其
        # prior/hc_prior/ttg全部是未经市场校准的纯模型先验——compute_recommendations
        # 内部用它们算edge_v = model_p - mkt_p 和凯利下注比例, 等于绕过了系统自己
        # 引以为傲的对数池市场融合校准层, 直接拿"模型自己有多自信"当依据算真金白银
        # 的购买建议, 而不是"融合市场信息后还剩多少edge"。改为显式传入刚计算好的
        # 校准后验(had_post/hhad_post/ttg_post), 字段名保持一致, compute_recommendations
        # 内部逻辑不用改。)
        rec["recommendations"] = compute_recommendations(
            m, {"prior": had_post, "hc_prior": hhad_post, "ttg": ttg_post})
        predictions.append(rec)

    _atomic_write_text(DATA_DIR / "predictions.json",
        json.dumps(predictions, ensure_ascii=False, indent=2))
    # 追加到历史预测日志(供 backtest.py 对比真实结果用, 每场比赛只留一条快照, 语义不可动)
    _append_prediction_log(predictions)
    # 追加到趋势序列(独立文件, 每场比赛多个时间点, 供前端画近12h概率变化图)
    try:
        from odds_trend import record_snapshot
        record_snapshot(predictions)
    except Exception as e:
        print(f"  ⚠ 趋势记录失败: {e}")
    html_out = render_html(predictions)
    _atomic_write_text(SITE_DIR / "index.html", html_out)
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
    _atomic_write_text(hist_file, json.dumps(history, ensure_ascii=False, indent=2))


# 审计修复2026-07-02: run_pipeline()此前完全无锁, /api/refresh(每个HTTP请求
# 独立线程处理, 见下方ThreadedHTTPServer)与_auto_refresh_loop(后台daemon线程,
# 默认10min一轮)可能同时各自调用一遍run_pipeline(), 两者都会读旧文件→内存
# 计算→整份写回, 存在竞态(后写入的覆盖先写入的, 或读到另一线程写了一半的
# 半成品json)。push_odds.sh的curl --max-time 90超时后不会取消服务端仍在跑的
# run_pipeline(), 慢查询与下一轮cron/auto_refresh_loop重叠会放大这个风险。
# 用一把全局锁保证任意时刻只有一次run_pipeline()在执行, 拿不到锁的请求直接
# 返回"已有刷新在进行中"而不是并发跑一遍。
_pipeline_lock = threading.Lock()


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

    def _handle_refresh(self):
        """/api/refresh 的共享实现(GET/POST都会走这里)。

        (审计修复2026-07-02: 此前do_GET/do_POST各自重复一份, 且都是先
        send_response(200)再跑run_pipeline() —— 一旦run_pipeline()抛异常
        (比如weather.json损坏, 已在get_adjustments里补了try/except但保留
        这层作为最后防线), 响应头已经发出200, 客户端会拿到一个"成功"状态码
        但空/不完整的body, 前端r.json()解析报错却又走不到"网络错误"分支,
        错误现象和真实原因完全对不上。现在改成run_pipeline()跑完(或抛异常)
        之后才决定发200还是500。
        同时用_pipeline_lock防止/api/refresh与_auto_refresh_loop并发执行:
        拿不到锁直接返回"已有刷新在进行中", 不会排队等锁导致请求堆积。
        """
        if not _pipeline_lock.acquire(blocking=False):
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"refresh already in progress"}')
            return
        try:
            run_pipeline()
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)},
                                       ensure_ascii=False).encode("utf-8"))
            return
        finally:
            _pipeline_lock.release()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        if self.path == "/api/refresh":
            self._handle_refresh()
        elif self.path == "/api/version":
            # 轻量版本端点: 前端轮询此值, 变化即说明有新数据 -> 提示+平滑刷新
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            pf = DATA_DIR / "predictions.json"
            ver = int(pf.stat().st_mtime) if pf.exists() else 0
            self.wfile.write(json.dumps({"version": ver}).encode("utf-8"))
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
            self._handle_refresh()
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
    每轮同时检测能否重训模型参数(防空转: 样本数没变化就跳过, 不在噪声里空转)。

    (2026-07-02改动: 此前DC参数进化硬编码"每天14点一次", 用户反馈"场外因素/盘口
    随时在变, 不该一天只用一个结果"。改为每轮循环(默认10min)都检测: 若已配对
    样本数(真实完赛场次)相比上次检测有变化, 才重新跑一次诊断+调参; 样本数不变
    则说明没有新的真实结果可学, 强行按固定时钟重算只会让参数在同一批数据的
    浮点误差里空转, 没有信息增益还浪费算力, 因此跳过。
    权重重训(step5_learn, 拉取最新历史比赛CSV)仍保留每日一次, 因为它的输入源
    (international_results.csv)本身就是按天更新的GitHub仓库, 更高频没有意义。)"""
    import datetime
    last_retrain_date = None
    last_evolution_n = None
    while True:
        time.sleep(interval)
        try:
            with _pipeline_lock:  # 与/api/refresh互斥, 避免同时跑两遍pipeline
                run_pipeline()
            now = datetime.datetime.now()

            # DC核心参数(RHO/HOME_ADV/AVG_GOALS)自进化: 每轮都检测样本是否变化
            try:
                from evolve_groupstage import run_evolution
                probe = run_evolution(write=False)  # 先廉价探测,不落盘
                n = probe.get("n", 0)
                if n != last_evolution_n:
                    prev_n = last_evolution_n  # 修复: 打印前先存旧值, 否则日志会显示"29→29"
                    r = run_evolution(write=True)  # 样本真的变了才重新写override
                    last_evolution_n = n
                    tag = f"首次运行→{n}" if prev_n is None else f"样本{prev_n}→{n}"
                    if r.get("written"):
                        ep = r["evolved_params"]
                        print(f"[{now:%Y-%m-%d %H:%M:%S}] 🧬 DC参数进化({tag}, "
                              f"命中{r['hit_rate']:.1%}): RHO={ep['rho']} HOME_ADV={ep['home_adv']} AVG_GOALS={ep['avg_goals']}")
                    else:
                        print(f"[{now:%Y-%m-%d %H:%M:%S}] 🧬 DC参数({tag})但: {r.get('reason')}")
                # n未变时静默跳过, 不刷日志噪声
            except Exception as e:
                print(f"  ⚠ DC参数进化检测失败: {e}")

            # 权重重训(拉取历史CSV, 按天更新的数据源, 保留每日一次即可)
            if now.hour == 14 and (last_retrain_date is None or last_retrain_date != now.date()):
                print(f"[{now:%Y-%m-%d %H:%M:%S}] 每日权重重训触发...")
                try:
                    from self_evolving_loop import step5_learn
                    step5_learn()
                    print(f"  ✅ 权重重训完成")
                except Exception as e:
                    print(f"  ⚠ 权重重训失败: {e}")
                last_retrain_date = now.date()
        except Exception as e:
            print(f"  ⚠ 自动刷新失败: {e}")


def main():
    # 审计修复2026-07-02: 此前裸调用, 启动时若第一次run_pipeline()就抛异常
    # (网络抖动/依赖缺失等), 会阻止HTTP服务绑定端口, systemd(Restart=always,
    # RestartSec=5)检测到进程退出会持续重启, 若故障没解决就陷入快速重启循环。
    # _auto_refresh_loop已经有同款try/except保护, 这里补上让二者一致: 首次
    # 失败仅记录日志, --serve模式仍会启动HTTP服务(用已有的predictions.json/
    # index.html兜底展示旧数据, 好于完全连不上服务)。
    try:
        run_pipeline()
    except Exception as e:
        print(f"  ⚠ 启动时首次pipeline执行失败: {e}")
        print("  仍会启动HTTP服务, 用已有数据文件兜底展示(若存在)")
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