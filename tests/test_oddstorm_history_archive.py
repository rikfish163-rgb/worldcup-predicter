from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from league_platform import oddstorm_history_archive as archive


NOW = datetime(2026, 8, 16, 7, 15, tzinfo=timezone.utc)


def _snapshot() -> dict:
    return {
        "as_of": NOW.isoformat(),
        "oddstorm": {
            "lines": [
                {
                    "match_id": "m-1",
                    "home_team": "A",
                    "away_team": "B",
                    "kickoff_at": "2026-08-18T10:35:00+00:00",
                    "handicap": {
                        "line": 0,
                        "home_odds_id": "101",
                        "away_odds_id": "102",
                    },
                    "total": {
                        "line": 2.5,
                        "over_odds_id": "201",
                        "under_odds_id": "202",
                    },
                },
                {
                    "match_id": "m-1",
                    "home_team": "A",
                    "away_team": "B",
                    "kickoff_at": "2026-08-18T10:35:00+00:00",
                    "handicap": {
                        "line": 0.25,
                        "home_odds_id": "103",
                        "away_odds_id": "104",
                    },
                },
            ]
        },
    }


def test_capture_fails_closed_before_history_network_without_written_permission(
    tmp_path: Path,
    monkeypatch,
):
    live = tmp_path / "current.json"
    output = tmp_path / "oddstorm_history.jsonl"
    live.write_text(json.dumps(_snapshot()), encoding="utf-8")

    def exploding_fetch(*_args, **_kwargs):
        raise AssertionError("history network must not open without written permission")

    monkeypatch.setattr(archive, "fetch_oddstorm_history", exploding_fetch)
    report = archive.capture(live, output, now=NOW, max_requests=2)

    assert report["status"] == "rights_blocked"
    assert report["fetched_count"] == 0
    assert report["appended"] == 0
    assert report["network_opened"] is False
    assert report["terms_url"] == "https://www.oddstorm.com/terms"
    assert not output.exists()


def test_capture_rotates_and_appends_display_only_history(tmp_path: Path, monkeypatch):
    live = tmp_path / "current.json"
    output = tmp_path / "oddstorm_history.jsonl"
    live.write_text(json.dumps(_snapshot()), encoding="utf-8")

    calls: list[str] = []

    def fake_fetch(odds_id, *, fixture_kickoff_at, now, authorization_reference):
        assert authorization_reference == "test-written-permission"
        calls.append(str(odds_id))
        return {
            "history": [
                {
                    "odds_id": str(odds_id),
                    "decimal_odds": 1.8,
                    "provider_date_raw": "Sun 16 Aug 06:00",
                    "effective_at": "2026-08-16T05:00:00+00:00",
                    "observed_at": now.isoformat(),
                    "available_at": now.isoformat(),
                    "enters_model": True,
                    "source": {
                        "url": f"https://www.oddstorm.com/odds/oddhistory?id={odds_id}",
                        "raw_sha256": "a" * 64,
                    },
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(archive, "fetch_oddstorm_history", fake_fetch)
    first = archive.capture(
        live,
        output,
        now=NOW,
        max_requests=2,
        authorization_reference="test-written-permission",
    )
    assert first["candidate_count"] == 6
    assert first["selected_count"] == 2
    assert first["appended"] == 2
    assert len(calls) == 2

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert all(row["enters_model"] is False for row in rows)
    assert all(
        row["model_exclusion_reason"] == "independent_market_fixture_join_unproven"
        for row in rows
    )
    assert all(row["record_type"] == "oddstorm_history" for row in rows)
    assert all(row["history"]["source"]["raw_sha256"] == "a" * 64 for row in rows)

    # Stay within the same 15-minute rotation slot so only the capture time
    # changes; the semantic observation must still deduplicate.
    second = archive.capture(
        live,
        output,
        now=NOW.replace(second=NOW.second + 1),
        max_requests=2,
        authorization_reference="test-written-permission",
    )
    assert second["appended"] == 0
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2
    assert all(len(row["observation_key"]) == 64 for row in rows)


def test_capture_deduplicates_legacy_rows_by_semantic_identity(tmp_path: Path, monkeypatch):
    live = tmp_path / "current.json"
    output = tmp_path / "oddstorm_history.jsonl"
    live.write_text(json.dumps(_snapshot()), encoding="utf-8")
    legacy = {
        "record_type": "oddstorm_history",
        "match_id": "m-1",
        "market": "asian_handicap",
        "line": 0,
        "side": "home",
        "odds_id": "101",
        "history": {
            "odds_id": "101",
            "decimal_odds": 1.8,
            "provider_date_raw": "Sun 16 Aug 06:00",
            "effective_at": "2026-08-16T05:00:00+00:00",
            "effective_at_source": "provider_timestamp_year_inferred_from_fixture",
        },
        "content_sha256": "b" * 64,
    }
    output.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    def fake_fetch(odds_id, *, fixture_kickoff_at, now, authorization_reference):
        assert authorization_reference == "test-written-permission"
        if str(odds_id) != "101":
            return {"history": [], "errors": []}
        return {
            "history": [
                {
                    "odds_id": str(odds_id),
                    "decimal_odds": 1.8,
                    "provider_date_raw": "Sun 16 Aug 06:00",
                    "effective_at": "2026-08-16T05:00:00+00:00",
                    "effective_at_source": "provider_relative_age_from_observed_at",
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(archive, "fetch_oddstorm_history", fake_fetch)
    report = archive.capture(
        live,
        output,
        now=NOW,
        max_requests=6,
        authorization_reference="test-written-permission",
    )
    assert report["appended"] == 0
    assert len(output.read_text(encoding="utf-8").splitlines()) == 1


def test_capture_keeps_provider_errors_in_cycle_result(tmp_path: Path, monkeypatch):
    live = tmp_path / "current.json"
    output = tmp_path / "oddstorm_history.jsonl"
    live.write_text(json.dumps(_snapshot()), encoding="utf-8")

    def fake_fetch(odds_id, *, fixture_kickoff_at, now, authorization_reference):
        assert authorization_reference == "test-written-permission"
        return {"history": [], "errors": [{"stage": "fetch_or_parse", "error": "timeout"}]}

    monkeypatch.setattr(archive, "fetch_oddstorm_history", fake_fetch)
    report = archive.capture(
        live,
        output,
        now=NOW,
        max_requests=1,
        authorization_reference="test-written-permission",
    )
    assert report["status"] == "degraded"
    assert report["appended"] == 0
    assert report["fetch_errors"][0]["odds_id"]
    assert not output.exists()
