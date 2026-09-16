from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from league_platform.prospective_archive import append_predictions, validate_prediction


def _lock(tmp_path: Path) -> dict:
    model_file = tmp_path / "model.py"
    model_file.write_text("locked model\n", encoding="utf-8")
    return {
        "status": "pending_prospective_window",
        "model_name": "strict_dynamic_elo_scoreline_v2_causal_rho",
        "freeze_model_name": "strict_dynamic_elo_scoreline_stage_replay_v1",
        "model_version_sha256": "a" * 64,
        "model_files": [
            {"path": str(model_file), "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest()}
        ],
    }


def _prediction() -> dict:
    return {
        "fixture_id": "prospective:1",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-30T15:00:00+00:00",
        "freeze_stage": "t_minus_24h",
        "freeze_cutoff_at": "2026-08-29T15:00:00+00:00",
        "prediction_observed_at": "2026-08-29T16:00:00+00:00",
        "as_of": "2026-08-29T16:00:00+00:00",
        "model_version": "strict_dynamic_elo_scoreline_stage_replay_v1",
        "feature_times": [],
        "primary_probability_1x2": {"home": 0.4, "draw": 0.3, "away": 0.3},
    }


def test_append_predictions_is_idempotent_and_preserves_conflicts(tmp_path: Path):
    lock = _lock(tmp_path)
    output = tmp_path / "predictions.jsonl"
    prediction = _prediction()
    assert append_predictions(output, [prediction], lock=lock) == {
        "appended": 1,
        "skipped_duplicate": 0,
        "conflicts": 0,
    }
    later_poll = {
        **prediction,
        "as_of": "2026-08-29T17:00:00+00:00",
        "prediction_observed_at": "2026-08-29T17:00:00+00:00",
        "kickoff_time_observed_at": "2026-08-29T17:01:00+00:00",
    }
    assert append_predictions(output, [later_poll], lock=lock) == {
        "appended": 0,
        "skipped_duplicate": 1,
        "conflicts": 0,
    }
    changed = {**prediction, "primary_probability_1x2": {"home": 0.41, "draw": 0.29, "away": 0.30}}
    assert append_predictions(output, [changed], lock=lock) == {
        "appended": 1,
        "skipped_duplicate": 0,
        "conflicts": 1,
    }
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[1]["conflict"] is True
    assert rows[1]["conflict_with"] == [rows[0]["content_sha256"]]


def test_trace_only_source_changes_do_not_create_freeze_conflict(tmp_path: Path):
    lock = _lock(tmp_path)
    output = tmp_path / "predictions.jsonl"
    prediction = {
        **_prediction(),
        "source_file": "data/live/current.json",
        "source_name": "ESPN",
        "source_sha256": "a" * 64,
        "provider_fixture_id": "native-1",
        "feature_times": ["2026-08-29T14:00:00+00:00"],
        "live_feature_sources": [
            {"field": "market_1x2", "raw_sha256": "b" * 64, "observed_at": "2026-08-29T14:00:00+00:00"}
        ],
        "market_retrieved_at": "2026-08-29T14:00:00+00:00",
        "market_time_bound_at": "2026-08-29T14:00:00+00:00",
        "fixture_freeze_observed_at": "2026-08-29T14:00:00+00:00",
        "fixture_observation_basis": "current_fixture_source",
    }
    assert append_predictions(output, [prediction], lock=lock)["appended"] == 1
    polled = {
        **prediction,
        "source_sha256": "c" * 64,
        "feature_times": ["2026-08-29T14:30:00+00:00"],
        "live_feature_sources": [
            {"field": "market_1x2", "raw_sha256": "d" * 64, "observed_at": "2026-08-29T14:30:00+00:00"}
        ],
        "market_retrieved_at": "2026-08-29T14:30:00+00:00",
        "market_time_bound_at": "2026-08-29T14:30:00+00:00",
        "fixture_freeze_observed_at": "2026-08-29T14:30:00+00:00",
        "fixture_observation_basis": "archived_fixture_snapshot",
    }
    assert append_predictions(output, [polled], lock=lock) == {
        "appended": 0,
        "skipped_duplicate": 1,
        "conflicts": 0,
    }


def test_lineup_confirmation_poll_is_logically_idempotent(tmp_path: Path):
    lock = _lock(tmp_path)
    output = tmp_path / "predictions.jsonl"
    prediction = {
        **_prediction(),
        "kickoff_at": "2026-08-30T18:00:00+00:00",
        "freeze_stage": "lineup_confirmation",
        "freeze_cutoff_at": "2026-08-30T16:00:00+00:00",
        "lineup_observed_at": "2026-08-30T16:00:00+00:00",
        "lineup_confirmation_fingerprint": "b" * 64,
        "prediction_observed_at": "2026-08-30T16:05:00+00:00",
        "as_of": "2026-08-30T16:05:00+00:00",
    }
    assert append_predictions(output, [prediction], lock=lock)["appended"] == 1

    later_poll = {
        **prediction,
        "freeze_cutoff_at": "2026-08-30T16:20:00+00:00",
        "lineup_observed_at": "2026-08-30T16:20:00+00:00",
        "prediction_observed_at": "2026-08-30T16:25:00+00:00",
        "as_of": "2026-08-30T16:25:00+00:00",
    }
    assert append_predictions(output, [later_poll], lock=lock) == {
        "appended": 0,
        "skipped_duplicate": 1,
        "conflicts": 0,
    }

    changed = {
        **later_poll,
        "primary_probability_1x2": {"home": 0.41, "draw": 0.29, "away": 0.30},
    }
    assert append_predictions(output, [changed], lock=lock) == {
        "appended": 0,
        "skipped_duplicate": 1,
        "conflicts": 0,
    }


def test_lineup_confirmation_without_complete_xi_fingerprint_is_rejected(tmp_path: Path):
    lock = _lock(tmp_path)
    prediction = {
        **_prediction(),
        "kickoff_at": "2026-08-30T18:00:00+00:00",
        "freeze_stage": "lineup_confirmation",
        "freeze_cutoff_at": "2026-08-30T16:00:00+00:00",
        "lineup_observed_at": "2026-08-30T16:00:00+00:00",
        "prediction_observed_at": "2026-08-30T16:05:00+00:00",
        "as_of": "2026-08-30T16:05:00+00:00",
    }

    with pytest.raises(ValueError, match="complete XI fingerprint"):
        validate_prediction(prediction, lock=lock)


def test_lineup_confirmation_ignores_market_poll_when_xi_is_unchanged(tmp_path: Path):
    lock = _lock(tmp_path)
    output = tmp_path / "predictions.jsonl"
    prediction = {
        **_prediction(),
        "kickoff_at": "2026-08-30T18:00:00+00:00",
        "freeze_stage": "lineup_confirmation",
        "freeze_cutoff_at": "2026-08-30T16:00:00+00:00",
        "lineup_observed_at": "2026-08-30T16:00:00+00:00",
        "prediction_observed_at": "2026-08-30T16:05:00+00:00",
        "as_of": "2026-08-30T16:05:00+00:00",
        "lineup_confirmation_fingerprint": "b" * 64,
        "market_probability": {"home": 0.4, "draw": 0.3, "away": 0.3},
    }
    assert append_predictions(output, [prediction], lock=lock) == {
        "appended": 1,
        "skipped_duplicate": 0,
        "conflicts": 0,
    }
    later_poll = {
        **prediction,
        "freeze_cutoff_at": "2026-08-30T16:20:00+00:00",
        "lineup_observed_at": "2026-08-30T16:20:00+00:00",
        "prediction_observed_at": "2026-08-30T16:25:00+00:00",
        "as_of": "2026-08-30T16:25:00+00:00",
        "market_probability": {"home": 0.35, "draw": 0.32, "away": 0.33},
    }
    assert append_predictions(output, [later_poll], lock=lock) == {
        "appended": 0,
        "skipped_duplicate": 1,
        "conflicts": 0,
    }
    changed_xi = {**later_poll, "lineup_confirmation_fingerprint": "c" * 64}
    assert append_predictions(output, [changed_xi], lock=lock) == {
        "appended": 1,
        "skipped_duplicate": 0,
        "conflicts": 1,
    }


def test_conflict_only_trace_migration_can_append_clean_canonical_row(tmp_path: Path):
    lock = _lock(tmp_path)
    output = tmp_path / "predictions.jsonl"
    prediction = _prediction()
    assert append_predictions(output, [prediction], lock=lock)["appended"] == 1
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    rows[0]["conflict"] = True
    rows[0]["conflict_with"] = ["legacy-audit-digest"]
    output.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")

    assert append_predictions(output, [prediction], lock=lock) == {
        "appended": 1,
        "skipped_duplicate": 0,
        "conflicts": 0,
    }
    repaired = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert repaired[-1]["conflict"] is False


def test_prediction_archive_rejects_future_features_and_results(tmp_path: Path):
    lock = _lock(tmp_path)
    prediction = _prediction()
    future_feature = {**prediction, "feature_times": ["2026-08-29T15:01:00+00:00"]}
    with pytest.raises(ValueError, match="feature timestamp"):
        validate_prediction(future_feature, lock=lock)
    with pytest.raises(ValueError, match="result fields"):
        validate_prediction({**prediction, "outcome_1x2": "home"}, lock=lock)
    with pytest.raises(ValueError, match="fixture observation"):
        validate_prediction(
            {
                **prediction,
                "fixture_freeze_observed_at": "2026-08-29T15:01:00+00:00",
                "fixture_observation_basis": "archived_fixture_snapshot",
            },
            lock=lock,
        )


def test_prediction_archive_rejects_fixture_before_locked_window(tmp_path: Path):
    lock = _lock(tmp_path)
    lock["evaluation_window_started_at"] = "2026-08-31T00:00:00+00:00"
    with pytest.raises(ValueError, match="predates the locked prospective evaluation window"):
        validate_prediction(_prediction(), lock=lock)


def test_prediction_archive_rejects_freeze_cutoff_before_locked_window(tmp_path: Path):
    lock = _lock(tmp_path)
    lock["evaluation_window_started_at"] = "2026-08-29T15:30:00+00:00"

    with pytest.raises(ValueError, match="freeze_cutoff_at predates"):
        validate_prediction(_prediction(), lock=lock)


def test_append_predictions_preserves_same_payload_across_lock_migration(tmp_path: Path):
    lock = _lock(tmp_path)
    output = tmp_path / "predictions.jsonl"
    prediction = _prediction()
    assert append_predictions(output, [prediction], lock=lock)["appended"] == 1
    migrated = {**lock, "model_version_sha256": "b" * 64}
    result = append_predictions(output, [prediction], lock=migrated)
    assert result == {"appended": 1, "skipped_duplicate": 0, "conflicts": 0}
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["content_sha256"] == rows[1]["content_sha256"]
    assert rows[0]["record_key"] != rows[1]["record_key"]
    assert rows[0]["model_version_sha256"] != rows[1]["model_version_sha256"]


def test_append_predictions_allows_changed_payload_after_lock_migration(tmp_path: Path):
    lock = _lock(tmp_path)
    output = tmp_path / "predictions.jsonl"
    prediction = _prediction()
    assert append_predictions(output, [prediction], lock=lock)["appended"] == 1
    migrated = {**lock, "model_version_sha256": "b" * 64}
    changed = {
        **prediction,
        "primary_probability_1x2": {"home": 0.41, "draw": 0.29, "away": 0.30},
    }
    assert append_predictions(output, [changed], lock=migrated) == {
        "appended": 1,
        "skipped_duplicate": 0,
        "conflicts": 0,
    }
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["conflict"] is False
    assert rows[-1]["conflict_with"] == []


def test_same_model_hash_does_not_merge_rows_across_lock_windows(tmp_path: Path):
    lock = {
        **_lock(tmp_path),
        "evaluation_window_started_at": "2026-08-29T15:00:00+00:00",
    }
    output = tmp_path / "predictions.jsonl"
    before_migration = {
        **_prediction(),
        "kickoff_at": "2026-08-30T20:00:00+00:00",
        "freeze_stage": "lineup_confirmation",
        "freeze_cutoff_at": "2026-08-29T16:00:00+00:00",
        "lineup_observed_at": "2026-08-29T16:00:00+00:00",
        "lineup_confirmation_fingerprint": "b" * 64,
        "prediction_observed_at": "2026-08-29T16:05:00+00:00",
        "as_of": "2026-08-29T16:05:00+00:00",
    }
    assert append_predictions(output, [before_migration], lock=lock)["appended"] == 1

    migrated = {
        **lock,
        "evaluation_window_started_at": "2026-08-29T17:00:00+00:00",
    }
    after_migration = {
        **before_migration,
        "freeze_cutoff_at": "2026-08-29T18:00:00+00:00",
        "lineup_observed_at": "2026-08-29T18:00:00+00:00",
        "prediction_observed_at": "2026-08-29T18:05:00+00:00",
        "as_of": "2026-08-29T18:05:00+00:00",
    }

    assert append_predictions(output, [after_migration], lock=migrated) == {
        "appended": 1,
        "skipped_duplicate": 0,
        "conflicts": 0,
    }
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["model_version_sha256"] == rows[1]["model_version_sha256"]
    assert rows[0]["record_key"] != rows[1]["record_key"]
    assert rows[1]["conflict"] is False
    assert rows[1]["model_lock_window_started_at"] == "2026-08-29T17:00:00+00:00"
