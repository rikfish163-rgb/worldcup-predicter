"""
Fetch xG profiles for 2026 World Cup national team core players.

Uses soccerdata's Understat scraper to pull npxG/90 and xA/90 from
the 2024-25 club season, then aggregates per national team.

Output: wc_analysis/data/xg_profiles.json
"""

import json
import sys
import time
from pathlib import Path
from statistics import median

import soccerdata as sd

# === National team rosters: Chinese name -> list of key attacking players ===
# Only players likely in Understat (top-5 European leagues).
TEAM_ROSTERS = {
    "法国": [
        "Kylian Mbappe-Lottin", "Antoine Griezmann", "Ousmane Dembélé",
        "Bradley Barcola", "Randal Kolo Muani",
    ],
    "葡萄牙": [
        "Rafael Leão", "Bruno Fernandes", "Bernardo Silva",
        "Pedro Neto", "Diogo Jota",
    ],
    "英格兰": [
        "Bukayo Saka", "Phil Foden", "Cole Palmer",
        "Jude Bellingham", "Anthony Gordon",
    ],
    "巴西": [
        "Vinícius Júnior", "Rodrygo", "Raphinha",
        "Sávio", "Bruno Guimarães",
    ],
    "克罗地亚": [
        "Luka Modric", "Mateo Kovacic", "Andrej Kramaric",
        "Lovro Majer", "Igor Matanovic",
    ],
    "哥伦比亚": [
        "Luis Díaz", "Jhon Arias", "James Rodriguez",
        "Jhon Duran", "Luis Sinisterra",
    ],
    "摩洛哥": [
        "Achraf Hakimi", "Hakim Ziyech", "Brahim Diaz",
        "Youssef En-Nesyri", "Azzedine Ounahi",
    ],
    "瑞士": [
        "Granit Xhaka", "Xherdan Shaqiri", "Ruben Vargas",
        "Breel Embolo", "Dan Ndoye",
    ],
    "韩国": [
        "Son Heung-Min", "Hwang Hee-Chan", "Lee Kang-In",
        "Bae Jun-Ho", "Jeong Woo-Yeong",
    ],
    "墨西哥": [
        "Edson Álvarez", "Santiago Giménez", "Hirving Lozano",
        "Raul Jimenez", "Diego Lainez",
    ],
    "挪威": [
        "Erling Haaland", "Martin Odegaard", "Alexander Sørloth",
        "Antonio Nusa", "Oscar Bobb",
    ],
    "加拿大": [
        "Jonathan Christian David", "Alphonso Davies", "Tajon Buchanan",
        "Cyle Larin", "Ismaël Koné",
    ],
    "苏格兰": [
        "Scott McTominay", "John McGinn", "Andrew Robertson",
        "Billy Gilmour", "Che Adams",
    ],
    "捷克": [
        "Patrik Schick", "Adam Hlozek", "Antonin Barak",
        "Mojmir Chytil", "Vaclav Cerny",
    ],
    "波黑": [
        "Edin Dzeko", "Rade Krunic", "Benjamin Tahirovic",
        "Ermedin Demirovic", "Sead Kolasinac",
    ],
    "约旦": [
        # Most players not in top-5 leagues
        "Mousa Al-Taamari", "Yazan Al-Naimat",
    ],
    "阿尔及利亚": [
        "Said Benrahma", "Riyad Mahrez", "Amine Gouiri",
        "Adam Ounas", "Ismael Bennacer",
    ],
    "乌兹别克斯坦": [
        # Few players in top-5 leagues
        "Eldor Shomurodov", "Abduqodir Khusanov",
    ],
    "加纳": [
        "Mohammed Kudus", "Thomas Partey", "Antoine Semenyo",
        "Jordan Ayew", "Iñaki Williams",
    ],
    "巴拿马": [
        # Very few in top-5 leagues
        "Adalberto Carrasquilla",
    ],
    "刚果金": [
        "Chancel Mbemba", "Yoane Wissa", "Fiston Mayele",
        "Arthur Masuaku", "Samuel Moutoussamy",
    ],
    "卡塔尔": [
        # No players in top-5 European leagues
    ],
    "海地": [
        # Few players in top-5 leagues
        "Frantzdy Pierrot", "Derrick Etienne",
    ],
    "南非": [
        # Few players in top-5 leagues
        "Percy Tau",
    ],
}

# All Understat leagues
ALL_LEAGUES = [
    "ENG-Premier League",
    "ESP-La Liga",
    "GER-Bundesliga",
    "ITA-Serie A",
    "FRA-Ligue 1",
]

SEASON = "2425"
MIN_MINUTES = 450  # At least 5 full matches to count


def load_all_player_stats() -> "pd.DataFrame":
    """Load player season stats from all 5 leagues."""
    import pandas as pd

    frames = []
    for league in ALL_LEAGUES:
        print(f"  Fetching {league} {SEASON}...")
        try:
            us = sd.Understat(leagues=league, seasons=SEASON)
            df = us.read_player_season_stats(force_cache=True)
            frames.append(df)
            time.sleep(1)  # polite rate limiting
        except Exception as e:
            print(f"  WARNING: Failed to fetch {league}: {e}")
            continue

    if not frames:
        print("ERROR: Could not fetch any league data.")
        sys.exit(1)

    combined = pd.concat(frames)
    # Reset multi-index for easier manipulation
    combined = combined.reset_index()
    return combined


def _normalize(name: str) -> str:
    """Normalize accented characters for comparison."""
    import unicodedata
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode()


def find_player(df, player_name: str) -> dict | None:
    """
    Find a player by name in the combined dataframe.
    Returns dict with npxg90, xa90, minutes or None if not found.
    Uses strict matching to avoid false positives.
    """
    # Strategy 1: Exact match
    mask = df["player"] == player_name
    if not mask.any():
        # Strategy 2: Case-insensitive exact match (handles accent differences)
        norm_target = _normalize(player_name).lower()
        norm_col = df["player"].apply(lambda x: _normalize(x).lower())
        mask = norm_col == norm_target

    if not mask.any():
        # Strategy 3: Full name contained in player field or vice versa
        mask = df["player"].str.contains(player_name, case=False, na=False, regex=False)
        if not mask.any():
            # Also try normalized version
            norm_target = _normalize(player_name)
            mask = df["player"].apply(lambda x: _normalize(x)).str.contains(
                norm_target, case=False, na=False, regex=False
            )

    if not mask.any():
        # Strategy 4: Multi-token match - require ALL tokens of the search name
        # to appear in the player name (to avoid "Pol Lozano" for "Hirving Lozano")
        tokens = _normalize(player_name).lower().split()
        if len(tokens) >= 2:
            def matches_all_tokens(player):
                p = _normalize(player).lower()
                return all(t in p for t in tokens)
            mask = df["player"].apply(matches_all_tokens)

    if not mask.any():
        return None

    # Take the row with most minutes if multiple matches
    matches = df[mask].copy()
    if matches.empty:
        return None

    row = matches.sort_values("minutes", ascending=False).iloc[0]
    minutes = row["minutes"]
    if minutes < MIN_MINUTES:
        return None

    npxg90 = round((row["np_xg"] / minutes) * 90, 3)
    xa90 = round((row["xa"] / minutes) * 90, 3)

    return {
        "name": row["player"],
        "minutes": int(minutes),
        "npxg90": npxg90,
        "xa90": xa90,
    }


def main():
    print("=== Fetch xG Profiles for 2026 World Cup Teams ===\n")

    # Step 1: Load all player stats
    print("[1/3] Loading player stats from Understat (5 leagues)...")
    df = load_all_player_stats()
    print(f"  Total player-season records: {len(df)}\n")

    # Step 2: Match players to national teams
    print("[2/3] Matching players to national team rosters...")
    results = {}

    for team_cn, roster in TEAM_ROSTERS.items():
        if not roster:
            results[team_cn] = {
                "attack_xg90": None,
                "creative_xa90": None,
                "players_found": 0,
                "players_missing": roster if roster else ["(no players listed)"],
            }
            continue

        found_players = []
        missing_players = []

        for player_name in roster:
            info = find_player(df, player_name)
            if info:
                found_players.append(info)
            else:
                missing_players.append(player_name)

        if found_players:
            npxg_values = [p["npxg90"] for p in found_players]
            xa_values = [p["xa90"] for p in found_players]
            attack_score = round(median(npxg_values), 3)
            creative_score = round(median(xa_values), 3)
        else:
            attack_score = None
            creative_score = None

        results[team_cn] = {
            "attack_xg90": attack_score,
            "creative_xa90": creative_score,
            "players_found": len(found_players),
            "players_missing": missing_players,
        }

        found_names = [p["name"] for p in found_players]
        print(f"  {team_cn}: {len(found_players)} found, {len(missing_players)} missing")
        if found_players:
            print(f"    Found: {', '.join(found_names)}")
        if missing_players:
            print(f"    Missing: {', '.join(missing_players)}")

    # Step 3: Save output
    print("\n[3/3] Saving results...")
    output_dir = Path(__file__).parent / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "xg_profiles.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"  Saved to: {output_path}")

    # Summary
    print("\n=== Summary ===")
    ranked = sorted(
        [(t, d) for t, d in results.items() if d["attack_xg90"] is not None],
        key=lambda x: x[1]["attack_xg90"],
        reverse=True,
    )
    print("\nTop 10 by attack xG/90:")
    for i, (team, data) in enumerate(ranked[:10], 1):
        print(f"  {i}. {team}: attack={data['attack_xg90']}, creative={data['creative_xa90']}")


if __name__ == "__main__":
    main()
