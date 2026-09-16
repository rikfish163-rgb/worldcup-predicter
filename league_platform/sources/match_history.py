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
from league_platform.sources.espn_kickoff import apply_kickoff_enrichment, load_kickoff_enrichment


class MatchHistorySource:
    """Normalize the repository's real MatchHistory cache without network writes."""

    def __init__(
        self,
        data_dir: Path,
        *,
        now: datetime | None = None,
        kickoff_enrichment_path: Path | None = None,
    ):
        self.data_dir = data_dir
        self.kickoff_enrichment_path = kickoff_enrichment_path
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
        frame = pd.concat(frames, ignore_index=True).copy()
        time_values = frame.get("Time", pd.Series(pd.NA, index=frame.index))
        time_text = time_values.astype("string").str.strip()
        frame["_kickoff_time_quality"] = (
            time_text.notna()
            & ~time_text.isin(["", "nan", "NaN", "<NA>"])
        ).map({True: "exact", False: "date_only"})
        frame["_kickoff"] = self._parse_kickoff(frame, league.timezone)
        invalid_kickoff_rows = int(frame["_kickoff"].isna().sum())
        if invalid_kickoff_rows:
            # Malformed dates are quarantined rather than converted into a
            # fake timestamp (NaT can otherwise reach future prediction code).
            frame = frame.loc[frame["_kickoff"].notna()].copy()
        if frame.empty:
            return SourceResult(
                competition_id=league.id,
                status="unavailable",
                matches=[],
                latest_event_at=None,
                message="所有本地记录的开赛时间均无法解析，已隔离且不会进入模型。",
                quality={
                    "row_count": 0,
                    "invalid_kickoff_rows": invalid_kickoff_rows,
                    "duplicate_fixture_rate": 0.0,
                    "score_completeness": 0.0,
                    "odds_completeness": 0.0,
                },
            )
        matches = [self._row_to_match(row, league.id) for _, row in frame.iterrows()]
        enrichment_stats = {
            "kickoff_enrichment_rows": 0,
            "kickoff_enrichment_applied": 0,
            "kickoff_enrichment_remaining_date_only": int(
                sum(match.kickoff_time_quality == "date_only" for match in matches)
            ),
            "kickoff_enrichment_error": 0,
        }
        if self.kickoff_enrichment_path is not None and self.kickoff_enrichment_path.exists():
            try:
                records = load_kickoff_enrichment(self.kickoff_enrichment_path)
                matches, applied = apply_kickoff_enrichment(matches, records)
                enrichment_stats.update(
                    {
                        "kickoff_enrichment_rows": len(records),
                        "kickoff_enrichment_applied": applied["applied"],
                        "kickoff_enrichment_remaining_date_only": applied[
                            "remaining_date_only"
                        ],
                    }
                )
            except (OSError, ValueError):
                # An optional enrichment source cannot invalidate the original
                # provider cache.  Preserve the base rows and expose the
                # failure numerically for the source-health audit.
                enrichment_stats["kickoff_enrichment_error"] = 1
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
        # Use the provider's closing average when available.  The non-C
        # columns are opening averages and remain an explicit per-row
        # fallback for older/incomplete files.
        odds_columns = {"AvgCH", "AvgCD", "AvgCA"} if {"AvgCH", "AvgCD", "AvgCA"}.issubset(frame.columns) else {"AvgH", "AvgD", "AvgA"}
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
                "invalid_kickoff_rows": invalid_kickoff_rows,
                "date_only_kickoff_rows": int(
                    (frame["_kickoff_time_quality"] == "date_only").sum()
                ),
                "duplicate_fixture_rate": round(duplicate_rate, 6),
                "score_completeness": round(score_completeness, 6),
                "odds_completeness": round(odds_completeness, 6),
                **enrichment_stats,
            },
        )

    @staticmethod
    def _read_file(path: Path) -> pd.DataFrame:
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            frame = pd.read_csv(path, encoding="latin-1")
        metadata = pd.DataFrame(
            {
                "_source_file": path.name,
                "_source_row": range(2, len(frame) + 2),
                "_source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "_season": path.stem.rsplit("_", 1)[-1],
            },
            index=frame.index,
        )
        return pd.concat([frame, metadata], axis=1)

    @staticmethod
    def _parse_kickoff(frame: pd.DataFrame, timezone_name: str) -> pd.Series:
        # A missing kickoff time is not equivalent to noon.  Use midnight only
        # as a date ordering anchor; ``_kickoff_time_quality`` makes the
        # uncertainty explicit and the walk-forward evaluator batches the
        # affected calendar date conservatively.
        time_values = frame.get("Time", pd.Series(pd.NA, index=frame.index))
        time_values = time_values.astype("string").str.strip()
        valid_time = time_values.notna() & ~time_values.isin(
            ["", "nan", "NaN", "<NA>"]
        )
        time_values = time_values.where(valid_time, "00:00")
        naive = pd.to_datetime(
            frame["Date"].astype("string").str.strip() + " " + time_values,
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
            kickoff_time_quality=str(row.get("_kickoff_time_quality") or "exact"),
            kickoff_time_source="football-data.co.uk",
            market_time_basis=MatchHistorySource._market_time_basis(row),
            market_opening_probability=MatchHistorySource._probability_from_odds(
                (row.get("AvgH"), row.get("AvgD"), row.get("AvgA"))
            ),
            market_closing_probability=MatchHistorySource._probability_from_odds(
                (row.get("AvgCH"), row.get("AvgCD"), row.get("AvgCA"))
            ),
            handicap_line=MatchHistorySource._number(row.get("AHCh"), row.get("AHh")),
            handicap_probability=MatchHistorySource._two_way_probability(
                row.get("AvgCAHH"), row.get("AvgCAHA"), fallback=(row.get("AvgAHH"), row.get("AvgAHA")), labels=("win", "loss")
            ),
            total_line=2.5 if MatchHistorySource._two_way_probability(
                row.get("AvgC>2.5"), row.get("AvgC<2.5"), fallback=(row.get("Avg>2.5"), row.get("Avg<2.5"))
            ) else None,
            total_probability=MatchHistorySource._two_way_probability(
                row.get("AvgC>2.5"), row.get("AvgC<2.5"), fallback=(row.get("Avg>2.5"), row.get("Avg<2.5")), labels=("over", "under")
            ),
            total_opening_probability=MatchHistorySource._two_way_probability(
                row.get("Avg>2.5"), row.get("Avg<2.5"), labels=("over", "under")
            ),
            total_closing_probability=MatchHistorySource._two_way_probability(
                row.get("AvgC>2.5"), row.get("AvgC<2.5"), labels=("over", "under")
            ),
        )

    @staticmethod
    def _probability_from_odds(odds) -> ProbabilitySet | None:
        if odds is None or len(odds) != 3:
            return None
        try:
            values = [float(value) for value in odds]
        except (TypeError, ValueError):
            return None
        if any(pd.isna(value) or value <= 1 for value in values):
            return None
        inverse = [1 / value for value in values]
        total = sum(inverse)
        home = round(inverse[0] / total, 8)
        draw = round(inverse[1] / total, 8)
        return ProbabilitySet(
            home=home,
            draw=draw,
            # Keep the serialized distribution normalized after rounding.
            away=round(1.0 - home - draw, 8),
        )

    @staticmethod
    def _market_probability(row: pd.Series) -> ProbabilitySet | None:
        closing = MatchHistorySource._probability_from_odds(
            (row.get("AvgCH"), row.get("AvgCD"), row.get("AvgCA"))
        )
        return closing or MatchHistorySource._probability_from_odds(
            (row.get("AvgH"), row.get("AvgD"), row.get("AvgA"))
        )

    @staticmethod
    def _market_time_basis(row: pd.Series) -> str:
        """Describe the pre-kickoff time semantics of the historical odds field."""

        closing = [row.get("AvgCH"), row.get("AvgCD"), row.get("AvgCA")]
        if all(pd.notna(value) and float(value) > 1 for value in closing):
            return "closing_average_pre_kickoff"
        opening = [row.get("AvgH"), row.get("AvgD"), row.get("AvgA")]
        if all(pd.notna(value) and float(value) > 1 for value in opening):
            return "opening_average_fallback_pre_kickoff"
        return "unavailable"

    @staticmethod
    def _number(value, fallback=None) -> float | None:
        for candidate in (value, fallback):
            if candidate is None or pd.isna(candidate):
                continue
            try:
                number = float(candidate)
            except (TypeError, ValueError):
                continue
            if pd.notna(number):
                return number
        return None

    @staticmethod
    def _two_way_probability(first, second, *, fallback=(None, None), labels=("first", "second")) -> dict[str, float] | None:
        first = MatchHistorySource._number(first, fallback[0])
        second = MatchHistorySource._number(second, fallback[1])
        if first is None or second is None or first <= 1 or second <= 1:
            return None
        inverse = (1 / first, 1 / second)
        total = sum(inverse)
        return {labels[0]: round(inverse[0] / total, 6), labels[1]: round(inverse[1] / total, 6)}
