from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform import publish_openligadb_cache as publisher


def test_cache_payload_is_canonical_and_contains_only_the_fixed_source_contract() -> None:
    season = 2026
    body = publisher.build_cache_payload(
        [{"matchID": 1, "team1": {"teamName": "A"}}],
        season=season,
        league="bl2",
        retrieved_at="2026-09-02T12:00:00.000Z",
    )

    decoded = json.loads(body)
    assert decoded == {
        "league": "bl2",
        "payload": [{"matchID": 1, "team1": {"teamName": "A"}}],
        "retrievedAt": "2026-09-02T12:00:00.000Z",
        "schema": "matchline.openligadb.current.cache.v1",
        "season": season,
        "sourceUrl": "https://api.openligadb.de/getmatchdata/bl2/2026",
    }
    assert body == publisher.canonical_json_bytes(decoded)


def test_openligadb_source_urls_are_limited_to_the_three_supported_leagues() -> None:
    assert publisher.source_url_for_season(2026, "bl1").endswith("/bl1/2026")
    assert publisher.source_url_for_season(2026, "bl2").endswith("/bl2/2026")
    assert publisher.source_url_for_season(2026, "bl3").endswith("/bl3/2026")
    with pytest.raises(ValueError, match="allowlisted"):
        publisher.source_url_for_season(2026, "world-cup")


def test_cache_endpoint_is_fixed_to_the_public_sites_route() -> None:
    assert publisher.normalized_endpoint(publisher.DEFAULT_ENDPOINT) == publisher.DEFAULT_ENDPOINT
    for value in (
        "https://evil.example/api/v1/openligadb",
        "https://matchline-intelligence.willif57kbkd.chatgpt.site/api/v1/openligadb?next=evil",
        "http://matchline-intelligence.willif57kbkd.chatgpt.site/api/v1/openligadb",
    ):
        with pytest.raises(ValueError, match="fixed Sites"):
            publisher.normalized_endpoint(value)


def test_current_season_switches_at_july_boundary() -> None:
    assert publisher.current_season(datetime(2026, 6, 30, tzinfo=timezone.utc)) == 2025
    assert publisher.current_season(datetime(2026, 7, 1, tzinfo=timezone.utc)) == 2026


def test_openligadb_timer_has_no_local_snapshot_path_or_unbounded_endpoint() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/matchline-openligadb-cache.service").read_text(encoding="utf-8")
    timer = (root / "deploy/systemd/matchline-openligadb-cache.timer").read_text(encoding="utf-8")
    assert "publish_openligadb_cache --league all" in service
    assert "EnvironmentFile=%h/.config/matchline/matchline-sites.env" in service
    assert "offline_snapshot" not in service
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=true" in timer
