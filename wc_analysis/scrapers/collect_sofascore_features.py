#!/usr/bin/env python3
"""
SofaScore 赛前特征采集器 — 世界杯参赛队近 N 场真实比赛统计聚合。

═══════════════════════════════════════════════════════════════════════════════
价值验证裁决背景(读我):
  价值验证报告对 40+ SofaScore 候选指标做了去共线裁决。核心结论:
    1. 现有模型已被去水体彩盘口锚定(log-pool w=0.6-0.7), 市场吸收了一切公开信息。
    2. Elo(整体实力) + Understat xG(进攻质量) 已占据主要信号维。
    3. 世界杯样本=72场, 每队每年~10场, 新管线(selenium→Cloudflare→中文映射)脆弱性本身负EV。
    4. `sofascore_cache/` 一行数据都没落盘 —— 候选≠已验证数据。

  因此本采集器**只采集与现有信号正交、且可能通过对抗审查的最少特征**:
    - def_xga90  : 近 N 场场均被预期进球 (xGA/90) —— 防守质量, Elo 是净值不区分攻防,
                   Understat 档案只有进攻端 attack_xg90 → 这是唯一正交的真实维度。
    - att_xg90   : 近 N 场场均预期进球 (xG/90)  —— 与 Understat 交叉校验, 不直接用于集成。

  采到的数据**不假设可用**: 集成端按 matches_found 门控, 缺/少数据队中性处理。
  这是"数据诚实"原则: 没数据的队不要用代理值假装。
═══════════════════════════════════════════════════════════════════════════════

用法:
  python3 wc_analysis/scrapers/collect_sofascore_features.py [--last N] [--headless]

输出:
  wc_analysis/data/sofascore_features.json
  {
    "_meta": {...采集元数据...},
    "<中文队名>": {
        "team_id": int, "sofa_name": str,
        "matches_found": int,           # 实际解析到 xG 的比赛数 (门控依据)
        "att_xg90": float | null,       # 近N场场均 xG
        "def_xga90": float | null,      # 近N场场均 被xG (防守质量, 主集成特征)
        "source": "sofascore_team_events"
    }, ...
  }
"""
from __future__ import annotations
import sys
import json
import time
import argparse
from pathlib import Path

# 让脚本可独立运行: 把 sofascore_client 所在目录加入 path
sys.path.insert(0, str(Path(__file__).parent))
from sofascore_client import SofaScoreClient  # noqa: E402

DATA_DIR = Path(__file__).parent.parent / "data"
GROUPS_FILE = DATA_DIR / "groups_2026.json"
OUT_FILE = DATA_DIR / "sofascore_features.json"

# 中文队名 ← → SofaScore 搜索英文名。
# SofaScore 国家队命名通常用通用英文国名; 个别国家有官方变体, 列在 aliases。
# (key = 中文, 与 predict.py TEAM_DB 对齐; 缺映射的队 → 跳过, 不臆造)
TEAM_MAP = {
    "荷兰": ["Netherlands"], "瑞典": ["Sweden"], "德国": ["Germany"],
    "科特迪瓦": ["Ivory Coast", "Cote d'Ivoire"], "厄瓜多尔": ["Ecuador"],
    "库拉索": ["Curacao", "Curaçao"], "突尼斯": ["Tunisia"], "日本": ["Japan"],
    "西班牙": ["Spain"], "沙特阿拉伯": ["Saudi Arabia"], "比利时": ["Belgium"],
    "伊朗": ["Iran"], "乌拉圭": ["Uruguay"], "佛得角": ["Cape Verde", "Cabo Verde"],
    "新西兰": ["New Zealand"], "埃及": ["Egypt"], "阿根廷": ["Argentina"],
    "奥地利": ["Austria"], "法国": ["France"], "伊拉克": ["Iraq"],
    "挪威": ["Norway"], "塞内加尔": ["Senegal"], "约旦": ["Jordan"],
    "阿尔及利亚": ["Algeria"], "英格兰": ["England"], "克罗地亚": ["Croatia"],
    "葡萄牙": ["Portugal"], "哥伦比亚": ["Colombia"], "巴西": ["Brazil"],
    "摩洛哥": ["Morocco"], "加纳": ["Ghana"], "巴拿马": ["Panama"],
    "韩国": ["South Korea", "Korea Republic"], "墨西哥": ["Mexico"],
    "美国": ["United States", "USA"], "加拿大": ["Canada"],
    "澳大利亚": ["Australia"], "瑞士": ["Switzerland"],
    "乌兹别克斯坦": ["Uzbekistan"], "刚果(金)": ["DR Congo", "Congo DR"],
    "南非": ["South Africa"], "卡塔尔": ["Qatar"], "捷克": ["Czechia", "Czech Republic"],
    "波黑": ["Bosnia and Herzegovina", "Bosnia & Herzegovina"], "海地": ["Haiti"],
    "苏格兰": ["Scotland"], "土耳其": ["Turkey", "Türkiye"], "巴拉圭": ["Paraguay"],
    "委内瑞拉": ["Venezuela"], "秘鲁": ["Peru"], "智利": ["Chile"],
    "玻利维亚": ["Bolivia"],
}


def _to_float(v):
    """SofaScore xG 字段可能是 '1.23' 字符串或 float; 非法 → None."""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f >= 0 else None
    except (ValueError, TypeError):
        return None


def search_team_id(client: SofaScoreClient, names: list[str]) -> tuple[int | None, str | None]:
    """用 SofaScore 搜索端点找国家队 team id。只接受 type=team 且 sport=football。"""
    for name in names:
        from urllib.parse import quote
        d = client._get(f"search/all?q={quote(name)}")
        if not d:
            continue
        for res in d.get("results", []):
            if res.get("type") != "team":
                continue
            entity = res.get("entity", {})
            sport = (entity.get("sport") or {}).get("name", "")
            # 国家队: national == True 优先
            if sport == "Football" and entity.get("id"):
                if entity.get("national") or name.lower() in (entity.get("name", "")).lower():
                    return entity["id"], entity.get("name")
    return None, None


def collect_team_xg(client: SofaScoreClient, team_id: int, last_n: int) -> dict:
    """拉近 last_n 场已完赛比赛, 从 statistics 端点解析每场 xG / xGA, 聚合 per90。"""
    att_vals, def_vals = [], []
    pages_needed = (last_n // 30) + 1  # team events 每页 ~30 场
    events = []
    for page in range(pages_needed):
        evs = client.team_events(team_id, page=page)
        if not evs:
            break
        events.extend(evs)
        if len(events) >= last_n:
            break
    # 只取已完赛 (status code 100 = ended)
    finished = [e for e in events if (e.get("status") or {}).get("code") == 100]
    finished = finished[-last_n:] if len(finished) > last_n else finished

    for ev in finished:
        ev_id = ev.get("id")
        if not ev_id:
            continue
        home_id = (ev.get("homeTeam") or {}).get("id")
        is_home = (home_id == team_id)
        stats = client.event_statistics(ev_id)
        if not stats or not stats.get("statistics"):
            continue
        # 找 "Expected goals" 指标 (ALL period)
        xg_home = xg_away = None
        for period in stats["statistics"]:
            if period.get("period") != "ALL":
                continue
            for group in period.get("groups", []):
                for item in group.get("statisticsItems", []):
                    nm = (item.get("name") or "").lower()
                    if "expected goals" in nm or item.get("key") == "expectedGoals":
                        xg_home = _to_float(item.get("homeValue", item.get("home")))
                        xg_away = _to_float(item.get("awayValue", item.get("away")))
        if xg_home is None or xg_away is None:
            continue
        own_xg = xg_home if is_home else xg_away
        opp_xg = xg_away if is_home else xg_home
        if own_xg is not None:
            att_vals.append(own_xg)
        if opp_xg is not None:
            def_vals.append(opp_xg)

    matches_found = min(len(att_vals), len(def_vals))
    return {
        "matches_found": matches_found,
        "att_xg90": round(sum(att_vals) / len(att_vals), 4) if att_vals else None,
        "def_xga90": round(sum(def_vals) / len(def_vals), 4) if def_vals else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=10, help="聚合近N场 (默认10)")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--no-headless", dest="headless", action="store_false")
    ap.add_argument("--limit", type=int, default=0, help="只采前N队(调试用), 0=全部")
    args = ap.parse_args()

    # 参赛队 = groups_2026 里所有英文名 → 反查中文 (经 TEAM_MAP 的中文 key)
    teams_cn = list(TEAM_MAP.keys())
    if args.limit:
        teams_cn = teams_cn[: args.limit]

    out = {
        "_meta": {
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_n": args.last,
            "source": "sofascore",
            "features": ["att_xg90", "def_xga90"],
            "note": "matches_found 为门控依据; 缺数据队集成端中性处理(不惩罚不加成)",
        }
    }

    teams_with_data = 0
    teams_with_id = 0
    try:
        with SofaScoreClient(headless=args.headless) as client:
            for cn in teams_cn:
                names = TEAM_MAP[cn]
                print(f"[{cn}] 搜索 team id ...", flush=True)
                tid, sofa_name = search_team_id(client, names)
                if not tid:
                    print(f"  ✗ {cn}: 未找到 SofaScore team id", flush=True)
                    out[cn] = {"matches_found": 0, "att_xg90": None,
                               "def_xga90": None, "team_id": None,
                               "source": "sofascore_team_events"}
                    continue
                teams_with_id += 1
                feats = collect_team_xg(client, tid, args.last)
                feats.update({"team_id": tid, "sofa_name": sofa_name,
                              "source": "sofascore_team_events"})
                out[cn] = feats
                if feats["matches_found"] >= 1:
                    teams_with_data += 1
                print(f"  ✓ {cn} (id={tid}, {sofa_name}): "
                      f"matches={feats['matches_found']}, "
                      f"xg90={feats['att_xg90']}, xga90={feats['def_xga90']}", flush=True)
                # 中途落盘, 防中断丢全部
                OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    except Exception as e:
        print(f"!! 采集中断: {type(e).__name__}: {e}", flush=True)

    out["_meta"]["teams_total"] = len(teams_cn)
    out["_meta"]["teams_with_id"] = teams_with_id
    out["_meta"]["teams_with_data"] = teams_with_data
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成: {teams_with_data}/{len(teams_cn)} 队采到 xG 数据 → {OUT_FILE}")


if __name__ == "__main__":
    main()
