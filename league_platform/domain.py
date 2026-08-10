"""Serializable domain contracts shared by sources, models, APIs, and UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal


SourceStatus = Literal["fresh", "delayed", "stale", "unavailable"]
MatchStatus = Literal["upcoming", "live", "finished", "postponed", "cancelled"]


@dataclass(frozen=True, slots=True)
class Score:
    home: int
    away: int
    halftime_home: int | None = None
    halftime_away: int | None = None


@dataclass(frozen=True, slots=True)
class ProbabilitySet:
    home: float
    draw: float
    away: float


@dataclass(frozen=True, slots=True)
class Match:
    id: str
    competition_id: str
    season: str
    kickoff_at: datetime
    home_team: str
    away_team: str
    home_team_id: str
    away_team_id: str
    status: MatchStatus
    score: Score | None
    market_probability: ProbabilitySet | None
    source_file: str
    source_sha256: str
    provider_fixture_id: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["kickoff_at"] = self.kickoff_at.isoformat()
        payload["source"] = {
            "name": "football-data.co.uk",
            "file": self.source_file,
            "sha256": self.source_sha256,
            "provider_fixture_id": self.provider_fixture_id,
            "retrieved_at": None,
            "license_status": "provider_terms_require_review",
        }
        payload.pop("source_file")
        payload.pop("source_sha256")
        payload.pop("provider_fixture_id")
        return payload


@dataclass(slots=True)
class SourceResult:
    competition_id: str
    status: SourceStatus
    matches: list[Match]
    latest_event_at: datetime | None
    message: str
    quality: dict[str, float | int]
