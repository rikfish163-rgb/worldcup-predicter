from __future__ import annotations

import json
from pathlib import Path

from league_platform.prospective_market_audit import append_market_baselines


def _write_fixture_files(tmp_path: Path, *, retrieved_at: str) -> tuple[Path, Path, Path]:
    archive = tmp_path / "predictions.jsonl"
    snapshots = tmp_path / "archive" / "raw"
    snapshots.mkdir(parents=True)
    raw_hash = "a" * 64
    market = {
        "fixture_id": "espn:1",
        "probability": {"home": 0.4, "draw": 0.3, "away": 0.3},
        "retrieved_at": retrieved_at,
        "source": {"raw_sha256": raw_hash, "url": "https://example.test/market"},
    }
    (snapshots / "snapshot.json").write_text(
        json.dumps(
            {
                "as_of": retrieved_at,
                "espn_markets": {"markets": [market]},
            }
        ),
        encoding="utf-8",
    )
    prediction = {
        "fixture_id": "espn:1",
        "competition_id": "test-league",
        "freeze_stage": "t_minus_24h",
        "freeze_cutoff_at": "2026-08-29T15:00:00+00:00",
        "model_version": "model-v1",
        "scoreline_probability": {"home": 0.5, "draw": 0.2, "away": 0.3},
        "live_feature_sources": [
            {"field": "market_1x2", "raw_sha256": raw_hash}
        ],
    }
    archive.write_text(
        json.dumps(
            {
                "freeze_key": "freeze-1",
                "prediction": prediction,
                "conflict": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return archive, tmp_path / "archive", tmp_path / "market-baselines.jsonl"


def test_market_baseline_is_causal_and_idempotent(tmp_path: Path):
    prediction, snapshots, output = _write_fixture_files(
        tmp_path, retrieved_at="2026-08-29T14:30:00+00:00"
    )

    first = append_market_baselines(prediction, snapshots, output)
    second = append_market_baselines(prediction, snapshots, output)

    assert first == {
        "appended": 1,
        "skipped_duplicate": 0,
        "conflicts": 0,
        "diagnostics": 0,
        "diagnostic_reasons": {},
        "diagnostic_items": [],
        "matched_rows": 1,
    }
    assert second["appended"] == 0
    assert second["skipped_duplicate"] == 1
    row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert row["market_probability"] == {"away": 0.3, "draw": 0.3, "home": 0.4}
    assert row["probability_delta_model_minus_market"] == {
        "away": 0.0,
        "draw": -0.09999999999999998,
        "home": 0.09999999999999998,
    }
    assert row["market_raw_sha256"] == "a" * 64
    assert row["conflict"] is False


def test_market_baseline_rejects_post_cutoff_observation(tmp_path: Path):
    prediction, snapshots, output = _write_fixture_files(
        tmp_path, retrieved_at="2026-08-29T15:01:00+00:00"
    )

    result = append_market_baselines(prediction, snapshots, output)

    assert result["matched_rows"] == 0
    assert result["diagnostics"] == 1
    assert result["diagnostic_reasons"] == {"no_causal_market_snapshot": 1}
    assert result["diagnostic_items"][0]["fixture_id"] == "espn:1"
    assert not output.exists()


def test_lineup_stage_market_unavailability_is_distinct_from_missing_provenance(tmp_path: Path):
    archive = tmp_path / "predictions.jsonl"
    output = tmp_path / "market-baselines.jsonl"
    prediction = {
        "fixture_id": "espn:lineup-only",
        "competition_id": "test-league",
        "freeze_stage": "lineup_confirmation",
        "freeze_cutoff_at": "2026-08-29T15:00:00+00:00",
        "market_probability": None,
        "scoreline_probability": {"home": 0.5, "draw": 0.2, "away": 0.3},
        "live_feature_sources": [],
    }
    archive.write_text(
        json.dumps(
            {
                "freeze_key": "freeze-lineup-only",
                "prediction": prediction,
                "conflict": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = append_market_baselines(archive, tmp_path / "archive", output)

    assert result["matched_rows"] == 0
    assert result["diagnostics"] == 1
    assert result["diagnostic_reasons"] == {"market_unavailable_at_lineup_stage": 1}
    assert result["diagnostic_items"] == [
        {
            "line": 1,
            "reason": "market_unavailable_at_lineup_stage",
            "fixture_id": "espn:lineup-only",
        }
    ]
    assert not output.exists()


def test_pre_lineup_market_provenance_gap_remains_a_hard_diagnostic(tmp_path: Path):
    archive = tmp_path / "predictions.jsonl"
    output = tmp_path / "market-baselines.jsonl"
    archive.write_text(
        json.dumps(
            {
                "freeze_key": "freeze-market-gap",
                "prediction": {
                    "fixture_id": "espn:market-gap",
                    "freeze_stage": "t_minus_24h",
                    "market_probability": None,
                    "live_feature_sources": [],
                },
                "conflict": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = append_market_baselines(archive, tmp_path / "archive", output)

    assert result["diagnostic_reasons"] == {"market_feature_source_missing": 1}
    assert result["diagnostic_items"] == [
        {
            "line": 1,
            "reason": "market_feature_source_missing",
            "fixture_id": "espn:market-gap",
        }
    ]
    assert not output.exists()


def test_lock_migration_does_not_turn_changed_market_observation_into_conflict(tmp_path: Path):
    archive = tmp_path / "predictions.jsonl"
    snapshots = tmp_path / "archive" / "raw"
    snapshots.mkdir(parents=True)
    markets = []
    records = []
    for lock_sha, raw_hash, retrieved_at in (
        ("a" * 64, "b" * 64, "2026-08-29T14:00:00+00:00"),
        ("c" * 64, "d" * 64, "2026-08-29T14:30:00+00:00"),
    ):
        markets.append(
            {
                "fixture_id": "espn:migrated",
                "probability": {"home": 0.4, "draw": 0.3, "away": 0.3},
                "retrieved_at": retrieved_at,
                "source": {"raw_sha256": raw_hash, "url": "https://example.test/market"},
            }
        )
        records.append(
            {
                "freeze_key": "same-logical-freeze",
                "model_version_sha256": lock_sha,
                "conflict": False,
                "prediction": {
                    "fixture_id": "espn:migrated",
                    "competition_id": "test-league",
                    "freeze_stage": "lineup_confirmation",
                    "freeze_cutoff_at": "2026-08-29T15:00:00+00:00",
                    "model_version": "model-v1",
                    "scoreline_probability": {"home": 0.5, "draw": 0.2, "away": 0.3},
                    "live_feature_sources": [{"field": "market_1x2", "raw_sha256": raw_hash}],
                },
            }
        )
    (snapshots / "snapshot.json").write_text(
        json.dumps({"as_of": "2026-08-29T14:30:00+00:00", "espn_markets": {"markets": markets}}),
        encoding="utf-8",
    )
    archive.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")

    result = append_market_baselines(archive, tmp_path / "archive", tmp_path / "market-baselines.jsonl")

    assert result["appended"] == 2
    assert result["conflicts"] == 0
