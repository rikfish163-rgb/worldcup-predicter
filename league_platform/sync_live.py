"""Fetch diverse current sources and atomically publish one as-of snapshot."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from league_platform.live_sources import (
    fetch_espn_fixtures,
    fetch_espn_markets,
    fetch_understat_features,
)


DEFAULT_OUTPUT = Path("data/live/current.json")


def sync(output: Path = DEFAULT_OUTPUT, *, now: datetime | None = None) -> dict:
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    espn = fetch_espn_fixtures(now=as_of)
    espn_markets = fetch_espn_markets(espn["fixtures"], now=as_of)
    snapshot = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "roles": {
            "fixtures_and_results": "ESPN",
            "recent_xg_and_form": "Understat",
            "historical_training": ["football-data.co.uk", "OpenFootball"],
            "current_market": "ESPN event summary / named bookmaker",
            "injuries_and_lineups": None,
        },
        "espn": espn,
        "espn_markets": espn_markets,
        "understat": fetch_understat_features(now=as_of),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(output)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    snapshot = sync(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "as_of": snapshot["as_of"],
                "fixtures": len(snapshot["espn"]["fixtures"]),
                "team_features": len(snapshot["understat"]["team_features"]),
                "markets": len(snapshot["espn_markets"]["markets"]),
                "errors": len(snapshot["espn"]["errors"])
                + len(snapshot["espn_markets"]["errors"])
                + len(snapshot["understat"]["errors"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
