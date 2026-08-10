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
from datetime import datetime, timezone
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
    """SofaScore xG 字段可能是 '1.23' 字符串或 float; 非法 → None。
    注: 仅用于 xG/xGA(非负量)。门将 Goals prevented 可为负, 用 _to_signed_float。"""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f >= 0 else None
    except (ValueError, TypeError):
        return None


def _to_signed_float(v):
    """门将 'Goals prevented' = post-shot xG(xGOT) - 实际失球, 可正可负。
    不做 >=0 过滤, 否则会丢掉所有"扑救净贡献为负(失球比 xGOT 多)"的真实样本。"""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v):
    """门将 Total/Big saves 为整数计数; 非法 → None。"""
    if v is None:
        return None
    try:
        return int(v)
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
    """拉近 last_n 场已完赛比赛, 从 statistics 端点解析每场 xG / xGA / 门将 / 纪律, 聚合 per90;
    并从 team_events 的 startTimestamp 计算休息天数(纯赛程, 不需 statistics)。"""
    att_vals, def_vals = [], []
    gk_gp_vals = []        # 门将 Goals prevented (扑救净贡献, 可负; 诊断: 仅世界杯正赛产出, 稀疏)
    gk_saves_vals = []     # Total saves (整数计数; 每场都有, 满覆盖)
    gk_bigsaves_vals = []  # Big saves (整数计数; 每场都有)
    # 纪律风险: 红牌→少打一人→λ冲击; 黄牌/犯规反映纪律倾向
    disc_red_vals = []     # 本场本队红牌数
    disc_yellow_vals = []  # 本场本队黄牌数
    disc_fouls_vals = []   # 本场本队犯规数
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

    # ─── 休息天数 (538 官方泊松参数, 纯赛程数据, 不依赖 statistics) ───
    # 取最近一场已完赛距今天数。覆盖率应 100%(只要有任一已完赛事件)。
    rest_days = None
    last_match_ts = None
    if finished:
        ts_list = [e.get("startTimestamp") for e in finished
                   if isinstance(e.get("startTimestamp"), (int, float))]
        if ts_list:
            last_match_ts = max(ts_list)
            now = datetime.now(timezone.utc).timestamp()
            rd = (now - last_match_ts) / 86400.0
            # 防御: 若赛程时间戳在未来(数据异常/时钟差), 置 0 不取负
            rest_days = round(max(rd, 0.0), 1)

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
        # 一次遍历 ALL period, 同时取 xG / Goalkeeping / 纪律(Match overview) 组指标
        xg_home = xg_away = None
        gp_home = gp_away = None          # Goals prevented
        sv_home = sv_away = None          # Total saves
        bs_home = bs_away = None          # Big saves
        red_home = red_away = None        # Red cards
        yel_home = yel_away = None        # Yellow cards
        foul_home = foul_away = None      # Fouls
        for period in stats["statistics"]:
            if period.get("period") != "ALL":
                continue
            for group in period.get("groups", []):
                grp_name = group.get("groupName")
                for item in group.get("statisticsItems", []):
                    nm = (item.get("name") or "").lower()
                    key = item.get("key")
                    hv = item.get("homeValue", item.get("home"))
                    av = item.get("awayValue", item.get("away"))
                    if "expected goals" in nm or key == "expectedGoals":
                        xg_home = _to_float(hv)
                        xg_away = _to_float(av)
                    # 纪律: 卡牌/犯规在 "Match overview" 组, 用稳定 key 匹配防同名
                    elif key == "redCards":
                        red_home = _to_int(hv)
                        red_away = _to_int(av)
                    elif key == "yellowCards":
                        yel_home = _to_int(hv)
                        yel_away = _to_int(av)
                    elif key == "fouls":
                        foul_home = _to_int(hv)
                        foul_away = _to_int(av)
                    # 门将指标限定在 Goalkeeping 组, 避免 "Match overview" 同名重复
                    elif grp_name == "Goalkeeping":
                        if nm == "goals prevented" or key == "goalsPrevented":
                            gp_home = _to_signed_float(hv)
                            gp_away = _to_signed_float(av)
                        elif nm == "total saves":
                            sv_home = _to_int(hv)
                            sv_away = _to_int(av)
                        elif nm == "big saves":
                            bs_home = _to_int(hv)
                            bs_away = _to_int(av)

        # xG/xGA: 两侧都要有才计入(与原逻辑一致)
        if xg_home is not None and xg_away is not None:
            own_xg = xg_home if is_home else xg_away
            opp_xg = xg_away if is_home else xg_home
            att_vals.append(own_xg)
            def_vals.append(opp_xg)

        # 门将 Goals prevented: 本队门将的扑救净贡献(本场单值即可计入, 独立门控)
        own_gp = gp_home if is_home else gp_away
        if own_gp is not None:
            gk_gp_vals.append(own_gp)
        own_sv = sv_home if is_home else sv_away
        if own_sv is not None:
            gk_saves_vals.append(own_sv)
        own_bs = bs_home if is_home else bs_away
        if own_bs is not None:
            gk_bigsaves_vals.append(own_bs)

        # 纪律: 本队这一侧的红/黄/犯规。
        # 关键数据语义(实采诊断坐实): SofaScore 在两队该项均为 0 时**整行省略**
        #   redCards/yellowCards row, 而 fouls 行几乎每场都在。
        #   若按"行缺失=无数据"会把红牌均值算成 1.0(只数有红牌的场)→ 严重上偏假数据。
        # 正确处理: 以 fouls 行存在 = 本场 Match overview 统计有效(已被解析)为锚,
        #   此时缺失的 red/yellow 行真实含义是 **0 张**, 而非缺数据 → 补 0。
        own_foul = foul_home if is_home else foul_away
        if own_foul is not None:
            # 本场统计有效 → 红/黄缺行即真实 0
            disc_fouls_vals.append(own_foul)
            own_red = red_home if is_home else red_away
            disc_red_vals.append(own_red if own_red is not None else 0)
            own_yel = yel_home if is_home else yel_away
            disc_yellow_vals.append(own_yel if own_yel is not None else 0)

    matches_found = min(len(att_vals), len(def_vals))
    return {
        "matches_found": matches_found,
        "att_xg90": round(sum(att_vals) / len(att_vals), 4) if att_vals else None,
        "def_xga90": round(sum(def_vals) / len(def_vals), 4) if def_vals else None,
        # 门将 shot-stopping: 场均 Goals prevented (xGOT - 失球), 正=超水平扑救
        # 诊断结论: Goals prevented 仅世界杯正赛产出, 永远凑不满5场 → 不作主门控特征。
        # 降级用 gk_saves_pg / gk_big_saves_pg (每场都有, 满覆盖)做门将活跃度代理。
        "gk_matches": len(gk_gp_vals),
        "gk_goals_prevented": (round(sum(gk_gp_vals) / len(gk_gp_vals), 4)
                               if gk_gp_vals else None),
        "gk_saves_matches": len(gk_saves_vals),
        "gk_saves_pg": (round(sum(gk_saves_vals) / len(gk_saves_vals), 4)
                        if gk_saves_vals else None),
        "gk_big_saves_pg": (round(sum(gk_bigsaves_vals) / len(gk_bigsaves_vals), 4)
                            if gk_bigsaves_vals else None),
        # 纪律风险: 场均红牌/黄牌/犯规。红牌→少打一人→对手 λ 上修, 本队 λ 下修。
        # discipline_matches = 有效统计场数(以 fouls 行存在为锚), red/yellow/fouls 同样本。
        # 红/黄缺行已补 0(SofaScore 零卡省略整行), 故均值无上偏。
        "discipline_matches": len(disc_red_vals),
        "red_cards_pg": (round(sum(disc_red_vals) / len(disc_red_vals), 4)
                         if disc_red_vals else None),
        "yellow_cards_pg": (round(sum(disc_yellow_vals) / len(disc_yellow_vals), 4)
                            if disc_yellow_vals else None),
        "fouls_pg": (round(sum(disc_fouls_vals) / len(disc_fouls_vals), 4)
                     if disc_fouls_vals else None),
        # 休息天数 (538 泊松参数): 最近一场已完赛距今天数。纯赛程, 覆盖率≈100%。
        "rest_days": rest_days,
        "last_match_ts": last_match_ts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=10, help="聚合近N场 (默认10)")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--no-headless", dest="headless", action="store_false")
    ap.add_argument("--limit", type=int, default=0, help="从 offset 起只采 N 队(分批用), 0=到末尾")
    ap.add_argument("--offset", type=int, default=0, help="从第 offset 队开始(分批断点续采)")
    ap.add_argument("--rate", type=float, default=2.5,
                    help="请求间隔秒 (Cloudflare 友好, 默认 2.5; 过快会被拦截)")
    args = ap.parse_args()

    # 参赛队 = groups_2026 里所有英文名 → 反查中文 (经 TEAM_MAP 的中文 key)
    all_teams_cn = list(TEAM_MAP.keys())
    teams_cn = all_teams_cn[args.offset:]
    if args.limit:
        teams_cn = teams_cn[: args.limit]

    # 合并式落盘: 从已有文件 seed, 保留先前已采队 + 本批新增/更新。
    # SofaScore UC 会话在持续高频请求下会被 Cloudflare 标记, 故分批(fresh session)采集,
    # 每批只覆盖本批队, 其余队保持不动 —— 断点续采 + 崩溃安全。
    out = {}
    if OUT_FILE.exists():
        try:
            out = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            out = {}
    out["_meta"] = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_n": args.last,
        "source": "sofascore",
        "features": ["att_xg90", "def_xga90",
                     "gk_goals_prevented", "gk_saves_pg", "gk_big_saves_pg",
                     "red_cards_pg", "yellow_cards_pg", "fouls_pg", "rest_days"],
        "note": ("matches_found 为 xG 门控; discipline_matches 为纪律门控; "
                 "gk_matches 为 Goals prevented 门控(诊断: 仅世界杯正赛产出, 永远<5场, "
                 "已弃作主门控; gk_saves_pg/gk_big_saves_pg 满覆盖可作活跃度代理, "
                 "门控见 gk_saves_matches)。"
                 "缺数据队集成端中性处理(不惩罚不加成)。"
                 "red/yellow/fouls_pg = 近N场场均红牌/黄牌/犯规(红牌→少打一人→λ冲击)。"
                 "rest_days = 最近一场已完赛距采集时刻天数(538 泊松参数, 纯赛程, 覆盖率≈100%)。"),
    }

    # team_id 缓存: SofaScore search/all 端点常被 Cloudflare 间歇拦截, 而 team_id
    # 是稳定标识。复用上次成功落盘的 team_id, 跳过脆弱的搜索步骤(同范式, 仅更鲁棒)。
    id_cache = {}
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            for k, v in prev.items():
                if k != "_meta" and isinstance(v, dict) and v.get("team_id"):
                    id_cache[k] = (v["team_id"], v.get("sofa_name"))
        except Exception:
            pass

    teams_with_data = 0
    teams_with_id = 0
    try:
        with SofaScoreClient(headless=args.headless, rate_limit=args.rate) as client:
            for cn in teams_cn:
                names = TEAM_MAP[cn]
                if cn in id_cache:
                    tid, sofa_name = id_cache[cn]
                    print(f"[{cn}] 复用缓存 team id={tid} ...", flush=True)
                else:
                    print(f"[{cn}] 搜索 team id ...", flush=True)
                    tid, sofa_name = search_team_id(client, names)
                if not tid:
                    print(f"  ✗ {cn}: 未找到 SofaScore team id", flush=True)
                    out[cn] = {"matches_found": 0, "att_xg90": None,
                               "def_xga90": None,
                               "gk_matches": 0, "gk_goals_prevented": None,
                               "gk_saves_matches": 0, "gk_saves_pg": None,
                               "gk_big_saves_pg": None,
                               "discipline_matches": 0, "red_cards_pg": None,
                               "yellow_cards_pg": None, "fouls_pg": None,
                               "rest_days": None, "last_match_ts": None,
                               "team_id": None,
                               "source": "sofascore_team_events"}
                    continue
                teams_with_id += 1
                feats = collect_team_xg(client, tid, args.last)
                feats.update({"team_id": tid, "sofa_name": sofa_name,
                              "source": "sofascore_team_events"})
                # 合并守卫: 若本批被 Cloudflare 拦截(所有信号都=0/None)而旧记录有数据,
                # 保留旧记录, 不用空结果覆盖真实数据。
                # 注: rest_days 仅需 team_events(不需 statistics), 故纳入"本批有效"判定。
                prev_rec = out.get(cn) or {}
                prev_score = ((prev_rec.get("matches_found") or 0)
                              + (prev_rec.get("gk_matches") or 0)
                              + (prev_rec.get("discipline_matches") or 0)
                              + (1 if prev_rec.get("rest_days") is not None else 0))
                new_score = (feats["matches_found"] + feats["gk_matches"]
                             + feats["discipline_matches"]
                             + (1 if feats["rest_days"] is not None else 0))
                if new_score == 0 and prev_score > 0:
                    print(f"  ⊘ {cn}: 本批空(可能被拦截), 保留旧记录 "
                          f"(matches={prev_rec.get('matches_found')}, "
                          f"gk_m={prev_rec.get('gk_matches')}, "
                          f"disc={prev_rec.get('discipline_matches')})", flush=True)
                    continue
                out[cn] = feats
                if feats["matches_found"] >= 1:
                    teams_with_data += 1
                print(f"  ✓ {cn} (id={tid}, {sofa_name}): "
                      f"matches={feats['matches_found']}, "
                      f"xga90={feats['def_xga90']}, "
                      f"disc_m={feats['discipline_matches']}, "
                      f"red={feats['red_cards_pg']}, yel={feats['yellow_cards_pg']}, "
                      f"foul={feats['fouls_pg']}, rest_d={feats['rest_days']}", flush=True)
                # 中途落盘, 防中断丢全部
                OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    except Exception as e:
        print(f"!! 采集中断: {type(e).__name__}: {e}", flush=True)

    # 全量统计(基于合并后的完整 out, 跨批累计, 而非仅本批)
    recs = [(k, v) for k, v in out.items() if k != "_meta" and isinstance(v, dict)]
    out["_meta"]["teams_total"] = len(all_teams_cn)
    out["_meta"]["teams_with_id"] = sum(1 for _, v in recs if v.get("team_id"))
    out["_meta"]["teams_with_data"] = sum(1 for _, v in recs if (v.get("matches_found") or 0) >= 1)
    out["_meta"]["xg_gate_pass"] = sum(1 for _, v in recs if (v.get("matches_found") or 0) >= 5)
    # 门将 Goals prevented 门控统计 (≥5 场为通过门控, 与 xG 同范式)
    gk_any = sum(1 for _, v in recs if (v.get("gk_matches") or 0) >= 1)
    gk_gated = sum(1 for _, v in recs if (v.get("gk_matches") or 0) >= 5)
    out["_meta"]["teams_with_gk_data"] = gk_any
    out["_meta"]["teams_gk_gate_pass"] = gk_gated
    # 门将活跃度代理 (Total saves, 满覆盖, ≥5场门控)
    gk_sv_any = sum(1 for _, v in recs if (v.get("gk_saves_matches") or 0) >= 1)
    gk_sv_gated = sum(1 for _, v in recs if (v.get("gk_saves_matches") or 0) >= 5)
    out["_meta"]["teams_with_gk_saves"] = gk_sv_any
    out["_meta"]["teams_gk_saves_gate_pass"] = gk_sv_gated
    # 纪律门控 (≥5 场)
    disc_any = sum(1 for _, v in recs if (v.get("discipline_matches") or 0) >= 1)
    disc_gated = sum(1 for _, v in recs if (v.get("discipline_matches") or 0) >= 5)
    out["_meta"]["teams_with_discipline"] = disc_any
    out["_meta"]["teams_discipline_gate_pass"] = disc_gated
    # 休息天数覆盖 (应≈100%)
    rest_any = sum(1 for _, v in recs if v.get("rest_days") is not None)
    out["_meta"]["teams_with_rest_days"] = rest_any
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n本批完成 (offset={args.offset}, {len(teams_cn)} 队)。"
          f"全量: gk_gp {gk_any}队(≥5门控 {gk_gated}); "
          f"gk_saves {gk_sv_any}队(≥5门控 {gk_sv_gated}); "
          f"纪律 {disc_any}队(≥5门控 {disc_gated}); "
          f"rest_days {rest_any}队 → {OUT_FILE}")


if __name__ == "__main__":
    main()
