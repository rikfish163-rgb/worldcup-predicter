"""Fetch diverse current sources and atomically publish one as-of snapshot."""

from __future__ import annotations

import argparse
import fcntl
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from league_platform.catalog import LEAGUES
from league_platform.source_archive import DEFAULT_ARCHIVE_DIR, archive_source_snapshot
from league_platform.live_sources import (
    fetch_espn_fixtures,
    fetch_espn_markets,
    fetch_understat_features,
)
from league_platform.live_sources.news import fetch_news
from league_platform.live_sources.open_meteo import fetch_open_meteo_weather
from league_platform.live_sources.sofascore import fetch_sofascore_prematch


DEFAULT_OUTPUT = Path("data/live/current.json")


def _sync_unlocked(
    output: Path,
    *,
    now: datetime | None = None,
    archive_dir: Path | None = None,
) -> dict:
    reference_time = now
    if reference_time is not None and reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    espn = fetch_espn_fixtures(now=reference_time)
    espn_markets = fetch_espn_markets(espn["fixtures"], now=reference_time)
    understat = fetch_understat_features(now=reference_time)
    news = fetch_news(
        now=reference_time,
        queries=[f"{league.name_en} injury lineup press conference" for league in LEAGUES],
    )
    source_now = reference_time or datetime.now(timezone.utc)
    current_horizon = source_now + timedelta(days=16)
    upcoming = [
        fixture
        for fixture in espn["fixtures"]
        if fixture["status"] == "upcoming"
        and datetime.fromisoformat(fixture["kickoff_at"]).astimezone(timezone.utc)
        <= current_horizon
    ]
    weather = fetch_open_meteo_weather(
        upcoming,
        now=reference_time,
    )
    sofascore = fetch_sofascore_prematch(
        upcoming,
        now=reference_time,
    )
    as_of = reference_time or datetime.now(timezone.utc)
    snapshot = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": [league.id for league in LEAGUES],
        "roles": {
            "fixtures_and_results": "ESPN",
            "recent_xg_and_form": "Understat",
            "historical_training": ["football-data.co.uk", "OpenFootball"],
            "current_market": "ESPN event summary / named bookmaker",
            "injuries_and_lineups": "SofaScore provider-reported; not authoritative",
        },
        "espn": espn,
        "espn_markets": espn_markets,
        "understat": understat,
        "news": news,
        "weather": weather,
        "sofascore": sofascore,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(output)
    archive_source_snapshot(
        snapshot,
        archive_dir or output.parent / DEFAULT_ARCHIVE_DIR.name,
        now=as_of,
    )
    return snapshot


def sync(
    output: Path = DEFAULT_OUTPUT,
    *,
    now: datetime | None = None,
    archive_dir: Path | None = None,
) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_name(f"{output.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another current-data sync is already running") from exc
        return _sync_unlocked(output, now=now, archive_dir=archive_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    args = parser.parse_args()
    snapshot = sync(args.output, archive_dir=args.archive_dir)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "as_of": snapshot["as_of"],
                "fixtures": len(snapshot["espn"]["fixtures"]),
                "team_features": len(snapshot["understat"]["team_features"]),
                "markets": len(snapshot["espn_markets"]["markets"]),
                "news_items": snapshot["news"]["item_count"],
                "weather": len(snapshot["weather"]["weather"]),
                "sofascore_events": len(snapshot["sofascore"]["events"]),
                "errors": len(snapshot["espn"]["errors"])
                + len(snapshot["espn_markets"]["errors"])
                + len(snapshot["understat"]["errors"])
                + len(snapshot["news"]["errors"])
                + len(snapshot["weather"]["errors"])
                + len(snapshot["sofascore"]["errors"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
