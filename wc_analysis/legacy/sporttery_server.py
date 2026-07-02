#!/usr/bin/env python3
"""HTTP server on 4090 that exposes latest sporttery data.

Run on 4090 with:
    python3 sporttery_server.py --port 8765
VPS fetches via:
    curl http://110os9214fc69.vicp.fun:41380/odds.json
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SPORTTERY_URL = ("https://webapi.sporttery.cn/gateway/jc/football/"
                 "getMatchCalculatorV1.qry?poolCode=hhad,had,crs,ttg,hafu")
DATA_DIR = Path(__file__).parent / "data"
PARSED = DATA_DIR / "odds_parsed.json"
RAW = DATA_DIR / "sporttery_raw.json"


def fetch_sporttery_raw() -> dict:
    req = urllib.request.Request(SPORTTERY_URL, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://static.sporttery.cn/",
        "Origin": "https://www.sporttery.cn",
        "sec-ch-ua": '"Chromium";v="120", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def parse_matches(match_info: list[dict]) -> list[dict]:
    matches = []
    for day in match_info:
        match_date = day.get("matchDate", "")
        for m in day.get("subMatchList", []):
            rec = {
                "home": m.get("homeTeamAllName", ""),
                "away": m.get("awayTeamAllName", ""),
                "home_en": m.get("homeTeamAbbEnName", ""),
                "away_en": m.get("awayTeamAbbEnName", ""),
                "date": m.get("matchDate", match_date),
                "time": m.get("matchTime", ""),
                "league": m.get("leagueAbbName", ""),
                "num": m.get("matchNumStr", ""),
            }
            had = m.get("had") or {}
            if had.get("h"):
                odds = {"h": float(had["h"]), "d": float(had["d"]), "a": float(had["a"])}
                rec["had_odds"] = odds
                rec["had_prob"] = _devig(odds)
                rec["had_update"] = f"{had.get('updateDate','')} {had.get('updateTime','')}"
            hhad = m.get("hhad") or {}
            if hhad.get("h"):
                rec["hhad_line"] = hhad.get("goalLineValue", "")
                odds = {"h": float(hhad["h"]), "d": float(hhad["d"]), "a": float(hhad["a"])}
                rec["hhad_odds"] = odds
                rec["hhad_prob"] = _devig(odds)
                rec["hhad_update"] = f"{hhad.get('updateDate','')} {hhad.get('updateTime','')}"
            ttg = m.get("ttg") or {}
            if ttg.get("s0"):
                rec["ttg_odds"] = {str(i): float(ttg[f"s{i}"]) for i in range(8) if ttg.get(f"s{i}")}
            matches.append(rec)
    return matches


def _devig(odds: dict) -> dict:
    inv = {k: 1.0 / v for k, v in odds.items() if v > 0}
    s = sum(inv.values())
    return {k: round(v / s, 5) for k, v in inv.items()} if s else {}


def refresh_cache() -> int:
    """Refresh cached odds data; return match count."""
    try:
        raw = fetch_sporttery_raw()
        matches = parse_matches(raw.get("value", {}).get("matchInfoList", []))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RAW.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        PARSED.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(matches)
    except Exception as e:
        print(f"  ⚠ Refresh failed: {e}", file=sys.stderr)
        return -1


class SportteryHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/odds.json":
            n = refresh_cache()
            if n <= 0:
                # Return cached if refresh failed
                if not PARSED.exists():
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"error":"no data"}')
                    return
            data = PARSED.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            info = {
                "ok": True,
                "parsed_exists": PARSED.exists(),
                "parsed_age": (time.time() - PARSED.stat().st_mtime) if PARSED.exists() else None,
                "raw_exists": RAW.exists(),
            }
            self.wfile.write(json.dumps(info).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}")


def main():
    port = 8765
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        port = int(sys.argv[i + 1])

    # Initial refresh
    print(f"[{time.strftime('%H:%M:%S')}] Initial refresh...")
    n = refresh_cache()
    print(f"  → {n} matches cached")

    # Background refresh every 30 min
    def _bg():
        while True:
            time.sleep(1800)  # 30 min
            try:
                n = refresh_cache()
                print(f"[{time.strftime('%H:%M:%S')}] background refresh: {n} matches")
            except Exception as e:
                print(f"  bg error: {e}")
    threading.Thread(target=_bg, daemon=True).start()

    print(f"[{time.strftime('%H:%M:%S')}] HTTP server on :{port}")
    print(f"  GET /odds.json - {len(PARSED.read_text()) if PARSED.exists() else 0} bytes")
    print(f"  GET /health")
    httpd = http.server.HTTPServer(("0.0.0.0", port), SportteryHandler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
