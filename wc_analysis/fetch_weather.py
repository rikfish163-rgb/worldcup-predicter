#!/usr/bin/env python3
"""抓 4 个场馆赛时天气 (open-meteo forecast, 无需 key)。

四场北京时间 6/21 01:00~12:00, 换算美加墨当地均为 6/20 白天/傍晚。
取各场馆当地比赛时段的小时级预报。
"""
import json, urllib.request
from pathlib import Path
DATA_DIR = Path(__file__).parent / "data"

# (lat, lon, 当地比赛日, 当地大致开赛小时, 球场类型)
VENUES = {
    "荷兰vs瑞典":   (29.6847, -95.4107, "2026-06-20", 12, "NRG/Reliant 休斯顿·可开合顶+空调"),
    "德国vs科特迪瓦": (43.6332, -79.4185, "2026-06-20", 16, "BMO Field 多伦多·露天"),
    "厄瓜多尔vs库拉索": (39.0489, -94.4839, "2026-06-20", 19, "Arrowhead 堪萨斯城·露天"),
    "突尼斯vs日本":  (25.6691, -100.2444, "2026-06-20", 22, "BBVA 蒙特雷·露天"),
}
HDR = {"User-Agent": "Mozilla/5.0"}

def main():
    out = {}
    for name, (lat, lon, date, hour, stadium) in VENUES.items():
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               f"&hourly=temperature_2m,relative_humidity_2m,precipitation_probability,precipitation,wind_speed_10m"
               f"&start_date={date}&end_date={date}&timezone=auto")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=25) as r:
                d = json.loads(r.read())
            h = d["hourly"]
            # 取开赛 hour 及之后2小时
            idxs = [i for i in range(len(h["time"])) if hour <= int(h["time"][i][11:13]) <= hour+2]
            if not idxs:
                idxs = list(range(max(0,hour), min(len(h["time"]), hour+3)))
            rec = {
                "stadium": stadium,
                "kickoff_local": f"{date} {hour}:00",
                "temp_c": [h["temperature_2m"][i] for i in idxs],
                "humidity": [h["relative_humidity_2m"][i] for i in idxs],
                "precip_prob": [h["precipitation_probability"][i] for i in idxs],
                "precip_mm": [h["precipitation"][i] for i in idxs],
                "wind_kmh": [h["wind_speed_10m"][i] for i in idxs],
            }
            out[name] = rec
            t = rec["temp_c"]; pp = rec["precip_prob"]; wd = rec["wind_kmh"]; hm=rec["humidity"]
            print(f"{name}: 温{min(t):.0f}-{max(t):.0f}°C 湿{sum(hm)/len(hm):.0f}% 降水概率{max(pp)}% 风{max(wd):.0f}km/h | {stadium}")
        except Exception as e:
            out[name] = {"error": str(e)[:100], "stadium": stadium}
            print(f"{name}: ERR {type(e).__name__} {str(e)[:80]}")
    (DATA_DIR / "weather.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已存 weather.json")

if __name__ == "__main__":
    main()
