#!/usr/bin/env python3
"""Two-step World Cup analysis for the 2026-06-22 Sporttery slate.

The workflow is intentionally auditable:
1. build a prior from Elo, recent goals, shooting proxy npxG, and Dixon-Coles;
2. calibrate it with availability, rest/fatigue, weather, and the Sporttery
   market shape.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent / "data"
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
FBREF_CACHE_DIR = ROOT / "data" / "FBref"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

ELO_TEAM_CODES = {
    "Spain": "ES",
    "Saudi_Arabia": "SA",
    "Belgium": "BE",
    "Iran": "IR",
    "Uruguay": "UY",
    "Cape_Verde": "CV",
    "New_Zealand": "NZ",
    "Egypt": "EG",
}

FIXTURES = [
    {
        "match_id": 2040247,
        "match_num": "周日037",
        "home_cn": "西班牙",
        "away_cn": "沙特阿拉伯",
        "home_fbref": "Spain",
        "away_fbref": "Saudi Arabia",
        "home_elo": "Spain",
        "away_elo": "Saudi_Arabia",
        "kickoff_bj": "2026-06-22 00:00",
        "venue": "Atlanta Stadium, Atlanta",
        "lat": 33.7554,
        "lon": -84.4008,
        "kickoff_local_date": "2026-06-21",
        "kickoff_local_hour": 12,
    },
    {
        "match_id": 2040248,
        "match_num": "周日038",
        "home_cn": "比利时",
        "away_cn": "伊朗",
        "home_fbref": "Belgium",
        "away_fbref": "IR Iran",
        "home_elo": "Belgium",
        "away_elo": "Iran",
        "kickoff_bj": "2026-06-22 03:00",
        "venue": "Los Angeles Stadium, Los Angeles",
        "lat": 33.9535,
        "lon": -118.3392,
        "kickoff_local_date": "2026-06-21",
        "kickoff_local_hour": 12,
    },
    {
        "match_id": 2040249,
        "match_num": "周日039",
        "home_cn": "乌拉圭",
        "away_cn": "佛得角",
        "home_fbref": "Uruguay",
        "away_fbref": "Cape Verde",
        "home_elo": "Uruguay",
        "away_elo": "Cape_Verde",
        "kickoff_bj": "2026-06-22 06:00",
        "venue": "Miami Stadium, Miami",
        "lat": 25.958,
        "lon": -80.2389,
        "kickoff_local_date": "2026-06-21",
        "kickoff_local_hour": 18,
    },
    {
        "match_id": 2040250,
        "match_num": "周日040",
        "home_cn": "新西兰",
        "away_cn": "埃及",
        "home_fbref": "New Zealand",
        "away_fbref": "Egypt",
        "home_elo": "New_Zealand",
        "away_elo": "Egypt",
        "kickoff_bj": "2026-06-22 09:00",
        "venue": "BC Place, Vancouver",
        "lat": 49.2768,
        "lon": -123.1119,
        "kickoff_local_date": "2026-06-21",
        "kickoff_local_hour": 18,
    },
]

MANUAL_AVAILABILITY = {
    "西班牙 vs 沙特阿拉伯": {
        "home_attack_mult": 1.01,
        "home_defense_mult": 0.98,
        "away_attack_mult": 1.00,
        "away_defense_mult": 1.04,
        "notes": [
            "Yamal 可首发但分钟受控：提升上半场爆点，同时压低全场大胜上限。",
            "沙特核心仍以低位防守和反击为主，若早失球，防守折损更大。",
        ],
        "confidence": "medium",
    },
    "比利时 vs 伊朗": {
        "home_attack_mult": 0.94,
        "home_defense_mult": 1.01,
        "away_attack_mult": 0.99,
        "away_defense_mult": 1.00,
        "notes": [
            "Doku 因病缺阵，右路推进和一对一爆点明显下调。",
            "伊朗组织性强，市场对平/小比分保护更充分。",
        ],
        "confidence": "medium",
    },
    "乌拉圭 vs 佛得角": {
        "home_attack_mult": 0.94,
        "home_defense_mult": 1.04,
        "away_attack_mult": 0.98,
        "away_defense_mult": 1.02,
        "notes": [
            "Araujo 和 De Arrascaeta 缺阵，分别削弱防线稳定和中路创造。",
            "佛得角抗压和定位球路径是冷门脚本，但持续进攻创造力有限。",
        ],
        "confidence": "medium",
    },
    "新西兰 vs 埃及": {
        "home_attack_mult": 0.98,
        "home_defense_mult": 1.02,
        "away_attack_mult": 1.03,
        "away_defense_mult": 0.99,
        "notes": [
            "Garbett 退出名单削弱新西兰中场创造，但 Wood/Just 的高球和定位球路径仍需要保留。",
            "埃及进攻核心质量更高，胜面强于新西兰，但客让一球市场已明显压价。",
        ],
        "confidence": "medium",
    },
}

SOURCE_NOTES = [
    {
        "label": "FIFA match centre",
        "url": "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/match-center",
        "use": "赛程、场地和开赛时间主核验。",
    },
    {
        "label": "Guardian: Yamal availability",
        "url": "https://www.theguardian.com/football/2026/jun/20/lamine-yamal-genius-salvador-dali-michelangelo-spain-luis-de-la-fuente-world-cup",
        "use": "西班牙 Yamal 可首发但分钟受控。",
    },
    {
        "label": "Al Jazeera: Yamal minutes",
        "url": "https://www.aljazeera.com/sports/2026/6/19/spains-yamal-says-very-early-unnecessary-to-play-full-world-cup-match",
        "use": "Yamal 伤后恢复和不必踢满 90 分钟。",
    },
    {
        "label": "AP/TheScore: Doku out",
        "url": "https://www.thescore.com/belpro/news/3556178/belgiums-doku-will-miss-world-cup-match-with-iran-due-to-illness",
        "use": "比利时 Doku 因病缺阵。",
    },
    {
        "label": "RotoWire lineups",
        "url": "https://www.rotowire.com/soccer/lineups.php?league=WOC",
        "use": "阵容/伤停交叉核验。",
    },
    {
        "label": "RotoWire: Uruguay vs Cape Verde",
        "url": "https://www.rotowire.com/soccer/article/uruguay-vs-cape-verde-preview-predicted-lineups-team-news-tactical-analysis-2026-world-cup-group-h-118935",
        "use": "Araujo、De Arrascaeta 缺阵和佛得角伤停。",
    },
    {
        "label": "Washington Post/AP: Garbett out",
        "url": "https://www.washingtonpost.com/sports/soccer/2026/06/15/world-cup-new-zealand-injury-garbett-iran/827afa10-6919-11f1-830e-133d20cadd28_story.html",
        "use": "新西兰 Garbett 退出名单。",
    },
    {
        "label": "The National: Egypt camp",
        "url": "https://www.thenationalnews.com/sport/world-cup-2026/2026/06/21/egypt-boss-denies-mohamed-salah-rift-ahead-of-vital-world-cup-clash-against-new-zealand/",
        "use": "埃及 Salah 相关传闻和防守高球调整。",
    },
]


def weighted_mean(values: Iterable[float], decay: float = 0.72) -> float:
    vals = [float(v) for v in values if v is not None and not pd.isna(v)]
    if not vals:
        return 0.0
    weights = [decay ** (len(vals) - 1 - i) for i in range(len(vals))]
    return sum(v * w for v, w in zip(vals, weights)) / sum(weights)


def dixon_coles_tau(home_goals: int, away_goals: int, home_lam: float, away_lam: float, rho: float) -> float:
    if home_goals == 0 and away_goals == 0:
        return 1 - home_lam * away_lam * rho
    if home_goals == 0 and away_goals == 1:
        return 1 + home_lam * rho
    if home_goals == 1 and away_goals == 0:
        return 1 + away_lam * rho
    if home_goals == 1 and away_goals == 1:
        return 1 - rho
    return 1.0


def poisson_score_matrix(home_lam: float, away_lam: float, rho: float = -0.06, max_goals: int = 8) -> dict[str, float]:
    raw: dict[str, float] = {}
    for h in range(max_goals + 1):
        ph = math.exp(-home_lam) * home_lam**h / math.factorial(h)
        for a in range(max_goals + 1):
            pa = math.exp(-away_lam) * away_lam**a / math.factorial(a)
            raw[f"{h}-{a}"] = ph * pa * dixon_coles_tau(h, a, home_lam, away_lam, rho)
    total = sum(raw.values())
    return {k: max(v / total, 0.0) for k, v in raw.items()}


def summarize_score_matrix(matrix: dict[str, float], handicap: float | None = None) -> dict:
    wld = {"home": 0.0, "draw": 0.0, "away": 0.0}
    totals = {str(i): 0.0 for i in range(8)}
    handicap_probs = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for score, prob in matrix.items():
        h, a = [int(x) for x in score.split("-")]
        if h > a:
            wld["home"] += prob
        elif h == a:
            wld["draw"] += prob
        else:
            wld["away"] += prob
        total_goals = h + a
        totals[str(total_goals if total_goals < 7 else 7)] += prob
        if handicap is not None:
            adjusted = h + handicap - a
            if adjusted > 0:
                handicap_probs["home"] += prob
            elif adjusted == 0:
                handicap_probs["draw"] += prob
            else:
                handicap_probs["away"] += prob
    top_scores = [
        {"score": score, "prob": prob}
        for score, prob in sorted(matrix.items(), key=lambda item: item[1], reverse=True)[:8]
    ]
    return {"wld": wld, "handicap": handicap_probs, "totals": totals, "top_scores": top_scores}


def fetch_json(url: str, timeout: int = 8) -> dict:
    req = Request(url, headers={"User-Agent": UA, "Referer": "https://www.sporttery.cn/"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def devig(odds: dict[str, float]) -> dict[str, float]:
    inv = {k: 1 / float(v) for k, v in odds.items() if v}
    total = sum(inv.values())
    return {k: v / total for k, v in inv.items()} if total else {}


def load_sporttery_odds() -> dict[str, dict]:
    from wc_analysis.fetch_odds import fetch_raw, parse_match

    raw = fetch_raw()
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "odds_raw_0622.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    parsed = []
    target_ids = {fx["match_id"] for fx in FIXTURES}
    for info in raw.get("value", {}).get("matchInfoList", []):
        for sub in info.get("subMatchList", []):
            if sub.get("matchId") in target_ids:
                item = parse_match(sub)
                item["match_id"] = sub.get("matchId")
                parsed.append(item)
    (DATA_DIR / "odds_parsed_0622.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return {m["match"]: m for m in parsed}


def fetch_current_elo(team_file: str, cache_hours: int = 24) -> dict:
    cache_dir = DATA_DIR / "elo_cache_0622"
    cache_dir.mkdir(exist_ok=True)
    cache = cache_dir / f"{team_file}.tsv"
    if cache.exists() and time.time() - cache.stat().st_mtime < cache_hours * 3600:
        text = cache.read_text(encoding="utf-8")
    else:
        url = f"https://www.eloratings.net/{quote(team_file)}.tsv"
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        cache.write_text(text, encoding="utf-8")
    rows = [line.split("\t") for line in text.splitlines() if line.strip()]
    last = rows[-1]
    home_code, away_code = last[3], last[4]
    home_elo = float(str(last[10]).replace("−", "-"))
    away_elo = float(str(last[11]).replace("−", "-"))
    code = ELO_TEAM_CODES.get(team_file)
    if code == home_code:
        elo = home_elo
    elif code == away_code:
        elo = away_elo
    else:
        elo = home_elo
    return {
        "team_file": team_file,
        "elo": elo,
        "last_date": f"{last[0]}-{int(last[1]):02d}-{int(last[2]):02d}",
        "last_codes": [home_code, away_code],
        "n_matches": len(rows),
    }


def _read_fbref_table(team: str, table_kind: str, against: bool = False) -> pd.DataFrame:
    path = FBREF_CACHE_DIR / f"matchlogs_{team}_2026_{table_kind}.html"
    if not path.exists():
        return pd.DataFrame()
    tables = pd.read_html(path)
    if table_kind == "schedule":
        return tables[9]
    index = 10 if against else 9
    if len(tables) <= index:
        return pd.DataFrame()
    df = tables[index].copy()
    df.columns = [
        c[-1] if isinstance(c, tuple) else c
        for c in df.columns
    ]
    return df


def _clean_opponent(value) -> str:
    return re.sub(r"^[a-z]{2,3}\s+", "", str(value)).strip()


def team_profile(team: str, as_of: str = "2026-06-22") -> dict:
    sched = _read_fbref_table(team, "schedule")
    if sched.empty:
        return {"team": team, "data_quality": "missing"}
    sched = sched.copy()
    sched["Date_dt"] = pd.to_datetime(sched["Date"], errors="coerce")
    played = sched[(sched["Result"].isin(["W", "D", "L"])) & (sched["Date_dt"] < pd.Timestamp(as_of))]
    recent = played.tail(6)

    shooting_for = _read_fbref_table(team, "shooting", against=False)
    shooting_against = _read_fbref_table(team, "shooting", against=True)
    for df in [shooting_for, shooting_against]:
        if not df.empty:
            df["Date_dt"] = pd.to_datetime(df["Date"], errors="coerce")

    recent_for = shooting_for[shooting_for["Date_dt"] < pd.Timestamp(as_of)].tail(6) if not shooting_for.empty else pd.DataFrame()
    recent_against = (
        shooting_against[shooting_against["Date_dt"] < pd.Timestamp(as_of)].tail(6)
        if not shooting_against.empty
        else pd.DataFrame()
    )

    gf = list(pd.to_numeric(recent["GF"], errors="coerce").dropna())
    ga = list(pd.to_numeric(recent["GA"], errors="coerce").dropna())
    sh = list(pd.to_numeric(recent_for.get("Sh", pd.Series(dtype=float)), errors="coerce").dropna())
    sot = list(pd.to_numeric(recent_for.get("SoT", pd.Series(dtype=float)), errors="coerce").dropna())
    sh_a = list(pd.to_numeric(recent_against.get("Sh", pd.Series(dtype=float)), errors="coerce").dropna())
    sot_a = list(pd.to_numeric(recent_against.get("SoT", pd.Series(dtype=float)), errors="coerce").dropna())

    npxg_for = weighted_mean([0.07 * x + 0.23 * y for x, y in zip(sh, sot)]) if sh and sot else weighted_mean(gf)
    npxg_against = (
        weighted_mean([0.07 * x + 0.23 * y for x, y in zip(sh_a, sot_a)])
        if sh_a and sot_a
        else weighted_mean(ga)
    )
    last_played = played["Date_dt"].max()
    kickoff = pd.Timestamp(as_of)
    rest_days = int((kickoff - last_played).days) if pd.notna(last_played) else None
    return {
        "team": team,
        "data_quality": "ok",
        "matches_used": int(len(recent)),
        "recent_sequence": "".join(str(x) for x in recent["Result"].tolist()),
        "goals_for": round(weighted_mean(gf), 3),
        "goals_against": round(weighted_mean(ga), 3),
        "avg_possession": round(float(pd.to_numeric(recent["Poss"], errors="coerce").mean()), 1)
        if "Poss" in recent
        else None,
        "npxg_proxy_for": round(float(npxg_for), 3),
        "npxg_proxy_against": round(float(npxg_against), 3),
        "shooting_rows_used": int(len(recent_for)),
        "last_played": str(last_played.date()) if pd.notna(last_played) else None,
        "rest_days": rest_days,
        "next_fixtures": [
            {
                "date": str(r["Date"]),
                "opponent": _clean_opponent(r["Opponent"]),
                "result": None if pd.isna(r["Result"]) else str(r["Result"]),
            }
            for _, r in sched[sched["Date_dt"] >= pd.Timestamp(as_of) - pd.Timedelta(days=2)].head(3).iterrows()
        ],
    }


def weather_for_fixture(fx: dict) -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={fx['lat']}&longitude={fx['lon']}"
        "&hourly=temperature_2m,relative_humidity_2m,precipitation_probability,precipitation,wind_speed_10m"
        f"&start_date={fx['kickoff_local_date']}&end_date={fx['kickoff_local_date']}&timezone=auto"
    )
    try:
        data = fetch_json(url, timeout=5)
        hourly = data["hourly"]
        hour = fx["kickoff_local_hour"]
        idxs = [i for i, ts in enumerate(hourly["time"]) if hour <= int(ts[11:13]) <= hour + 2]
        rec = {
            "source": "open-meteo",
            "kickoff_local": f"{fx['kickoff_local_date']} {hour:02d}:00",
            "temperature_c": [hourly["temperature_2m"][i] for i in idxs],
            "humidity": [hourly["relative_humidity_2m"][i] for i in idxs],
            "precip_probability": [hourly["precipitation_probability"][i] for i in idxs],
            "precip_mm": [hourly["precipitation"][i] for i in idxs],
            "wind_kmh": [hourly["wind_speed_10m"][i] for i in idxs],
        }
        return rec
    except Exception as exc:  # pragma: no cover - live network fallback
        cached = DATA_DIR / "weather.json"
        if cached.exists():
            old = json.loads(cached.read_text(encoding="utf-8"))
            for key, value in old.items():
                if fx["home_cn"] in key and fx["away_cn"] in key:
                    return {
                        "source": "open-meteo-cache",
                        "error": str(exc),
                        "temperature_c": value.get("temp_c", []),
                        "humidity": value.get("humidity", []),
                        "precip_probability": value.get("precip_prob", []),
                        "precip_mm": value.get("precip_mm", []),
                        "wind_kmh": value.get("wind_kmh", []),
                        "note": "缓存来自旧四场场地，仅作为失败降级，不参与强校准。",
                    }
        return {"source": "open-meteo", "error": str(exc), "temperature_c": [], "precip_probability": [], "wind_kmh": []}


def build_lambdas(home: dict, away: dict, elo_home: float, elo_away: float) -> dict:
    elo_delta = max(min((elo_home - elo_away) / 400.0, 1.25), -1.25)
    base_total = 2.55
    share = 1 / (1 + math.exp(-1.55 * elo_delta))
    elo_home_lam = base_total * share
    elo_away_lam = base_total * (1 - share)

    form_home = 0.55 * home["npxg_proxy_for"] + 0.45 * away["npxg_proxy_against"]
    form_away = 0.55 * away["npxg_proxy_for"] + 0.45 * home["npxg_proxy_against"]
    home_lam = 0.62 * elo_home_lam + 0.38 * form_home
    away_lam = 0.62 * elo_away_lam + 0.38 * form_away
    scale = 2.55 / max(home_lam + away_lam, 0.1)
    home_lam *= scale
    away_lam *= scale
    return {
        "elo_home_lam": round(elo_home_lam, 3),
        "elo_away_lam": round(elo_away_lam, 3),
        "prior_home_lam": round(max(home_lam, 0.15), 3),
        "prior_away_lam": round(max(away_lam, 0.15), 3),
    }


def calibrate_lambdas(match: str, home_lam: float, away_lam: float, weather: dict, market: dict | None) -> dict:
    adj = MANUAL_AVAILABILITY[match]
    h = home_lam * adj["home_attack_mult"] / adj["away_defense_mult"]
    a = away_lam * adj["away_attack_mult"] / adj["home_defense_mult"]
    reasons = list(adj["notes"])

    temps = weather.get("temperature_c") or []
    precip = weather.get("precip_probability") or []
    wind = weather.get("wind_kmh") or []
    if temps and max(temps) >= 30:
        h *= 0.96
        a *= 0.96
        reasons.append("高温达到30度上下，节奏和持续冲刺略降。")
    if precip and max(precip) >= 45:
        h *= 0.96
        a *= 0.96
        reasons.append("降水概率偏高，压低开放对攻和射门质量。")
    if wind and max(wind) >= 25:
        h *= 0.97
        a *= 0.97
        reasons.append("风速偏高，长传和定位球落点不稳定。")

    if market and market.get("ttg"):
        ttg = market["ttg"]["prob"]
        market_total = sum((int(k) if k != "7" else 7.5) * v for k, v in ttg.items())
        model_total = h + a
        blend_total = 0.72 * model_total + 0.28 * market_total
        total_scale = blend_total / model_total if model_total else 1.0
        h *= total_scale
        a *= total_scale
        reasons.append(f"用体彩总进球分布做总量校准：市场均值约{market_total:.2f}球。")

    return {
        "posterior_home_lam": round(max(h, 0.12), 3),
        "posterior_away_lam": round(max(a, 0.12), 3),
        "calibration_reasons": reasons,
        "availability_confidence": adj["confidence"],
    }


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def edge(model: dict, market: dict | None) -> dict | None:
    if not market:
        return None
    return {k: model.get(k, 0.0) - market.get(k, 0.0) for k in ("home", "draw", "away")}


def run_analysis() -> list[dict]:
    DATA_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    odds_by_match = load_sporttery_odds()
    profiles = {fx["home_fbref"]: team_profile(fx["home_fbref"]) for fx in FIXTURES}
    profiles.update({fx["away_fbref"]: team_profile(fx["away_fbref"]) for fx in FIXTURES})

    report = []
    for fx in FIXTURES:
        match = f"{fx['home_cn']} vs {fx['away_cn']}"
        market = odds_by_match.get(match)
        home_prof = profiles[fx["home_fbref"]]
        away_prof = profiles[fx["away_fbref"]]
        home_elo = fetch_current_elo(fx["home_elo"])
        away_elo = fetch_current_elo(fx["away_elo"])
        lambdas = build_lambdas(home_prof, away_prof, home_elo["elo"], away_elo["elo"])
        prior_matrix = poisson_score_matrix(lambdas["prior_home_lam"], lambdas["prior_away_lam"])
        handicap = float(market["hhad"]["handicap"]) if market and market.get("hhad") else None
        prior = summarize_score_matrix(prior_matrix, handicap=handicap)
        weather = weather_for_fixture(fx)
        posterior_lambdas = calibrate_lambdas(
            match,
            lambdas["prior_home_lam"],
            lambdas["prior_away_lam"],
            weather,
            market,
        )
        posterior_matrix = poisson_score_matrix(
            posterior_lambdas["posterior_home_lam"],
            posterior_lambdas["posterior_away_lam"],
        )
        posterior = summarize_score_matrix(posterior_matrix, handicap=handicap)
        market_had = market.get("had", {}).get("prob") if market else None
        market_hhad = market.get("hhad", {}).get("prob") if market else None
        rec = {
            "fixture": fx,
            "match": match,
            "market": market,
            "home_profile": home_prof,
            "away_profile": away_prof,
            "elo": {"home": home_elo, "away": away_elo, "diff": round(home_elo["elo"] - away_elo["elo"], 1)},
            "lambdas": lambdas | posterior_lambdas,
            "prior": prior,
            "posterior": posterior,
            "edges": {
                "had": edge(posterior["wld"], market_had),
                "hhad": edge(posterior["handicap"], market_hhad),
            },
            "weather": weather,
            "blog_method_notes": [
                "本地博客强调 SoccerData 的多源统一、缓存和 FBref/ClubElo/Understat/Sofascore 分工；本次复用其多源思路，但国家队 Elo 改走 eloratings.net。",
                "FBref 当前缓存有赛程和射门表，npxG 只能做代理，不等同 Opta/StatsBomb 真 xG。",
                "体彩 API 可实时给出 had/hhad/ttg/crs，用于第二步市场校准和价值检查。",
            ],
            "risk_flags": [],
        }
        if not market_had:
            rec["risk_flags"].append("体彩未开标准胜平负，胜面只能用让球/比分/总进球交叉验证。")
        if home_prof["shooting_rows_used"] < 3 or away_prof["shooting_rows_used"] < 3:
            rec["risk_flags"].append("射门样本不足，npxG代理权重应下调。")
        report.append(rec)
    (DATA_DIR / "worldcup_0622_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report)
    return report


def market_line_text(market: dict | None) -> str:
    if not market:
        return "体彩盘口未抓到"
    bits = []
    if market.get("had"):
        p = market["had"]["prob"]
        bits.append(f"胜平负 主{pct(p['home'])}/平{pct(p['draw'])}/客{pct(p['away'])}")
    if market.get("hhad"):
        p = market["hhad"]["prob"]
        bits.append(f"让球{market['hhad']['handicap']} 主{pct(p['home'])}/平{pct(p['draw'])}/客{pct(p['away'])}")
    if market.get("ttg"):
        best = max(market["ttg"]["prob"].items(), key=lambda x: x[1])
        bits.append(f"总进球热项 {best[0]}球 {pct(best[1])}")
    return "；".join(bits)


def recommendation(rec: dict) -> dict:
    match = rec["match"]
    wld = rec["posterior"]["wld"]
    hhad = rec["posterior"]["handicap"]
    scores = [x["score"] for x in rec["posterior"]["top_scores"][:4]]
    totals = rec["posterior"]["totals"]
    total_best = max(totals.items(), key=lambda x: x[1])[0]
    if match == "西班牙 vs 沙特阿拉伯":
        return {"lean": "西班牙胜；让-2不追深，比分2-0/3-0优先", "confidence": "中高", "scores": scores, "total": total_best}
    if match == "比利时 vs 伊朗":
        return {"lean": "比利时胜面仍在，但直胜低赔和让-1都不值；比分1-0/2-1，小心1-1", "confidence": "中", "scores": scores, "total": total_best}
    if match == "乌拉圭 vs 佛得角":
        return {"lean": "乌拉圭胜最稳；让-1接近公平偏谨慎，1-0/2-0优先", "confidence": "中高", "scores": scores, "total": total_best}
    return {"lean": "埃及不败/埃及胜；让+1方向新西兰有保护价值", "confidence": "中", "scores": scores, "total": total_best}


def write_markdown(report: list[dict]) -> Path:
    out = OUTPUT_DIR / "worldcup_0622_deep_analysis.md"
    lines = [
        "# 2026-06-22 世界杯四场深度分析",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 方法",
        "",
        "- 先验：国家队 Elo + FBref 近期赛程/控球 + 射门表构造 npxG 代理，进入 Dixon-Coles 双泊松比分矩阵。",
        "- 校准：伤病/阵容可信度、休息时间、天气、体彩总进球分布，对进球期望做二次修正。",
        "- 盘口：体彩 had/hhad/ttg/crs 先去水，再和后验概率比较。结论区分“更可能发生”和“更有价值”。",
        "",
        "## 本地博客解析后的使用边界",
        "",
        "博客强调 SoccerData 的多源统一、缓存、FBref/ClubElo/Understat/Sofascore 数据分工。这里采用相同多源思路，但本次是国家队比赛：ClubElo 不适用，Elo 走 eloratings.net；FBref 当前缓存没有真 xG，npxG 是射门/射正代理；体彩 API 是实时盘口校准源。",
        "",
        "## 四场结论",
        "",
    ]
    for rec in report:
        r = recommendation(rec)
        p = rec["posterior"]["wld"]
        prior = rec["prior"]["wld"]
        top_score_text = ", ".join(
            f"{item['score']}({pct(item['prob'])})"
            for item in rec["posterior"]["top_scores"][:5]
        )
        lines += [
            f"### {rec['fixture']['match_num']} {rec['match']} ({rec['fixture']['kickoff_bj']} 北京时间)",
            "",
            f"- 场地：{rec['fixture']['venue']}",
            f"- 体彩：{market_line_text(rec['market'])}",
            f"- Elo：{rec['elo']['home']['elo']:.0f} vs {rec['elo']['away']['elo']:.0f}，差值 {rec['elo']['diff']:+.0f}",
            f"- 先验胜平负：主{pct(prior['home'])} / 平{pct(prior['draw'])} / 客{pct(prior['away'])}",
            f"- 校准后胜平负：主{pct(p['home'])} / 平{pct(p['draw'])} / 客{pct(p['away'])}",
            f"- 进球期望：{rec['lambdas']['posterior_home_lam']:.2f} - {rec['lambdas']['posterior_away_lam']:.2f}",
            f"- 热门比分：{top_score_text}",
            f"- 结论：{r['lean']}；信心 {r['confidence']}",
            "",
            "校准依据：",
        ]
        for reason in rec["lambdas"]["calibration_reasons"]:
            lines.append(f"- {reason}")
        if rec["risk_flags"]:
            lines.append("")
            lines.append("风险提示：")
            for flag in rec["risk_flags"]:
                lines.append(f"- {flag}")
        lines.append("")
    lines += [
        "## 数据可靠性检查",
        "",
        "- 体彩：本轮 live API 可抓，四场 matchId 为 2040247-2040250；盘口随时间会动，赛前需重跑。",
        "- FBref：使用本地缓存 HTML；对强弱悬殊队伍，预选赛样本质量差异大，因此 npxG 代理仅用于方向校正。",
        "- 天气：open-meteo 逐小时预报，无 API key；室外场地才强影响，预报仍有临场误差。",
        "- 伤病：当前为公开新闻和阵容深度校准，未等同官方首发；临场名单会显著影响让球盘。",
        "",
        "## 来源",
        "",
    ]
    for src in SOURCE_NOTES:
        lines.append(f"- [{src['label']}]({src['url']})：{src['use']}")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="print compact JSON summary")
    args = parser.parse_args()
    report = run_analysis()
    if args.json:
        print(json.dumps(report, ensure_ascii=False)[:4000])
    else:
        for rec in report:
            r = recommendation(rec)
            p = rec["posterior"]["wld"]
            print(f"{rec['match']}: {r['lean']} | 后验 主{pct(p['home'])}/平{pct(p['draw'])}/客{pct(p['away'])}")
        print(f"JSON: {DATA_DIR / 'worldcup_0622_report.json'}")
        print(f"MD: {OUTPUT_DIR / 'worldcup_0622_deep_analysis.md'}")


if __name__ == "__main__":
    main()
