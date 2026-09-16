from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

import league_platform.publish_cycle as cycle_publisher
from league_platform.publish_cycle import (
    _snapshot_canonical_sha256,
    publish_cycle as _production_publish_cycle,
)


_REAL_ARCHIVE_PUBLICATION_IDENTITY = cycle_publisher._archive_publication_identity
_REAL_STAGE_EVALUATION_PUBLICATION_STATUS = cycle_publisher.stage_evaluation_publication_status


@pytest.fixture(autouse=True)
def _validated_unit_publication_gates(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        cycle_publisher,
        "require_publication_maturity",
        lambda *_args, **_kwargs: {"status": "pass", "receiptSha256": "d" * 64},
    )
    monkeypatch.setattr(
        cycle_publisher,
        "_archive_publication_identity",
        lambda *_args, **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        cycle_publisher,
        "stage_evaluation_publication_status",
        lambda: {"status": "ok", "reason": "verified_test_fixture"},
    )


def _write_publication_identity(
    tmp_path: Path,
    *,
    model_version: str = "locked-model-v1",
    model_hash: str | None = None,
    **lock_overrides,
) -> dict[str, Path]:
    snapshot_path = tmp_path / "current.json"
    if not snapshot_path.exists():
        snapshot_path.write_text(
            json.dumps({"as_of": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_as_of = snapshot.get("as_of")
    if not isinstance(snapshot_as_of, str):
        snapshot_as_of = datetime.now(timezone.utc).isoformat()
    model_file = Path("league_platform/publish_cycle.py")
    lock = tmp_path / "prospective-model-lock.json"
    model_files = [
        {
            "path": str(model_file),
            "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
        }
    ]
    lock_payload = {
        "status": "pending_prospective_window",
        "freeze_model_name": model_version,
        "model_files": model_files,
        **lock_overrides,
    }
    if model_hash is None:
        model_hash = hashlib.sha256(
            json.dumps(
                {
                    "model_name": lock_payload.get("model_name"),
                    "freeze_model_name": lock_payload.get("freeze_model_name"),
                    "model_files": model_files,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    lock_payload["model_version_sha256"] = model_hash
    lock.write_text(json.dumps(lock_payload), encoding="utf-8")
    cycle = tmp_path / "prospective-cycle-latest.json"
    finished_at = datetime.now(timezone.utc)
    cycle.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "status": "pending_prospective_window",
                "started_at": (finished_at - timedelta(seconds=1)).isoformat(),
                "finished_at": finished_at.isoformat(),
                "capture": {"as_of": snapshot_as_of},
                "sync": {
                    "as_of": snapshot_as_of,
                    "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                },
                "artifacts": {
                    "model_lock": str(lock.resolve()),
                    "model_version_sha256": model_hash,
                    "live_snapshot": str(snapshot_path.resolve()),
                },
            }
        ),
        encoding="utf-8",
    )
    return {"stage_lock_path": lock, "cycle_evidence_path": cycle}


def publish_cycle(**kwargs):
    """Run a cycle with an explicit valid lock identity unless overridden."""

    if "stage_lock_path" not in kwargs:
        snapshot_path = Path(kwargs["snapshot_path"])
        kwargs.update(_write_publication_identity(snapshot_path.parent))
    elif "cycle_evidence_path" not in kwargs:
        lock_path = Path(kwargs["stage_lock_path"])
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lock = {}
        cycle_path = lock_path.with_name(f"{lock_path.stem}.cycle.json")
        cycle_path.write_text(
            json.dumps(
                {
                    "status": "pending_prospective_window",
                    "artifacts": {
                        "model_lock": str(lock_path.resolve()),
                        "model_version_sha256": lock.get(
                            "model_version_sha256",
                            "0" * 64,
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )
        kwargs["cycle_evidence_path"] = cycle_path
    kwargs.setdefault(
        "publish_source_health_diagnostics_fn",
        lambda *_args, **_kwargs: {
            "status": "dry_run",
            "failed": 0,
            "requests": 0,
        },
    )
    kwargs.setdefault(
        "publish_lineup_diagnostics_fn",
        lambda *_args, **_kwargs: {
            "status": "dry_run",
            "failed": 0,
            "requests": 0,
        },
    )
    kwargs.setdefault(
        "publish_stage_evaluations_fn",
        lambda *_args, **_kwargs: {
            "status": "blocked",
            "stageRows": 0,
            "reason": "stage_evaluation_publication_unavailable",
        },
    )
    if not kwargs.get("dry_run"):
        kwargs.setdefault(
            "stage_evaluations_endpoint",
            "https://example.test/stage-evaluations",
        )
        kwargs.setdefault(
            "publication_endpoint",
            "https://example.test/publication/register",
        )
    return _production_publish_cycle(**kwargs)


def test_archive_identity_binds_cycle_path_raw_hash_and_evaluation(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "prospective_predictions.jsonl"
    archive.write_text('{"prediction":{"fixture_id":"one"}}\n', encoding="utf-8")
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    evaluation = tmp_path / "prospective-evaluation-current.json"
    evaluation.write_text(
        json.dumps(
            {
                "prediction_archive": str(archive.resolve()),
                "prediction_archive_sha256": archive_sha,
                "scored_n": 0,
                "pending_n": 0,
            }
        ),
        encoding="utf-8",
    )
    cycle = tmp_path / "prospective-cycle-latest.json"
    cycle.write_text(
        json.dumps(
            {
                "artifacts": {
                    "prediction_archive": str(archive.resolve()),
                    "prediction_archive_sha256": archive_sha,
                    "evaluation_evidence": str(evaluation.resolve()),
                    "evaluation_evidence_sha256": hashlib.sha256(
                        evaluation.read_bytes()
                    ).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )

    valid = _REAL_ARCHIVE_PUBLICATION_IDENTITY(cycle, archive)
    archive.write_text('{"prediction":{"fixture_id":"replacement"}}\n', encoding="utf-8")
    replaced = _REAL_ARCHIVE_PUBLICATION_IDENTITY(cycle, archive)

    assert valid["status"] == "ok"
    assert valid["predictionArchiveSha256"] == archive_sha
    assert replaced["status"] == "blocked"
    assert replaced["reason"] == "prediction_archive_hash_mismatch"


def test_cycle_blocks_missing_prospective_lock_before_any_publisher(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "dry_run", "failed": 0, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        stage_lock_path=tmp_path / "missing-lock.json",
        cycle_evidence_path=tmp_path / "missing-cycle.json",
        dry_run=True,
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_source_health_diagnostics_fn=should_not_publish,
        publish_lineup_diagnostics_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
        publish_stage_evaluations_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "prospective_lock_not_found"
    assert result["phases"] == {}
    assert calls == []


def test_cycle_blocks_unavailable_stage_stream_before_any_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _write_publication_identity(tmp_path)
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "ok"}

    monkeypatch.setattr(
        cycle_publisher,
        "stage_evaluation_publication_status",
        _REAL_STAGE_EVALUATION_PUBLICATION_STATUS,
    )
    result = _production_publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        publication_epoch="production-v1",
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_source_health_diagnostics_fn=should_not_publish,
        publish_lineup_diagnostics_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
        publish_stage_evaluations_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "stage_evaluation_publication_unavailable"
    assert result["phases"] == {}
    assert calls == []


def test_live_cycle_marks_stage_evaluation_not_applicable_when_none_are_scored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _write_publication_identity(tmp_path)
    stage_calls: list[str] = []
    manifest_payloads: list[dict] = []
    monkeypatch.setattr(
        cycle_publisher,
        "_archive_publication_identity",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "evaluationScoredN": 0,
            "evaluationPendingN": 7,
        },
    )
    monkeypatch.setattr(
        cycle_publisher,
        "stage_evaluation_publication_status",
        _REAL_STAGE_EVALUATION_PUBLICATION_STATUS,
    )
    monkeypatch.setattr(
        cycle_publisher,
        "_snapshot_freshness",
        lambda *_args, **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        cycle_publisher,
        "_snapshot_publication",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    result = _production_publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        fixtures_endpoint="https://example.test/fixtures",
        intelligence_endpoint="https://example.test/ingest",
        forecast_register_endpoint="https://example.test/forecast/register",
        forecast_endpoint="https://example.test/forecast",
        publication_endpoint="https://example.test/publication/register",
        token=hashlib.sha256(b"fixture-auth").hexdigest(),
        publication_epoch="production-v1",
        max_snapshot_age_seconds=1800,
        require_publication_diagnostic=True,
        publish_snapshot_fn=lambda *_args, **_kwargs: {
            "status": "ok",
            "fixtures": 1,
            "failed": 0,
        },
        publish_ledger_fn=lambda *_args, **_kwargs: {
            "status": "ok",
            "rows": 0,
            "failed": 0,
        },
        publish_source_health_diagnostics_fn=lambda *_args, **_kwargs: {
            "status": "ok",
            "published": 0,
            "failed": 0,
        },
        publish_lineup_diagnostics_fn=lambda *_args, **_kwargs: {
            "status": "ok",
            "published": 0,
            "failed": 0,
        },
        publish_archive_fn=lambda *_args, **_kwargs: {
            "status": "ok",
            "groups": 0,
            "stageRows": 0,
            "published": 0,
            "skipped": [],
        },
        publish_stage_evaluations_fn=lambda *_args, **_kwargs: stage_calls.append(
            "stage_evaluations"
        ),
        publish_site_manifest_fn=lambda payload, **_kwargs: (
            manifest_payloads.append(payload) or {"status": "ok", "published": 1, "requests": 1}
        ),
    )

    assert stage_calls == []
    assert result["status"] == "ok"
    assert result["stageEvaluationPublication"] == {
        "status": "not_applicable",
        "reason": "no_scored_stage_rows",
    }
    assert result["phases"]["stage_evaluations"] == {
        "status": "ok",
        "stageRows": 0,
        "published": 0,
        "requests": 0,
        "notApplicable": True,
        "reason": "no_scored_stage_rows",
    }
    assert [payload["status"] for payload in manifest_payloads] == [
        "publishing",
        "complete",
    ]
    assert set(manifest_payloads[0]["counts"].values()) == {0}
    assert manifest_payloads[1]["counts"]["stageEvaluations"] == 0
    assert manifest_payloads[1]["phases"]["stageEvaluations"] == {
        "status": "not_applicable",
        "records": 0,
        "reason": "no scored stage rows",
    }


def test_live_cycle_barrier_failure_prevents_every_data_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _write_publication_identity(tmp_path)
    data_calls: list[str] = []
    monkeypatch.setattr(
        cycle_publisher,
        "_archive_publication_identity",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "evaluationScoredN": 0,
            "evaluationPendingN": 1,
        },
    )
    monkeypatch.setattr(
        cycle_publisher,
        "_snapshot_freshness",
        lambda *_args, **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        cycle_publisher,
        "_snapshot_publication",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    def data_publisher(*_args, **_kwargs):
        data_calls.append("called")
        return {"status": "ok"}

    result = _production_publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        fixtures_endpoint="https://example.test/fixtures",
        intelligence_endpoint="https://example.test/ingest",
        forecast_register_endpoint="https://example.test/forecast/register",
        forecast_endpoint="https://example.test/forecast",
        publication_endpoint="https://example.test/publication/register",
        token=hashlib.sha256(b"fixture-auth").hexdigest(),
        publication_epoch="production-v1",
        max_snapshot_age_seconds=1800,
        require_publication_diagnostic=True,
        publish_snapshot_fn=data_publisher,
        publish_ledger_fn=data_publisher,
        publish_source_health_diagnostics_fn=data_publisher,
        publish_lineup_diagnostics_fn=data_publisher,
        publish_archive_fn=data_publisher,
        publish_stage_evaluations_fn=data_publisher,
        publish_site_manifest_fn=lambda *_args, **_kwargs: {
            "status": "failed",
            "published": 0,
            "requests": 1,
        },
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "publication_barrier_failed"
    assert result["phases"] == {}
    assert data_calls == []


def test_cycle_dry_run_reports_unavailable_stage_stream_without_calling_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _write_publication_identity(tmp_path)
    stage_calls: list[str] = []
    monkeypatch.setattr(
        cycle_publisher,
        "stage_evaluation_publication_status",
        _REAL_STAGE_EVALUATION_PUBLICATION_STATUS,
    )

    result = _production_publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=lambda *_args, **_kwargs: {
            "status": "dry_run",
            "failed": 0,
        },
        publish_ledger_fn=lambda *_args, **_kwargs: {
            "status": "dry_run",
            "failed": 0,
        },
        publish_source_health_diagnostics_fn=lambda *_args, **_kwargs: {
            "status": "dry_run",
            "failed": 0,
        },
        publish_lineup_diagnostics_fn=lambda *_args, **_kwargs: {
            "status": "dry_run",
            "failed": 0,
        },
        publish_archive_fn=lambda *_args, **_kwargs: {
            "status": "dry_run",
            "published": 0,
            "skipped": [],
        },
        publish_stage_evaluations_fn=lambda *_args, **_kwargs: stage_calls.append(
            "stage_evaluations"
        ),
    )

    assert stage_calls == []
    assert result["phases"]["stage_evaluations"] == {
        "status": "blocked",
        "stageRows": 0,
        "reason": "stage_evaluation_publication_unavailable",
    }
    assert result["status"] == "dry_run_partial"


def test_cycle_blocks_invalid_lock_inventory_before_any_publisher(
    tmp_path: Path,
) -> None:
    identity = _write_publication_identity(tmp_path)
    lock = json.loads(identity["stage_lock_path"].read_text(encoding="utf-8"))
    lock["model_files"][0]["sha256"] = "0" * 64
    identity["stage_lock_path"].write_text(json.dumps(lock), encoding="utf-8")
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "dry_run", "failed": 0, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "prospective_lock_integrity_failed"
    assert "model file hash mismatch" in result["prospectiveLock"]["error"]
    assert calls == []


def test_drifted_v259_repository_lock_blocks_every_remote_publisher() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    v259_lock = repository_root / "tests/fixtures/evidence/prospective-model-lock-v259.json"
    lock = json.loads(v259_lock.read_text(encoding="utf-8"))
    inventory = lock.get("model_files")
    assert isinstance(inventory, list) and len(inventory) == 40
    matching = sum(
        hashlib.sha256((repository_root / row["path"]).read_bytes()).hexdigest() == row["sha256"]
        for row in inventory
    )
    assert matching < len(inventory)
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "ok"}

    result = _production_publish_cycle(
        snapshot_path=repository_root / "data/live/current.json",
        ledger_path=repository_root / "data/intelligence/observations.jsonl",
        archive_path=repository_root / "data/prospective_predictions.jsonl",
        stage_lock_path=v259_lock,
        cycle_evidence_path=(repository_root / "docs/evidence/prospective-cycle-latest.json"),
        publication_epoch="production-v260",
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_source_health_diagnostics_fn=should_not_publish,
        publish_lineup_diagnostics_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
        publish_stage_evaluations_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "prospective_lock_integrity_failed"
    assert calls == []


def test_cycle_blocks_mismatched_cycle_model_identity_before_any_publisher(
    tmp_path: Path,
) -> None:
    identity = _write_publication_identity(tmp_path)
    cycle = json.loads(identity["cycle_evidence_path"].read_text(encoding="utf-8"))
    cycle["artifacts"]["model_version_sha256"] = "b" * 64
    identity["cycle_evidence_path"].write_text(json.dumps(cycle), encoding="utf-8")
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "dry_run", "failed": 0, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "prospective_cycle_model_identity_mismatch"
    assert calls == []


def test_cycle_recomputes_and_blocks_tampered_aggregate_model_hash(
    tmp_path: Path,
) -> None:
    identity = _write_publication_identity(tmp_path)
    tampered_hash = "f" * 64
    lock = json.loads(identity["stage_lock_path"].read_text(encoding="utf-8"))
    lock["model_version_sha256"] = tampered_hash
    identity["stage_lock_path"].write_text(json.dumps(lock), encoding="utf-8")
    cycle = json.loads(identity["cycle_evidence_path"].read_text(encoding="utf-8"))
    cycle["artifacts"]["model_version_sha256"] = tampered_hash
    identity["cycle_evidence_path"].write_text(json.dumps(cycle), encoding="utf-8")
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "dry_run", "failed": 0, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "prospective_lock_integrity_failed"
    assert "aggregate model hash mismatch" in result["prospectiveLock"]["error"]
    assert calls == []


def test_cycle_blocks_stale_evidence_before_any_publisher(tmp_path: Path) -> None:
    identity = _write_publication_identity(tmp_path)
    cycle = json.loads(identity["cycle_evidence_path"].read_text(encoding="utf-8"))
    cycle["finished_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    cycle["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=2, seconds=1)).isoformat()
    identity["cycle_evidence_path"].write_text(json.dumps(cycle), encoding="utf-8")
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "dry_run", "failed": 0, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        max_snapshot_age_seconds=1800,
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "prospective_cycle_stale"
    assert calls == []


def test_cycle_blocks_snapshot_identity_mismatch_before_any_publisher(
    tmp_path: Path,
) -> None:
    identity = _write_publication_identity(tmp_path)
    cycle = json.loads(identity["cycle_evidence_path"].read_text(encoding="utf-8"))
    cycle["capture"]["as_of"] = "2026-08-01T00:00:00+00:00"
    identity["cycle_evidence_path"].write_text(json.dumps(cycle), encoding="utf-8")
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "dry_run", "failed": 0, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "prospective_cycle_snapshot_identity_mismatch"
    assert calls == []


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"schema_version": "0.0.0"}, "prospective_cycle_schema_invalid"),
        ({"status": "failed"}, "prospective_cycle_status_not_publishable"),
        (
            {"finished_at": "2026-08-24T22:00:00"},
            "prospective_cycle_time_invalid",
        ),
    ],
)
def test_cycle_blocks_invalid_evidence_contract_before_any_publisher(
    tmp_path: Path,
    mutation: dict,
    reason: str,
) -> None:
    identity = _write_publication_identity(tmp_path)
    cycle = json.loads(identity["cycle_evidence_path"].read_text(encoding="utf-8"))
    cycle.update(mutation)
    identity["cycle_evidence_path"].write_text(json.dumps(cycle), encoding="utf-8")
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "dry_run", "failed": 0, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == reason
    assert calls == []


def test_cycle_runs_fixture_intelligence_then_forecast_in_dry_run(tmp_path: Path):
    calls: list[str] = []

    def fixtures(*_args, **_kwargs):
        calls.append("fixtures")
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **_kwargs):
        calls.append("intelligence")
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **_kwargs):
        calls.append("forecast")
        return {"published": 1, "skipped": [], "failed": 0}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )
    assert calls == ["fixtures", "intelligence", "forecast"]
    # The default stage-evaluation pass has no archive in this isolated test,
    # so the cycle truthfully reports a partial dry run while still publishing
    # the fixture/intelligence/forecast phases.
    assert result["status"] == "dry_run_partial"
    assert result["phases"]["forecast"]["published"] == 1
    assert result["phases"]["lineup_diagnostics"]["status"] == "dry_run"


def test_cycle_passes_current_snapshot_to_news_resolver(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    seen = {}

    def fixtures(*_args, **_kwargs):
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **kwargs):
        seen["snapshot_path"] = kwargs.get("snapshot_path")
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **_kwargs):
        return {"status": "dry_run", "published": 1, "skipped": [], "failed": 0}

    publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )

    assert seen["snapshot_path"] == snapshot


def test_cycle_forwards_limit_to_intelligence_stream(tmp_path: Path):
    seen = {}

    def fixtures(*_args, **kwargs):
        seen["fixture_limit"] = kwargs.get("limit")
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **kwargs):
        seen["intelligence_limit"] = kwargs.get("limit")
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **kwargs):
        seen["forecast_limit"] = kwargs.get("limit")
        return {"status": "dry_run", "published": 1, "skipped": [], "failed": 0}

    publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        limit=20,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )

    assert seen == {"fixture_limit": 20, "intelligence_limit": 20, "forecast_limit": 20}


def test_cycle_forwards_durable_openfootball_archive_to_publishers(tmp_path: Path):
    seen: dict[str, object] = {}
    archive_dir = Path(
        "/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw"
    )

    def fixtures(*_args, **kwargs):
        seen["fixture_archive"] = kwargs.get("openfootball_raw_archive_dir")
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **_kwargs):
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **kwargs):
        seen["forecast_archive"] = kwargs.get("openfootball_raw_archive_dir")
        return {"status": "dry_run", "published": 1, "skipped": [], "failed": 0}

    publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        openfootball_raw_archive_dir=archive_dir,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )

    assert seen == {
        "fixture_archive": archive_dir,
        "forecast_archive": archive_dir,
    }


def test_cycle_uses_d1_safe_batch_sizes(tmp_path: Path):
    seen = {}

    def fixtures(*_args, **kwargs):
        seen["fixture_batch_size"] = kwargs.get("batch_size")
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **kwargs):
        seen["intelligence_batch_size"] = kwargs.get("batch_size")
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **_kwargs):
        return {"status": "dry_run", "published": 1, "skipped": [], "failed": 0}

    publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )

    assert seen == {"fixture_batch_size": 25, "intelligence_batch_size": 25}


def test_cycle_limits_forecast_publication_to_locked_model(tmp_path: Path):
    identity = _write_publication_identity(tmp_path)
    seen = {}

    def fixtures(*_args, **_kwargs):
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **_kwargs):
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **kwargs):
        seen["model_version"] = kwargs.get("model_version")
        return {"status": "dry_run", "published": 1, "skipped": [], "failed": 0}

    publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )

    assert seen == {"model_version": "locked-model-v1"}


def test_cycle_limits_forecast_publication_to_locked_model_digest(tmp_path: Path):
    identity = _write_publication_identity(tmp_path)
    seen = {}

    def fixtures(*_args, **_kwargs):
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **_kwargs):
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **kwargs):
        seen.update({key: kwargs.get(key) for key in ("model_version", "model_version_sha256")})
        return {"status": "dry_run", "published": 1, "skipped": [], "failed": 0}

    publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )

    lock = json.loads(identity["stage_lock_path"].read_text(encoding="utf-8"))
    assert seen == {
        "model_version": "locked-model-v1",
        "model_version_sha256": lock["model_version_sha256"],
    }


def test_cycle_limits_intelligence_publication_to_lock_window(tmp_path: Path):
    identity = _write_publication_identity(
        tmp_path,
        evaluation_window_started_at="2026-08-17T17:57:41+00:00",
    )
    seen = {}

    def fixtures(*_args, **_kwargs):
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **kwargs):
        seen["min_observed_at"] = kwargs.get("min_observed_at")
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **_kwargs):
        return {"status": "dry_run", "published": 0, "skipped": [], "failed": 0}

    publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        **identity,
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )

    assert seen == {"min_observed_at": "2026-08-17T17:57:41+00:00"}


def test_cycle_publishes_lineup_diagnostics_without_blocking_forecast(tmp_path: Path):
    calls: list[str] = []
    snapshot = tmp_path / "current.json"
    snapshot.write_text(
        json.dumps({"as_of": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )

    def fixtures(*_args, **_kwargs):
        calls.append("fixtures")
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **_kwargs):
        calls.append("intelligence")
        return {"status": "dry_run", "failed": 0}

    def diagnostics(*_args, **_kwargs):
        calls.append("lineup_diagnostics")
        return {"status": "failed", "failed": 1}

    def source_health(*_args, **_kwargs):
        calls.append("source_health")
        return {"status": "ok", "failed": 0, "published": 18}

    def forecast(*_args, **_kwargs):
        calls.append("forecast")
        return {"status": "dry_run", "published": 1, "skipped": [], "failed": 0}

    result = publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_source_health_diagnostics_fn=source_health,
        publish_lineup_diagnostics_fn=diagnostics,
        publish_archive_fn=forecast,
    )
    assert calls == ["fixtures", "intelligence", "source_health", "lineup_diagnostics", "forecast"]
    assert result["phases"]["forecast"]["published"] == 1
    assert result["status"] == "dry_run_partial"


def test_cycle_blocks_forecast_when_fixture_publication_fails(tmp_path: Path):
    calls: list[str] = []

    def fixtures(*_args, **_kwargs):
        calls.append("fixtures")
        return {"status": "partial", "failed": 1}

    def intelligence(*_args, **_kwargs):
        calls.append("intelligence")
        return {"status": "ok", "failed": 0}

    def forecast(*_args, **_kwargs):
        calls.append("forecast")
        return {"published": 1, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )
    assert calls == ["fixtures", "intelligence"]
    assert result["status"] == "dry_run_partial"
    assert result["phases"]["forecast"]["status"] == "blocked"


def test_cycle_blocks_forecast_when_intelligence_publication_fails(tmp_path: Path):
    calls: list[str] = []

    def fixtures(*_args, **_kwargs):
        calls.append("fixtures")
        return {"status": "ok", "failed": 0}

    def intelligence(*_args, **_kwargs):
        calls.append("intelligence")
        return {"status": "failed", "failed": 1}

    def forecast(*_args, **_kwargs):
        calls.append("forecast")
        return {"published": 1, "skipped": []}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
    )
    assert calls == ["fixtures", "intelligence"]
    assert result["status"] == "dry_run_partial"
    assert result["phases"]["forecast"]["skipped"] == [
        {"reason": "intelligence_publication_failed"}
    ]


def test_cycle_runs_stage_evaluation_after_forecast_when_injected(tmp_path: Path):
    calls: list[str] = []

    def fixtures(*_args, **_kwargs):
        calls.append("fixtures")
        return {"status": "dry_run", "failed": 0}

    def intelligence(*_args, **_kwargs):
        calls.append("intelligence")
        return {"status": "dry_run", "failed": 0}

    def forecast(*_args, **_kwargs):
        calls.append("forecast")
        return {"status": "dry_run", "published": 1, "skipped": [], "failed": 0}

    def stage_evaluations(*_args, **_kwargs):
        calls.append("stage_evaluations")
        return {"status": "dry_run", "stageRows": 4}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=fixtures,
        publish_ledger_fn=intelligence,
        publish_archive_fn=forecast,
        publish_stage_evaluations_fn=stage_evaluations,
    )
    assert calls == ["fixtures", "intelligence", "forecast", "stage_evaluations"]
    assert result["phases"]["stage_evaluations"]["stageRows"] == 4


def test_cycle_commits_publication_manifest_only_after_every_phase(tmp_path: Path):
    calls: list[str] = []
    manifest_payload: dict = {}

    def phase(name: str, result: dict):
        def run(*_args, **_kwargs):
            calls.append(name)
            return result

        return run

    def manifest(payload: dict, **_kwargs):
        calls.append("publication_manifest")
        manifest_payload.update(payload)
        return {"status": "dry_run", "published": 0, "requests": 0}

    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=phase("fixtures", {"status": "dry_run", "fixtures": 2, "failed": 0}),
        publish_ledger_fn=phase("intelligence", {"status": "dry_run", "rows": 1, "failed": 0}),
        publish_source_health_diagnostics_fn=phase(
            "source_health", {"status": "dry_run", "published": 3, "failed": 0}
        ),
        publish_lineup_diagnostics_fn=phase(
            "lineups", {"status": "dry_run", "published": 1, "failed": 0}
        ),
        publish_archive_fn=phase(
            "forecast",
            {"status": "dry_run", "groups": 1, "stageRows": 2, "published": 1, "skipped": []},
        ),
        publish_stage_evaluations_fn=phase(
            "stage_evaluations", {"status": "dry_run", "stageRows": 2}
        ),
        publish_site_manifest_fn=manifest,
    )

    assert calls == [
        "fixtures",
        "intelligence",
        "source_health",
        "lineups",
        "forecast",
        "stage_evaluations",
        "publication_manifest",
    ]
    assert result["status"] == "dry_run"
    assert manifest_payload["counts"] == {
        "fixtures": 2,
        "intelligenceObservations": 1,
        "predictionGroups": 1,
        "predictionStages": 2,
        "stageEvaluations": 2,
    }


def test_cycle_never_commits_manifest_after_a_partial_phase(tmp_path: Path):
    manifest_calls: list[dict] = []
    result = publish_cycle(
        snapshot_path=tmp_path / "current.json",
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        publish_snapshot_fn=lambda *_args, **_kwargs: {"status": "partial", "failed": 1},
        publish_ledger_fn=lambda *_args, **_kwargs: {"status": "dry_run", "rows": 0, "failed": 0},
        publish_site_manifest_fn=lambda payload, **_kwargs: (
            manifest_calls.append(payload) or {"status": "dry_run"}
        ),
    )

    assert manifest_calls == []
    assert result["status"] == "dry_run_partial"
    assert result["phases"]["publication_manifest"]["reason"] == "publication_phases_incomplete"


def test_live_cycle_requires_all_endpoints_and_token(tmp_path: Path):
    with pytest.raises(ValueError, match="all required publication endpoints"):
        publish_cycle(
            snapshot_path=tmp_path / "current.json",
            ledger_path=tmp_path / "observations.jsonl",
            archive_path=tmp_path / "predictions.jsonl",
            publication_epoch="production-v1",
        )


def test_live_cycle_missing_stage_endpoint_blocks_before_every_publisher(
    tmp_path: Path,
) -> None:
    identity = _write_publication_identity(tmp_path)
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "ok"}

    with pytest.raises(ValueError, match="all required publication endpoints"):
        _production_publish_cycle(
            snapshot_path=tmp_path / "current.json",
            ledger_path=tmp_path / "observations.jsonl",
            archive_path=tmp_path / "predictions.jsonl",
            **identity,
            fixtures_endpoint="https://example.test/fixtures",
            intelligence_endpoint="https://example.test/ingest",
            forecast_register_endpoint="https://example.test/forecast/register",
            forecast_endpoint="https://example.test/forecast",
            publication_endpoint="https://example.test/publication/register",
            token=hashlib.sha256(b"fixture-auth").hexdigest(),
            publication_epoch="production-v1",
            publish_snapshot_fn=should_not_publish,
            publish_ledger_fn=should_not_publish,
            publish_source_health_diagnostics_fn=should_not_publish,
            publish_lineup_diagnostics_fn=should_not_publish,
            publish_archive_fn=should_not_publish,
            publish_stage_evaluations_fn=should_not_publish,
        )

    assert calls == []


def test_live_cycle_requires_freshness_and_atomic_publication_gates(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "ok", "failed": 0, "skipped": []}

    with pytest.raises(
        ValueError,
        match="max_snapshot_age_seconds and publication diagnostic",
    ):
        publish_cycle(
            snapshot_path=tmp_path / "current.json",
            ledger_path=tmp_path / "observations.jsonl",
            archive_path=tmp_path / "predictions.jsonl",
            fixtures_endpoint="https://example.test/fixtures",
            intelligence_endpoint="https://example.test/ingest",
            forecast_register_endpoint="https://example.test/forecast/register",
            forecast_endpoint="https://example.test/forecast",
            token=hashlib.sha256(b"fixture-auth").hexdigest(),
            publication_epoch="production-v1",
            publish_snapshot_fn=should_not_publish,
            publish_ledger_fn=should_not_publish,
            publish_archive_fn=should_not_publish,
        )

    assert calls == []


def test_live_cycle_blocks_when_snapshot_is_stale_before_any_publish(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    snapshot.write_text(json.dumps({"as_of": stale}), encoding="utf-8")
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "ok", "failed": 0}

    result = publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        fixtures_endpoint="https://example.test/fixtures",
        intelligence_endpoint="https://example.test/ingest",
        forecast_register_endpoint="https://example.test/forecast/register",
        forecast_endpoint="https://example.test/forecast",
        token=hashlib.sha256(b"fixture-auth").hexdigest(),
        publication_epoch="production-v1",
        max_snapshot_age_seconds=1800,
        require_publication_diagnostic=True,
        publish_snapshot_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "snapshot_stale"
    assert result["snapshotFreshness"]["maxAgeSeconds"] == 1800
    assert calls == []


def test_live_cycle_accepts_a_recent_snapshot_with_a_timezone(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    snapshot.write_text(
        json.dumps({"as_of": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )

    result = publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        max_snapshot_age_seconds=1800,
        publish_snapshot_fn=lambda *_args, **_kwargs: {"status": "dry_run", "failed": 0},
        publish_ledger_fn=lambda *_args, **_kwargs: {"status": "dry_run", "failed": 0},
        publish_archive_fn=lambda *_args, **_kwargs: {
            "status": "dry_run",
            "published": 0,
            "skipped": [],
        },
    )

    assert result["snapshotFreshness"]["status"] == "ok"


def test_live_cycle_requires_atomic_publication_diagnostic_when_requested(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    snapshot.write_text(
        json.dumps({"as_of": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    result = publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        max_snapshot_age_seconds=1800,
        require_publication_diagnostic=True,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "publication_diagnostic_not_found"
    assert result["snapshotPublication"]["status"] == "blocked"


def test_live_cycle_accepts_matching_atomic_publication_diagnostic(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "fixture_feed": {
            "provider": "OpenFootball",
            "source_contract": {
                "fact_source": "OpenFootball",
                "license": "CC0-1.0",
            },
            "fixtures": [
                {
                    "id": "openfootball:test:fixture-one",
                    "competition_id": "premier-league",
                    "season": "2026-27",
                    "source": {
                        "name": "OpenFootball",
                        "source_id": "openfootball:football.json:2026-27:en.1",
                        "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
                        "license": "CC0-1.0",
                    },
                }
            ],
        },
    }
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    diagnostic = {
        "schema_version": "1.0.0",
        "status": "published",
        "output": str(snapshot),
        "published": {
            "as_of": payload["as_of"],
            "fixture_count": 1,
            "content_sha256": _snapshot_canonical_sha256(payload),
        },
    }
    (tmp_path / "current.publication.json").write_text(json.dumps(diagnostic), encoding="utf-8")

    result = publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        max_snapshot_age_seconds=1800,
        require_publication_diagnostic=True,
        publish_snapshot_fn=lambda *_args, **_kwargs: {"status": "dry_run", "failed": 0},
        publish_ledger_fn=lambda *_args, **_kwargs: {"status": "dry_run", "failed": 0},
        publish_archive_fn=lambda *_args, **_kwargs: {
            "status": "dry_run",
            "published": 0,
            "skipped": [],
        },
    )

    assert result["snapshotPublication"]["status"] == "ok"
    assert result["snapshotPublication"]["contentSha256"] == _snapshot_canonical_sha256(payload)


def test_live_cycle_blocks_atomically_published_unlicensed_legacy_espn_snapshot(
    tmp_path: Path,
):
    snapshot = tmp_path / "current.json"
    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "espn": {
            "provider": "ESPN",
            "fixtures": [{"id": "espn:cached"}],
        },
    }
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    diagnostic = {
        "schema_version": "1.0.0",
        "status": "published",
        "output": str(snapshot),
        "published": {
            "as_of": payload["as_of"],
            "fixture_count": 1,
            "content_sha256": _snapshot_canonical_sha256(payload),
        },
    }
    (tmp_path / "current.publication.json").write_text(json.dumps(diagnostic), encoding="utf-8")
    calls: list[str] = []

    def should_not_publish(*_args, **_kwargs):
        calls.append("published")
        return {"status": "dry_run", "failed": 0}

    result = publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        require_publication_diagnostic=True,
        publish_snapshot_fn=should_not_publish,
        publish_ledger_fn=should_not_publish,
        publish_archive_fn=should_not_publish,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "legacy_espn_serving_rights_missing"
    assert result["snapshotPublication"]["status"] == "blocked"
    assert calls == []


def test_live_cycle_blocks_mismatched_atomic_publication_diagnostic(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    payload = {"as_of": datetime.now(timezone.utc).isoformat(), "espn": {"fixtures": []}}
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    diagnostic = {
        "status": "published",
        "output": str(snapshot),
        "published": {
            "as_of": payload["as_of"],
            "fixture_count": 0,
            "content_sha256": "0" * 64,
        },
    }
    (tmp_path / "current.publication.json").write_text(json.dumps(diagnostic), encoding="utf-8")

    result = publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        require_publication_diagnostic=True,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "publication_content_hash_mismatch"


def test_live_cycle_blocks_publication_diagnostic_for_another_runtime(tmp_path: Path):
    snapshot = tmp_path / "current.json"
    payload = {"as_of": datetime.now(timezone.utc).isoformat(), "espn": {"fixtures": []}}
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    diagnostic = {
        "status": "published",
        "output": str(tmp_path / "other-runtime" / "current.json"),
        "published": {
            "as_of": payload["as_of"],
            "fixture_count": 0,
            "content_sha256": _snapshot_canonical_sha256(payload),
        },
    }
    (tmp_path / "current.publication.json").write_text(json.dumps(diagnostic), encoding="utf-8")

    result = publish_cycle(
        snapshot_path=snapshot,
        ledger_path=tmp_path / "observations.jsonl",
        archive_path=tmp_path / "predictions.jsonl",
        dry_run=True,
        require_publication_diagnostic=True,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "publication_output_mismatch"
