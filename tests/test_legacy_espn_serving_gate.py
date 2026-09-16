from datetime import datetime, timezone
import json
from pathlib import Path

from league_platform.current import EXPECTED_SOURCE_ROLES, attach_current_data
from league_platform.fixture_serving import fixture_serving_status
from league_platform.snapshot import build_platform_snapshot


def test_missing_malformed_and_empty_canonical_fixture_feeds_fail_closed() -> None:
    cases = [
        ({}, "canonical_fixture_feed_missing"),
        ({"fixture_feed": {}}, "canonical_fixture_feed_malformed"),
        (
            {"fixture_feed": {"provider": "Unknown", "fixtures": "not-a-list"}},
            "canonical_fixture_feed_malformed",
        ),
        (
            {"fixture_feed": {"provider": "Unknown", "fixtures": []}},
            "canonical_fixture_feed_empty",
        ),
    ]

    for snapshot, expected_reason in cases:
        decision = fixture_serving_status(snapshot)
        assert decision["status"] == "blocked"
        assert decision["reason"] == expected_reason


def test_unlicensed_legacy_espn_snapshot_is_replayable_but_not_served(tmp_path: Path) -> None:
    as_of = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)
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
        "roles": EXPECTED_SOURCE_ROLES,
        "espn": {
            "provider": "ESPN",
            "fixtures": [
                {
                    "id": "espn:cached",
                    "competition_id": "premier-league",
                    "kickoff_at": "2026-08-25T19:00:00+00:00",
                    "status": "upcoming",
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

    assert result["current_data"]["status"] == "rights_blocked"
    assert result["current_data"]["fixture_count"] == 0
    assert result["current_data"]["network_opened"] is False
    assert result["current_data"]["terms_url"] == "https://disneytermsofuse.com/english/"
    assert not any(row.get("id") == "espn:cached" for row in result["matches"])
