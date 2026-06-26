"""Compute group standings from real-time match results and derive motivation factors.

Inputs:
  data/groups_2026.json - group composition
  data/wc_results.json - completed match results [{home, away, home_goals, away_goals, date}, ...]
  data/upcoming_matches.json - upcoming matches to compute motivation for

Output:
  data/standings.json - current standings with motivation scores
"""
import json
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def load_json(p, default=None):
    if not p.exists():
        return default if default is not None else {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def compute_standings(groups, results):
    """Compute standings for each group from results.
    Returns: {group_letter: [{team, pts, gf, ga, gd, played, wins, draws, losses}, ...]}
    """
    standings = {l: {} for l in groups}
    for r in results:
        # 跳过未完成的比赛
        if r.get("scheduled") or r.get("home_goals") is None:
            continue
        # Find which group this match is in
        home_team = r["home"]
        away_team = r["away"]
        hg, ag = r["home_goals"], r["away_goals"]
        # Find the group
        grp_letter = None
        for letter, teams in groups.items():
            if home_team in teams and away_team in teams:
                grp_letter = letter
                break
        if grp_letter is None:
            continue
        # Init if not yet
        for t in [home_team, away_team]:
            if t not in standings[grp_letter]:
                standings[grp_letter][t] = {
                    "team": t, "pts": 0, "gf": 0, "ga": 0, "gd": 0,
                    "played": 0, "wins": 0, "draws": 0, "losses": 0
                }
        h = standings[grp_letter][home_team]
        a = standings[grp_letter][away_team]
        h["gf"] += hg; h["ga"] += ag; h["played"] += 1
        a["gf"] += ag; a["ga"] += hg; a["played"] += 1
        if hg > ag:
            h["pts"] += 3; h["wins"] += 1
            a["losses"] += 1
        elif hg < ag:
            a["pts"] += 3; a["wins"] += 1
            h["losses"] += 1
        else:
            h["pts"] += 1; a["pts"] += 1
            h["draws"] += 1; a["draws"] += 1
    # Compute GD
    for letter in standings:
        for t in standings[letter]:
            s = standings[letter][t]
            s["gd"] = s["gf"] - s["ga"]
    # Sort by pts, GD, GF
    sorted_standings = {}
    for letter, teams in standings.items():
        sorted_list = sorted(teams.values(), key=lambda s: (-s["pts"], -s["gd"], -s["gf"]))
        sorted_standings[letter] = sorted_list
    return sorted_standings


def compute_motivation(standings, groups=None, today_str=None):
    """For each team, compute motivation factor:
    - 1.00 = normal effort (group stage, mid-table)
    - 1.10 = must-win (close to qualification, group decider)
    - 0.90 = already qualified (may rotate)
    - 0.85 = eliminated
    - 0.95 = confirmed in knockouts (knockout stage)
    """
    if today_str is None:
        today_str = datetime.now().strftime("%Y-%m-%d")
    motivation = {}
    # First, ensure every team in every group has an entry (default 1.0)
    if groups:
        for letter, team_list in groups.items():
            for tname in team_list:
                if tname not in standings.get(letter, {}):
                    if letter not in standings:
                        standings[letter] = {}
                    standings[letter][tname] = {
                        "team": tname, "pts": 0, "gf": 0, "ga": 0, "gd": 0,
                        "played": 0, "wins": 0, "draws": 0, "losses": 0
                    }
    for letter, teams in standings.items():
        n = len(teams)
        if n == 0:
            continue
        # Sort by pts, GD, GF
        sorted_teams = sorted(teams.values(), key=lambda s: (-s["pts"], -s["gd"], -s["gf"]))
        # Compute games remaining (each team plays 3 group matches)
        for i, t in enumerate(sorted_teams):
            played = t["played"]
            remaining = 3 - played
            t["motivation"] = 1.0
            t["status"] = "fighting"
            t["group"] = letter
            t["rank"] = i + 1
            if remaining == 0:
                # Group stage finished
                if i < 2:
                    t["status"] = "qualified_top2"
                    t["motivation"] = 0.92  # 可能轮换主力
                elif i == 2 and t["pts"] >= 3:
                    t["status"] = "fighting_3rd"
                    t["motivation"] = 1.02  # 争夺8个最好第3名额
                else:
                    t["status"] = "eliminated"
                    t["motivation"] = 0.82
            else:
                # 还有比赛 - 战意判断
                pts = t["pts"]
                if i == 0 and pts >= 6:
                    # 已经基本锁定, 末战可能轮换
                    t["status"] = "near_qualified"
                    t["motivation"] = 0.94
                elif i >= 2 and pts < 3 and remaining == 1:
                    # 已被淘汰
                    t["status"] = "eliminated"
                    t["motivation"] = 0.82
                elif i == 1 and remaining == 1:
                    # 小组第2 末战
                    t["status"] = "fighting"
                    t["motivation"] = 1.05
                elif i == 0 and remaining == 1 and pts <= 3:
                    # 榜首末战 必拼
                    t["status"] = "must_win"
                    t["motivation"] = 1.08
                else:
                    t["status"] = "fighting"
                    t["motivation"] = 1.03
            motivation[t["team"]] = {
                "motivation": t["motivation"],
                "status": t["status"],
                "group": letter,
                "rank": i + 1,
                "pts": t["pts"],
                "played": t["played"],
                "remaining": remaining,
                "gd": t["gd"],
                "gf": t["gf"],
                "ga": t["ga"],
            }
    return motivation


def main():
    groups = load_json(DATA_DIR / "groups_2026.json", {})
    results_data = load_json(DATA_DIR / "wc_results.json", {"results": []})
    results = results_data.get("results", results_data) if isinstance(results_data, dict) else results_data
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"Computing standings as of {today_str}...")
    standings_dict = compute_standings(groups, results)
    print("\nStandings:")
    for letter in sorted(standings_dict):
        teams = standings_dict[letter]
        if teams:
            line = f"  {letter}: "
            for t in teams:
                line += f"{t['team']}({t['pts']}分, GD{t['gd']:+d}) "
            print(line)
    # Pass dict version (sorted list version breaks key access)
    standings_as_dict = {l: {t["team"]: t for t in teams} for l, teams in standings_dict.items()}
    motivation = compute_motivation(standings_as_dict, groups, today_str)
    save_json(DATA_DIR / "standings.json", {
        "as_of": today_str,
        "standings": standings_dict,
        "motivation": motivation,
    })
    print(f"\nMotivation factors (extremes):")
    items = sorted(motivation.items(), key=lambda kv: kv[1]["motivation"])
    print(f"  Lowest (rotation/dead rubber):")
    for t, m in items[:3]:
        print(f"    {t}: {m['motivation']:.2f} ({m['status']})")
    print(f"  Highest (must-win):")
    for t, m in items[-3:]:
        print(f"    {t}: {m['motivation']:.2f} ({m['status']})")
    print(f"\nSaved to {DATA_DIR / 'standings.json'}")


if __name__ == "__main__":
    main()
