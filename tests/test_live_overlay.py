from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
from types import SimpleNamespace

import league_platform.live_overlay as live_overlay
from league_platform.live_overlay import (
    poll_openligadb_once,
    poll_openligadb_once_locked,
    poll_once,
    poll_once_locked,
    project_live_overlay,
    select_openligadb_live_targets,
    select_live_fixtures,
)


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def snapshot() -> dict:
    return {
        "espn": {
            "fixtures": [
                {
                    "id": "espn:live-1",
                    "competition_id": "premier-league",
                    "kickoff_at": "2026-08-21T10:30:00+00:00",
                    "status": "live",
                    "home_team": "Home",
                    "away_team": "Away",
                    "source": {"native_fixture_id": "live-1"},
                },
                {
                    "id": "espn:stale-upcoming",
                    "competition_id": "premier-league",
                    "kickoff_at": "2026-08-21T11:45:00+00:00",
                    "status": "upcoming",
                    "home_team": "Soon",
                    "away_team": "Later",
                    "source": {"native_fixture_id": "stale-upcoming"},
                },
                {
                    "id": "espn:future",
                    "competition_id": "premier-league",
                    "kickoff_at": "2026-08-21T13:00:00+00:00",
                    "status": "upcoming",
                    "source": {"native_fixture_id": "future"},
                },
                {
                    "id": "espn:finished",
                    "competition_id": "premier-league",
                    "kickoff_at": "2026-08-21T09:00:00+00:00",
                    "status": "finished",
                    "source": {"native_fixture_id": "finished"},
                },
            ]
        }
    }


def openfootball_snapshot() -> dict:
    canonical = {
        "id": "openfootball:bundesliga:fixture-1",
        "competition_id": "bundesliga",
        "kickoff_at": "2026-08-21T11:45:00+00:00",
        "status": "upcoming",
        "home_team": "FC Bayern München",
        "away_team": "VfB Stuttgart",
        "source": {
            "name": "OpenFootball",
            "license": "CC0-1.0",
            "raw_sha256": "a" * 64,
        },
    }
    return {
        "fixture_feed": {"fixtures": [canonical]},
        "openligadb": {
            "provider": "OpenLigaDB",
            "matches": [{
                "id": "openligadb:83156",
                "provider_match_id": "83156",
                "competition_id": "bundesliga",
                "kickoff_at": "2026-08-21T11:45:00+00:00",
                "status": "upcoming",
                "home_team": "FC Bayern München",
                "away_team": "VfB Stuttgart",
                "source": {
                    "name": "OpenLigaDB",
                    "url": "https://api.openligadb.de/getmatchdata/bl1/2026",
                    "license": "ODbL-1.0",
                    "license_url": "https://www.openligadb.de/lizenz",
                    "attribution_required": True,
                    "raw_sha256": "b" * 64,
                },
            }],
        },
    }


def test_select_live_fixtures_repairs_lagging_upcoming_status_without_future_rows() -> None:
    rows = select_live_fixtures(snapshot(), now=NOW)
    assert [row["id"] for row in rows] == ["espn:live-1", "espn:stale-upcoming"]
    assert all(row["kickoff_at"] <= NOW.isoformat() for row in rows)


def test_openligadb_live_target_requires_exact_canonical_fixture_join() -> None:
    source_snapshot = openfootball_snapshot()
    rows = select_live_fixtures(source_snapshot, now=NOW)
    assert [row["id"] for row in rows] == ["openfootball:bundesliga:fixture-1"]

    targets = select_openligadb_live_targets(source_snapshot, rows)
    assert targets == [{
        **rows[0],
        "provider": "OpenLigaDB",
        "provider_match_id": "83156",
    }]

    source_snapshot["openligadb"]["matches"][0]["kickoff_at"] = "2026-08-21T11:46:00+00:00"
    assert select_openligadb_live_targets(source_snapshot, rows) == []


def test_live_overlay_is_display_only_and_bounded() -> None:
    rows = select_live_fixtures(snapshot(), now=NOW)
    overlay = project_live_overlay(
        rows,
        {
            "fixture_updates": [{
                "fixture_id": "espn:live-1",
                "status": "live",
                "score": {"home": 1, "away": 0},
                "clock": "42'",
                "period": 1,
                "observed_at": "2026-08-21T11:59:30+00:00",
                "source": {"name": "ESPN event summary"},
            }],
            "incidents": [{
                "fixture_id": "espn:live-1",
                "retrieved_at": "2026-08-21T11:59:30+00:00",
                "events": [{"event_id": "goal-1", "type": "goal"}],
                "source": {"name": "ESPN event summary"},
            }],
            "match_stats": [{
                "fixture_id": "espn:live-1",
                "retrieved_at": "2026-08-21T11:59:29+00:00",
                "teams": {},
                "source": {"name": "ESPN event summary"},
            }],
            "errors": [],
        },
        now=NOW,
    )
    assert overlay["schema_version"] == "matchline.live_overlay.v1"
    assert overlay["status"] == "ok"
    live = next(row for row in overlay["fixtures"] if row["fixture_id"] == "espn:live-1")
    assert live["score"] == {"home": 1, "away": 0}
    assert live["events"] == [{"event_id": "goal-1", "type": "goal"}]
    assert live["enters_model"] is False
    assert live["model_eligible"] is False
    assert live["model_exclusion_reason"] == "live_or_postmatch_event"


def test_poll_once_writes_atomic_current_and_append_only_archive(tmp_path: Path) -> None:
    live_path = tmp_path / "current.json"
    output = tmp_path / "live_overlay.json"
    public_output = tmp_path / "public" / "live_overlay.json"
    served_output = tmp_path / "dist" / "client" / "live_overlay.json"
    archive_dir = tmp_path / "archive"
    live_path.write_text(json.dumps(snapshot()), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_fetch(fixtures: list[dict], **_kwargs):
        calls.append([row["id"] for row in fixtures])
        return {"fixture_updates": [], "incidents": [], "match_stats": [], "errors": []}

    result = poll_once(
        live_path=live_path,
        output=output,
        public_output=public_output,
        served_output=served_output,
        archive_dir=archive_dir,
        now=NOW,
        fetcher=fake_fetch,
        min_free_bytes=0,
    )
    assert result["status"] == "ok"
    assert calls == [["espn:live-1", "espn:stale-upcoming"]]
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == "matchline.live_overlay.v1"
    assert json.loads(public_output.read_text(encoding="utf-8"))["schema_version"] == "matchline.live_overlay.v1"
    assert json.loads(served_output.read_text(encoding="utf-8"))["schema_version"] == "matchline.live_overlay.v1"
    archives = list(archive_dir.glob("*.json"))
    assert len(archives) == 1


def test_openligadb_poll_is_explicit_display_only_and_keeps_attribution(tmp_path: Path) -> None:
    live_path = tmp_path / "current.json"
    output = tmp_path / "live_overlay.json"
    live_path.write_text(json.dumps(openfootball_snapshot()), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_fetch(targets: list[dict], **_kwargs):
        calls.append([row["provider_match_id"] for row in targets])
        return {
            "source": {
                "name": "OpenLigaDB",
                "role": "live_display_only",
                "license": "ODbL-1.0",
                "license_url": "https://www.openligadb.de/lizenz",
                "attribution_required": True,
                "share_alike_required": True,
            },
            "fixture_updates": [{
                "fixture_id": "openfootball:bundesliga:fixture-1",
                "status": "in_progress_window",
                "score": {"home": 1, "away": 0},
                "observed_at": NOW.isoformat(),
                "source": {
                    "name": "OpenLigaDB",
                    "license": "ODbL-1.0",
                    "license_url": "https://www.openligadb.de/lizenz",
                    "attribution_required": True,
                    "share_alike_required": True,
                },
            }],
            "incidents": [],
            "match_stats": [],
            "errors": [],
        }

    result = poll_openligadb_once(
        live_path=live_path,
        output=output,
        archive_dir=None,
        now=NOW,
        fetcher=fake_fetch,
        min_free_bytes=0,
    )

    assert calls == [["83156"]]
    assert result["status"] == "ok"
    assert result["source"]["name"] == "OpenLigaDB"
    assert result["source"]["license"] == "ODbL-1.0"
    assert result["source"]["share_alike_required"] is True
    assert result["policy"]["model_eligible"] is False
    assert result["fixtures"][0]["enters_model"] is False
    assert result["fixtures"][0]["source"]["share_alike_required"] is True
    assert json.loads(output.read_text(encoding="utf-8"))["source"]["attribution_required"] is True


def test_poll_once_does_not_call_provider_when_no_fixture_is_live(tmp_path: Path) -> None:
    live_path = tmp_path / "current.json"
    output = tmp_path / "live_overlay.json"
    live_path.write_text(json.dumps({"espn": {"fixtures": []}}), encoding="utf-8")
    called = False

    def fake_fetch(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    result = poll_once(live_path=live_path, output=output, archive_dir=None, now=NOW, fetcher=fake_fetch, min_free_bytes=0)
    assert result["status"] == "no_live_fixtures"
    assert called is False


def test_default_espn_live_overlay_fails_closed_before_network_without_written_permission(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "current.json"
    output = tmp_path / "live_overlay.json"
    live_path.write_text(json.dumps(snapshot()), encoding="utf-8")

    result = poll_once(
        live_path=live_path,
        output=output,
        archive_dir=None,
        now=NOW,
        fetcher=live_overlay.fetch_espn_markets,
        min_free_bytes=0,
    )

    assert result["status"] == "rights_blocked"
    assert result["fixture_count"] == 0
    assert result["source"]["terms_url"] == "https://disneytermsofuse.com/english/"
    assert result["policy"]["network_opened"] is False
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "rights_blocked"


def test_poll_once_blocks_before_external_snapshot_read_when_output_filesystem_is_full(tmp_path: Path) -> None:
    live_path = tmp_path / "current.json"
    output = tmp_path / "live_overlay.json"
    public_output = tmp_path / "public" / "live_overlay.json"
    live_path.write_text(json.dumps(snapshot()), encoding="utf-8")
    called = False

    def fake_fetch(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    def full_disk(_path: Path):
        return SimpleNamespace(free=0)

    result = poll_once(
        live_path=live_path,
        output=output,
        public_output=public_output,
        archive_dir=None,
        now=NOW,
        fetcher=fake_fetch,
        min_free_bytes=1,
        disk_usage_fn=full_disk,
    )
    assert result["status"] == "blocked_storage"
    assert result["storage"]["blocked_paths"] == [str(public_output)]
    assert called is False
    assert not output.exists()


def test_locked_poll_preserves_last_overlay_while_prospective_cycle_owns_runtime(tmp_path: Path) -> None:
    live_path = tmp_path / "current.json"
    output = tmp_path / "live_overlay.json"
    lock_path = tmp_path / "prospective-cycle.lock"
    live_path.write_text(json.dumps(snapshot()), encoding="utf-8")
    output.write_text('{"status":"last_good"}\n', encoding="utf-8")
    called = False

    def fake_fetch(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = poll_once_locked(
            live_path=live_path,
            output=output,
            archive_dir=None,
            lock_path=lock_path,
            now=NOW,
            fetcher=fake_fetch,
            min_free_bytes=0,
        )

    assert result["status"] == "busy"
    assert result["errors"] == ["runtime_mutation_lock_busy"]
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "last_good"
    assert called is False


def test_locked_openligadb_poll_preserves_last_overlay_while_runtime_is_busy(tmp_path: Path) -> None:
    live_path = tmp_path / "current.json"
    output = tmp_path / "live_overlay.json"
    lock_path = tmp_path / "prospective-cycle.lock"
    live_path.write_text(json.dumps(openfootball_snapshot()), encoding="utf-8")
    output.write_text('{"status":"last_good"}\n', encoding="utf-8")
    called = False

    def fake_fetch(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = poll_openligadb_once_locked(
            live_path=live_path,
            output=output,
            archive_dir=None,
            lock_path=lock_path,
            now=NOW,
            fetcher=fake_fetch,
            min_free_bytes=0,
        )

    assert result["status"] == "busy"
    assert result["source"]["name"] == "OpenLigaDB"
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "last_good"
    assert called is False
