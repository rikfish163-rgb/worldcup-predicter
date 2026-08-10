"""Parse pinned OpenFootball Chinese Super League text snapshots."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from league_platform.domain import Match, Score, SourceResult
from league_platform.identity import team_id


DATE_RE = re.compile(
    r"^\s{2}(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:\s+(\d{4}))?\s*$"
)
MATCH_RE = re.compile(
    r"^\s+(?:(\d{1,2}:\d{2})\s+)?(.+?)\s{2,}v\s+(.+?)\s{2,}"
    r"(\d+)-(\d+)(?:\s+\((\d+)-(\d+)\))?"
)


class OpenFootballSource:
    """Load CSL history under OpenFootball's public-domain data license."""

    def __init__(self, data_dir: Path, *, now: datetime | None = None):
        self.data_dir = data_dir
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)

    def load(self, league_id: str = "csl") -> SourceResult:
        if league_id != "csl":
            raise ValueError(f"OpenFootballSource does not support {league_id}")
        files = sorted(self.data_dir.glob("CSL_*.txt"))
        if not files:
            return SourceResult(
                competition_id="csl",
                status="unavailable",
                matches=[],
                latest_event_at=None,
                message="没有找到可校验恢复的中超 OpenFootball 历史数据。",
                quality={
                    "row_count": 0,
                    "duplicate_fixture_rate": 0.0,
                    "score_completeness": 0.0,
                    "odds_completeness": 0.0,
                },
            )
        matches = [match for path in files for match in self._parse_file(path)]
        matches.sort(key=lambda match: match.kickoff_at, reverse=True)
        ids = [match.id for match in matches]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate fixtures detected for csl")
        latest = matches[0].kickoff_at
        age = self.now.astimezone(timezone.utc) - latest.astimezone(timezone.utc)
        status = "stale" if age > timedelta(days=30) else "fresh"
        return SourceResult(
            competition_id="csl",
            status=status,
            matches=matches,
            latest_event_at=latest,
            message=f"最近一场数据为 {latest.date().isoformat()}，仅适合历史分析与回测。",
            quality={
                "row_count": len(matches),
                "duplicate_fixture_rate": 0.0,
                "score_completeness": 1.0,
                "odds_completeness": 0.0,
            },
        )

    @staticmethod
    def _parse_file(path: Path) -> list[Match]:
        year = int(path.stem.rsplit("_", 1)[-1])
        season = str(year)
        source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        current_date = None
        current_time = "12:00"
        matches = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            date_match = DATE_RE.match(line)
            if date_match:
                month, day, explicit_year = date_match.groups()
                current_date = datetime.strptime(
                    f"{explicit_year or year}-{month}-{day}", "%Y-%b-%d"
                ).date()
                current_time = "12:00"
                continue
            match = MATCH_RE.match(line)
            if not match or current_date is None:
                continue
            time_value, home, away, home_goals, away_goals, ht_home, ht_away = match.groups()
            if time_value:
                current_time = time_value
            hour, minute = (int(value) for value in current_time.split(":"))
            kickoff = datetime(
                current_date.year,
                current_date.month,
                current_date.day,
                hour,
                minute,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
            home = home.strip()
            away = away.strip()
            home_id = team_id("csl", home)
            away_id = team_id("csl", away)
            identity = f"csl|{season}|{kickoff.isoformat()}|{home_id}|{away_id}"
            matches.append(
                Match(
                    id=hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False).hexdigest()[
                        :16
                    ],
                    competition_id="csl",
                    season=season,
                    kickoff_at=kickoff,
                    home_team=home,
                    away_team=away,
                    home_team_id=home_id,
                    away_team_id=away_id,
                    status="finished",
                    score=Score(
                        home=int(home_goals),
                        away=int(away_goals),
                        halftime_home=int(ht_home) if ht_home is not None else None,
                        halftime_away=int(ht_away) if ht_away is not None else None,
                    ),
                    market_probability=None,
                    source_file=path.name,
                    source_sha256=source_sha256,
                    provider_fixture_id=f"{path.name}:{line_number}",
                    source_name="OpenFootball",
                    source_license_status="CC0-1.0",
                )
            )
        return matches
