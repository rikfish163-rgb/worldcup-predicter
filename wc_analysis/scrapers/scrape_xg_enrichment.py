"""
Scrape xG / team-strength data for feature enrichment using TLS-fingerprint
requests (NOT Selenium, which is CAPTCHA'd on FBref).

Sources attempted:
  - Understat  (TLS)   -> PRIMARY xG source: home_xg, away_xg, goals, ppda, np_xg, xP
  - ClubElo    (HTTP)  -> current Elo ratings for 616 teams
  - ESPN       (HTTP)  -> read_schedule (no scores/xG in this lib version; documented)
  - MatchHistory (HTTP)-> cached CSVs only (live fetch 503); odds + shots, NO xG

Output directory: data/understat_enriched/
  raw/        -> raw DataFrames as-is from each scraper
  cleaned/    -> standardized schema:
                   date, home, away, home_score, away_score, home_xg, away_xg
                   (+ source-specific extra columns kept alongside)

Data acquisition only -- no model code is modified.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

import soccerdata as sd

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
ROOT = Path("/home/hetaisheng/soccerdata")
OUT_DIR = ROOT / "data" / "understat_enriched"
RAW_DIR = OUT_DIR / "raw"
CLEAN_DIR = OUT_DIR / "cleaned"
RAW_DIR.mkdir(parents=True, exist_ok=True)
CLEAN_DIR.mkdir(parents=True, exist_ok=True)

LEAGUES = [
    "ENG-Premier League",
    "ESP-La Liga",
    "ITA-Serie A",
    "GER-Bundesliga",
    "FRA-Ligue 1",
]
SEASONS = ["2022", "2023", "2024"]

# MatchHistory cached CSV league-code map (data/MatchHistory/<code>_<yyyy>.csv)
MH_LEAGUE_CODE = {
    "ENG-Premier League": "E0",
    "ESP-La Liga": "SP1",
    "ITA-Serie A": "I1",
    "GER-Bundesliga": "D1",
    "FRA-Ligue 1": "F1",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("xg_scrape")

# Standardized column set
STD_COLS = ["date", "home", "away", "home_score", "away_score", "home_xg", "away_xg"]

results: list[dict] = []  # accumulator for final report


def _record(source, league, season, rows, xg_coverage, status, file=""):
    results.append(
        {
            "source": source,
            "league": league,
            "season": season,
            "rows": rows,
            "xg_coverage_pct": xg_coverage,
            "status": status,
            "file": file,
        }
    )
    log.info(
        "%s | %s %s | rows=%d xG=%.1f%% | %s | %s",
        source, league, season, rows, xg_coverage, status, file,
    )


# --------------------------------------------------------------------------- #
# 1. Understat  (TLS-fingerprint) -- PRIMARY xG source
# --------------------------------------------------------------------------- #
def scrape_understat() -> pd.DataFrame:
    """Scrape team match stats (with xG) for all leagues/seasons via Understat TLS."""
    all_clean = []
    for league in LEAGUES:
        for season in SEASONS:
            tag = f"{league}_{season}"
            try:
                u = sd.Understat(leagues=league, seasons=season)
                stats = u.read_team_match_stats()
                if stats is None or stats.empty:
                    _record("Understat", league, season, 0, 0.0, "EMPTY")
                    continue

                # --- raw save ---
                raw_file = RAW_DIR / f"understat_{tag}.csv"
                stats.reset_index().to_csv(raw_file, index=False)

                # --- cleaned standardized version ---
                clean = pd.DataFrame(
                    {
                        "date": pd.to_datetime(stats["date"]),
                        "home": stats["home_team"],
                        "away": stats["away_team"],
                        "home_score": stats["home_goals"],
                        "away_score": stats["away_goals"],
                        "home_xg": stats["home_xg"],
                        "away_xg": stats["away_xg"],
                        # bonus features kept for enrichment
                        "home_np_xg": stats["home_np_xg"],
                        "away_np_xg": stats["away_np_xg"],
                        "home_xpts": stats["home_expected_points"],
                        "away_xpts": stats["away_expected_points"],
                        "home_ppda": stats["home_ppda"],
                        "away_ppda": stats["away_ppda"],
                        "home_deep": stats["home_deep_completions"],
                        "away_deep": stats["away_deep_completions"],
                        "league": league,
                        "season": season,
                        "source": "Understat",
                    }
                )
                clean_file = CLEAN_DIR / f"understat_{tag}.csv"
                clean.to_csv(clean_file, index=False)

                xg_cov = (
                    100.0
                    * clean["home_xg"].notna().sum()
                    / len(clean)
                    if len(clean)
                    else 0.0
                )
                _record(
                    "Understat", league, season, len(clean), xg_cov, "OK",
                    file=str(clean_file.relative_to(OUT_DIR)),
                )
                all_clean.append(clean)
            except Exception as e:
                _record("Understat", league, season, 0, 0.0, f"FAIL: {e}")

    if all_clean:
        combined = pd.concat(all_clean, ignore_index=True)
        combined.to_csv(OUT_DIR / "understat_all.csv", index=False)
        log.info(
            "Understat combined: %d rows -> %s",
            len(combined),
            OUT_DIR / "understat_all.csv",
        )
        return combined
    return pd.DataFrame()


# --------------------------------------------------------------------------- #
# 2. ClubElo  (simple HTTP) -- current team-strength ratings
# --------------------------------------------------------------------------- #
def scrape_clubelo() -> pd.DataFrame:
    """Get current ClubElo ratings (works via plain HTTP, no TLS fingerprint needed)."""
    try:
        ce = sd.ClubElo()
        ratings = ce.read_by_date(datetime.today().strftime("%Y-%m-%d"))
        if ratings is None or ratings.empty:
            _record("ClubElo", "ALL", "current", 0, 0.0, "EMPTY")
            return pd.DataFrame()

        # raw
        raw_file = RAW_DIR / "clubelo_current.csv"
        ratings.reset_index().to_csv(raw_file, index=False)

        # cleaned: standardized schema does not really apply (no matches),
        # so expose team + elo + rank + country + league
        clean = pd.DataFrame(
            {
                "team": ratings.index,
                "elo": ratings["elo"],
                "rank": ratings["rank"],
                "country": ratings["country"],
                "level": ratings["level"],
                "league": ratings["league"],
                "valid_from": ratings["from"],
                "valid_to": ratings["to"],
                "source": "ClubElo",
            }
        )
        clean_file = CLEAN_DIR / "clubelo_current.csv"
        clean.to_csv(clean_file, index=False)

        # also save a date-stamped historical snapshot for reproducibility
        hist_file = RAW_DIR / f"clubelo_{datetime.today():%Y%m%d}.csv"
        ratings.reset_index().to_csv(hist_file, index=False)

        _record(
            "ClubElo", "ALL", "current", len(clean), 0.0, "OK",
            file=str(clean_file.relative_to(OUT_DIR)),
        )
        log.info(
            "ClubElo top 5: %s",
            clean.sort_values("elo", ascending=False)
            .head(5)[["team", "elo", "league"]]
            .to_string(index=False),
        )
        return clean
    except Exception as e:
        _record("ClubElo", "ALL", "current", 0, 0.0, f"FAIL: {e}")
        return pd.DataFrame()


# --------------------------------------------------------------------------- #
# 3. ESPN  (HTTP) -- read_schedule (NO scores/xG in this lib version)
# --------------------------------------------------------------------------- #
def scrape_espn() -> pd.DataFrame:
    """
    NOTE on limitations (verified against soccerdata 1.9.0):
      * ESPN.available_leagues() returns only the 5 European leagues --
        'INT-World Cup' is NOT supported.
      * The ESPN class has NO `read_scores()` method. Available methods are
        read_lineup / read_matchsheet / read_schedule.
      * read_schedule() returns only date/home/away/game_id -- NO score, NO xG.
      * read_matchsheet() returns per-match event data (lineups etc.), not xG.
    We still scrape read_schedule for the 5 EU leagues so the match calendar
    (with ESPN game_ids) is available for later joining, and we document that
    ESPN does not contribute xG.
    """
    all_clean = []
    for league in LEAGUES:
        for season in SEASONS:
            tag = f"{league}_{season}"
            try:
                espn = sd.ESPN(leagues=league, seasons=season)
                sched = espn.read_schedule()
                if sched is None or sched.empty:
                    _record("ESPN", league, season, 0, 0.0, "EMPTY")
                    continue
                raw_file = RAW_DIR / f"espn_schedule_{tag}.csv"
                sched.reset_index().to_csv(raw_file, index=False)

                clean = pd.DataFrame(
                    {
                        "date": pd.to_datetime(sched["date"]),
                        "home": sched["home_team"],
                        "away": sched["away_team"],
                        "home_score": pd.NA,
                        "away_score": pd.NA,
                        "home_xg": pd.NA,
                        "away_xg": pd.NA,
                        "espn_game_id": sched["game_id"],
                        "league": league,
                        "season": season,
                        "source": "ESPN",
                    }
                )
                clean_file = CLEAN_DIR / f"espn_schedule_{tag}.csv"
                clean.to_csv(clean_file, index=False)
                _record(
                    "ESPN", league, season, len(clean), 0.0, "OK (no xG/score)",
                    file=str(clean_file.relative_to(OUT_DIR)),
                )
                all_clean.append(clean)
            except Exception as e:
                _record("ESPN", league, season, 0, 0.0, f"FAIL: {e}")

    if all_clean:
        combined = pd.concat(all_clean, ignore_index=True)
        combined.to_csv(OUT_DIR / "espn_schedule_all.csv", index=False)
        log.info("ESPN combined schedule: %d rows", len(combined))
        return combined
    return pd.DataFrame()


# --------------------------------------------------------------------------- #
# 4. MatchHistory  (cached CSVs; live fetch is 503 on football-data.co.uk)
# --------------------------------------------------------------------------- #
def scrape_matchhistory_cached() -> pd.DataFrame:
    """
    Process already-cached MatchHistory CSVs in data/MatchHistory/.
    Live scraping football-data.co.uk is currently 503 (Service Unavailable),
    so we reuse the on-disk cache. MatchHistory has NO xG columns -- only
    shots (HS/AS/HST/AST), corners, fouls, cards, and betting odds -- so
    xG coverage is 0% by construction, but scores are present (FTHG/FTAG).
    """
    mh_dir = ROOT / "data" / "MatchHistory"
    all_clean = []
    # Cached files use season-label YYYY where e.g. 2324 = 2023/24.
    # Map our target seasons -> cache suffixes that exist.
    season_suffix = {"2022": "2223", "2023": "2324", "2024": "2425"}
    for league, code in MH_LEAGUE_CODE.items():
        for season, suffix in season_suffix.items():
            csv = mh_dir / f"{code}_{suffix}.csv"
            tag = f"{league}_{season}"
            if not csv.exists():
                # try the season-start label (2122, 2223, 2324) — only some exist
                _record("MatchHistory", league, season, 0, 0.0, "NO CACHE")
                continue
            try:
                df = pd.read_csv(csv)
                raw_file = RAW_DIR / f"matchhistory_{tag}.csv"
                df.to_csv(raw_file, index=False)

                clean = pd.DataFrame(
                    {
                        "date": pd.to_datetime(df["Date"], errors="coerce"),
                        "home": df["HomeTeam"],
                        "away": df["AwayTeam"],
                        "home_score": df["FTHG"],
                        "away_score": df["FTAG"],
                        "home_xg": pd.NA,
                        "away_xg": pd.NA,
                        # useful non-xG enrichment kept alongside
                        "home_shots": df.get("HS"),
                        "away_shots": df.get("AS"),
                        "home_sot": df.get("HST"),
                        "away_sot": df.get("AST"),
                        "home_corners": df.get("HC"),
                        "away_corners": df.get("AC"),
                        "home_fouls": df.get("HF"),
                        "away_fouls": df.get("AF"),
                        "home_yellow": df.get("HY"),
                        "away_yellow": df.get("AY"),
                        "home_red": df.get("HR"),
                        "away_red": df.get("AR"),
                        "b365_home": df.get("B365H"),
                        "b365_draw": df.get("B365D"),
                        "b365_away": df.get("B365A"),
                        "league": league,
                        "season": season,
                        "source": "MatchHistory",
                    }
                )
                clean_file = CLEAN_DIR / f"matchhistory_{tag}.csv"
                clean.to_csv(clean_file, index=False)
                _record(
                    "MatchHistory", league, season, len(clean), 0.0,
                    "OK (cached; no xG, has scores+odds)",
                    file=str(clean_file.relative_to(OUT_DIR)),
                )
                all_clean.append(clean)
            except Exception as e:
                _record("MatchHistory", league, season, 0, 0.0, f"FAIL: {e}")

    if all_clean:
        combined = pd.concat(all_clean, ignore_index=True)
        combined.to_csv(OUT_DIR / "matchhistory_all.csv", index=False)
        log.info("MatchHistory combined (cached): %d rows", len(combined))
        return combined
    return pd.DataFrame()


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def write_report(
    understat: pd.DataFrame,
    clubelo: pd.DataFrame,
    espn: pd.DataFrame,
    matchhistory: pd.DataFrame,
) -> None:
    rep = pd.DataFrame(results)
    rep_file = OUT_DIR / "scrape_report.csv"
    rep.to_csv(rep_file, index=False)

    log.info("=" * 70)
    log.info("SCRAPING REPORT")
    log.info("=" * 70)
    log.info("Full per-file report: %s", rep_file)
    print("\n" + rep.to_string(index=False))

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if not understat.empty:
        xg_cov = 100.0 * understat["home_xg"].notna().sum() / len(understat)
        print(
            f"Understat  : {len(understat):5d} matches | "
            f"xG coverage {xg_cov:.1f}% | 5 leagues x 3 seasons (2022-2024)"
        )
    else:
        print("Understat  : 0 matches (FAILED)")
    if not clubelo.empty:
        print(f"ClubElo    : {len(clubelo):5d} teams  | current Elo ratings")
    else:
        print("ClubElo    : 0 teams (FAILED)")
    if not espn.empty:
        print(
            f"ESPN       : {len(espn):5d} matches | schedule only, "
            "NO scores/xG (lib has no read_scores; no INT-World Cup support)"
        )
    else:
        print("ESPN       : 0 matches (FAILED or empty)")
    if not matchhistory.empty:
        print(
            f"MatchHistory: {len(matchhistory):5d} matches | cached, "
            "NO xG (has scores + shots + odds)"
        )
    else:
        print("MatchHistory: 0 matches (no cache / 503)")

    # Failures
    fails = rep[rep["status"].str.startswith("FAIL", na=False)]
    if not fails.empty:
        print("\nFAILURES:")
        print(fails.to_string(index=False))

    print("\nOutput dir:", OUT_DIR)
    print("  raw/      - raw scraper output")
    print("  cleaned/  - standardized schema (date, home, away, scores, xG)")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    log.info("Starting xG / enrichment scrape (TLS + HTTP, no Selenium)")
    understat = scrape_understat()
    clubelo = scrape_clubelo()
    espn = scrape_espn()
    matchhistory = scrape_matchhistory_cached()
    write_report(understat, clubelo, espn, matchhistory)


if __name__ == "__main__":
    main()
