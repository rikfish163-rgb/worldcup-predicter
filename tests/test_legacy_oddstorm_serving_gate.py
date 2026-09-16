from datetime import datetime, timezone
import json
from pathlib import Path

from league_platform.current import CURRENT_SOURCE_ROLES, attach_current_data
from league_platform.snapshot import build_platform_snapshot


def test_unlicensed_legacy_oddstorm_snapshot_is_replayable_but_not_served(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 8, 24, 13, 30, tzinfo=timezone.utc)
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": [
            "bundesliga",
            "championship",
            "csl",
            "la-liga",
            "ligue-1",
            "premier-league",
            "serie-a",
        ],
        "roles": CURRENT_SOURCE_ROLES,
        "fixture_feed": {
            "provider": "OpenFootball",
            "retrieved_at": as_of.isoformat(),
            "status": "empty",
            "fixtures": [],
            "date_only_fixtures": [],
            "errors": [],
        },
        "espn": {
            "provider": "ESPN",
            "status": "rights_blocked",
            "fixtures": [],
            "errors": [],
            "network_opened": False,
        },
        "espn_markets": {
            "provider": "ESPN event summary",
            "status": "rights_blocked",
            "markets": [],
            "team_status": [],
            "errors": [],
            "network_opened": False,
        },
        "espn_rosters": {
            "provider": "ESPN team roster",
            "status": "rights_blocked",
            "rosters": [],
            "errors": [],
            "network_opened": False,
        },
        "understat": {"provider": "Understat", "team_features": [], "errors": []},
        "oddstorm": {
            "provider": "OddStorm public bookmaker comparison",
            "retrieved_at": as_of.isoformat(),
            "status": "available",
            "lines": [
                {
                    "match_id": "legacy-market-row",
                    "kickoff_at": "2026-08-28T11:35:00+00:00",
                    "source": {
                        "name": "OddStorm public bookmaker comparison",
                        "url": (
                            "https://www.oddstorm.com/asianodds/league/"
                            "2182861-china-chinese-super-league"
                        ),
                        "retrieved_at": as_of.isoformat(),
                        "raw_sha256": "a" * 64,
                    },
                }
            ],
            "errors": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")

    result = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")),
        live_path,
        now=as_of,
    )

    current = result["current_data"]
    assert current["model_admission"]["eligible"] is False
    assert any(
        item["section"] == "oddstorm"
        and item["source_id"] == "oddstorm_market_comparison"
        and item["decision"] == "block"
        and item["fact_count"] == 1
        for item in current["rights_quarantine"]
    )
    assert "oddstorm" not in result
