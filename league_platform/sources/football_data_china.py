"""Read the public Football-Data China Super League results/odds CSV.

The source is a current public aggregate file rather than a frozen package
asset.  This module is therefore opt-in and records the downloaded file hash;
callers must archive the exact response before using it for a backtest.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from league_platform.domain import Match, ProbabilitySet, Score
from league_platform.identity import team_id
from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


URL = "https://www.football-data.co.uk/new/CHN.csv"
HOST = "www.football-data.co.uk"
TIMEZONE = ZoneInfo("Asia/Shanghai")


def _probability(values: tuple[object, object, object]) -> ProbabilitySet | None:
    try:
        odds = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if any(pd.isna(value) or value <= 1 for value in odds):
        return None
    inverse = [1 / value for value in odds]
    total = sum(inverse)
    home = round(inverse[0] / total, 8)
    draw = round(inverse[1] / total, 8)
    return ProbabilitySet(home=home, draw=draw, away=round(1 - home - draw, 8))


def parse_football_data_china_csv(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str = URL,
    source_file: str = "CHN.csv",
) -> list[Match]:
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != HOST
        or parsed_url.path != "/new/CHN.csv"
    ):
        raise ValueError("Football-Data China URL is not allowlisted")
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware")
    try:
        frame = pd.read_csv(io.BytesIO(payload), encoding="utf-8-sig")
    except Exception as exc:
        raise ValueError("Football-Data China CSV could not be parsed") from exc
    required = {"Season", "Date", "Time", "Home", "Away", "HG", "AG"}
    if not required.issubset(frame.columns):
        raise ValueError("Football-Data China CSV is missing required columns")
    source_sha256 = hashlib.sha256(payload).hexdigest()
    matches: list[Match] = []
    for row_number, row in frame.iterrows():
        try:
            date_text = str(row["Date"]).strip()
            time_text = str(row["Time"]).strip()
            kickoff = datetime.strptime(f"{date_text} {time_text}", "%d/%m/%Y %H:%M").replace(
                tzinfo=TIMEZONE
            )
            home = str(row["Home"]).strip()
            away = str(row["Away"]).strip()
            home_goals = int(row["HG"])
            away_goals = int(row["AG"])
        except (TypeError, ValueError, OverflowError):
            continue
        home_id = team_id("csl", home)
        away_id = team_id("csl", away)
        season = str(row["Season"])
        identity = f"csl|{season}|{kickoff.isoformat()}|{home_id}|{away_id}"
        match_id = hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        opening = _probability((row.get("PSCH"), row.get("PSCD"), row.get("PSCA")))
        closing = _probability((row.get("AvgCH"), row.get("AvgCD"), row.get("AvgCA")))
        matches.append(
            Match(
                id=match_id,
                competition_id="csl",
                season=season,
                kickoff_at=kickoff,
                home_team=home,
                away_team=away,
                home_team_id=home_id,
                away_team_id=away_id,
                status="finished",
                score=Score(home=home_goals, away=away_goals),
                market_probability=closing or opening,
                source_file=source_file,
                source_sha256=source_sha256,
                provider_fixture_id=f"football-data-china:{row_number + 2}",
                source_name="football-data.co.uk China",
                source_license_status="provider_terms_require_review",
                market_time_basis="closing_average_pre_kickoff"
                if closing
                else "opening_average_fallback_pre_kickoff"
                if opening
                else "unavailable",
                market_opening_probability=opening,
                market_closing_probability=closing,
            )
        )
    return matches


def fetch_football_data_china(
    *,
    now: datetime | None = None,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    return rights_blocked_fetch_envelope(
        SourceId.FOOTBALL_DATA_CHINA_PUBLIC_CSV,
        provider="football-data.co.uk China",
        now=now,
        empty_fields=("matches",),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    request = __import__("urllib.request", fromlist=["Request"]).Request(
        URL,
        headers={"Accept": "text/csv", "User-Agent": "Matchline/1.0"},
    )
    fetcher = opener or __import__("urllib.request", fromlist=["urlopen"]).build_opener()
    open_method = getattr(fetcher, "open", None) or fetcher
    with open_method(request, timeout=30) as response:
        final_url = response.geturl()
        if final_url != URL:
            raise ValueError("Football-Data China response redirected")
        payload = response.read(20 * 1024 * 1024 + 1)
    if len(payload) > 20 * 1024 * 1024:
        raise ValueError("Football-Data China response exceeded 20 MiB")
    matches = parse_football_data_china_csv(payload, retrieved_at=reference)
    return {
        "provider": "football-data.co.uk China",
        "url": URL,
        "retrieved_at": reference.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "matches": matches,
    }


__all__ = ["URL", "fetch_football_data_china", "parse_football_data_china_csv"]
