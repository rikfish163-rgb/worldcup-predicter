#!/usr/bin/env python3
"""Scrape sporttery.cn from 4090 (China IP) and push to VPS.

The 4090 box is on a Chinese residential IP, so the WAF lets it through.
The VPS (Singapore) is blocked, so we relay the data.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
SPORTTERY_URL = ("https://webapi.sporttery.cn/gateway/jc/football/"
                 "getMatchCalculatorV1.qry?poolCode=hhad,had,crs,ttg,hafu")

# 体彩3字母代码 → 英文全名 (用于战意系统匹配)
CODE_TO_EN = {
    "POR": "Portugal", "UZB": "Uzbekistan", "COL": "Colombia", "COD": "DR Congo",
    "DRC": "DR Congo", "ENG": "England", "GHA": "Ghana", "PAN": "Panama", "PAM": "Panama",
    "CRO": "Croatia", "MEX": "Mexico", "CZE": "Czech Republic", "KOR": "South Korea",
    "CAN": "Canada", "SUI": "Switzerland", "BIH": "Bosnia and Herzegovina",
    "QAT": "Qatar", "SCO": "Scotland", "BRA": "Brazil", "MAR": "Morocco",
    "HAI": "Haiti", "USA": "United States", "TUR": "Turkey", "PAR": "Paraguay", "PGY": "Paraguay",
    "AUS": "Australia", "AUA": "Australia", "CUR": "Curaçao",
    "CIV": "Ivory Coast", "ECU": "Ecuador", "GER": "Germany",
    "NED": "Netherlands", "HOL": "Netherlands", "NET": "Netherlands",
    "JPN": "Japan", "JPN1": "Japan", "JAP": "Japan",
    "SWE": "Sweden", "TUN": "Tunisia", "KSA": "Saudi Arabia", "SAR": "Saudi Arabia",
    "URU": "Uruguay", "ESP": "Spain", "SPA": "Spain", "EGY": "Egypt",
    "IRN": "Iran", "IRA": "Iran", "NZL": "New Zealand", "NZD": "New Zealand",
    "BEL": "Belgium", "BEG": "Belgium", "FRA": "France",
    "NOR": "Norway", "NOW": "Norway", "SEN": "Senegal",
    "IRQ": "Iraq", "ARG": "Argentina", "AUT": "Austria", "ALG": "Algeria",
    "JOR": "Jordan", "CMR": "Cameroon", "RSA": "South Africa",
    "CPV": "Cape Verde", "CVI": "Cape Verde", "GRL": "Cape Verde", "CAA": "Cape Verde",
    "COM": "Colombia", "POG": "Paraguay", "GUY": "Guyana", "CHA": "Chad", "GUI": "Guinea",
}

DATA_DIR = Path(__file__).parent / "data"
PARSED = DATA_DIR / "odds_parsed.json"
VPS = "ubuntu@170.106.198.250"
VPS_DIR = "~/soccerdata/wc_analysis/data"


def fetch_sporttery() -> list[dict]:
    """Fetch raw sporttery JSON. Try desktop UA first, fall back to mobile UA on 403/567."""
    last_err = None
    for ua, referer in [(UA_DESKTOP, "https://static.sporttery.cn/"),
                         (UA_MOBILE, "https://m.sporttery.cn/")]:
        try:
            req = urllib.request.Request(SPORTTERY_URL, headers={
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": referer,
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = json.loads(r.read())
            return raw.get("value", {}).get("matchInfoList", [])
        except Exception as e:
            last_err = e
            err_str = str(e)
            if "403" in err_str or "567" in err_str or "Forbidden" in err_str:
                continue  # try next UA
            raise
    raise last_err if last_err else RuntimeError("All UA strategies failed")


def parse_matches(match_info: list[dict]) -> list[dict]:
    matches = []
    for day in match_info:
        match_date = day.get("matchDate", "")
        for m in day.get("subMatchList", []):
            rec = {
                "home": m.get("homeTeamAllName", ""),
                "away": m.get("awayTeamAllName", ""),
                "home_en": CODE_TO_EN.get(m.get("homeTeamAbbEnName", ""), m.get("homeTeamAbbEnName", "")),
                "away_en": CODE_TO_EN.get(m.get("awayTeamAbbEnName", ""), m.get("awayTeamAbbEnName", "")),
                "date": m.get("matchDate", match_date),
                "time": m.get("matchTime", ""),
                "league": m.get("leagueAbbName", ""),
                "num": m.get("matchNumStr", ""),
                "match_id": m.get("matchId", ""),
            }
            # HAD: 胜平负 (1X2) - 不开则不存
            had = m.get("had") or {}
            if had.get("h") and float(had.get("h", 0)) > 0:
                odds = {"h": float(had["h"]), "d": float(had["d"]), "a": float(had["a"])}
                rec["had_odds"] = odds
                rec["had_prob"] = _devig(odds)
                rec["had_update"] = f"{had.get('updateDate','')} {had.get('updateTime','')}"
            # HHAD: 让球胜平负
            hhad = m.get("hhad") or {}
            if hhad.get("h") and float(hhad.get("h", 0)) > 0:
                rec["hhad_line"] = hhad.get("goalLineValue", "")
                odds = {"h": float(hhad["h"]), "d": float(hhad["d"]), "a": float(hhad["a"])}
                rec["hhad_odds"] = odds
                rec["hhad_prob"] = _devig(odds)
                rec["hhad_update"] = f"{hhad.get('updateDate','')} {hhad.get('updateTime','')}"
            # TTG: 总进球 (0-7+)
            ttg = m.get("ttg") or {}
            if ttg.get("s0") and float(ttg.get("s0", 0)) > 0:
                rec["ttg_odds"] = {str(i): float(ttg[f"s{i}"]) for i in range(8) if ttg.get(f"s{i}") and float(ttg.get(f"s{i}", 0)) > 0}
                rec["ttg_prob"] = _devig(rec["ttg_odds"])
                rec["ttg_update"] = f"{ttg.get('updateDate','')} {ttg.get('updateTime','')}"
            # HAFU: 半全场 (9 outcomes)
            hafu = m.get("hafu") or {}
            hafu_outcomes = ["hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa"]
            hafu_labels = {"hh": "胜胜", "hd": "胜平", "ha": "胜负",
                           "dh": "平胜", "dd": "平平", "da": "平负",
                           "ah": "负胜", "ad": "负平", "aa": "负负"}
            hafu_odds = {}
            for o in hafu_outcomes:
                v = hafu.get(o)
                if v and float(v) > 0:
                    hafu_odds[hafu_labels[o]] = float(v)
            if hafu_odds:
                rec["hafu_odds"] = hafu_odds
                rec["hafu_prob"] = _devig(hafu_odds)
                rec["hafu_update"] = f"{hafu.get('updateDate','')} {hafu.get('updateTime','')}"
            # CRS: 比分 (主要比分)
            crs = m.get("crs") or {}
            crs_odds = {}
            score_labels = ["00", "10", "20", "30", "40", "50",
                            "11", "21", "31", "41",
                            "22", "32",
                            "33",
                            "01", "02", "03", "04", "05",
                            "12", "13", "14",
                            "23", "24",
                            "1sa", "1sd", "1sh"]  # 1sa=胜其他, 1sd=平其他, 1sh=负其他
            for s in score_labels:
                key = f"s{s}s{s}" if len(s) == 2 else f"s1s{s[-1]}"
                v = crs.get(key)
                if v and float(v) > 0 and float(v) < 100:  # 100+ 为无效盘
                    crs_odds[s] = float(v)
            if crs_odds:
                rec["crs_odds"] = crs_odds
                rec["crs_update"] = f"{crs.get('updateDate','')} {crs.get('updateTime','')}"
            matches.append(rec)
    return matches


def _devig(odds: dict) -> dict:
    inv = {k: 1.0 / v for k, v in odds.items() if v > 0}
    s = sum(inv.values())
    return {k: round(v / s, 5) for k, v in inv.items()} if s else {}


def push_to_vps(local: Path) -> bool:
    """SCP the parsed file to VPS."""
    cmd = ["scp", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
           str(local), f"{VPS}:{VPS_DIR}/"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            print(f"  ✅ Pushed to VPS: {VPS}:{VPS_DIR}/{local.name}")
            return True
        print(f"  ⚠ SCP failed: {r.stderr.strip()}")
        return False
    except Exception as e:
        print(f"  ⚠ SCP error: {e}")
        return False


def main() -> int:
    print(f"[{time.strftime('%H:%M:%S')}] Fetching sporttery from 4090 (China IP)...")
    try:
        match_info = fetch_sporttery()
    except Exception as e:
        print(f"  ✗ Fetch failed: {e}")
        return 1

    matches = parse_matches(match_info)
    print(f"  ✅ Got {len(matches)} matches")
    if not matches:
        print("  ⚠ Empty result, skipping")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PARSED.write_text(
        json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✅ Saved to {PARSED.name}")

    if "--push" in sys.argv:
        push_to_vps(PARSED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
