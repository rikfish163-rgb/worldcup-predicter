from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _checkpoint_api():
    try:
        from league_platform.runtime_checkpoint import (
            create_checkpoint,
            restore_checkpoint,
            verify_checkpoint,
        )
    except ModuleNotFoundError:
        pytest.fail("runtime ledger checkpoint implementation is missing")
    except ImportError:
        pytest.fail("runtime ledger checkpoint verification or restore is missing")
    return create_checkpoint, restore_checkpoint, verify_checkpoint


def test_checkpoint_persists_small_critical_ledgers_without_copying_intelligence_history(
    tmp_path: Path,
):
    runtime = tmp_path / "arbitrary-tmpfs-window"
    durable = tmp_path / "durable"
    (runtime / "intelligence").mkdir(parents=True)
    (runtime / "archive" / "raw").mkdir(parents=True)
    (runtime / "prospective_predictions.jsonl").write_text('{"record":1}\n', encoding="utf-8")
    (runtime / "runtime-only-evaluation-current.json").write_text(
        '{"status":"pending_prospective_window"}\n',
        encoding="utf-8",
    )
    (runtime / "intelligence" / "observations.jsonl").write_text(
        '{"fact":1}\n',
        encoding="utf-8",
    )
    (runtime / "archive" / "raw" / "large-source.json").write_text(
        '{"not":"part of the ledger checkpoint"}\n',
        encoding="utf-8",
    )

    create_checkpoint, _, _ = _checkpoint_api()
    receipt = create_checkpoint(runtime, durable)

    checkpoint = Path(receipt["checkpoint_dir"])
    assert receipt["status"] == "checkpointed"
    assert receipt["runtime_dir"] == str(runtime.resolve())
    assert receipt["recovery_scope"] == "critical_ledgers_only"
    assert receipt["file_count"] == 2
    assert {row["path"] for row in receipt["files"]} == {
        "prospective_predictions.jsonl",
        "runtime-only-evaluation-current.json",
    }
    assert (checkpoint / "prospective_predictions.jsonl").read_text(encoding="utf-8") == '{"record":1}\n'
    assert not (checkpoint / "intelligence" / "observations.jsonl").exists()
    assert not (checkpoint / "archive" / "raw" / "large-source.json").exists()
    manifest = json.loads((durable / "runtime-checkpoint-manifest.json").read_text(encoding="utf-8"))
    assert manifest["checkpoint_dir"] == checkpoint.name
    assert manifest["state_sha256"] == receipt["state_sha256"]


def test_verify_checkpoint_rejects_a_tampered_ledger(tmp_path: Path):
    runtime = tmp_path / "runtime"
    durable = tmp_path / "durable"
    runtime.mkdir()
    (runtime / "prospective_predictions.jsonl").write_text('{"record":1}\n', encoding="utf-8")

    create_checkpoint, _, verify_checkpoint = _checkpoint_api()
    receipt = create_checkpoint(runtime, durable)
    checkpoint = Path(receipt["checkpoint_dir"])
    (checkpoint / "prospective_predictions.jsonl").write_text('{"record":2}\n', encoding="utf-8")

    with pytest.raises(OSError, match="hash mismatch"):
        verify_checkpoint(durable)


def test_restore_checkpoint_verifies_then_restores_only_manifest_files(tmp_path: Path):
    runtime = tmp_path / "runtime"
    durable = tmp_path / "durable"
    restored = tmp_path / "restored-runtime"
    (runtime / "intelligence").mkdir(parents=True)
    (runtime / "prospective_predictions.jsonl").write_text('{"record":1}\n', encoding="utf-8")
    (runtime / "intelligence" / "observations.jsonl").write_text('{"fact":1}\n', encoding="utf-8")

    create_checkpoint, restore_checkpoint, _ = _checkpoint_api()
    receipt = create_checkpoint(runtime, durable)
    checkpoint = Path(receipt["checkpoint_dir"])
    (checkpoint / "unlisted-secret.txt").write_text("must not restore\n", encoding="utf-8")

    result = restore_checkpoint(durable, restored)

    assert result["status"] == "restored"
    assert result["file_count"] == 1
    assert (restored / "prospective_predictions.jsonl").read_text(encoding="utf-8") == '{"record":1}\n'
    assert not (restored / "intelligence" / "observations.jsonl").exists()
    assert not (restored / "unlisted-secret.txt").exists()


def test_checkpoint_cli_creates_and_verifies_a_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    from league_platform.runtime_checkpoint import main

    runtime = tmp_path / "runtime"
    durable = tmp_path / "durable"
    runtime.mkdir()
    (runtime / "prospective_predictions.jsonl").write_text('{"record":1}\n', encoding="utf-8")

    assert main(["create", "--runtime-dir", str(runtime), "--checkpoint-root", str(durable)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "checkpointed"

    assert main(["verify", "--checkpoint-root", str(durable)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "verified"
    assert verified["state_sha256"] == created["state_sha256"]


def test_checkpoint_retains_only_four_complete_generations(tmp_path: Path):
    runtime = tmp_path / "runtime"
    durable = tmp_path / "durable"
    runtime.mkdir()
    created: list[dict] = []
    started_at = datetime(2026, 8, 25, tzinfo=UTC)

    create_checkpoint, _, verify_checkpoint = _checkpoint_api()
    for index in range(6):
        (runtime / "prospective_predictions.jsonl").write_text(
            json.dumps({"record": index}) + "\n",
            encoding="utf-8",
        )
        created.append(
            create_checkpoint(
                runtime,
                durable,
                now=started_at + timedelta(minutes=index),
            )
        )

    checkpoint_dirs = {
        path.name for path in durable.iterdir() if path.name.startswith("checkpoint-")
    }
    assert checkpoint_dirs == {
        Path(receipt["checkpoint_dir"]).name for receipt in created[-4:]
    }
    verified = verify_checkpoint(durable)
    assert Path(verified["checkpoint_dir"]).name == Path(created[-1]["checkpoint_dir"]).name


def test_checkpoint_retention_never_follows_symlinks_or_deletes_unmanaged_paths(
    tmp_path: Path,
):
    runtime = tmp_path / "runtime"
    durable = tmp_path / "durable"
    outside = tmp_path / "outside"
    runtime.mkdir()
    durable.mkdir()
    outside.mkdir()
    marker = outside / "must-survive.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    symlink = durable / f"checkpoint-{'f' * 20}"
    symlink.symlink_to(outside, target_is_directory=True)
    unmanaged = durable / "checkpoint-user-not-content-addressed"
    unmanaged.mkdir()

    create_checkpoint, _, _ = _checkpoint_api()
    for index in range(6):
        (runtime / "prospective_predictions.jsonl").write_text(
            json.dumps({"record": index}) + "\n",
            encoding="utf-8",
        )
        create_checkpoint(runtime, durable, retain=2)

    assert symlink.is_symlink()
    assert unmanaged.is_dir()
    assert marker.read_text(encoding="utf-8") == "preserve\n"
