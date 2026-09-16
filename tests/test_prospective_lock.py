from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import league_platform.prospective_lock as lock_module
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCES,
)
from league_platform.openfootball_history_sync import (
    RECEIPT_ARCHIVE_NAME,
    RECEIPT_SCHEMA_VERSION,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
    load_verified_openfootball_archive,
)
from league_platform.prospective_lock import refresh_lock
from league_platform.sources.openfootball_verified import (
    VerifiedOpenFootballHistorySource,
)


V260_OBSERVED_BEFORE = datetime(2026, 8, 25, 2, 30, tzinfo=timezone.utc)
EXPECTED_V260_MANDATORY_FILES = (
    "league_platform/prospective_lock.py",
    "league_platform/source_rights.py",
    "league_platform/openfootball_raw_archive.py",
    "league_platform/openfootball_history_sync.py",
    "league_platform/sources/openfootball_verified.py",
    "league_platform/model_admission.py",
    "league_platform/strict_report.py",
    "league_platform/store.py",
    "league_platform/runtime_paths.py",
    "league_platform/prospective_cycle.py",
    "league_platform/platform_verification.py",
    "league_platform/maturity_validator.py",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _v259_lock(repository_root: Path) -> dict:
    model_file = repository_root / "legacy_model.py"
    model_file.write_text("legacy locked model\n", encoding="utf-8")
    return {
        "schema_version": "1.0.0",
        "status": "pending_prospective_window",
        "locked_at": "2026-08-24T20:35:28+00:00",
        "evaluation_window_started_at": "2026-08-24T20:35:29+00:00",
        "model_name": "strict-model-v259",
        "freeze_model_name": "strict-freeze-v259",
        "model_version_sha256": "a" * 64,
        "model_files": [
            {
                "path": "legacy_model.py",
                "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
            }
        ],
        "evaluation_window": "v259 pending window",
        "results_not_used_for_selection": True,
        "sample_requirements_met": False,
        "all_required_targets_scored": False,
        "prediction_freezes_verified": False,
        "notes": ["v259 pending evidence"],
    }


def _write_v260_inventory_files(repository_root: Path) -> None:
    for relative_path in EXPECTED_V260_MANDATORY_FILES:
        path = repository_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture bytes for {relative_path}\n", encoding="utf-8")


def _store_full_history_and_receipt(
    archive_root: Path,
    receipt_path: Path,
) -> tuple[dict, dict]:
    archive = OpenFootballRawArchive(archive_root)
    for index, source_id in enumerate(OPENFOOTBALL_HISTORY_SOURCE_IDS, start=1):
        config = OPENFOOTBALL_HISTORY_SOURCES[source_id]
        year = int(config["season"].split("-", maxsplit=1)[0])
        payload = (
            "= OpenFootball lock fixture\n"
            "▪ Matchday 1\n"
            f"Sat Aug 8 {year}\n"
            f"  12:45  Lock Home {index}  1-0 (1-0)  Lock Away {index}\n"
        ).encode()
        archive.store(
            OpenFootballRawObservation(
                source_id=source_id,
                url=config["url"],
                retrieved_at=V260_OBSERVED_BEFORE,
                payload=payload,
                source_format=config["format"],
                competition_id=config["competition_id"],
                season=config["season"],
                timezone_name=config["timezone"],
                license="CC0-1.0",
            )
        )
    raw_candidate = load_verified_openfootball_archive(
        archive_root,
        source_ids=OPENFOOTBALL_HISTORY_SOURCE_IDS,
        observed_before=V260_OBSERVED_BEFORE,
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "verified_raw_history",
        "observed_at": V260_OBSERVED_BEFORE.isoformat(),
        "source_count": 66,
        "source_ids": sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS),
        "parsed_fixture_count": 66,
        "finished_fixture_count": 66,
        "unfinished_fixture_count": 0,
        "admission_sha256": raw_candidate["admission_sha256"],
        "rows_sha256": raw_candidate["rows_sha256"],
        "source_manifest_sha256": raw_candidate["source_manifest_sha256"],
        "parser_contract_sha256": raw_candidate["parser_contract_sha256"],
        "errors": [],
    }
    payload = _canonical_bytes(receipt) + b"\n"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    immutable = (
        archive_root
        / RECEIPT_ARCHIVE_NAME
        / "sha256"
        / digest[:2]
        / f"{digest}.json"
    )
    immutable.parent.mkdir(parents=True, exist_ok=True)
    immutable.write_bytes(payload)
    return raw_candidate, receipt


def test_build_v260_lock_replays_all_raw_history_and_forces_full_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    original = _v259_lock(repository_root)
    original_before = json.loads(json.dumps(original))
    _write_v260_inventory_files(repository_root)
    archive_root = tmp_path / "openfootball-raw"
    receipt_path = tmp_path / "runtime" / "openfootball-history-current.json"
    raw_candidate, receipt = _store_full_history_and_receipt(
        archive_root,
        receipt_path,
    )
    expected_admission = VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=V260_OBSERVED_BEFORE,
        source_ids=OPENFOOTBALL_HISTORY_SOURCE_IDS,
    ).admission_manifest()
    archive_before = {
        path.relative_to(archive_root): path.read_bytes()
        for path in archive_root.rglob("*")
        if path.is_file()
    }
    receipt_before = receipt_path.read_bytes()
    monkeypatch.setattr(
        lock_module,
        "_require_durable_path",
        lambda value, **_kwargs: Path(value),
        raising=False,
    )

    built = lock_module.build_v260_lock(
        original,
        repository_root=repository_root,
        raw_archive_dir=archive_root,
        history_receipt_path=receipt_path,
        locked_at=datetime(2026, 8, 25, 3, tzinfo=timezone.utc),
        migration="v260-verified-openfootball-history",
        reason="bind the verified OpenFootball raw-history training admission",
    )

    assert built is not original
    assert original == original_before
    assert receipt_path.read_bytes() == receipt_before
    assert {
        path.relative_to(archive_root): path.read_bytes()
        for path in archive_root.rglob("*")
        if path.is_file()
    } == archive_before
    assert built["schema_version"] == "1.0.0"
    assert built["status"] == "pending_prospective_window"
    assert built["policy_version"] == "v260"
    assert built["observed_before"] == V260_OBSERVED_BEFORE.isoformat()
    assert built["source_ids"] == sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    assert built["locked_at"] == "2026-08-25T03:00:00+00:00"
    assert built["evaluation_window_started_at"] == "2026-08-25T03:00:01+00:00"
    assert built["training_admission"] == {
        "schema_version": "matchline.prospective_training_admission.v1",
        "status": "training_admitted",
        "policy_version": "v260",
        "observed_before": V260_OBSERVED_BEFORE.isoformat(),
        "source_ids": sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS),
        "training_admission_sha256": expected_admission["admission_sha256"],
        "raw_admission_sha256": raw_candidate["admission_sha256"],
        "source_manifest_sha256": raw_candidate["source_manifest_sha256"],
        "parser_contract_sha256": raw_candidate["parser_contract_sha256"],
        "raw_rows_sha256": raw_candidate["rows_sha256"],
        "domain_rows_sha256": expected_admission["domain_rows_sha256"],
    }
    assert built["training_admission"]["raw_admission_sha256"] == receipt[
        "admission_sha256"
    ]
    paths = [entry["path"] for entry in built["model_files"]]
    assert paths == ["legacy_model.py", *EXPECTED_V260_MANDATORY_FILES]
    assert len(paths) == len(set(paths))
    for entry in built["model_files"]:
        assert entry["sha256"] == hashlib.sha256(
            (repository_root / entry["path"]).read_bytes()
        ).hexdigest()
    expected_model_version = hashlib.sha256(
        _canonical_bytes(
            {
                "model_name": built["model_name"],
                "freeze_model_name": built["freeze_model_name"],
                "model_files": built["model_files"],
            }
        )
    ).hexdigest()
    assert built["model_version_sha256"] == expected_model_version


def test_build_v260_lock_rejects_volatile_evidence_before_any_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    original = _v259_lock(repository_root)
    calls = 0

    def forbidden_replay(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("volatile evidence must be rejected before replay")

    monkeypatch.setattr(
        lock_module,
        "load_verified_openfootball_archive",
        forbidden_replay,
    )
    monkeypatch.setattr(
        lock_module,
        "VerifiedOpenFootballHistorySource",
        forbidden_replay,
    )

    with pytest.raises(ValueError, match="durable"):
        lock_module.build_v260_lock(
            original,
            repository_root=repository_root,
            raw_archive_dir=tmp_path / "openfootball-raw",
            history_receipt_path=tmp_path / "history-current.json",
            locked_at=datetime(2026, 8, 25, 3, tzinfo=timezone.utc),
            migration="v260-verified-openfootball-history",
            reason="bind the verified OpenFootball raw-history training admission",
        )

    assert calls == 0
    assert "policy_version" not in original


@pytest.mark.parametrize("expected", ["directory", "file"])
def test_durable_path_gate_rejects_symlinked_paths_and_components(
    tmp_path: Path,
    expected: str,
) -> None:
    workspace = Path(__file__).resolve().parents[1]
    link = tmp_path / "durable-link"
    link.symlink_to(
        workspace if expected == "directory" else workspace / "pyproject.toml",
        target_is_directory=expected == "directory",
    )

    with pytest.raises(ValueError, match="symlink"):
        lock_module._require_durable_path(
            link,
            field="candidate_path",
            expected=expected,
            must_exist=True,
        )

    directory_link = tmp_path / "workspace-link"
    directory_link.symlink_to(workspace, target_is_directory=True)
    nested = (
        directory_link / "league_platform"
        if expected == "directory"
        else directory_link / "pyproject.toml"
    )
    with pytest.raises(ValueError, match="symlink"):
        lock_module._require_durable_path(
            nested,
            field="candidate_path",
            expected=expected,
            must_exist=True,
        )


def test_v260_mandatory_inventory_rejects_a_symlinked_code_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    original = _v259_lock(repository_root)
    _write_v260_inventory_files(repository_root)
    symlinked = repository_root / "league_platform/source_rights.py"
    symlinked.unlink()
    symlinked.symlink_to(repository_root / "legacy_model.py")
    archive_root = tmp_path / "openfootball-raw"
    receipt_path = tmp_path / "runtime/history-current.json"
    _store_full_history_and_receipt(archive_root, receipt_path)
    monkeypatch.setattr(
        lock_module,
        "_require_durable_path",
        lambda value, **_kwargs: Path(value),
    )

    with pytest.raises(ValueError, match="symlink"):
        lock_module.build_v260_lock(
            original,
            repository_root=repository_root,
            raw_archive_dir=archive_root,
            history_receipt_path=receipt_path,
            locked_at=datetime(2026, 8, 25, 3, tzinfo=timezone.utc),
            migration="v260-verified-openfootball-history",
            reason="bind the verified OpenFootball raw-history training admission",
        )


def test_v260_builder_rejects_symlinked_immutable_history_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    original = _v259_lock(repository_root)
    _write_v260_inventory_files(repository_root)
    archive_root = tmp_path / "openfootball-raw"
    receipt_path = tmp_path / "runtime/history-current.json"
    _store_full_history_and_receipt(archive_root, receipt_path)
    payload = receipt_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    immutable = (
        archive_root
        / RECEIPT_ARCHIVE_NAME
        / "sha256"
        / digest[:2]
        / f"{digest}.json"
    )
    external = tmp_path / "outside-receipt.json"
    external.write_bytes(payload)
    immutable.unlink()
    immutable.symlink_to(external)
    monkeypatch.setattr(
        lock_module,
        "_require_durable_path",
        lambda value, **_kwargs: Path(value),
    )

    with pytest.raises(ValueError, match="symlink"):
        lock_module.build_v260_lock(
            original,
            repository_root=repository_root,
            raw_archive_dir=archive_root,
            history_receipt_path=receipt_path,
            locked_at=datetime(2026, 8, 25, 3, tzinfo=timezone.utc),
            migration="v260-verified-openfootball-history",
            reason="bind the verified OpenFootball raw-history training admission",
        )


def test_candidate_writer_rejects_existing_output_before_loading_any_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_lock = tmp_path / "prospective-model-lock-v259.json"
    input_bytes = b'{"status":"pending_prospective_window"}\n'
    input_lock.write_bytes(input_bytes)
    output_lock = tmp_path / "prospective-model-lock-v260.json"
    output_bytes = b'{"status":"do-not-overwrite"}\n'
    output_lock.write_bytes(output_bytes)
    calls = 0

    def forbidden_build(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("existing output must block before evidence replay")

    monkeypatch.setattr(lock_module, "build_v260_lock", forbidden_build)
    monkeypatch.setattr(
        lock_module,
        "_require_durable_path",
        lambda value, **_kwargs: Path(value),
    )

    with pytest.raises(FileExistsError, match="output"):
        lock_module.create_v260_lock_candidate(
            input_lock_path=input_lock,
            output_lock_path=output_lock,
            repository_root=tmp_path,
            raw_archive_dir=tmp_path / "raw",
            history_receipt_path=tmp_path / "history-current.json",
            locked_at=datetime(2026, 8, 25, 3, tzinfo=timezone.utc),
            migration="v260-verified-openfootball-history",
            reason="bind the verified OpenFootball raw-history training admission",
        )

    assert calls == 0
    assert input_lock.read_bytes() == input_bytes
    assert output_lock.read_bytes() == output_bytes


def test_v260_cli_requires_distinct_paths_and_blocks_volatile_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def forbidden_create(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("CLI path validation must precede candidate creation")

    monkeypatch.setattr(lock_module, "create_v260_lock_candidate", forbidden_create)
    input_lock = tmp_path / "prospective-model-lock-v259.json"
    input_lock.write_text('{"status":"pending_prospective_window"}\n')
    common = [
        "--input-lock",
        str(input_lock),
        "--repository-root",
        str(Path(__file__).resolve().parents[1]),
        "--openfootball-raw-archive",
        str(tmp_path / "raw"),
        "--history-receipt",
        str(tmp_path / "history-current.json"),
        "--locked-at",
        "2026-08-25T03:00:00+00:00",
        "--migration",
        "v260-verified-openfootball-history",
        "--reason",
        "bind the verified OpenFootball raw-history training admission",
    ]

    with pytest.raises(SystemExit) as missing_output:
        lock_module.main(common)
    assert missing_output.value.code == 2

    exit_code = lock_module.main(
        [*common, "--output-lock", str(tmp_path / "prospective-model-lock-v260.json")]
    )

    assert exit_code == 2
    assert calls == 0
    blocked = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert blocked["status"] == "blocked"
    assert blocked["stage"] == "path_validation"
    assert not (tmp_path / "prospective-model-lock-v260.json").exists()


def _lock(tmp_path: Path) -> dict:
    model_file = tmp_path / "model.py"
    model_file.write_text("locked model\n", encoding="utf-8")
    return {
        "status": "pending_prospective_window",
        "locked_at": "2026-08-18T00:00:00+00:00",
        "evaluation_window_started_at": "2026-08-18T00:00:01+00:00",
        "model_name": "model-v1",
        "freeze_model_name": "freeze-v1",
        "model_version_sha256": "a" * 64,
        "model_files": [{"path": str(model_file), "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest()}],
        "evaluation_window": "old window",
        "results_not_used_for_selection": True,
        "sample_requirements_met": False,
        "all_required_targets_scored": False,
        "prediction_freezes_verified": False,
        "notes": ["old note"],
    }


def test_refresh_lock_updates_inventory_and_starts_a_new_window(tmp_path: Path):
    original = _lock(tmp_path)
    refreshed = refresh_lock(
        original,
        repository_root=tmp_path,
        locked_at=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        migration="v65-player-ledger",
        reason="append-only player evidence binding changed the locked ingestion inventory",
    )

    assert original["model_version_sha256"] == "a" * 64
    assert refreshed["locked_at"] == "2026-08-18T01:00:00+00:00"
    assert refreshed["evaluation_window_started_at"] == "2026-08-18T01:00:01+00:00"
    assert refreshed["model_version_sha256"] != original["model_version_sha256"]
    assert refreshed["model_files"][0]["sha256"] == hashlib.sha256((tmp_path / "model.py").read_bytes()).hexdigest()
    assert refreshed["sample_requirements_met"] is False
    assert "v65-player-ledger" in refreshed["evaluation_window"]
    assert refreshed["notes"][-1].startswith("v65-player-ledger explicit lock migration")


def test_refresh_lock_can_add_explicit_model_dependencies_without_duplicates(tmp_path: Path):
    original = _lock(tmp_path)
    dependency = tmp_path / "identity.py"
    dependency.write_text("identity dependency\n", encoding="utf-8")

    refreshed = refresh_lock(
        original,
        repository_root=tmp_path,
        locked_at=datetime(2026, 8, 18, 1, 1, tzinfo=timezone.utc),
        migration="v96-understat-identity",
        reason="explicit provider aliases changed causal feature attachment",
        additional_model_files=(str(dependency), str(dependency)),
    )

    paths = [entry["path"] for entry in refreshed["model_files"]]
    assert paths.count(str(dependency)) == 1
    assert refreshed["model_files"][-1]["sha256"] == hashlib.sha256(dependency.read_bytes()).hexdigest()


def test_refresh_lock_fails_closed_for_passed_or_non_monotonic_locks(tmp_path: Path):
    lock = _lock(tmp_path)
    with pytest.raises(ValueError, match="pending"):
        refresh_lock({**lock, "status": "passed"}, repository_root=tmp_path, locked_at=datetime(2026, 8, 18, 1, tzinfo=timezone.utc), migration="v65", reason="reason")
    with pytest.raises(ValueError, match="later"):
        refresh_lock(lock, repository_root=tmp_path, locked_at=datetime(2026, 8, 18, tzinfo=timezone.utc), migration="v65", reason="reason")


def test_refresh_lock_rejects_missing_inventory_files(tmp_path: Path):
    lock = _lock(tmp_path)
    lock["model_files"] = [{"path": "missing.py", "sha256": "a" * 64}]
    with pytest.raises(ValueError, match="missing"):
        refresh_lock(lock, repository_root=tmp_path, locked_at=datetime(2026, 8, 18, 1, tzinfo=timezone.utc), migration="v65", reason="reason")
