from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path

import pytest

from league_platform.facts_snapshot_refresh import refresh


NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _durable_raw_root(tmp_path: Path) -> Path:
    root = tmp_path / "openfootball-raw"
    root.mkdir()
    (root / "manifest.jsonl").write_text('{"schema_version":"1.0.0"}\n', encoding="utf-8")
    return root


def _snapshot() -> dict:
    return {
        "as_of": NOW.isoformat(),
        "fixture_feed": {
            "fixtures": [
                {"id": "upcoming-1", "status": "upcoming"},
                {"id": "finished-1", "status": "finished"},
                {"id": "live-1", "status": "live"},
            ]
        },
        "openfootball_current": {"status": "ok"},
        "wikidata_entities": {"status": "partial"},
        "met_norway_weather": {"status": "partial"},
    }


def test_refresh_serializes_with_cycle_and_returns_facts_only_summary(tmp_path: Path) -> None:
    raw_root = _durable_raw_root(tmp_path)
    output = tmp_path / "runtime" / "current.json"
    cycle_lock = tmp_path / "runtime" / "prospective-cycle.lock"
    calls: list[dict] = []

    def fake_sync(path: Path, **kwargs):
        calls.append({"path": path, **kwargs})
        return _snapshot()

    result = refresh(
        output,
        archive_dir=tmp_path / "runtime" / "archive",
        openfootball_raw_archive_dir=raw_root,
        cycle_lock_path=cycle_lock,
        now=NOW,
        sync_fn=fake_sync,
    )

    assert result["schema_version"] == "matchline.facts_snapshot_refresh.v1"
    assert result["lane"] == "facts_only"
    assert result["model_lane"] == "not_run"
    assert result["fixtures"] == 3
    assert result["upcoming"] == 1
    assert result["finished"] == 1
    assert result["live"] == 1
    assert result["provider_statuses"] == {
        "met_norway_weather": "partial",
        "openfootball_current": "ok",
        "wikidata_entities": "partial",
    }
    assert calls and calls[0]["openfootball_raw_archive_dir"] == raw_root
    assert calls[0]["enable_wikidata"] is True


def test_refresh_rejects_volatile_or_missing_raw_archive_before_sync(tmp_path: Path) -> None:
    output = tmp_path / "current.json"
    called = False

    def fake_sync(*_args, **_kwargs):
        nonlocal called
        called = True
        return _snapshot()

    with pytest.raises(Exception, match="raw archive"):
        refresh(
            output,
            openfootball_raw_archive_dir=tmp_path / "missing",
            sync_fn=fake_sync,
        )
    assert called is False


def test_refresh_fails_closed_when_cycle_lock_is_busy(tmp_path: Path) -> None:
    raw_root = _durable_raw_root(tmp_path)
    lock_path = tmp_path / "prospective-cycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another Matchline facts"):
            refresh(
                tmp_path / "current.json",
                openfootball_raw_archive_dir=raw_root,
                cycle_lock_path=lock_path,
                sync_fn=lambda *_args, **_kwargs: _snapshot(),
            )
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def test_summary_does_not_include_snapshot_payload_or_prediction_fields(tmp_path: Path) -> None:
    raw_root = _durable_raw_root(tmp_path)
    snapshot = _snapshot()
    snapshot["predictions"] = [{"homeWin": 0.9}]
    result = refresh(
        tmp_path / "current.json",
        openfootball_raw_archive_dir=raw_root,
        cycle_lock_path=tmp_path / "lock",
        sync_fn=lambda *_args, **_kwargs: snapshot,
    )
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert "predictions" not in serialized
    assert "homeWin" not in serialized
