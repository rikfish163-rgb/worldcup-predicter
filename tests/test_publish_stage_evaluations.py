from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import league_platform.publish_stage_evaluations as publisher
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
    load_verified_openfootball_archive,
)
from league_platform.publish_stage_evaluations import (
    build_stage_evaluation_rows,
    publish_stage_evaluations,
)


OPENFOOTBALL_SOURCE_ID = "openfootball:football.json:2026-27:en.1"
RAW_OBSERVED_AT = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def verified_result_archive() -> tuple[Path, dict]:
    with TemporaryDirectory(prefix="matchline-stage-raw-", dir="/tmp") as directory:
        raw_archive_dir = Path(directory)
        config = OPENFOOTBALL_SOURCES[OPENFOOTBALL_SOURCE_ID]
        payload = json.dumps(
            {
                "matches": [
                    {
                        "round": "Matchday 1",
                        "date": "2026-08-30",
                        "time": "16:00",
                        "team1": "Arsenal FC",
                        "team2": "Coventry City FC",
                        "score": {"ft": [2, 1], "ht": [1, 0]},
                    }
                ]
            },
            separators=(",", ":"),
        ).encode()
        OpenFootballRawArchive(raw_archive_dir).store(
            OpenFootballRawObservation(
                source_id=OPENFOOTBALL_SOURCE_ID,
                url=config["url"],
                retrieved_at=RAW_OBSERVED_AT,
                payload=payload,
                source_format=config["format"],
                competition_id=config["competition_id"],
                season=config["season"],
                timezone_name=config["timezone"],
                license="CC0-1.0",
            )
        )
        row = load_verified_openfootball_archive(
            raw_archive_dir,
            source_ids=[OPENFOOTBALL_SOURCE_ID],
            observed_before=RAW_OBSERVED_AT + timedelta(minutes=5),
        )["rows"][0]
        yield raw_archive_dir, row


@pytest.fixture(autouse=True)
def _validated_unit_maturity(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        publisher,
        "require_publication_maturity",
        lambda *_args, **_kwargs: {"status": "pass", "receiptSha256": "d" * 64},
    )


def _scored_row(*, digest: str = "a" * 64) -> dict:
    return {
        "fixture_id": "espn:123",
        "provider_fixture_id": "123",
        "competition_id": "premier-league",
        "freeze_stage": "t_minus_24h",
        "model_version": "model-v1",
        "model_version_sha256": digest,
        "stage_locked_at": "2026-08-30T10:00:00+00:00",
        "result_observed_at": "2026-08-31T18:00:00+00:00",
        "result_fingerprint": "b" * 64,
        "targets": {
            "three_way": {
                "brier": 0.2,
                "log_loss": 0.8,
                "market_brier": 0.3,
                "market_log_loss": 0.7,
            },
            "scoreline": {"log_loss": 2.1, "top5_hit": True},
        },
    }


def test_build_stage_evaluation_rows_preserves_model_lock_and_market_comparison() -> None:
    rows = build_stage_evaluation_rows([_scored_row()], evaluated_at="2026-09-01T00:00:00+00:00")
    assert len(rows) == 2
    three_way = next(row for row in rows if row["target"] == "three_way")
    assert three_way["provider"] == "ESPN"
    assert three_way["providerFixtureId"] == "123"
    assert three_way["modelVersion"] == "model-v1@lock-aaaaaaaaaaaaaaaa"
    assert three_way["modelVersionSha256"] == "a" * 64
    assert three_way["metrics"] == {"brier": 0.2, "logLoss": 0.8}
    assert three_way["marketMetrics"] == {"brier": 0.3, "logLoss": 0.7}


def test_build_stage_evaluation_rows_fails_closed_without_lock_digest() -> None:
    row = _scored_row()
    row.pop("model_version_sha256")
    assert build_stage_evaluation_rows([row]) == []


def test_publish_stage_evaluations_dry_run_does_not_require_endpoint(
    tmp_path: Path,
    verified_result_archive: tuple[Path, dict],
) -> None:
    raw_archive_dir, verified = verified_result_archive
    archive = tmp_path / "predictions.jsonl"
    snapshot_dir = tmp_path / "archive" / "raw"
    snapshot_dir.mkdir(parents=True)
    prediction = {
        "fixture_id": verified["id"],
        "provider_fixture_id": verified["id"],
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-30T15:00:00+00:00",
        "freeze_stage": "t_minus_24h",
        "freeze_cutoff_at": "2026-08-29T15:00:00+00:00",
        "prediction_observed_at": "2026-08-29T16:00:00+00:00",
        "model_version": "model-v1",
        "scoreline_probability": {"home": 0.4, "draw": 0.6, "away": 0.0},
        "total_goals_probability": {"0": 0.6, "1": 0.0, "2": 0.0, "3": 0.4, "4": 0.0, "5+": 0.0},
        "scoreline_matrix": {"2-1": 0.4, "0-0": 0.6},
        "half_full_probability": {f"{half}/{full}": 1 / 9 for half in "HDA" for full in "HDA"},
        "feature_times": [],
    }
    archive.write_text(
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
    (snapshot_dir / "result.json").write_text(
        json.dumps(
            {
                "as_of": "2026-08-30T18:05:00+00:00",
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "fixtures": [verified],
                    "errors": [],
                },
            }
        ),
        encoding="utf-8",
    )
    model_file = tmp_path / "model.py"
    model_file.write_text("locked", encoding="utf-8")
    lock = {
        "status": "pending_prospective_window",
        "results_not_used_for_selection": True,
        "model_version_sha256": "a" * 64,
        "model_files": [
            {
                "path": str(model_file),
                "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
            }
        ],
    }
    result = publish_stage_evaluations(
        archive,
        tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        endpoint="",
        token="",
        lock=lock,
        dry_run=True,
    )
    assert result["status"] == "dry_run"
    assert result["publicationGate"] == {
        "status": "blocked",
        "reason": "stage_evaluation_publication_unavailable",
    }
    assert result["stageRows"] == 4
    assert result["published"] == 4


def test_live_stage_evaluation_is_blocked_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified_result_archive: tuple[Path, dict],
) -> None:
    raw_archive_dir, _ = verified_result_archive
    archive = tmp_path / "predictions.jsonl"
    archive.write_text("", encoding="utf-8")
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(
        RuntimeError,
        match="stage_evaluation_publication_unavailable",
    ):
        publish_stage_evaluations(
            archive,
            tmp_path / "archive",
            openfootball_raw_archive_dir=raw_archive_dir,
            endpoint="https://production.example/api/stage-evaluations",
            token="secret",
            lock={},
            publication_epoch="production-v1",
        )

    assert calls == []


@pytest.mark.parametrize("mode", ["volatile", "missing", "malformed"])
def test_stage_publisher_rejects_invalid_raw_archive_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    archive = tmp_path / "predictions.jsonl"
    archive.write_bytes(b"existing-predictions\n")
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with TemporaryDirectory(prefix="matchline-invalid-raw-", dir="/tmp") as directory:
        raw_archive_dir = (
            Path("/dev/shm/forged-openfootball-raw")
            if mode == "volatile"
            else Path(directory) / mode
        )
        if mode == "malformed":
            raw_archive_dir.mkdir()
            (raw_archive_dir / "manifest.jsonl").write_text("{", encoding="utf-8")
        with pytest.raises(ValueError, match="durable"):
            publish_stage_evaluations(
                archive,
                tmp_path / "archive",
                openfootball_raw_archive_dir=raw_archive_dir,
                endpoint="https://production.example/api/stage-evaluations",
                token="secret",
                lock={},
                publication_epoch="production-v1",
            )

    assert calls == []
    assert archive.read_bytes() == b"existing-predictions\n"


def test_empty_stage_evaluation_response_does_not_complete_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified_result_archive: tuple[Path, dict],
) -> None:
    raw_archive_dir, _ = verified_result_archive
    archive = tmp_path / "predictions.jsonl"
    archive.write_text("", encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"
    model_file = tmp_path / "model.py"
    model_file.write_text("locked", encoding="utf-8")
    lock = {
        "status": "pending_prospective_window",
        "results_not_used_for_selection": True,
        "model_version_sha256": "a" * 64,
        "model_files": [
            {
                "path": str(model_file),
                "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
            }
        ],
    }
    monkeypatch.setattr(
        publisher,
        "evaluate_archive",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "scored_rows": [_scored_row()],
        },
    )
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda *_args, **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        publisher,
        "stage_evaluation_publication_status",
        lambda: {"status": "ok", "reason": "verified_test_fixture"},
    )

    with pytest.raises(
        RuntimeError,
        match="stage evaluation endpoint returned an unexpected response",
    ):
        publish_stage_evaluations(
            archive,
            tmp_path / "archive",
            openfootball_raw_archive_dir=raw_archive_dir,
            endpoint="https://production.example/api/stage-evaluations",
            token="secret",
            lock=lock,
            receipts_path=receipts,
            publication_epoch="production-v1",
        )

    assert not receipts.exists()


def test_stage_publisher_cli_raw_archive_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock = tmp_path / "lock.json"
    lock.write_text("{}", encoding="utf-8")
    snapshot_archive = tmp_path / "runtime" / "archive"
    captured: list[Path] = []

    def fake_publish(*_args, **kwargs):
        captured.append(kwargs["openfootball_raw_archive_dir"])
        return {"status": "dry_run"}

    monkeypatch.setattr(publisher, "publish_stage_evaluations", fake_publish)
    monkeypatch.setenv(
        "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR",
        str(tmp_path / "from-env"),
    )
    common = [
        "--snapshot-archive",
        str(snapshot_archive),
        "--lock",
        str(lock),
        "--dry-run",
    ]
    assert (
        publisher.main(
            [
                *common,
                "--openfootball-raw-archive-dir",
                str(tmp_path / "from-flag"),
            ]
        )
        == 0
    )
    assert publisher.main(common) == 0
    monkeypatch.delenv("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR")
    assert publisher.main(common) == 0

    assert captured == [
        tmp_path / "from-flag",
        tmp_path / "from-env",
        snapshot_archive.parent / "openfootball-raw",
    ]
    assert len(capsys.readouterr().out.splitlines()) == 3
