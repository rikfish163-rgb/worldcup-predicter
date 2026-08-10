"""Read cached football-data.co.uk CSV files into the platform contract."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from league_platform.catalog import get_league
from league_platform.domain import Match, ProbabilitySet, Score, SourceResult
from league_platform.identity import team_id


class MatchHistorySource:
    """Normalize the repository's real MatchHistory cache without network writes."""

    def __init__(self, data_dir: Path, *, now: datetime | None = None):
        self.data_dir = data_dir
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)

    def load(self, league_id: str) -> SourceResult:
        league = get_league(league_id)
        if not league.match_history_code:
            return SourceResult(
                competition_id=league.id,
                status="unavailable",
                matches=[],
                latest_event_at=None,
                message="中超实时赛程与结果数据源尚未接入；平台不会用样例数据冒充。",
                quality={
                    "row_count": 0,
                    "duplicate_fixture_rate": 0.0,
                    "score_completeness": 0.0,
                    "odds_completeness": 0.0,
                },
            )

        files = sorted(self.data_dir.glob(f"{league.match_history_code}_*.csv"))
        if not files:
            return SourceResult(
                competition_id=league.id,
                status="unavailable",
                matches=[],
                latest_event_at=None,
                message="没有找到可验证的本地比赛数据。",
                quality={
                    "row_count": 0,
                    "duplicate_fixture_rate": 0.0,
                    "score_completeness": 0.0,
                    "odds_completeness": 0.0,
                },
            )

        frames = [self._read_file(path) for path in files]
        frame = pd.concat(frames, ignore_index=True)
        frame["_kickoff"] = self._parse_kickoff(frame, league.timezone)
        matches = [self._row_to_match(row, league.id) for _, row in frame.iterrows()]
        matches.sort(key=lambda match: match.kickoff_at, reverse=True)

        key = pd.DataFrame(
            {
                "date": frame["_kickoff"].dt.date,
                "home": frame["HomeTeam"].astype(str).str.strip().str.casefold(),
                "away": frame["AwayTeam"].astype(str).str.strip().str.casefold(),
            }
        )
        duplicate_rate = float(key.duplicated(keep=False).mean())
        if duplicate_rate:
            raise ValueError(f"duplicate fixtures detected for {league.id}: {duplicate_rate:.2%}")
        score_completeness = float(1 - frame[["FTHG", "FTAG"]].isna().any(axis=1).mean())
        odds_columns = {"AvgH", "AvgD", "AvgA"}
        odds_completeness = (
            float(1 - frame[list(odds_columns)].isna().any(axis=1).mean())
            if odds_columns.issubset(frame.columns)
            else 0.0
        )
        latest_event_at = max(match.kickoff_at for match in matches)
        age = self.now.astimezone(timezone.utc) - latest_event_at.astimezone(timezone.utc)
        status = "stale" if age > timedelta(days=30) else "fresh"
        message = (
            f"最近一场数据为 {latest_event_at.date().isoformat()}，仅适合历史分析与回测。"
            if status == "stale"
            else "本地缓存处于可用时间窗口内。"
        )
        return SourceResult(
            competition_id=league.id,
            status=status,
            matches=matches,
            latest_event_at=latest_event_at,
            message=message,
            quality={
                "row_count": len(frame),
                "duplicate_fixture_rate": round(duplicate_rate, 6),
                "score_completeness": round(score_completeness, 6),
                "odds_completeness": round(odds_completeness, 6),
            },
        )

    @staticmethod
    def _read_file(path: Path) -> pd.DataFrame:
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            frame = pd.read_csv(path, encoding="latin-1")
        frame["_source_file"] = path.name
        frame["_source_row"] = range(2, len(frame) + 2)
        frame["_source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        frame["_season"] = path.stem.rsplit("_", 1)[-1]
        return frame

    @staticmethod
    def _parse_kickoff(frame: pd.DataFrame, timezone_name: str) -> pd.Series:
        time_values = frame.get("Time", pd.Series("12:00", index=frame.index)).fillna("12:00")
        naive = pd.to_datetime(
            frame["Date"].astype(str) + " " + time_values.astype(str),
            format="mixed",
            dayfirst=True,
            errors="coerce",
        )
        return naive.dt.tz_localize(
            ZoneInfo(timezone_name), ambiguous="NaT", nonexistent="shift_forward"
        )

    @staticmethod
    def _row_to_match(row: pd.Series, league_id: str) -> Match:
        kickoff = row["_kickoff"].to_pydatetime()
        home_team = str(row["HomeTeam"]).strip()
        away_team = str(row["AwayTeam"]).strip()
        home_team_id = team_id(league_id, home_team)
        away_team_id = team_id(league_id, away_team)
        identity = "|".join(
            [
                league_id,
                str(row["_season"]),
                kickoff.isoformat(),
                home_team_id,
                away_team_id,
            ]
        )
        match_id = hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        score = None
        if pd.notna(row.get("FTHG")) and pd.notna(row.get("FTAG")):
            score = Score(
                home=int(row["FTHG"]),
                away=int(row["FTAG"]),
                halftime_home=int(row["HTHG"]) if pd.notna(row.get("HTHG")) else None,
                halftime_away=int(row["HTAG"]) if pd.notna(row.get("HTAG")) else None,
            )
        return Match(
            id=match_id,
            competition_id=league_id,
            season=str(row["_season"]),
            kickoff_at=kickoff,
            home_team=home_team,
            away_team=away_team,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            status="finished" if score is not None else "upcoming",
            score=score,
            market_probability=MatchHistorySource._market_probability(row),
            source_file=str(row["_source_file"]),
            source_sha256=str(row["_source_sha256"]),
            provider_fixture_id=f"{row['_source_file']}:{int(row['_source_row'])}",
            source_name="football-data.co.uk",
            source_license_status="provider_terms_require_review",
        )

    @staticmethod
    def _market_probability(row: pd.Series) -> ProbabilitySet | None:
        odds = [row.get("AvgH"), row.get("AvgD"), row.get("AvgA")]
        if any(pd.isna(value) or float(value) <= 1 for value in odds):
            return None
        inverse = [1 / float(value) for value in odds]
        total = sum(inverse)
        return ProbabilitySet(
            home=round(inverse[0] / total, 4),
            draw=round(inverse[1] / total, 4),
            away=round(inverse[2] / total, 4),
        )
