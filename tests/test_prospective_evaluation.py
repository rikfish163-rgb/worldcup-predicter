from __future__ import annotations

import json
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import league_platform.prospective_evaluation as evaluation_module

from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
    load_verified_openfootball_archive,
)
from league_platform.prospective_evaluation import (
    evaluate_archive,
    load_first_results,
    load_first_results_admission,
    main,
)
from league_platform.prospective_archive import stable_prediction_digest


OPENFOOTBALL_SOURCE_ID = "openfootball:football.json:2026-27:en.1"
RAW_OBSERVED_AT = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)
SNAPSHOT_AS_OF = "2026-08-30T18:05:00+00:00"


def test_raw_archive_path_resolution_uses_flag_then_env_then_runtime_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_default = tmp_path / "runtime" / "openfootball-raw"
    environment = tmp_path / "from-env"
    explicit = tmp_path / "from-flag"
    monkeypatch.setenv("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR", str(environment))

    assert (
        evaluation_module.resolve_openfootball_raw_archive_dir(
            explicit,
            runtime_default=runtime_default,
        )
        == explicit
    )
    assert (
        evaluation_module.resolve_openfootball_raw_archive_dir(
            None,
            runtime_default=runtime_default,
        )
        == environment
    )
    monkeypatch.delenv("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR")
    assert (
        evaluation_module.resolve_openfootball_raw_archive_dir(
            None,
            runtime_default=runtime_default,
        )
        == runtime_default
    )


@pytest.mark.parametrize(
    "raw_path",
    [
        Path("/dev/shm/forged-openfootball-raw"),
        Path("/dev/shm/../shm/forged-openfootball-raw"),
    ],
)
def test_raw_archive_guard_rejects_volatile_paths(raw_path: Path) -> None:
    with pytest.raises(ValueError, match="durable"):
        evaluation_module.require_durable_openfootball_raw_archive(raw_path)


def _verified_openfootball_result(
    raw_archive_dir: Path,
    *,
    observed_at: datetime = RAW_OBSERVED_AT,
    score: tuple[int, int] = (2, 1),
    halftime: tuple[int, int] | None = (1, 0),
) -> dict:
    config = OPENFOOTBALL_SOURCES[OPENFOOTBALL_SOURCE_ID]
    score_payload: dict[str, list[int]] = {"ft": list(score)}
    if halftime is not None:
        score_payload["ht"] = list(halftime)
    payload = json.dumps(
        {
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-30",
                    "time": "16:00",
                    "team1": "Arsenal FC",
                    "team2": "Coventry City FC",
                    "score": score_payload,
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    OpenFootballRawArchive(raw_archive_dir).store(
        OpenFootballRawObservation(
            source_id=OPENFOOTBALL_SOURCE_ID,
            url=config["url"],
            retrieved_at=observed_at,
            payload=payload,
            source_format=config["format"],
            competition_id=config["competition_id"],
            season=config["season"],
            timezone_name=config["timezone"],
            license="CC0-1.0",
        )
    )
    return load_verified_openfootball_archive(
        raw_archive_dir,
        source_ids=[OPENFOOTBALL_SOURCE_ID],
        observed_before=observed_at + timedelta(minutes=5),
    )["rows"][0]


@pytest.fixture
def durable_raw_archive_dir() -> Path:
    with TemporaryDirectory(prefix="matchline-openfootball-", dir="/tmp") as directory:
        root = Path(directory)
        _verified_openfootball_result(root)
        yield root


def test_raw_archive_guard_accepts_verified_durable_archive(
    durable_raw_archive_dir: Path,
) -> None:
    before = {
        path.relative_to(durable_raw_archive_dir): path.read_bytes()
        for path in durable_raw_archive_dir.rglob("*")
        if path.is_file()
    }

    assert (
        evaluation_module.require_durable_openfootball_raw_archive(durable_raw_archive_dir)
        == durable_raw_archive_dir
    )
    after = {
        path.relative_to(durable_raw_archive_dir): path.read_bytes()
        for path in durable_raw_archive_dir.rglob("*")
        if path.is_file()
    }
    assert after == before


def _write_verified_snapshot(path: Path, fixture: dict, *, as_of: str = SNAPSHOT_AS_OF) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "as_of": as_of,
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "fixtures": [fixture],
                    "errors": [],
                },
            }
        ),
        encoding="utf-8",
    )


def _isolated_openligadb_section() -> dict:
    return {
        "provider": "OpenLigaDB",
        "display_lane": "isolated_current_only",
        "fixture_feed_eligible": False,
        "material_feature_eligible": False,
        "model_eligible": False,
        "redistribution_allowed": False,
        "training_eligible": False,
        "model_rights": {"decision": "block"},
        "training_rights": {"decision": "block"},
        "redistribution_rights": {"decision": "block"},
        "matches": [
            {
                "id": "openligadb:self-authorized",
                "competition_id": "bundesliga",
                "kickoff_at": "2026-08-30T15:00:00+00:00",
                "home_team": "Home",
                "away_team": "Away",
                "status": "finished",
                "score": {"home": 9, "away": 0},
                "display_lane": "isolated_current_only",
                "enters_model": False,
                "model_eligible": False,
                "redistribution_allowed": False,
                "training_eligible": False,
                "source": {"name": "OpenLigaDB"},
            }
        ],
    }


def _prediction(fixture_id: str = "espn:1") -> dict:
    matrix = {"2-1": 0.4, "0-0": 0.6}
    half_full = {f"{half}/{full}": 1 / 9 for half in "HDA" for full in "HDA"}
    return {
        "fixture_id": fixture_id,
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-30T15:00:00+00:00",
        "freeze_stage": "t_minus_24h",
        "freeze_cutoff_at": "2026-08-29T15:00:00+00:00",
        "prediction_observed_at": "2026-08-29T16:00:00+00:00",
        "model_version": "strict_dynamic_elo_scoreline_stage_replay_v1",
        "scoreline_probability": {"home": 0.5, "draw": 0.2, "away": 0.3},
        "total_goals_probability": {"0": 0.1, "1": 0.1, "2": 0.2, "3": 0.3, "4": 0.2, "5+": 0.1},
        "scoreline_matrix": matrix,
        "half_full_probability": half_full,
        "feature_times": [],
    }


def _write_snapshot(path: Path, *, as_of: str, fixture_id: str = "espn:1") -> None:
    path.write_text(
        json.dumps(
            {
                "as_of": as_of,
                "espn": {
                    "fixtures": [
                        {
                            "id": fixture_id,
                            "competition_id": "premier-league",
                            "kickoff_at": "2026-08-30T15:00:00+00:00",
                            "status": "finished",
                            "score": {"home": 2, "away": 1},
                            "halftime_score": {"home": 1, "away": 0},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def test_first_results_replays_verified_raw_and_preserves_both_archives(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    fixture = _verified_openfootball_result(raw_archive_dir)
    snapshot_path = tmp_path / "snapshots" / "raw" / "verified.json"
    _write_verified_snapshot(snapshot_path, fixture)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    loaded = load_first_results_admission(
        tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
    )

    assert loaded["conflicts"] == []
    assert loaded["results"][fixture["id"]]["observed_at"] == RAW_OBSERVED_AT.isoformat()
    assert loaded["results"][fixture["id"]]["source_name"] == "OpenFootball"
    assert loaded["admission"] == {
        "snapshots_checked": 1,
        "candidate_rows": 1,
        "admitted_rows": 1,
        "quarantined_rows": 0,
        "excluded_display_only_rows": 0,
        "verified_archive_replays": 1,
        "quarantine_reasons": {},
        "exclusion_reasons": {},
    }
    results, conflicts = load_first_results(
        tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
    )
    assert results == loaded["results"]
    assert conflicts == []
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_evaluator_scores_only_verified_row_from_adversarial_mixed_snapshot(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    raw_archive_dir = durable_raw_archive_dir
    verified = _verified_openfootball_result(raw_archive_dir)
    blocked = {
        **verified,
        "id": "espn:self-authorized",
        "source": {
            "name": "ESPN",
            "source_id": "espn_schedule_summary",
            "retrieved_at": RAW_OBSERVED_AT.isoformat(),
            "raw_sha256": "f" * 64,
            "commercial_reuse_verified": True,
            "training_allowed": True,
        },
    }
    snapshot_path = tmp_path / "snapshots" / "raw" / "mixed.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": SNAPSHOT_AS_OF,
                "fixture_feed": {
                    "provider": "mixed",
                    "fixtures": [verified, blocked],
                    "errors": [],
                },
                "openligadb": _isolated_openligadb_section(),
            }
        ),
        encoding="utf-8",
    )
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "record_key": fixture_id,
                    "freeze_key": fixture_id,
                    "content_sha256": fixture_id,
                    "conflict": False,
                    "prediction": _prediction(fixture_id),
                }
            )
            for fixture_id in (
                verified["id"],
                blocked["id"],
                "openligadb:self-authorized",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock={"results_not_used_for_selection": True},
        include_scored_rows=True,
    )

    assert result["scored_n"] == 1
    assert result["pending_n"] == 2
    assert result["scored_rows"][0]["fixture_id"] == verified["id"]
    assert result["scored_rows"][0]["result_observed_at"] == RAW_OBSERVED_AT.isoformat()
    assert result["result_admission"] == {
        "snapshots_checked": 1,
        "candidate_rows": 3,
        "admitted_rows": 1,
        "quarantined_rows": 1,
        "excluded_display_only_rows": 1,
        "verified_archive_replays": 1,
        "quarantine_reasons": {"provider_not_openfootball": 1},
        "exclusion_reasons": {"openligadb_display_only": 1},
    }
    assert result["promotion_eligible"] is False


def test_isolated_openligadb_display_does_not_dirty_verified_result_admission(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    raw_archive_dir = durable_raw_archive_dir
    verified = _verified_openfootball_result(raw_archive_dir)
    snapshot_path = tmp_path / "snapshots" / "raw" / "isolated-display.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": SNAPSHOT_AS_OF,
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "fixtures": [verified],
                    "errors": [],
                },
                "openligadb": _isolated_openligadb_section(),
            }
        ),
        encoding="utf-8",
    )
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "verified",
                "freeze_key": "verified",
                "content_sha256": "verified",
                "conflict": False,
                "prediction": _prediction(verified["id"]),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock={"results_not_used_for_selection": True},
    )

    assert result["scored_n"] == 1
    assert result["result_admission_clean"] is True
    assert result["result_admission"]["quarantined_rows"] == 0
    assert result["result_admission"]["excluded_display_only_rows"] == 1
    assert result["result_admission"]["exclusion_reasons"] == {"openligadb_display_only": 1}


def test_openligadb_canonical_injection_blocks_formal_evaluation(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    injected = {
        "id": "openligadb:injected",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-30T15:00:00+00:00",
        "home_team": "Home",
        "away_team": "Away",
        "status": "finished",
        "score": {"home": 9, "away": 0},
        "source": {
            "name": "OpenLigaDB",
            "source_id": "openligadb_secondary_results",
            "training_allowed": True,
        },
    }
    _write_verified_snapshot(tmp_path / "snapshots" / "raw" / "injected.json", injected)
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "injected",
                "freeze_key": "injected",
                "content_sha256": "injected",
                "conflict": False,
                "prediction": _prediction(injected["id"]),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "snapshots",
        openfootball_raw_archive_dir=durable_raw_archive_dir,
        lock={"results_not_used_for_selection": True},
    )

    assert result["scored_n"] == 0
    assert result["result_admission_clean"] is False
    assert result["promotion_eligible"] is False
    assert result["result_admission"]["quarantine_reasons"] == {"provider_not_openfootball": 1}


def test_blocked_only_outcome_cannot_improve_formal_evaluation_gate(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    raw_dir = tmp_path / "snapshots" / "raw"
    raw_dir.mkdir(parents=True)
    _write_snapshot(raw_dir / "espn.json", as_of=SNAPSHOT_AS_OF)
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "blocked",
                "freeze_key": "blocked",
                "content_sha256": "blocked",
                "conflict": False,
                "prediction": _prediction(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "snapshots",
        openfootball_raw_archive_dir=durable_raw_archive_dir,
        lock={"results_not_used_for_selection": True},
    )

    assert result["scored_n"] == 0
    assert result["pending_n"] == 1
    assert result["target_metrics"] == {}
    assert result["league_target_counts"] == {}
    assert result["sample_requirements_met"] is False
    assert result["all_required_targets_scored"] is False
    assert result["prediction_freezes_verified"] is False
    assert result["promotion_eligible"] is False
    assert result["status"] == "pending_prospective_window"
    assert result["result_admission"]["quarantine_reasons"] == {"provider_not_openfootball": 1}


def test_row_mismatch_does_not_score_or_advance_promotion(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    raw_archive_dir = durable_raw_archive_dir
    fixture = _verified_openfootball_result(raw_archive_dir)
    fixture["score"] = {"home": 8, "away": 0}
    _write_verified_snapshot(tmp_path / "snapshots" / "raw" / "mismatch.json", fixture)
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "mismatch",
                "freeze_key": "mismatch",
                "content_sha256": "mismatch",
                "conflict": False,
                "prediction": _prediction(fixture["id"]),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock={"results_not_used_for_selection": True},
    )

    assert result["scored_n"] == 0
    assert result["promotion_eligible"] is False
    assert result["result_admission"]["quarantine_reasons"] == {
        "verified_openfootball_row_mismatch": 1
    }


def test_cli_raw_archive_precedence_is_flag_then_env_then_runtime_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text("", encoding="utf-8")
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    lock_path = tmp_path / "lock.json"
    lock_path.write_text("{}", encoding="utf-8")
    captured: list[Path] = []

    def fake_evaluate(*_args, **kwargs):
        captured.append(kwargs["openfootball_raw_archive_dir"])
        return {
            "status": "pending_prospective_window",
            "scored_n": 0,
            "pending_n": 0,
            "sample_requirements_met": False,
            "all_required_targets_scored": False,
            "prediction_freezes_verified": False,
        }

    monkeypatch.setattr(evaluation_module, "evaluate_archive", fake_evaluate)
    monkeypatch.setenv(
        "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR",
        str(tmp_path / "from-env"),
    )
    output_path = tmp_path / "evaluation.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "prospective-evaluation",
            "--prediction-archive",
            str(prediction_path),
            "--snapshot-archive",
            str(snapshot_dir),
            "--lock",
            str(lock_path),
            "--openfootball-raw-archive-dir",
            str(tmp_path / "from-flag"),
            "--output",
            str(output_path),
        ],
    )

    main()
    monkeypatch.setattr(
        "sys.argv",
        [
            "prospective-evaluation",
            "--prediction-archive",
            str(prediction_path),
            "--snapshot-archive",
            str(snapshot_dir),
            "--lock",
            str(lock_path),
            "--output",
            str(output_path),
        ],
    )
    main()
    monkeypatch.delenv("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR")
    main()

    assert captured == [
        tmp_path / "from-flag",
        tmp_path / "from-env",
        snapshot_dir.parent / "openfootball-raw",
    ]


def _setup_verified_archive_result(
    tmp_path: Path,
    raw_archive_dir: Path,
    *,
    snapshot_name: str = "result.json",
) -> tuple[dict, Path]:
    row = _verified_openfootball_result(raw_archive_dir)
    _write_verified_snapshot(
        tmp_path / "archive" / "raw" / snapshot_name,
        row,
    )
    return row, raw_archive_dir


def test_evaluate_archive_scores_first_result_and_keeps_pending_rows(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    row, raw_archive_dir = _setup_verified_archive_result(
        tmp_path,
        durable_raw_archive_dir,
        snapshot_name="first.json",
    )
    _write_verified_snapshot(
        tmp_path / "archive" / "raw" / "later.json",
        row,
        as_of="2026-08-30T20:00:00+00:00",
    )
    prediction_path = tmp_path / "predictions.jsonl"
    records = [
        {
            "record_key": "r1",
            "freeze_key": "f1",
            "content_sha256": "c1",
            "conflict": False,
            "prediction": _prediction(row["id"]),
        },
        {
            "record_key": "r2",
            "freeze_key": "f2",
            "content_sha256": "c2",
            "conflict": False,
            "prediction": _prediction("openfootball:pending"),
        },
    ]
    prediction_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock={"results_not_used_for_selection": True},
    )

    assert result["scored_n"] == 1
    assert result["pending_n"] == 1
    assert result["result_conflicts"] == 0
    assert result["target_metrics"]["three_way"]["sample_n"] == 1
    assert result["sample_requirements_met"] is False
    assert result["prediction_freezes_verified"] is False


def test_evaluate_archive_quarantines_prediction_with_post_cutoff_factor_observation(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    row, raw_archive_dir = _setup_verified_archive_result(tmp_path, durable_raw_archive_dir)
    prediction = _prediction(row["id"])
    prediction["feature_times"] = ["2026-08-29T16:01:00+00:00"]
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "late-feature",
                "freeze_key": "late-feature",
                "content_sha256": "late-feature",
                "conflict": False,
                "prediction": prediction,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock={"results_not_used_for_selection": True},
    )

    assert result["scored_n"] == 0
    assert result["pending_n"] == 0
    assert result["invalid_records"] == [
        {
            "line": 1,
            "reason": "prediction_causal_contract:feature timestamp is after prediction cutoff",
        }
    ]


def test_evaluate_archive_can_export_stage_rows_without_changing_default_evidence(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    row, raw_archive_dir = _setup_verified_archive_result(
        tmp_path,
        durable_raw_archive_dir,
    )
    prediction = _prediction(row["id"])
    prediction["market_probability"] = {"home": 0.4, "draw": 0.3, "away": 0.3}
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "r1",
                "freeze_key": "f1",
                "conflict": False,
                "model_version_sha256": "a" * 64,
                "prediction": prediction,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    lock = {"results_not_used_for_selection": True}

    compact = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock=lock,
    )
    detailed = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock=lock,
        include_scored_rows=True,
    )

    assert "scored_rows" not in compact
    assert len(detailed["scored_rows"]) == 1
    scored_row = detailed["scored_rows"][0]
    assert scored_row["model_version_sha256"] == "a" * 64
    assert len(scored_row["result_fingerprint"]) == 64
    assert scored_row["targets"]["three_way"]["market_brier"] is not None


def test_evaluate_archive_ignores_legacy_observation_only_conflict(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    row, raw_archive_dir = _setup_verified_archive_result(
        tmp_path,
        durable_raw_archive_dir,
    )
    first = _prediction(row["id"])
    second = {**first, "kickoff_time_observed_at": "2026-08-29T16:01:00+00:00"}
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "record_key": key,
                    "freeze_key": "same-freeze",
                    "content_sha256": content,
                    "conflict": conflict,
                    "prediction": prediction,
                }
            )
            for key, content, conflict, prediction in (
                ("r1", stable_prediction_digest(first), False, first),
                ("r2", "legacy-digest-with-observation-time", True, second),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock={"results_not_used_for_selection": True},
    )

    assert result["scored_n"] == 1
    assert result["archive_conflicts"] == 0


def test_load_first_results_ignores_pre_kickoff_snapshot_copy(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    row = _verified_openfootball_result(raw_archive_dir)
    snapshot_dir = tmp_path / "archive" / "raw"
    _write_verified_snapshot(
        snapshot_dir / "pre-kickoff.json",
        row,
        as_of="2026-08-30T14:00:00+00:00",
    )
    _write_verified_snapshot(snapshot_dir / "result.json", row)

    results, conflicts = load_first_results(
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
    )

    assert results[row["id"]]["observed_at"] == RAW_OBSERVED_AT.isoformat()
    assert conflicts == []


def test_load_first_results_merges_verified_halftime_without_conflict(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    first = _verified_openfootball_result(
        raw_archive_dir,
        observed_at=RAW_OBSERVED_AT,
        halftime=None,
    )
    enriched_observed_at = RAW_OBSERVED_AT + timedelta(hours=2)
    enriched = _verified_openfootball_result(
        raw_archive_dir,
        observed_at=enriched_observed_at,
        halftime=(1, 0),
    )
    assert enriched["id"] == first["id"]
    snapshot_dir = tmp_path / "archive" / "raw"
    _write_verified_snapshot(snapshot_dir / "first.json", first)
    _write_verified_snapshot(
        snapshot_dir / "later.json",
        enriched,
        as_of=(enriched_observed_at + timedelta(minutes=5)).isoformat(),
    )

    results, conflicts = load_first_results(
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
    )

    assert conflicts == []
    assert results[first["id"]]["score"] == {"home": 2, "away": 1}
    assert results[first["id"]]["halftime_score"] == {"home": 1, "away": 0}
    assert results[first["id"]]["observed_at"] == RAW_OBSERVED_AT.isoformat()


def test_evaluate_archive_rejects_predictions_before_locked_window(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "r1",
                "freeze_key": "f1",
                "content_sha256": "c1",
                "conflict": False,
                "prediction": _prediction(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=durable_raw_archive_dir,
        lock={
            "results_not_used_for_selection": True,
            "evaluation_window_started_at": "2026-08-31T00:00:00+00:00",
        },
    )

    assert result["scored_n"] == 0
    assert result["invalid_records"] == [
        {
            "line": 1,
            "reason": "prediction_kickoff_predates_locked_evaluation_window",
        }
    ]


def test_evaluate_archive_does_not_count_prelock_prediction_as_pending(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    prediction = _prediction("openfootball:prelock-pending")
    model_sha = "a" * 64
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "prelock",
                "freeze_key": "prelock",
                "content_sha256": "prelock",
                "model_version_sha256": model_sha,
                "conflict": False,
                "prediction": prediction,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=durable_raw_archive_dir,
        lock={
            "results_not_used_for_selection": True,
            "freeze_model_name": prediction["model_version"],
            "model_version_sha256": model_sha,
            "evaluation_window_started_at": "2026-08-29T17:00:00+00:00",
        },
    )

    assert result["scored_n"] == 0
    assert result["pending_n"] == 0
    assert result["invalid_records"] == [
        {
            "line": 1,
            "reason": "prediction_observed_before_locked_evaluation_window",
        }
    ]


def test_prelock_row_does_not_occupy_current_lock_freeze_identity(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    model_sha = "a" * 64
    before_lock = {
        **_prediction("openfootball:lineup-after-lock"),
        "kickoff_at": "2026-08-30T20:00:00+00:00",
        "freeze_stage": "lineup_confirmation",
        "freeze_cutoff_at": "2026-08-29T16:00:00+00:00",
        "lineup_observed_at": "2026-08-29T16:00:00+00:00",
        "prediction_observed_at": "2026-08-29T16:05:00+00:00",
    }
    after_lock = {
        **before_lock,
        "freeze_cutoff_at": "2026-08-29T18:00:00+00:00",
        "lineup_observed_at": "2026-08-29T18:00:00+00:00",
        "prediction_observed_at": "2026-08-29T18:05:00+00:00",
    }
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "record_key": key,
                    "freeze_key": key,
                    "content_sha256": key,
                    "model_version_sha256": model_sha,
                    "conflict": False,
                    "prediction": prediction,
                }
            )
            for key, prediction in (("before", before_lock), ("after", after_lock))
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=durable_raw_archive_dir,
        lock={
            "results_not_used_for_selection": True,
            "freeze_model_name": before_lock["model_version"],
            "model_version_sha256": model_sha,
            "evaluation_window_started_at": "2026-08-29T17:00:00+00:00",
        },
    )

    assert result["pending_n"] == 1
    assert result["archive_conflicts"] == 0
    assert result["invalid_records"] == [
        {
            "line": 1,
            "reason": "prediction_observed_before_locked_evaluation_window",
        }
    ]


def test_evaluate_archive_excludes_superseded_model_records(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    row, raw_archive_dir = _setup_verified_archive_result(
        tmp_path,
        durable_raw_archive_dir,
    )
    current = _prediction(row["id"])
    legacy = {
        **current,
        "model_version": "strict_dynamic_elo_scoreline_stage_replay_v0",
    }
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "record_key": key,
                    "freeze_key": key,
                    "content_sha256": key,
                    "model_version_sha256": model_sha,
                    "conflict": False,
                    "prediction": prediction,
                }
            )
            for key, model_sha, prediction in (
                ("current", "b" * 64, current),
                ("legacy", "a" * 64, legacy),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock={
            "results_not_used_for_selection": True,
            "freeze_model_name": current["model_version"],
            "model_version_sha256": "b" * 64,
        },
    )

    assert result["scored_n"] == 1
    assert result["pending_n"] == 0
    assert result["legacy_model_records"] == 1


def test_evaluate_archive_does_not_score_conflicting_verified_results(
    tmp_path: Path,
    durable_raw_archive_dir: Path,
) -> None:
    raw_archive_dir = durable_raw_archive_dir
    first = _verified_openfootball_result(
        raw_archive_dir,
        observed_at=RAW_OBSERVED_AT,
        score=(2, 1),
    )
    corrected_observed_at = RAW_OBSERVED_AT + timedelta(hours=2)
    corrected = _verified_openfootball_result(
        raw_archive_dir,
        observed_at=corrected_observed_at,
        score=(1, 0),
    )
    assert corrected["id"] == first["id"]
    snapshot_dir = tmp_path / "archive" / "raw"
    _write_verified_snapshot(snapshot_dir / "first.json", first)
    _write_verified_snapshot(
        snapshot_dir / "later.json",
        corrected,
        as_of=(corrected_observed_at + timedelta(minutes=5)).isoformat(),
    )
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        json.dumps(
            {
                "record_key": "r1",
                "freeze_key": "f1",
                "content_sha256": "c1",
                "conflict": False,
                "prediction": _prediction(first["id"]),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = evaluate_archive(
        prediction_path,
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        lock={"results_not_used_for_selection": True},
    )

    assert result["scored_n"] == 0
    assert result["pending_n"] == 0
    assert result["result_conflicts"] == 1
    assert result["result_conflict_predictions"] == 1
    assert result["promotion_eligible"] is False
    assert result["status"] == "pending_prospective_window"
