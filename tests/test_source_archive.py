from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform.source_archive import (
    SnapshotArchiveError,
    archive_source_snapshot,
    build_quality_report,
    read_snapshot_archive,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _snapshot(as_of: str, *, errors: list[dict] | None = None, count: int = 1) -> dict:
    return {
        "schema_version": "1.0.0",
        "as_of": as_of,
        "espn": {
            "provider": "ESPN",
            "errors": errors or [],
            "fixtures": [{"id": str(index)} for index in range(count)],
        },
        "espn_markets": {
            "provider": "ESPN event summary",
            "errors": [],
            "markets": [{"fixture_id": str(index)} for index in range(count - 1)],
        },
        "understat": {
            "provider": "Understat",
            "errors": [],
            "team_features": [{"team": str(index)} for index in range(count + 1)],
        },
    }


def test_archive_is_append_only_and_deduplicates_raw_content(tmp_path: Path):
    archive_dir = tmp_path / "archive"
    first = _snapshot("2026-08-10T09:00:00+00:00", count=2)
    second = _snapshot("2026-08-10T10:00:00+00:00", count=3)

    first_record = archive_source_snapshot(first, archive_dir, now=NOW)
    duplicate = archive_source_snapshot(first, archive_dir, now=NOW)
    second_record = archive_source_snapshot(second, archive_dir, now=NOW)

    assert first_record["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert first_record["content_sha256"] != second_record["content_sha256"]
    assert len(list((archive_dir / "raw").glob("*.json"))) == 2
    assert len((archive_dir / "snapshots.jsonl").read_text().splitlines()) == 2

    records = read_snapshot_archive(archive_dir, now=NOW)
    assert [item["as_of"] for item in records] == [
        "2026-08-10T09:00:00+00:00",
        "2026-08-10T10:00:00+00:00",
    ]
    assert records[0]["fixture_count"] == 2
    assert records[0]["market_count"] == 1
    assert records[0]["xg_count"] == 3

    report = build_quality_report(archive_dir, now=NOW)
    assert report["snapshot_count"] == 2
    assert report["unique_content_count"] == 2
    assert report["latest"]["fixture_count"] == 3
    assert report["totals"] == {"fixtures": 5, "markets": 3, "xg": 7}


def test_archive_records_provider_degradation_and_counts(tmp_path: Path):
    payload = _snapshot("2026-08-10T09:00:00+00:00", errors=[{"error": "offline"}], count=2)

    record = archive_source_snapshot(payload, tmp_path, now=NOW)

    assert record["provider_status"]["espn"] == "degraded"
    assert record["provider_errors"]["espn"] == 1
    assert record["counts"] == {
        "fixtures": 2,
        "markets": 1,
        "xg": 3,
        "xg_team_features": 3,
    }


def test_archive_keeps_optional_top_level_provider_status_and_errors(tmp_path: Path):
    payload = _snapshot("2026-08-10T09:00:00+00:00")
    payload["news"] = {
        "provider": "news",
        "errors": [{"stage": "offline"}],
        "items": [],
    }
    payload["weather"] = {
        "provider": "weather",
        "errors": [],
        "observations": [{"fixture_id": "1"}],
    }

    record = archive_source_snapshot(payload, tmp_path, now=NOW)

    assert record["provider_status"]["news"] == "unavailable"
    assert record["provider_status"]["weather"] == "ok"
    assert record["provider_errors"]["news"] == 1
    assert record["provider_counts"]["weather"] == 1


@pytest.mark.parametrize(
    "payload, message",
    [
        (b"{not-json", "corrupt"),
        ({"as_of": "2026-08-12T00:00:00+00:00", "fixtures": []}, "future"),
    ],
)
def test_archive_rejects_corrupt_or_future_input(tmp_path: Path, payload, message: str):
    with pytest.raises(SnapshotArchiveError, match=message):
        archive_source_snapshot(payload, tmp_path, now=NOW)


def test_archive_rejects_oversized_input_without_creating_data(tmp_path: Path):
    payload = _snapshot("2026-08-10T09:00:00+00:00", count=1)
    raw = json.dumps(payload).encode()
    input_path = tmp_path / "current.json"
    input_path.write_bytes(raw)

    with pytest.raises(SnapshotArchiveError, match="maximum accepted size"):
        archive_source_snapshot(input_path, tmp_path / "archive", now=NOW, max_bytes=len(raw) - 1)

    assert not (tmp_path / "archive").exists()


def test_archive_reader_rejects_corrupt_manifest_and_raw(tmp_path: Path):
    archive_dir = tmp_path / "archive"
    record = archive_source_snapshot(
        _snapshot("2026-08-10T09:00:00+00:00"), archive_dir, now=NOW
    )
    manifest = archive_dir / "snapshots.jsonl"
    manifest.write_text(manifest.read_text() + "{broken\n", encoding="utf-8")
    with pytest.raises(SnapshotArchiveError, match="manifest JSON is corrupt"):
        read_snapshot_archive(archive_dir, now=NOW)

    manifest.write_text(
        json.dumps({**record, "duplicate": False}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    Path(archive_dir / record["raw_path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotArchiveError, match="content hash"):
        read_snapshot_archive(archive_dir, now=NOW)
