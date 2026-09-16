from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from league_platform.public_predictions import (
    build_public_predictions,
    build_stale_research_predictions,
)


LOCK_HASH = "a" * 64


def _prediction(*, stage: str, observed: str, cutoff: str, model_hash: str = LOCK_HASH) -> dict:
    return {
        "fixture_id": "espn:1",
        "competition_id": "csl",
        "kickoff_at": "2026-08-18T12:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "freeze_stage": stage,
        "freeze_cutoff_at": cutoff,
        "prediction_observed_at": observed,
        "model_version": "strict-test-v1",
        "model_version_sha256": model_hash,
        "scoreline_probability": {"home": 0.5, "draw": 0.25, "away": 0.25},
        "scoreline_matrix": {"1-0": 0.5, "0-0": 0.25, "0-1": 0.25},
        "total_goals_probability": {"0": 0.25, "1": 0.75},
        "half_full_probability": {"H/H": 0.5, "D/D": 0.25, "A/A": 0.25},
        "live_feature_fields": ["market_1x2"],
        "expected_goals": {"home": 1.2, "away": 0.8},
    }


def _record(prediction: dict, *, captured: str, conflict: bool = False, model_hash: str = LOCK_HASH) -> dict:
    return {
        "record_key": f"record-{captured}",
        "content_sha256": "b" * 64,
        "captured_at": captured,
        "conflict": conflict,
        "model_version_sha256": model_hash,
        "prediction": prediction,
    }


def test_public_projection_uses_latest_active_stage_and_blocks_drafts(tmp_path: Path):
    lock = tmp_path / "prospective-model-lock.json"
    lock.write_text(json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}), encoding="utf-8")
    archive = tmp_path / "prospective_predictions.jsonl"
    rows = [
        _record(
            _prediction(stage="t_minus_24h", observed="2026-08-17T12:05:00Z", cutoff="2026-08-17T12:00:00Z"),
            captured="2026-08-17T12:05:00Z",
        ),
        _record(
            _prediction(stage="t_minus_6h", observed="2026-08-18T06:05:00Z", cutoff="2026-08-18T06:00:00Z"),
            captured="2026-08-18T06:05:00Z",
        ),
        # A later poll for the same stage is selected, but a conflict is not.
        _record(
            _prediction(stage="t_minus_6h", observed="2026-08-18T06:06:00Z", cutoff="2026-08-18T06:00:00Z"),
            captured="2026-08-18T06:06:00Z",
            conflict=True,
        ),
        _record(
            _prediction(stage="t_minus_24h", observed="2026-08-17T12:05:00Z", cutoff="2026-08-17T12:00:00Z", model_hash="c" * 64),
            captured="2026-08-17T12:05:00Z",
            model_hash="c" * 64,
        ),
    ]
    archive.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    snapshot = {
        "generated_at": "2026-08-18T07:00:00Z",
        "current_data": {"as_of": "2026-08-18T07:00:00Z"},
        "matches": [
            {
                "id": "espn:1",
                "status": "upcoming",
                "kickoff_at": "2026-08-18T12:00:00Z",
            },
            {
                "id": "espn:2",
                "status": "upcoming",
                "kickoff_at": "2026-08-18T13:00:00Z",
            },
        ],
    }

    result = build_public_predictions(snapshot, archive_path=archive, lock_path=lock)

    assert result["prediction_source"] == "prospective_archive"
    assert [item["fixture_id"] for item in result["predictions"]] == ["espn:1"]
    prediction = result["predictions"][0]
    assert prediction["freeze_stage"] == "t_minus_6h"
    assert [item["status"] for item in prediction["freeze_versions"]] == ["available", "available", "pending", "pending"]
    assert prediction["coverage"]["level"] == "low"
    assert result["blocked"] == [{
        "fixture_id": "espn:2",
        "reason": "no_causal_freeze_record",
        "message": "尚未观察到满足冻结截点的当前模型归档；不生成即时草稿替代。",
    }]


def test_public_projection_only_lists_fixtures_inside_next_seven_days(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    archive.write_text("", encoding="utf-8")
    as_of = datetime(2026, 8, 18, 7, 0, tzinfo=timezone.utc)
    result = build_public_predictions(
        {
            "generated_at": as_of.isoformat(),
            "current_data": {"status": "fresh", "as_of": as_of.isoformat()},
            "matches": [
                {
                    "id": "inside-seven-days",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-25T07:00:00+00:00",
                },
                {
                    "id": "outside-seven-days",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-25T07:00:01+00:00",
                },
            ],
        },
        archive_path=archive,
        lock_path=lock,
    )

    assert [item["fixture_id"] for item in result["blocked"]] == ["inside-seven-days"]


def test_public_projection_coverage_is_total_and_blocked_rows_keep_identity(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    archive.write_text("", encoding="utf-8")
    snapshot = {
        "generated_at": "2026-08-18T07:00:00Z",
        "current_data": {"status": "fresh", "as_of": "2026-08-18T07:00:00Z"},
        "matches": [
            {
                "id": "espn:missing-freeze",
                "competition_id": "csl",
                "season": "2026",
                "kickoff_at": "2026-08-18T12:00:00Z",
                "home_team": "Home FC",
                "away_team": "Away FC",
                "home_team_id": "csl:home-fc",
                "away_team_id": "csl:away-fc",
                "status": "upcoming",
            },
            {
                "id": "outside-horizon",
                "competition_id": "csl",
                "kickoff_at": "2026-08-26T12:00:00Z",
                "status": "upcoming",
            },
        ],
    }

    result = build_public_predictions(snapshot, archive_path=archive, lock_path=lock)

    assert result["predictions"] == []
    assert len(result["predictions"]) + len(result["blocked"]) == 1
    blocked = result["blocked"][0]
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "no_causal_freeze_record"
    assert blocked["team_identities"]["home"]["team_id"] == "csl:home-fc"
    assert blocked["team_identities"]["away"]["team_id"] == "csl:away-fc"
    assert blocked["team_identities"]["home"]["canonical_name"] == "Home FC"
    assert blocked["team_identities"]["away"]["canonical_name"] == "Away FC"
    assert result["coverage"] == {
        "schema_version": "matchline.prediction_coverage.v1",
        "candidate_count": 1,
        "prediction_count": 0,
        "blocked_count": 1,
        "complete": True,
        "missing_fixture_ids": [],
    }


def test_public_projection_uses_fixture_feed_when_compact_matches_are_missing(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    archive.write_text("", encoding="utf-8")
    result = build_public_predictions(
        {
            "generated_at": "2026-08-18T07:00:00Z",
            "current_data": {"status": "fresh", "as_of": "2026-08-18T07:00:00Z"},
            "fixture_feed": {
                "provider": "OpenFootball",
                "fixtures": [
                    {
                        "id": "openfootball:csl:feed-only",
                        "competition_id": "csl",
                        "kickoff_at": "2026-08-18T12:00:00Z",
                        "home_team": "Feed Home",
                        "away_team": "Feed Away",
                        "status": "upcoming",
                    }
                ],
            },
        },
        archive_path=archive,
        lock_path=lock,
    )
    assert len(result["predictions"]) + len(result["blocked"]) == 1
    assert result["blocked"][0]["fixture_id"] == "openfootball:csl:feed-only"


def test_public_projection_blocks_date_only_fixture_feed_rows(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    archive.write_text("", encoding="utf-8")
    result = build_public_predictions(
        {
            "generated_at": "2026-08-18T07:00:00Z",
            "current_data": {"status": "fresh", "as_of": "2026-08-18T07:00:00Z"},
            "fixture_feed": {
                "provider": "OpenFootball",
                "fixtures": [],
                "date_only_fixtures": [
                    {
                        "id": "openfootball:csl:date-only",
                        "competition_id": "csl",
                        "kickoff_date": "2026-08-18",
                        "home_team": "Date Home",
                        "away_team": "Date Away",
                        "status": "upcoming",
                    }
                ],
            },
        },
        archive_path=archive,
        lock_path=lock,
    )

    assert result["predictions"] == []
    assert [item["fixture_id"] for item in result["blocked"]] == [
        "openfootball:csl:date-only"
    ]
    assert result["coverage"] == {
        "schema_version": "matchline.prediction_coverage.v1",
        "candidate_count": 1,
        "prediction_count": 0,
        "blocked_count": 1,
        "complete": True,
        "missing_fixture_ids": [],
    }


def test_public_projection_rejects_rows_observed_after_snapshot(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}), encoding="utf-8")
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        json.dumps(
            _record(
                _prediction(stage="t_minus_24h", observed="2026-08-18T08:01:00Z", cutoff="2026-08-18T08:00:00Z"),
                captured="2026-08-18T08:01:00Z",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    result = build_public_predictions(
        {
            "generated_at": "2026-08-18T08:00:00Z",
            "current_data": {"as_of": "2026-08-18T08:00:00Z"},
            "matches": [{"id": "espn:1", "status": "upcoming", "kickoff_at": "2026-08-18T12:00:00Z"}],
        },
        archive_path=archive,
        lock_path=lock,
    )
    assert result["predictions"] == []
    assert result["blocked"][0]["reason"] == "no_causal_freeze_record"


def test_public_projection_rejects_non_finite_probability(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    prediction = _prediction(
        stage="t_minus_24h",
        observed="2026-08-17T12:05:00Z",
        cutoff="2026-08-17T12:00:00Z",
    )
    prediction["scoreline_probability"] = {
        "home": float("nan"),
        "draw": 0.25,
        "away": 0.25,
    }
    archive.write_text(
        json.dumps(_record(prediction, captured="2026-08-17T12:05:00Z")) + "\n",
        encoding="utf-8",
    )
    result = build_public_predictions(
        {
            "generated_at": "2026-08-18T07:00:00Z",
            "current_data": {"status": "fresh", "as_of": "2026-08-18T07:00:00Z"},
            "matches": [
                {
                    "id": "espn:1",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-18T12:00:00Z",
                }
            ],
        },
        archive_path=archive,
        lock_path=lock,
    )

    assert result["predictions"] == []
    assert result["blocked"][0]["reason"] == "no_causal_freeze_record"


def test_public_projection_blocks_frozen_lane_when_source_snapshot_is_stale(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    prediction = _prediction(
        stage="t_minus_24h",
        observed="2026-08-18T08:05:00Z",
        cutoff="2026-08-18T08:00:00Z",
    )
    archive.write_text(json.dumps(_record(prediction, captured="2026-08-18T08:05:00Z")) + "\n", encoding="utf-8")
    result = build_public_predictions(
        {
            "generated_at": "2026-08-18T08:00:00Z",
            "current_data": {"status": "stale", "as_of": "2026-08-18T08:00:00Z"},
            "matches": [{"id": "espn:1", "status": "upcoming", "kickoff_at": "2026-08-18T12:00:00Z"}],
        },
        archive_path=archive,
        lock_path=lock,
    )
    assert result["status"] == "stale_snapshot"
    assert result["predictions"] == []
    assert result["blocked"][0]["reason"] == "current_source_snapshot_expired"


def test_stale_research_projection_keeps_future_archive_rows_explicitly_stale(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    prediction = _prediction(
        stage="t_minus_24h",
        observed="2026-08-18T08:05:00Z",
        cutoff="2026-08-18T08:00:00Z",
    )
    archive.write_text(
        json.dumps(_record(prediction, captured="2026-08-18T08:05:00Z")) + "\n",
        encoding="utf-8",
    )
    result = build_stale_research_predictions(
        {
            "generated_at": "2026-08-18T08:00:00Z",
            "current_data": {"status": "stale", "as_of": "2026-08-18T08:00:00Z"},
            "matches": [
                {
                    "id": "espn:1",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-18T12:00:00Z",
                }
            ],
        },
        archive_path=archive,
        lock_path=lock,
        now=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "stale_snapshot"
    assert len(result["predictions"]) == 1
    assert result["predictions"][0]["stale_snapshot"] is True
    assert result["predictions"][0]["research_snapshot_status"] == "stale_snapshot"
    assert result["prediction_source"] == "prospective_archive_stale_snapshot"


def test_stale_research_projection_never_keeps_kickoffs_before_reference_clock(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    prediction = _prediction(
        stage="t_minus_24h",
        observed="2026-08-18T08:05:00Z",
        cutoff="2026-08-18T08:00:00Z",
    )
    archive.write_text(
        json.dumps(_record(prediction, captured="2026-08-18T08:05:00Z")) + "\n",
        encoding="utf-8",
    )
    result = build_stale_research_predictions(
        {
            "generated_at": "2026-08-18T08:00:00Z",
            "current_data": {"status": "stale", "as_of": "2026-08-18T08:00:00Z"},
            "matches": [
                {"id": "espn:1", "status": "upcoming", "kickoff_at": "2026-08-18T12:00:00Z"},
                {"id": "espn:2", "status": "upcoming", "kickoff_at": "2026-08-18T18:00:00Z"},
            ],
        },
        archive_path=archive,
        lock_path=lock,
        now=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
    )
    assert result["predictions"] == []
    assert result["blocked"][0]["reason"] == "stale_snapshot_no_causal_archive"


def test_public_projection_supports_a_three_day_horizon(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    archive.write_text("", encoding="utf-8")
    as_of = datetime(2026, 8, 18, 7, 0, tzinfo=timezone.utc)
    result = build_public_predictions(
        {
            "generated_at": as_of.isoformat(),
            "current_data": {"status": "fresh", "as_of": as_of.isoformat()},
            "matches": [
                {
                    "id": "inside-three-days",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-21T07:00:00Z",
                },
                {
                    "id": "outside-three-days",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-22T07:00:00Z",
                },
            ],
        },
        archive_path=archive,
        lock_path=lock,
        prediction_horizon=timedelta(days=3),
    )

    assert [item["fixture_id"] for item in result["blocked"]] == [
        "inside-three-days"
    ]


def test_public_projection_rejects_non_finite_nested_probability(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"status": "pending_prospective_window", "model_version_sha256": LOCK_HASH}),
        encoding="utf-8",
    )
    archive = tmp_path / "archive.jsonl"
    prediction = _prediction(
        stage="t_minus_24h",
        observed="2026-08-17T12:05:00Z",
        cutoff="2026-08-17T12:00:00Z",
    )
    prediction["scoreline_matrix"]["1-0"] = float("nan")
    archive.write_text(
        json.dumps(_record(prediction, captured="2026-08-17T12:05:00Z")) + "\n",
        encoding="utf-8",
    )

    result = build_public_predictions(
        {
            "generated_at": "2026-08-18T07:00:00Z",
            "current_data": {"status": "fresh", "as_of": "2026-08-18T07:00:00Z"},
            "matches": [
                {
                    "id": "espn:1",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-18T12:00:00Z",
                }
            ],
        },
        archive_path=archive,
        lock_path=lock,
    )

    assert result["predictions"] == []
    assert result["blocked"][0]["reason"] == "no_causal_freeze_record"
