"""Causal historical Understat xG feature ledger.

The cleaned Understat CSVs are useful historical evidence, but they do not
carry the original HTTP observation time.  This module therefore assigns a
conservative availability time: ``effective_at + availability_lag``.  The
inference is explicit in every snapshot and is never treated as an exact
retrieval timestamp.

The ledger is an execution-independent feature layer.  Crawl4AI or another
fetch runtime may produce the source archive, but the archive's declared
Understat identity remains the fact source and the runtime is not promoted to
one.
"""

from __future__ import annotations

import csv
import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from league_platform.domain import Match
from league_platform.identity import team_id


UNDERSTAT_LEAGUE_NAMES = {
    "premier-league": "ENG-Premier League",
    "la-liga": "ESP-La Liga",
    "bundesliga": "GER-Bundesliga",
    "serie-a": "ITA-Serie A",
    "ligue-1": "FRA-Ligue 1",
}
UNDERSTAT_LEAGUE_SLUGS = {
    "premier-league": "EPL",
    "la-liga": "La_liga",
    "bundesliga": "Bundesliga",
    "serie-a": "Serie_A",
    "ligue-1": "Ligue_1",
}

DEFAULT_AVAILABILITY_LAG = timedelta(days=1)
DEFAULT_ROLLING_WINDOW = 5
DEFAULT_MIN_SAMPLE = 3


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_effective_at(value: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return _utc(parsed)


def _number(value: object, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid historical xG {field}: {value!r}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"invalid historical xG {field}: {value!r}")
    return parsed


@dataclass(frozen=True, slots=True)
class HistoricalXGObservation:
    """One team-side xG observation with an auditable availability bound."""

    league_id: str
    team_id: str
    team_name: str
    effective_at: datetime
    observed_at: datetime
    xg_for: float
    xg_against: float
    source_file: str
    source_sha256: str
    source_row: int

    @property
    def availability_basis(self) -> str:
        return "effective_at_plus_1d_conservative"

    def to_dict(self) -> dict[str, Any]:
        return {
            "league_id": self.league_id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "effective_at": self.effective_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "xg_for": self.xg_for,
            "xg_against": self.xg_against,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "source_row": self.source_row,
            "availability_basis": self.availability_basis,
            "observed_at_exact": False,
        }


class HistoricalXGIndex:
    """Index archived xG rows for cutoff-bounded team snapshots."""

    def __init__(
        self,
        observations: list[HistoricalXGObservation],
        *,
        availability_lag: timedelta = DEFAULT_AVAILABILITY_LAG,
        source_root: Path | None = None,
    ) -> None:
        if availability_lag <= timedelta(0):
            raise ValueError("availability_lag must be positive")
        self.observations = tuple(
            sorted(observations, key=lambda item: (item.effective_at, item.team_id, item.source_row))
        )
        self.availability_lag = availability_lag
        self.source_root = source_root
        by_team: dict[tuple[str, str], list[HistoricalXGObservation]] = defaultdict(list)
        for observation in self.observations:
            by_team[(observation.league_id, observation.team_id)].append(observation)
        self._by_team = {
            key: tuple(sorted(values, key=lambda item: item.effective_at))
            for key, values in by_team.items()
        }

    @property
    def source_file_count(self) -> int:
        return len({item.source_file for item in self.observations})

    def diagnostics(self) -> dict[str, Any]:
        leagues = sorted({item.league_id for item in self.observations})
        source_files = [
            {
                "path": path,
                "sha256": sha256,
                "row_count": sum(item.source_file == path for item in self.observations) // 2,
            }
            for path, sha256 in sorted(
                {(item.source_file, item.source_sha256) for item in self.observations}
            )
        ]
        return {
            "status": "ready" if self.observations else "empty",
            "schema": "historical_understat_xg_v1",
            "source_role": "Understat historical fact source",
            "execution_layer": "separate; Crawl4AI may fetch but is not a source identity",
            "observation_count": len(self.observations),
            "source_file_count": self.source_file_count,
            "source_files": source_files,
            "league_count": len(leagues),
            "leagues": leagues,
            "availability_lag_seconds": int(self.availability_lag.total_seconds()),
            "availability_time_policy": "effective_at_plus_1d_conservative",
            "observed_at_exact": False,
        }

    def _eligible(
        self,
        match: Match,
        *,
        team: str,
        cutoff_at: datetime,
        window: int,
        min_sample: int,
    ) -> tuple[HistoricalXGObservation, ...]:
        if window < 1 or min_sample < 1:
            raise ValueError("window and min_sample must be positive")
        cutoff = _utc(cutoff_at)
        rows = tuple(
            item
            for item in self._by_team.get((match.competition_id, team), ())
            if item.observed_at <= cutoff and item.effective_at < cutoff
        )
        selected = rows[-window:]
        return selected if len(selected) >= min_sample else ()

    @staticmethod
    def _side_snapshot(
        rows: tuple[HistoricalXGObservation, ...],
        *,
        source_url: str,
    ) -> dict[str, Any] | None:
        if not rows:
            return None
        latest = rows[-1]
        observed_at = max(item.observed_at for item in rows)
        return {
            "xg_for": round(sum(item.xg_for for item in rows) / len(rows), 6),
            "xg_against": round(sum(item.xg_against for item in rows) / len(rows), 6),
            "sample_n": len(rows),
            "last_effective_at": latest.effective_at.isoformat(),
            "observed_at": observed_at.isoformat(),
            "timestamp_quality": "inferred_conservative",
            "observed_at_basis": latest.availability_basis,
            "source": {
                "name": "Understat historical xG",
                "url": source_url,
                "observed_at": observed_at.isoformat(),
                "retrieved_at": observed_at.isoformat(),
                "observed_at_exact": False,
                "timestamp_quality": "inferred_conservative",
                "observed_at_basis": latest.availability_basis,
                "archive_file": latest.source_file,
                "archive_sha256": latest.source_sha256,
                "source_rows": [item.source_row for item in rows],
            },
        }

    def snapshot_for_match(
        self,
        match: Match,
        *,
        cutoff_at: datetime,
        window: int = DEFAULT_ROLLING_WINDOW,
        min_sample: int = DEFAULT_MIN_SAMPLE,
    ) -> dict[str, Any]:
        """Build a model-shaped snapshot without reading future xG rows."""

        league_name = UNDERSTAT_LEAGUE_NAMES.get(match.competition_id, match.competition_id)
        source_url = (
            f"https://understat.com/league/"
            f"{UNDERSTAT_LEAGUE_SLUGS.get(match.competition_id, match.competition_id)}"
        )
        home_rows = self._eligible(
            match,
            team=match.home_team_id,
            cutoff_at=cutoff_at,
            window=window,
            min_sample=min_sample,
        )
        away_rows = self._eligible(
            match,
            team=match.away_team_id,
            cutoff_at=cutoff_at,
            window=window,
            min_sample=min_sample,
        )
        return {
            "schema": "historical_understat_xg_snapshot_v1",
            "source_name": "Understat",
            "source_role": "fact_source",
            "league_name": league_name,
            "availability_lag_seconds": int(self.availability_lag.total_seconds()),
            "observed_at_exact": False,
            "observed_at_basis": "effective_at_plus_1d_conservative",
            "window": window,
            "min_sample": min_sample,
            "home": self._side_snapshot(home_rows, source_url=source_url),
            "away": self._side_snapshot(away_rows, source_url=source_url),
            "status": "ready" if home_rows and away_rows else "partial_or_missing",
        }


def _candidate_files(data_dir: Path, league_id: str) -> list[Path]:
    league_name = UNDERSTAT_LEAGUE_NAMES.get(league_id)
    if league_name is None:
        return []
    root = data_dir / "cleaned" if (data_dir / "cleaned").is_dir() else data_dir
    return sorted(root.glob(f"understat_{league_name}_*.csv"))


def load_historical_xg_index(
    data_dir: Path = Path("data/understat_enriched"),
    *,
    league_ids: tuple[str, ...] | None = None,
    availability_lag: timedelta = DEFAULT_AVAILABILITY_LAG,
) -> HistoricalXGIndex:
    """Load cleaned Understat match xG rows into a strict causal index."""

    selected_leagues = league_ids or tuple(UNDERSTAT_LEAGUE_NAMES)
    observations: list[HistoricalXGObservation] = []
    fixture_keys: set[tuple[str, datetime, str, str]] = set()
    for league_id in selected_leagues:
        for path in _candidate_files(data_dir, league_id):
            content = path.read_bytes()
            source_sha256 = hashlib.sha256(content).hexdigest()
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                required = {"date", "home", "away", "home_xg", "away_xg"}
                if not required.issubset(set(reader.fieldnames or ())):
                    raise ValueError(f"historical xG file missing columns: {path}")
                for source_row, row in enumerate(reader, 2):
                    effective_at = _parse_effective_at(str(row.get("date", "")))
                    home = str(row.get("home", "")).strip()
                    away = str(row.get("away", "")).strip()
                    if not home or not away:
                        raise ValueError(f"historical xG file has empty team at {path}:{source_row}")
                    key = (league_id, effective_at, home.casefold(), away.casefold())
                    if key in fixture_keys:
                        raise ValueError(f"duplicate historical xG fixture: {path}:{source_row}")
                    fixture_keys.add(key)
                    home_xg = _number(row.get("home_xg"), field="home_xg")
                    away_xg = _number(row.get("away_xg"), field="away_xg")
                    observed_at = effective_at + availability_lag
                    observations.extend(
                        (
                            HistoricalXGObservation(
                                league_id=league_id,
                                team_id=team_id(league_id, home),
                                team_name=home,
                                effective_at=effective_at,
                                observed_at=observed_at,
                                xg_for=home_xg,
                                xg_against=away_xg,
                                source_file=str(path),
                                source_sha256=source_sha256,
                                source_row=source_row,
                            ),
                            HistoricalXGObservation(
                                league_id=league_id,
                                team_id=team_id(league_id, away),
                                team_name=away,
                                effective_at=effective_at,
                                observed_at=observed_at,
                                xg_for=away_xg,
                                xg_against=home_xg,
                                source_file=str(path),
                                source_sha256=source_sha256,
                                source_row=source_row,
                            ),
                        )
                    )
    return HistoricalXGIndex(
        observations,
        availability_lag=availability_lag,
        source_root=data_dir,
    )
