from __future__ import annotations

import json
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from league_platform import prospective_cycle
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_HISTORY_SOURCE_IDS

from league_platform.prospective_cycle import (
    _assert_model_lock_continuity,
    _enrich_capture_blocked_diagnostics,
    _promote_model_lock_if_passed,
    _storage_guard,
    _write_json_atomic,
    run_cycle,
)


_REAL_RAW_ARCHIVE_GUARD = prospective_cycle.require_durable_openfootball_raw_archive


@pytest.fixture(autouse=True)
def _tmp_filesystem_budget(monkeypatch):
    """Keep orchestration tests independent of the host repository disk.

    Production keeps the 1 GiB guard. These tests write only to pytest's
    temporary directory, so a nearly-full host filesystem must not prevent
    them from exercising sync, capture, evaluation and failure ordering.
    Tests that specifically cover the guard pass an explicit budget.
    """

    monkeypatch.setitem(run_cycle.__kwdefaults__, "min_free_bytes", 0)
    # Production callers must pass this explicitly. Orchestration unit tests
    # isolate that separately verified trust-boundary guard while exercising
    # the stage ordering and forwarded keyword contracts below.
    monkeypatch.setitem(
        run_cycle.__kwdefaults__,
        "openfootball_raw_archive_dir",
        Path("/tmp/matchline-test-openfootball-raw"),
    )
    monkeypatch.setattr(
        prospective_cycle,
        "require_durable_openfootball_raw_archive",
        lambda path: Path(path),
    )


def _valid_pending_lock(tmp_path: Path) -> tuple[Path, dict]:
    model_file = tmp_path / "model.py"
    model_file.write_text("locked implementation\n", encoding="utf-8")
    lock = {
        "schema_version": "1.0.0",
        "status": "pending_prospective_window",
        "locked_at": "2026-08-13T03:00:00+00:00",
        "evaluation_window_started_at": "2026-08-14T00:00:00+00:00",
        "evaluation_window": "v260-test-window",
        "model_version_sha256": "",
        "model_name": "strict-model",
        "freeze_model_name": "locked-model",
        "model_files": [
            {"path": str(model_file), "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest()}
        ],
        "results_not_used_for_selection": True,
        "sample_requirements_met": False,
        "all_required_targets_scored": False,
        "prediction_freezes_verified": False,
        "policy_version": "v260",
        "observed_before": "2026-08-12T00:00:00+00:00",
        "source_ids": sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS),
        "training_admission": {
            "schema_version": "matchline.prospective_training_admission.v1",
            "status": "training_admitted",
            "policy_version": "v260",
            "observed_before": "2026-08-12T00:00:00+00:00",
            "source_ids": sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS),
            "training_admission_sha256": "1" * 64,
            "raw_admission_sha256": "2" * 64,
            "source_manifest_sha256": "3" * 64,
            "parser_contract_sha256": "4" * 64,
            "raw_rows_sha256": "5" * 64,
            "domain_rows_sha256": "6" * 64,
        },
        "notes": [],
    }
    lock["model_version_sha256"] = hashlib.sha256(
        json.dumps(
            {
                "model_name": lock["model_name"],
                "freeze_model_name": lock["freeze_model_name"],
                "model_files": lock["model_files"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    lock_path = tmp_path / "model-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return lock_path, lock


def test_storage_guard_uses_nearest_existing_ancestor_for_nested_new_targets(tmp_path):
    requested: list[Path] = []

    class Usage:
        free = 4096

    def disk_usage(path: Path):
        requested.append(Path(path))
        if not Path(path).exists():
            raise FileNotFoundError(path)
        return Usage()

    target = tmp_path / "runtime" / "intelligence" / "observations.jsonl"
    result = _storage_guard([target], min_free_bytes=1024, disk_usage_fn=disk_usage)

    assert result["status"] == "ok"
    assert requested == [tmp_path]


def _passed_evaluation() -> dict:
    return {
        "status": "passed",
        "generated_at": "2026-08-20T00:00:00+00:00",
        "scored_n": 12000,
        "result_conflicts": 0,
        "invalid_records": [],
        "results_not_used_for_selection": True,
        "sample_requirements_met": True,
        "all_required_targets_scored": True,
        "prediction_freezes_verified": True,
    }


def test_capture_diagnostics_expose_next_freeze_without_touching_model_inputs():
    captured = {
        "as_of": "2026-08-20T00:00:00+00:00",
        "blocked_diagnostics": {
            "items": [
                {
                    "reason": "no_freeze_cutoff_observed_before_as_of",
                    "competition_id": "premier-league",
                },
                {"reason": "market_feature_source_missing", "competition_id": "premier-league"},
            ]
        },
    }
    snapshot = {
        "espn": {
            "fixtures": [
                {
                    "id": "espn:next",
                    "competition_id": "premier-league",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-22T00:00:00+00:00",
                }
            ]
        }
    }

    result = _enrich_capture_blocked_diagnostics(captured, snapshot)

    assert result is captured
    assert result["blocked_diagnostics"]["items"][0]["next_fixture_id"] == "espn:next"
    assert result["blocked_diagnostics"]["items"][0]["next_freeze_stage"] == "t_minus_24h"
    assert result["blocked_diagnostics"]["items"][0]["next_freeze_cutoff_at"] == "2026-08-21T00:00:00+00:00"
    assert result["blocked_diagnostics"]["items"][1] == {
        "reason": "market_feature_source_missing",
        "competition_id": "premier-league",
    }


def test_model_lock_promotion_is_fail_closed_for_pending_evaluation(tmp_path: Path):
    lock_path, lock = _valid_pending_lock(tmp_path)
    before = lock_path.read_text(encoding="utf-8")

    current, promotion = _promote_model_lock_if_passed(
        lock_path,
        lock,
        {"status": "pending_prospective_window"},
        approved_lock_path=lock_path,
    )

    assert current == lock
    assert promotion["status"] == "not_promoted"
    assert lock_path.read_text(encoding="utf-8") == before


def test_cycle_refuses_lock_replacement_after_active_window_has_evidence(tmp_path: Path):
    lock_path, lock = _valid_pending_lock(tmp_path)
    lock["model_version_sha256"] = "b" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    evidence = tmp_path / "cycle.json"
    evidence.write_text(
        json.dumps({
            "status": "pending_prospective_window",
            "artifacts": {"model_version_sha256": "a" * 64},
        }),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="model lock changed"):
        _assert_model_lock_continuity(lock_path, evidence)


def test_cycle_rejects_model_lock_hash_drift_before_sync_and_preserves_predictions(
    tmp_path: Path,
):
    lock_path, lock = _valid_pending_lock(tmp_path)
    model_file = Path(lock["model_files"][0]["path"])
    model_file.write_text("changed implementation\n", encoding="utf-8")
    live = tmp_path / "current.json"
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_bytes(b"existing-predictions\n")
    calls: list[str] = []

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("model-lock mismatch must be rejected before sync")

    with pytest.raises(ValueError, match="model file hash mismatch"):
        run_cycle(
            live_path=live,
            lock_path=lock_path,
            approved_lock_path=lock_path,
            prediction_archive=predictions,
            cycle_lock_path=tmp_path / "cycle.lock",
            evidence_path=tmp_path / "cycle.json",
            history_path=tmp_path / "cycle.jsonl",
            sync_fn=should_not_sync,
            capture_fn=lambda **_kwargs: pytest.fail("capture must not run"),
        )

    assert calls == []
    assert not live.exists()
    assert predictions.read_bytes() == b"existing-predictions\n"
    failure = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["failure"]["stage"] == "model_lock_integrity"
    assert failure["failure"]["type"] == "ValueError"
    assert len((tmp_path / "cycle.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_cycle_rechecks_model_files_immediately_before_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A model-file replacement in the final pre-sync window must not sync."""

    lock_path, lock = _valid_pending_lock(tmp_path)
    model_file = Path(lock["model_files"][0]["path"])
    calls: list[str] = []
    mutated = False
    original_progress = prospective_cycle._write_stage_progress

    def mutate_before_sync(path: Path, **kwargs: object) -> None:
        nonlocal mutated
        original_progress(path, **kwargs)
        if kwargs.get("stage") == "sync" and not mutated:
            mutated = True
            model_file.write_text("replacement before sync\n", encoding="utf-8")

    monkeypatch.setattr(
        prospective_cycle,
        "_write_stage_progress",
        mutate_before_sync,
    )

    def should_not_sync(*_args: object, **_kwargs: object) -> None:
        calls.append("sync")
        raise AssertionError("model-file drift must be rejected before sync")

    with pytest.raises(ValueError, match="model file hash mismatch"):
        run_cycle(
            live_path=tmp_path / "current.json",
            lock_path=lock_path,
            approved_lock_path=lock_path,
            prediction_archive=tmp_path / "predictions.jsonl",
            cycle_lock_path=tmp_path / "cycle.lock",
            evidence_path=tmp_path / "cycle.json",
            history_path=tmp_path / "cycle.jsonl",
            sync_fn=should_not_sync,
        )

    assert calls == []
    failure = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert failure["failure"]["stage"] == "sync"


def test_strict_cycle_rejects_hash_only_lock_before_sync(tmp_path: Path):
    lock_path = tmp_path / "model-lock.json"
    lock_path.write_text(
        json.dumps({"model_version_sha256": "a" * 64}), encoding="utf-8"
    )
    calls: list[str] = []

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("strict lock preflight must run before sync")

    with pytest.raises(ValueError, match="schema_version is invalid|status is not scoreable"):
        run_cycle(
            live_path=tmp_path / "current.json",
            lock_path=lock_path,
            approved_lock_path=lock_path,
            prediction_archive=tmp_path / "predictions.jsonl",
            cycle_lock_path=tmp_path / "cycle.lock",
            evidence_path=tmp_path / "cycle.json",
            history_path=tmp_path / "cycle.jsonl",
            sync_fn=should_not_sync,
        )

    assert calls == []
    failure = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert failure["failure"]["stage"] == "model_lock_integrity"


def test_cycle_rejects_aggregate_model_identity_drift_before_sync(tmp_path: Path):
    """A valid per-file inventory is not enough to authorize a lock."""

    lock_path, lock = _valid_pending_lock(tmp_path)
    # Add the identity labels needed to make the aggregate digest meaningful,
    # then deliberately leave the stored aggregate at a stale value.
    lock.update(
        {
            "model_name": "strict-model",
            "freeze_model_name": "strict-freeze",
            "model_version_sha256": "c" * 64,
        }
    )
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    calls: list[str] = []

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("aggregate lock identity must be rejected before sync")

    with pytest.raises(ValueError, match="aggregate"):
        run_cycle(
            live_path=tmp_path / "current.json",
            lock_path=lock_path,
            approved_lock_path=lock_path,
            prediction_archive=tmp_path / "predictions.jsonl",
            cycle_lock_path=tmp_path / "cycle.lock",
            evidence_path=tmp_path / "cycle.json",
            history_path=tmp_path / "cycle.jsonl",
            sync_fn=should_not_sync,
        )

    assert calls == []
    assert not (tmp_path / "current.json").exists()


def test_formal_cycle_rejects_candidate_lock_without_approved_identity_match(tmp_path: Path):
    """A candidate may be used by a data-only lane, never by formal promotion."""

    (tmp_path / "candidate").mkdir()
    (tmp_path / "approved").mkdir()
    candidate_path, candidate = _valid_pending_lock(tmp_path / "candidate")
    approved_path, approved = _valid_pending_lock(tmp_path / "approved")
    # Keep the approved lock internally valid while changing its semantic
    # window identity; the formal lane must reject the candidate/approved
    # fingerprint mismatch before sync.
    approved["evaluation_window"] = "v260-approved-window"
    approved_path.write_text(json.dumps(approved), encoding="utf-8")
    calls: list[str] = []

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("candidate/approved mismatch must block before sync")

    with pytest.raises(ValueError, match="candidate.*approved|identity mismatch"):
        run_cycle(
            live_path=tmp_path / "current.json",
            lock_path=candidate_path,
            approved_lock_path=approved_path,
            prediction_archive=tmp_path / "predictions.jsonl",
            cycle_lock_path=tmp_path / "cycle.lock",
            evidence_path=tmp_path / "cycle.json",
            history_path=tmp_path / "cycle.jsonl",
            sync_fn=should_not_sync,
        )

    assert calls == []


def test_formal_cycle_requires_explicit_approved_lock_before_sync(tmp_path: Path):
    """The formal lane must never treat its lock input as self-approved."""

    lock_path, _lock = _valid_pending_lock(tmp_path)
    calls: list[str] = []

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("formal lane must bind an explicit approved lock first")

    with pytest.raises(ValueError, match="approved.*lock"):
        run_cycle(
            live_path=tmp_path / "current.json",
            lock_path=lock_path,
            prediction_archive=tmp_path / "predictions.jsonl",
            cycle_lock_path=tmp_path / "cycle.lock",
            evidence_path=tmp_path / "cycle.json",
            history_path=tmp_path / "cycle.jsonl",
            sync_fn=should_not_sync,
        )

    assert calls == []
    failure = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert failure["failure"]["stage"] == "model_lock_binding"
    assert failure["capture"] == {}
    assert failure["evaluation"] == {}


def test_formal_cycle_rejects_distinct_candidate_and_approved_paths_before_sync(
    tmp_path: Path,
):
    """Matching fingerprints do not make an isolated candidate the active lock."""

    candidate_dir = tmp_path / "candidate"
    approved_dir = tmp_path / "approved"
    candidate_dir.mkdir()
    approved_dir.mkdir()
    candidate_path, candidate = _valid_pending_lock(candidate_dir)
    approved_path = approved_dir / "model-lock.json"
    approved_path.write_bytes(candidate_path.read_bytes())
    calls: list[str] = []

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("candidate and approved paths must be identical in formal lane")

    with pytest.raises(ValueError, match="lock_path.*approved|approved.*lock_path"):
        run_cycle(
            live_path=tmp_path / "current.json",
            lock_path=candidate_path,
            approved_lock_path=approved_path,
            prediction_archive=tmp_path / "predictions.jsonl",
            cycle_lock_path=tmp_path / "cycle.lock",
            evidence_path=tmp_path / "cycle.json",
            history_path=tmp_path / "cycle.jsonl",
            sync_fn=should_not_sync,
        )

    assert calls == []
    failure = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert failure["failure"]["stage"] == "model_lock_binding"
    assert failure["capture"] == {}
    assert failure["evaluation"] == {}


def test_runtime_only_candidate_identity_is_persisted_in_cycle_evidence(tmp_path: Path):
    candidate_path, _candidate = _valid_pending_lock(tmp_path)
    live = tmp_path / "runtime" / "current.json"

    def fake_sync(output, **_kwargs):
        payload = {
            "as_of": "2026-08-22T00:00:00+00:00",
            "espn": {"fixtures": []},
            "espn_markets": {"markets": []},
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    report = run_cycle(
        live_path=live,
        archive_dir=tmp_path / "runtime" / "archive",
        intelligence_ledger=tmp_path / "runtime" / "intelligence.jsonl",
        lock_path=candidate_path,
        prediction_archive=tmp_path / "runtime" / "predictions.jsonl",
        cycle_lock_path=tmp_path / "runtime" / "cycle.lock",
        evidence_path=tmp_path / "runtime" / "cycle.json",
        history_path=tmp_path / "runtime" / "cycle.jsonl",
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=lambda *_args, **_kwargs: {
            "status": "pending_prospective_window",
            "scored_n": 0,
        },
        runtime_only=True,
        runtime_root=tmp_path / "runtime",
        min_free_bytes=0,
    )

    assert report["lock_identity"]["path"] == str(candidate_path)
    assert report["lock_identity"]["model_version_sha256"] == _candidate[
        "model_version_sha256"
    ]
    lock_bytes = candidate_path.read_bytes()
    assert report["lock_identity"]["sha256"] == hashlib.sha256(lock_bytes).hexdigest()
    assert report["lock_identity"]["lock_identity_sha256"]
    assert report["artifacts"]["model_lock_sha256"]
    assert report["artifacts"]["lock_identity_sha256"]


def test_runtime_only_cycle_without_explicit_candidate_lock_is_blocked_before_sync(
    tmp_path: Path,
):
    calls: list[str] = []

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("runtime-only lane must receive an explicit candidate path")

    with pytest.raises(ValueError, match="explicit.*candidate.*lock"):
        run_cycle(
            live_path=tmp_path / "runtime" / "current.json",
            archive_dir=tmp_path / "runtime" / "archive",
            intelligence_ledger=tmp_path / "runtime" / "intelligence.jsonl",
            prediction_archive=tmp_path / "runtime" / "predictions.jsonl",
            cycle_lock_path=tmp_path / "runtime" / "cycle.lock",
            evidence_path=tmp_path / "runtime" / "cycle.json",
            history_path=tmp_path / "runtime" / "cycle.jsonl",
            sync_fn=should_not_sync,
            runtime_only=True,
            runtime_root=tmp_path / "runtime",
            min_free_bytes=0,
        )

    assert calls == []


def test_cycle_allows_formal_lock_refresh_after_previous_window_finished(tmp_path: Path):
    lock_path, lock = _valid_pending_lock(tmp_path)
    lock["model_version_sha256"] = "b" * 64
    lock["locked_at"] = "2026-08-20T00:00:00+00:00"
    lock["evaluation_window_started_at"] = "2026-08-20T00:00:01+00:00"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    evidence = tmp_path / "cycle.json"
    evidence.write_text(
        json.dumps({
            "status": "pending_prospective_window",
            "finished_at": "2026-08-19T23:59:00+00:00",
            "artifacts": {"model_version_sha256": "a" * 64},
        }),
        encoding="utf-8",
    )

    # A formal refresh starts a strictly later window and must not be treated
    # as an in-window replacement of the model hash.
    _assert_model_lock_continuity(lock_path, evidence)


def test_model_lock_promotion_requires_all_evidence_and_writes_atomically(tmp_path: Path):
    lock_path, lock = _valid_pending_lock(tmp_path)
    promoted, promotion = _promote_model_lock_if_passed(
        lock_path,
        lock,
        _passed_evaluation(),
        approved_lock_path=lock_path,
        promoted_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert promotion["status"] == "promoted"
    assert promoted["status"] == "passed"
    assert promoted["sample_requirements_met"] is True
    assert promoted["prediction_freezes_verified"] is True
    saved = json.loads(lock_path.read_text(encoding="utf-8"))
    assert saved["prospective_evaluation_scored_n"] == 12000
    assert saved["prospective_evaluation_digest"]
    assert not list(tmp_path.glob(".model-lock.json.*.tmp"))


def test_model_lock_promotion_rejects_partial_pass_claim(tmp_path: Path):
    lock_path, lock = _valid_pending_lock(tmp_path)
    evaluation = _passed_evaluation()
    evaluation["prediction_freezes_verified"] = False

    with pytest.raises(ValueError, match="required evidence"):
        _promote_model_lock_if_passed(
            lock_path,
            lock,
            evaluation,
            approved_lock_path=lock_path,
        )

    assert json.loads(lock_path.read_text(encoding="utf-8"))["status"] == "pending_prospective_window"


def test_cycle_promotes_only_after_evaluator_passes(tmp_path: Path):
    lock_path, _ = _valid_pending_lock(tmp_path)
    live = tmp_path / "current.json"
    predictions = tmp_path / "predictions.jsonl"

    def fake_sync(output, **kwargs):
        payload = {
            "as_of": "2026-08-20T00:00:00+00:00",
            "espn": {"fixtures": []},
            "espn_markets": {"markets": []},
        }
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    report = run_cycle(
        live_path=live,
        lock_path=lock_path,
        approved_lock_path=lock_path,
        prediction_archive=predictions,
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        evaluation_evidence_path=tmp_path / "evaluation.json",
        history_path=tmp_path / "cycle.jsonl",
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=lambda *_args, **_kwargs: _passed_evaluation(),
    )

    assert report["status"] == "passed"
    assert report["lock_promotion"]["status"] == "promoted"
    assert json.loads(lock_path.read_text(encoding="utf-8"))["status"] == "passed"
    assert json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))["lock_promotion"]["status"] == "promoted"


def test_cycle_forwards_explicit_crawl4ai_config_to_sync(tmp_path: Path):
    lock_path, _ = _valid_pending_lock(tmp_path)
    live = tmp_path / "current.json"
    seen: dict[str, object] = {}

    def fake_sync(output, **kwargs):
        seen.update(kwargs)
        payload = {
            "as_of": "2026-08-20T00:00:00+00:00",
            "espn": {"fixtures": []},
            "espn_markets": {"markets": []},
        }
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    report = run_cycle(
        live_path=live,
        lock_path=lock_path,
        approved_lock_path=lock_path,
        prediction_archive=tmp_path / "predictions.jsonl",
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        history_path=tmp_path / "cycle.jsonl",
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=lambda *_args, **_kwargs: {"status": "pending_prospective_window", "scored_n": 0},
        crawl4ai_config_path=tmp_path / "operator.json",
        allow_empty_snapshot=True,
    )

    assert report["status"] == "pending_prospective_window"
    assert seen["crawl4ai_config_path"] == tmp_path / "operator.json"
    assert seen["allow_empty_snapshot"] is True
    assert seen["enable_wikidata"] is False


def test_cycle_isolates_empty_source_without_capturing_stale_pointer(tmp_path: Path):
    lock_path, _ = _valid_pending_lock(tmp_path)
    live = tmp_path / "current.json"
    previous = {
        "as_of": "2026-08-20T00:00:00+00:00",
        "espn": {"fixtures": [{"id": "espn:previous"}]},
        "publication": {
            "status": "blocked",
            "reason": "empty_fixture_set_would_replace_non_empty_snapshot",
        },
    }
    live.write_text(json.dumps(previous), encoding="utf-8")
    calls: list[str] = []

    def fake_sync(output, **_kwargs):
        calls.append("sync")
        return previous

    def should_not_capture(**_kwargs):
        calls.append("capture")
        raise AssertionError("blocked snapshot must not be captured")

    report = run_cycle(
        live_path=live,
        lock_path=lock_path,
        approved_lock_path=lock_path,
        prediction_archive=tmp_path / "predictions.jsonl",
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        evaluation_evidence_path=tmp_path / "evaluation.json",
        history_path=tmp_path / "cycle.jsonl",
        sync_fn=fake_sync,
        capture_fn=should_not_capture,
        evaluate_fn=lambda *_args, **_kwargs: {
            "status": "pending_prospective_window",
            "scored_n": 0,
            "pending_n": 1,
        },
    )

    assert calls == ["sync"]
    assert report["status"] == "pending_prospective_window"
    assert report["degraded"] is True
    assert report["publication"]["status"] == "blocked"
    assert report["publication"]["reason"] == "empty_fixture_set_would_replace_non_empty_snapshot"
    assert report["capture"] == {
        "status": "blocked",
        "predictions": 0,
        "reason": "live_snapshot_publication_blocked",
        "publication": previous["publication"],
        "model_entry_allowed": False,
    }
    assert report["oddstorm_history"] is None
    evidence = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert evidence["degraded"] is True
    assert evidence["publication"]["status"] == "blocked"
    assert "failure" not in evidence
    evaluation_evidence = json.loads(
        (tmp_path / "evaluation.json").read_text(encoding="utf-8")
    )
    assert evaluation_evidence["degraded"] is True
    assert evaluation_evidence["publication"]["status"] == "blocked"


def test_cycle_blocks_before_sync_when_filesystem_budget_is_low(tmp_path: Path):
    lock_path, _ = _valid_pending_lock(tmp_path)
    calls: list[str] = []

    class Usage:
        free = 0

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("storage guard must run before network sync")

    report = run_cycle(
        live_path=tmp_path / "current.json",
        archive_dir=tmp_path / "archive",
        intelligence_ledger=tmp_path / "intelligence.jsonl",
        lock_path=lock_path,
        approved_lock_path=lock_path,
        prediction_archive=tmp_path / "predictions.jsonl",
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        evaluation_evidence_path=tmp_path / "evaluation.json",
        history_path=tmp_path / "cycle.jsonl",
        sync_fn=should_not_sync,
        min_free_bytes=1024,
        disk_usage_fn=lambda _path: Usage(),
    )

    assert calls == []
    assert report["status"] == "blocked_storage"
    assert report["degraded"] is True
    assert report["storage"]["status"] == "blocked"
    assert report["capture"]["model_entry_allowed"] is False
    assert report["evaluation"]["reason"] == "insufficient_filesystem_space"
    saved = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert saved["status"] == "blocked_storage"
    assert saved["storage"]["blocked_paths"]
    evaluation = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert evaluation["status"] == "blocked_storage"
    assert evaluation["degraded"] is True


@pytest.mark.parametrize("mode", ["volatile", "missing", "malformed"])
def test_cycle_rejects_invalid_raw_archive_before_sync_and_preserves_predictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(
        prospective_cycle,
        "require_durable_openfootball_raw_archive",
        _REAL_RAW_ARCHIVE_GUARD,
    )
    lock_path, _ = _valid_pending_lock(tmp_path)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_bytes(b"existing-predictions\n")
    calls: list[str] = []

    def should_not_sync(*_args, **_kwargs):
        calls.append("sync")
        raise AssertionError("raw archive guard must run before sync")

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
            run_cycle(
                live_path=tmp_path / "current.json",
                lock_path=lock_path,
                approved_lock_path=lock_path,
                prediction_archive=predictions,
                cycle_lock_path=tmp_path / "cycle.lock",
                evidence_path=tmp_path / "cycle.json",
                evaluation_evidence_path=tmp_path / "evaluation.json",
                history_path=tmp_path / "cycle.jsonl",
                openfootball_raw_archive_dir=raw_archive_dir,
                sync_fn=should_not_sync,
            )

    assert calls == []
    assert predictions.read_bytes() == b"existing-predictions\n"
    evidence = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert evidence["failure"]["stage"] == "openfootball_raw_archive_guard"
    assert evidence["sync"]["status"] == "blocked"
    assert evidence["sync"]["reason"] == "openfootball_raw_archive_invalid"
    assert evidence["capture"] == {
        "status": "blocked",
        "predictions": 0,
        "reason": "openfootball_raw_archive_invalid",
        "model_entry_allowed": False,
    }
    assert evidence["evaluation"] == {
        "status": "blocked",
        "scored_n": 0,
        "pending_n": 0,
        "reason": "openfootball_raw_archive_invalid",
        "promotion_eligible": False,
    }
    assert json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8")) == evidence[
        "evaluation"
    ]


def test_cycle_budgets_repository_filesystem_for_post_chain_writes(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    runtime.mkdir()
    repo.mkdir()
    monkeypatch.chdir(repo)
    lock_path, _ = _valid_pending_lock(runtime)

    class Usage:
        def __init__(self, free: int):
            self.free = free

    def disk_usage(path: Path):
        return Usage(0 if path.resolve() == repo.resolve() else 2048)

    report = run_cycle(
        live_path=runtime / "current.json",
        archive_dir=runtime / "archive",
        intelligence_ledger=runtime / "intelligence.jsonl",
        lock_path=lock_path,
        approved_lock_path=lock_path,
        prediction_archive=runtime / "predictions.jsonl",
        cycle_lock_path=runtime / "cycle.lock",
        evidence_path=runtime / "cycle.json",
        evaluation_evidence_path=runtime / "evaluation.json",
        history_path=runtime / "cycle.jsonl",
        sync_fn=lambda *_args, **_kwargs: pytest.fail("repository guard must run first"),
        min_free_bytes=1024,
        disk_usage_fn=disk_usage,
    )

    assert report["status"] == "blocked_storage"
    assert str(repo) in report["storage"]["blocked_paths"]


def test_runtime_only_cycle_skips_repository_check_but_keeps_writes_below_runtime_root(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    runtime.mkdir()
    repo.mkdir()
    monkeypatch.chdir(repo)
    lock_path, _ = _valid_pending_lock(tmp_path)
    calls: list[str] = []

    class Usage:
        free = 2048

    def fake_sync(output, **_kwargs):
        calls.append("sync")
        payload = {
            "as_of": "2026-08-22T00:00:00+00:00",
            "espn": {"fixtures": [], "errors": []},
            "espn_markets": {"markets": []},
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        calls.append("capture")
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    report = run_cycle(
        live_path=runtime / "current.json",
        archive_dir=runtime / "archive",
        intelligence_ledger=runtime / "intelligence.jsonl",
        lock_path=lock_path,
        prediction_archive=runtime / "predictions.jsonl",
        cycle_lock_path=runtime / "cycle.lock",
        evidence_path=runtime / "cycle.json",
        evaluation_evidence_path=runtime / "evaluation.json",
        history_path=runtime / "cycle.jsonl",
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=lambda *_args, **_kwargs: {"status": "pending_prospective_window", "scored_n": 0},
        min_free_bytes=1024,
        disk_usage_fn=lambda _path: Usage(),
        runtime_only=True,
        runtime_root=runtime,
    )

    assert calls == ["sync", "capture"]
    assert report["status"] == "pending_prospective_window"
    assert report["runtime_only"] is True
    assert report["storage"]["repository_check_skipped"] is True
    assert report["lock_promotion"]["reason"] == "runtime_only_no_repository_writes"


def test_runtime_only_rejects_a_writable_path_outside_runtime_root(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with pytest.raises(ValueError, match="runtime-only cycle requires every writable path"):
        run_cycle(
            live_path=tmp_path / "outside-current.json",
            archive_dir=runtime / "archive",
            lock_path=tmp_path / "model-lock.json",
            prediction_archive=runtime / "predictions.jsonl",
            cycle_lock_path=runtime / "cycle.lock",
            evidence_path=runtime / "cycle.json",
            history_path=runtime / "cycle.jsonl",
            runtime_only=True,
            runtime_root=runtime,
        )


def test_interrupted_cycle_persists_an_explicit_interrupted_checkpoint(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lock_path, _ = _valid_pending_lock(tmp_path)

    def interrupted_sync(*_args, **_kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_cycle(
            live_path=runtime / "current.json",
            archive_dir=runtime / "archive",
            intelligence_ledger=runtime / "intelligence.jsonl",
            lock_path=lock_path,
            prediction_archive=runtime / "predictions.jsonl",
            cycle_lock_path=runtime / "cycle.lock",
            evidence_path=runtime / "cycle.json",
            evaluation_evidence_path=runtime / "evaluation.json",
            history_path=runtime / "cycle.jsonl",
            sync_fn=interrupted_sync,
            runtime_only=True,
            runtime_root=runtime,
            min_free_bytes=0,
        )

    saved = json.loads((runtime / "cycle.json").read_text(encoding="utf-8"))
    progress = json.loads((runtime / "prospective-cycle-progress.json").read_text(encoding="utf-8"))
    assert saved["status"] == "interrupted"
    assert saved["failure"]["operator_interrupted"] is True
    assert progress["status"] == "interrupted"


def test_cli_returns_nonzero_for_storage_block(monkeypatch, capsys):
    monkeypatch.setattr(
        prospective_cycle,
        "run_cycle",
        lambda **_kwargs: {
            "status": "blocked_storage",
            "started_at": "start",
            "finished_at": "finish",
            "sync": {},
            "capture": {},
            "market_audit": None,
            "oddstorm_history": None,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["prospective_cycle", "--approved-lock", "/tmp/approved-model-lock.json"],
    )

    assert prospective_cycle.main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "blocked_storage"


def test_runtime_only_cli_separates_receipts_from_strict_lane(tmp_path: Path, monkeypatch, capsys):
    captured_calls: list[dict[str, object]] = []

    def fake_run_cycle(**kwargs):
        captured_calls.append(kwargs)
        return {
            "status": "pending_prospective_window",
            "started_at": "start",
            "finished_at": "finish",
            "sync": {},
            "capture": {},
            "market_audit": None,
            "oddstorm_history": None,
        }

    monkeypatch.setattr(prospective_cycle, "run_cycle", fake_run_cycle)
    monkeypatch.setenv(
        "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR",
        str(tmp_path / "from-env"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prospective_cycle",
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--runtime-only",
            "--lock",
            str(tmp_path / "candidate-lock.json"),
            "--openfootball-raw-archive-dir",
            str(tmp_path / "from-flag"),
        ],
    )

    assert prospective_cycle.main() == 0
    captured = captured_calls[-1]
    assert captured["evidence_path"] == tmp_path / "runtime/runtime-only-cycle-latest.json"
    assert (
        captured["evaluation_evidence_path"]
        == tmp_path / "runtime/runtime-only-evaluation-current.json"
    )
    assert captured["history_path"] == tmp_path / "runtime/runtime-only-cycle.jsonl"
    assert captured["progress_path"] == tmp_path / "runtime/runtime-only-cycle-progress.json"
    # Shared live/archive writes must still use the operator runtime paths.
    assert captured["live_path"] == tmp_path / "runtime/current.json"
    assert captured["cycle_lock_path"] == tmp_path / "runtime/prospective-cycle.lock"
    assert captured["openfootball_raw_archive_dir"] == tmp_path / "from-flag"
    assert captured["enable_wikidata"] is False

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prospective_cycle",
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--runtime-only",
            "--lock",
            str(tmp_path / "candidate-lock.json"),
            "--enable-wikidata",
        ],
    )
    assert prospective_cycle.main() == 0
    assert captured_calls[-1]["enable_wikidata"] is True
    assert captured_calls[-1]["openfootball_raw_archive_dir"] == tmp_path / "from-env"
    monkeypatch.delenv("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prospective_cycle",
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--runtime-only",
            "--lock",
            str(tmp_path / "candidate-lock.json"),
        ],
    )
    assert prospective_cycle.main() == 0
    assert captured_calls[-1]["enable_wikidata"] is False
    assert captured_calls[-1]["openfootball_raw_archive_dir"] == (
        tmp_path / "runtime/openfootball-raw"
    )
    assert all(
        json.loads(line)["status"] == "pending_prospective_window"
        for line in capsys.readouterr().out.splitlines()
    )


def test_cycle_does_not_budget_read_only_model_lock_filesystem(tmp_path: Path):
    lock_path, _ = _valid_pending_lock(tmp_path)

    class Usage:
        free = 0

    class RuntimeUsage:
        free = 2048

    def fake_sync(output, **_kwargs):
        payload = {
            "as_of": "2026-08-20T00:00:00+00:00",
            "espn": {"fixtures": []},
            "espn_markets": {"markets": []},
        }
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    def disk_usage(path: Path):
        # The model lock is intentionally on a full repository filesystem in
        # this regression fixture; it is read-only until a passed evaluation
        # asks for the bounded promotion write.
        return Usage() if path == lock_path else RuntimeUsage()

    report = run_cycle(
        live_path=tmp_path / "current.json",
        archive_dir=tmp_path / "archive",
        intelligence_ledger=tmp_path / "intelligence.jsonl",
        lock_path=lock_path,
        approved_lock_path=lock_path,
        prediction_archive=tmp_path / "predictions.jsonl",
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        evaluation_evidence_path=tmp_path / "evaluation.json",
        history_path=tmp_path / "cycle.jsonl",
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=lambda *_args, **_kwargs: {
            "status": "pending_prospective_window",
            "scored_n": 0,
            "pending_n": 0,
        },
        min_free_bytes=1024,
        disk_usage_fn=disk_usage,
    )

    assert report["status"] == "pending_prospective_window"
    assert report["storage"]["status"] == "ok"
    assert str(lock_path) in report["storage"]["read_only_paths"]


def test_atomic_evidence_write_removes_partial_file_on_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / "evidence.json"
    original_write_text = Path.write_text

    def write_then_fail(path, *args, **kwargs):
        original_write_text(path, *args, **kwargs)
        raise OSError("simulated disk-full write failure")

    monkeypatch.setattr(Path, "write_text", write_then_fail)
    with pytest.raises(OSError, match="disk-full"):
        _write_json_atomic(target, {"status": "failed"})

    assert not target.exists()
    assert not list(tmp_path.glob(".evidence.json.*.tmp"))


def test_cycle_serializes_stages_and_writes_hashable_evidence(tmp_path: Path):
    calls: list[str] = []
    live = tmp_path / "current.json"
    model_lock, _ = _valid_pending_lock(tmp_path)
    prediction_archive = tmp_path / "predictions.jsonl"

    def fake_sync(output, **kwargs):
        assert not calls
        assert kwargs["openfootball_raw_archive_dir"] == Path(
            "/tmp/matchline-test-openfootball-raw"
        )
        calls.append("sync")
        output.write_text(
            json.dumps(
                {
                    "as_of": "2026-08-22T00:00:00+00:00",
                    "espn": {"fixtures": [1]},
                    "espn_markets": {"markets": [1]},
                }
            ),
            encoding="utf-8",
        )
        return json.loads(output.read_text(encoding="utf-8"))

    def fake_capture(**kwargs):
        assert calls == ["sync"]
        assert kwargs["openfootball_raw_archive_dir"] == Path(
            "/tmp/matchline-test-openfootball-raw"
        )
        calls.append("capture")
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0, "appended": 0}

    def fake_evaluate(
        prediction_path,
        archive_dir,
        *,
        openfootball_raw_archive_dir,
        lock,
    ):
        assert calls == ["sync", "capture"]
        assert prediction_path == prediction_archive
        assert openfootball_raw_archive_dir == Path("/tmp/matchline-test-openfootball-raw")
        assert lock["model_version_sha256"] == model_lock_payload["model_version_sha256"]
        calls.append("evaluate")
        return {"status": "pending_prospective_window", "scored_n": 0}

    model_lock_payload = json.loads(model_lock.read_text(encoding="utf-8"))
    result = run_cycle(
        live_path=live,
        lock_path=model_lock,
        approved_lock_path=model_lock,
        prediction_archive=prediction_archive,
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        evaluation_evidence_path=tmp_path / "evaluation.json",
        history_path=tmp_path / "cycle.jsonl",
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=fake_evaluate,
    )

    assert calls == ["sync", "capture", "evaluate"]
    assert result["status"] == "pending_prospective_window"
    assert result["lock_promotion"]["status"] == "not_promoted"
    saved = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert saved["sync"]["snapshot_sha256"]
    assert saved["artifacts"]["prediction_archive_sha256"]
    evaluation = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert evaluation["status"] == "pending_prospective_window"
    assert saved["artifacts"]["evaluation_evidence"] == str(tmp_path / "evaluation.json")
    assert saved["artifacts"]["evaluation_evidence_sha256"]
    assert len((tmp_path / "cycle.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_cycle_runs_market_audit_after_evaluation_under_same_lock(tmp_path: Path):
    calls: list[str] = []
    live = tmp_path / "current.json"
    model_lock, _ = _valid_pending_lock(tmp_path)
    prediction_archive = tmp_path / "predictions.jsonl"
    market_output = tmp_path / "market-baselines.jsonl"

    def fake_sync(output, **kwargs):
        calls.append("sync")
        payload = {"as_of": "2026-08-22T00:00:00+00:00", "espn": {}, "espn_markets": {}}
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        calls.append("capture")
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    def fake_evaluate(*args, **kwargs):
        calls.append("evaluate")
        return {"status": "pending_prospective_window", "scored_n": 0}

    def fake_market_audit(*args):
        calls.append("market_audit")
        assert args == (prediction_archive, tmp_path / "archive", market_output)
        return {
            "matched_rows": 0,
            "appended": 0,
            "diagnostics": 1,
            "diagnostic_reasons": {"market_feature_source_missing": 1},
            "diagnostic_items": [{"line": 7, "reason": "market_feature_source_missing"}],
        }

    report = run_cycle(
        live_path=live,
        archive_dir=tmp_path / "archive",
        lock_path=model_lock,
        approved_lock_path=model_lock,
        prediction_archive=prediction_archive,
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        history_path=tmp_path / "cycle.jsonl",
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=fake_evaluate,
        market_audit_fn=fake_market_audit,
        market_audit_output=market_output,
    )

    assert calls == ["sync", "capture", "evaluate", "market_audit"]
    assert report["market_audit"]["matched_rows"] == 0
    assert report["market_audit"]["diagnostic_reasons"] == {"market_feature_source_missing": 1}
    assert report["market_audit"]["diagnostic_items"][0]["line"] == 7
    assert report["artifacts"]["market_audit_output"] == str(market_output)
    assert report["artifacts"]["market_audit_output_sha256"] is None
    assert json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))["market_audit"]


def test_cycle_isolates_market_audit_failure_from_primary_result(tmp_path: Path):
    live = tmp_path / "current.json"
    model_lock, _ = _valid_pending_lock(tmp_path)

    def fake_sync(output, **kwargs):
        payload = {"as_of": "2026-08-22T00:00:00+00:00", "espn": {}, "espn_markets": {}}
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    def fake_evaluate(*args, **kwargs):
        return {"status": "pending_prospective_window", "scored_n": 0}

    def failing_market_audit(*args):
        raise ValueError("sidecar archive is temporarily unreadable")

    report = run_cycle(
        live_path=live,
        archive_dir=tmp_path / "archive",
        lock_path=model_lock,
        approved_lock_path=model_lock,
        prediction_archive=tmp_path / "predictions.jsonl",
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        history_path=tmp_path / "cycle.jsonl",
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=fake_evaluate,
        market_audit_fn=failing_market_audit,
        market_audit_output=tmp_path / "market-baselines.jsonl",
    )

    assert report["status"] == "pending_prospective_window"
    assert report["market_audit"] == {
        "status": "failed",
        "type": "ValueError",
        "message": "sidecar archive is temporarily unreadable",
    }


def test_cycle_isolates_oddstorm_history_failure_from_primary_result(tmp_path: Path):
    live = tmp_path / "current.json"
    model_lock, _ = _valid_pending_lock(tmp_path)

    def fake_sync(output, **kwargs):
        payload = {"as_of": "2026-08-22T00:00:00+00:00", "espn": {}, "espn_markets": {}}
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    def fake_evaluate(*args, **kwargs):
        return {"status": "pending_prospective_window", "scored_n": 0}

    def failing_history(*args, **kwargs):
        raise ValueError("OddStorm history is temporarily unavailable")

    report = run_cycle(
        live_path=live,
        archive_dir=tmp_path / "archive",
        lock_path=model_lock,
        approved_lock_path=model_lock,
        prediction_archive=tmp_path / "predictions.jsonl",
        cycle_lock_path=tmp_path / "cycle.lock",
        evidence_path=tmp_path / "cycle.json",
        history_path=tmp_path / "cycle.jsonl",
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=fake_evaluate,
        oddstorm_history_fn=failing_history,
        oddstorm_history_output=tmp_path / "oddstorm-history.jsonl",
    )

    assert report["status"] == "pending_prospective_window"
    assert report["oddstorm_history"] == {
        "status": "failed",
        "type": "ValueError",
        "message": "OddStorm history is temporarily unavailable",
        "output": str(tmp_path / "oddstorm-history.jsonl"),
    }


def test_systemd_scheduler_repeats_after_oneshot_activation():
    root = Path(__file__).parents[1]
    timer = (root / "deploy/systemd/matchline-prospective-cycle.timer").read_text(encoding="utf-8")
    service = (root / "deploy/systemd/matchline-prospective-cycle.service").read_text(
        encoding="utf-8"
    )

    # A Type=oneshot service is inactive between runs, so the timer must use
    # the inactive edge to continue scheduling after each completed cycle.
    assert "OnBootSec=5min" in timer
    assert "OnUnitInactiveSec=15min" in timer
    assert "OnUnitActiveSec=" not in timer
    assert "OnActiveSec=" not in timer
    assert "Unit=matchline-prospective-cycle.service" in timer
    assert "Type=oneshot" in service
    assert "RequiresMountsFor=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime" in service
    assert "ExecStart=/usr/bin/python3 -m league_platform.prospective_cycle" in service
    assert "--lock=${MATCHLINE_PROSPECTIVE_LOCK}" in service
    assert "Environment=MATCHLINE_PROSPECTIVE_APPROVED_LOCK=" in service
    assert "--approved-lock=${MATCHLINE_PROSPECTIVE_APPROVED_LOCK}" in service
    assert "--approved-lock=${MATCHLINE_PROSPECTIVE_LOCK}" not in service
    assert ".venv/bin/python" not in service
    assert "Environment=PYTHONPATH=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/python-deps-no-deps" in service
    assert "Environment=XDG_CACHE_HOME=/dev/shm/matchline-xdg-cache" in service
    assert "Environment=XDG_CONFIG_HOME=/dev/shm/matchline-xdg-config" in service
    assert "Environment=XDG_STATE_HOME=/dev/shm/matchline-xdg-state" in service
    assert (
        "Environment=MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR=/media/hetaisheng/"
        "044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw"
        in service
    )
    assert "--crawl4ai-config /home/hetaisheng/soccerdata/config/crawl4ai_operator_public.json" in service
    assert (
        "--openfootball-raw-archive-dir=${MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR}"
        in service
    )
    assert "ExecStartPost=" in service
    assert (
        "ExecStartPost=/bin/bash /home/hetaisheng/soccerdata/"
        "deploy/systemd/run-matchline-strict-report.sh"
    ) in service
    assert "espn-kickoff-enrichment" not in service
    assert "strict-backtest-2026-08-14-v16-current.json" not in service
    assert "--strict-report ${MATCHLINE_RUNTIME_DIR}/strict-backtest-current.json" in service
    assert "build_offline_bundle" in service
    assert "--evidence-dir /home/hetaisheng/soccerdata/docs/evidence" in service
    assert "--evidence-dir ${MATCHLINE_RUNTIME_DIR}" not in service
    assert "--runtime-evidence-dir ${MATCHLINE_RUNTIME_DIR}" in service
    assert "--output ${MATCHLINE_RUNTIME_DIR}/offline_full.js" in service
    assert "--compact-output ${MATCHLINE_RUNTIME_DIR}/offline_snapshot.json" in service
    assert "--compact-js-output ${MATCHLINE_RUNTIME_DIR}/league-platform-offline_data.js" in service
    assert "/home/hetaisheng/soccerdata/matchline_sites/public/offline_snapshot.json" not in service
    assert "/home/hetaisheng/soccerdata/league_platform/site/offline_data.js" not in service
    assert "run-matchline-sites-build.sh" not in service
    assert "runtime_evidence" in service
    assert "maturity_validator" in service
    assert "--output ${MATCHLINE_RUNTIME_DIR}/maturity-gate-current.json" in service


def test_strict_report_wrapper_keeps_content_addressed_history_and_uses_verified_raw_receipt():
    root = Path(__file__).parents[1]
    wrapper = (root / "deploy/systemd/run-matchline-strict-report.sh").read_text(
        encoding="utf-8"
    )

    assert "set -euo pipefail" in wrapper
    assert 'export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"' in wrapper
    assert 'export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/dev/shm/matchline-python-cache}"' in wrapper
    assert "--strict-report-from-openfootball-history" in wrapper
    assert "--openfootball-history-receipt" in wrapper
    assert "--openfootball-raw-archive" in wrapper
    assert 'python_executable="${repository_root}/.venv/bin/python"' in wrapper
    assert "--data-dir" not in wrapper
    assert "--kickoff-enrichment" not in wrapper
    assert "--observed-before" not in wrapper
    assert "--openfootball-source-id" not in wrapper
    assert 'pointer="${runtime_dir}/strict-backtest-current.json"' in wrapper
    assert 'cache_state="${runtime_dir}/strict-report-cache.json"' in wrapper
    assert 'archive="${reports_dir}/strict-backtest-${report_sha256}.json"' in wrapper
    assert "sha256sum" in wrapper
    assert "cmp -s" in wrapper
    assert "mv -n" in wrapper
    assert "docs/evidence/strict-backtest-2026-08-14-v16-current.json" not in wrapper
    assert "espn-kickoff-enrichment" not in wrapper


def test_strict_report_wrapper_can_bind_a_runtime_only_pointer_to_durable_history_root():
    root = Path(__file__).parents[1]
    wrapper = (root / "deploy/systemd/run-matchline-strict-report.sh").read_text(
        encoding="utf-8"
    )

    # The runtime-only lane keeps its small read-model pointers on tmpfs, but
    # the strict-history verifier requires the receipt and raw archive to sit
    # under one durable root. The wrapper must make both roots explicit and
    # bind the report to the active candidate lock instead of the superseded
    # repository default.
    assert 'history_runtime_root="${MATCHLINE_HISTORY_RUNTIME_ROOT:-${runtime_dir}}"' in wrapper
    assert 'lock_path="${MATCHLINE_STRICT_REPORT_LOCK:-${history_runtime_root}/strict-report.lock}"' in wrapper
    assert "flock -n 9" in wrapper
    assert "strict report archive mismatch" in wrapper
    assert "skipped_unchanged" in wrapper
    assert (
        'history_receipt="${MATCHLINE_OPENFOOTBALL_HISTORY_RECEIPT:-'
        '${history_runtime_root}/openfootball-history-refresh-current.json}"' in wrapper
    )
    assert (
        'prospective_lock="${MATCHLINE_PROSPECTIVE_LOCK:-'
        '${repository_root}/docs/evidence/prospective-model-lock-current.json}"' in wrapper
    )
    assert '--runtime-dir "${history_runtime_root}"' in wrapper
    assert '--openfootball-history-receipt "${history_receipt}"' in wrapper
    assert '--strict-report-prospective-lock "${prospective_lock}"' in wrapper
    assert "history_runtime_root" in wrapper


def test_cycle_refuses_concurrent_process_lock(tmp_path: Path):
    lock_path = tmp_path / "cycle.lock"
    with lock_path.open("a", encoding="utf-8") as held:
        import fcntl

        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            run_cycle(
                live_path=tmp_path / "current.json",
                lock_path=tmp_path / "model-lock.json",
                cycle_lock_path=lock_path,
                sync_fn=lambda *_args, **_kwargs: {},
            )


def test_cycle_fails_closed_when_snapshot_changes_after_sync(tmp_path: Path):
    live = tmp_path / "current.json"
    model_lock, _ = _valid_pending_lock(tmp_path)

    def fake_sync(output, **kwargs):
        output.write_text(json.dumps({"as_of": "2026-08-22T00:01:00+00:00"}), encoding="utf-8")
        # Simulate another writer replacing the snapshot before this cycle
        # validates the hand-off from sync to capture.
        return {"as_of": "2026-08-22T00:00:00+00:00"}

    # The mismatch is detected before capture is called, so no prediction
    # archive or evaluation can be produced from an untracked snapshot.
    with pytest.raises(RuntimeError, match="snapshot changed"):
        run_cycle(
            live_path=live,
            lock_path=model_lock,
            approved_lock_path=model_lock,
            cycle_lock_path=tmp_path / "cycle.lock",
            evidence_path=tmp_path / "cycle.json",
            history_path=tmp_path / "cycle.jsonl",
            sync_fn=fake_sync,
        )

    failure = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["failure"]["stage"] == "snapshot_validation"
    assert len((tmp_path / "cycle.jsonl").read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("failing_stage", ["capture", "evaluation"])
def test_cycle_persists_downstream_stage_failures(tmp_path: Path, failing_stage: str):
    live = tmp_path / "current.json"
    model_lock, _ = _valid_pending_lock(tmp_path)
    evidence = tmp_path / f"{failing_stage}.json"
    history = tmp_path / f"{failing_stage}.jsonl"

    def fake_sync(output, **kwargs):
        payload = {"as_of": "2026-08-22T00:00:00+00:00", "espn": {}, "espn_markets": {}}
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        if failing_stage == "capture":
            raise ValueError("capture source contract failed")
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    def fake_evaluate(*args, **kwargs):
        raise ValueError("evaluation archive is malformed")

    with pytest.raises(ValueError):
        run_cycle(
            live_path=live,
            lock_path=model_lock,
            approved_lock_path=model_lock,
            prediction_archive=tmp_path / "predictions.jsonl",
            cycle_lock_path=tmp_path / f"{failing_stage}.lock",
            evidence_path=evidence,
            history_path=history,
            sync_fn=fake_sync,
            capture_fn=fake_capture,
            evaluate_fn=fake_evaluate,
        )

    failure = json.loads(evidence.read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["failure"]["stage"] == failing_stage
    assert failure["failure"]["type"] == "ValueError"
    assert len(history.read_text(encoding="utf-8").splitlines()) == 1


def test_cycle_writes_durable_stage_progress_on_completion(tmp_path: Path):
    lock_path, _lock = _valid_pending_lock(tmp_path)
    live = tmp_path / "current.json"
    prediction_archive = tmp_path / "predictions.jsonl"
    cycle_lock = tmp_path / "cycle.lock"
    evidence = tmp_path / "cycle.json"
    history = tmp_path / "cycle.jsonl"

    def fake_sync(output, **kwargs):
        payload = {
            "as_of": "2026-08-22T00:00:00+00:00",
            "espn": {"fixtures": []},
            "espn_markets": {"markets": []},
        }
        output.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_capture(**kwargs):
        kwargs["output"].write_text("", encoding="utf-8")
        return {"predictions": 0}

    report = run_cycle(
        live_path=live,
        lock_path=lock_path,
        approved_lock_path=lock_path,
        prediction_archive=prediction_archive,
        cycle_lock_path=cycle_lock,
        evidence_path=evidence,
        history_path=history,
        now=datetime(2026, 8, 22, tzinfo=timezone.utc),
        sync_fn=fake_sync,
        capture_fn=fake_capture,
        evaluate_fn=lambda *_args, **_kwargs: {
            "status": "pending_prospective_window",
            "scored_n": 0,
            "pending_n": 0,
            "result_conflicts": 0,
            "invalid_records": [],
            "results_not_used_for_selection": True,
            "sample_requirements_met": False,
            "all_required_targets_scored": False,
            "prediction_freezes_verified": False,
        },
    )

    assert report["status"] == "pending_prospective_window"
    progress = json.loads(
        (tmp_path / "prospective-cycle-progress.json").read_text(encoding="utf-8")
    )
    assert progress["status"] == "completed"
    assert progress["stage"] == "complete"
