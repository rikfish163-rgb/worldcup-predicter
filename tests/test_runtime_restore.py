from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from league_platform.runtime_archive import pack_runtime
from league_platform.runtime_checkpoint import create_checkpoint
from league_platform.runtime_restore import restore_runtime


def _packed_runtime(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "source-runtime"
    archive = tmp_path / "archive"
    (runtime / "intelligence").mkdir(parents=True)
    (runtime / "current.json").write_text(
        '{"as_of":"2026-08-22T00:00:00+00:00"}\n', encoding="utf-8"
    )
    (runtime / "strict-backtest-current.json").write_text(
        '{"status":"research_only"}\n', encoding="utf-8"
    )
    (runtime / "intelligence" / "observations.jsonl").write_text(
        '{"kind":"news"}\n', encoding="utf-8"
    )
    packed = pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    assert packed["status"] == "archived"
    return archive / "runtime-archive-manifest.json", runtime


def test_restore_runtime_verifies_stages_and_installs_exact_file_set(tmp_path):
    manifest, source = _packed_runtime(tmp_path)
    destination = tmp_path / "restored-runtime"

    result = restore_runtime(manifest, destination, min_free_bytes=0)

    assert result["status"] == "restored"
    assert result["archive_status"] == "verified"
    assert result["restored_file_count"] == 3
    assert result["restored_bytes"] > 0
    assert json.loads((destination / "current.json").read_text(encoding="utf-8"))[
        "as_of"
    ] == "2026-08-22T00:00:00+00:00"
    assert (destination / "intelligence" / "observations.jsonl").read_bytes() == (
        source / "intelligence" / "observations.jsonl"
    ).read_bytes()
    assert (destination / "runtime-restore-receipt.json").is_file()
    assert not list(tmp_path.glob(".restored-runtime.restore-*"))


def test_restore_runtime_rejects_hash_mismatch_without_touching_destination(tmp_path):
    manifest, _source = _packed_runtime(tmp_path)
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    archive = manifest.parent / metadata["archive_file"]
    archive.write_bytes(archive.read_bytes() + b"corrupt")
    destination = tmp_path / "restored-runtime"

    result = restore_runtime(manifest, destination, min_free_bytes=0)

    assert result["status"] == "blocked_archive_verification"
    assert result["archive_status"] == "hash_mismatch"
    assert not destination.exists()


def test_restore_runtime_fails_closed_on_nonempty_incomplete_target(tmp_path):
    manifest, _source = _packed_runtime(tmp_path)
    destination = tmp_path / "restored-runtime"
    destination.mkdir()
    (destination / "newer-diagnostic.json").write_text('{"keep":true}\n', encoding="utf-8")

    result = restore_runtime(manifest, destination, min_free_bytes=0)

    assert result["status"] == "blocked_destination_not_empty"
    assert (destination / "newer-diagnostic.json").is_file()
    assert not (destination / "current.json").exists()


def test_restore_runtime_quarantines_incomplete_target_without_data_loss(tmp_path):
    manifest, _source = _packed_runtime(tmp_path)
    destination = tmp_path / "restored-runtime"
    destination.mkdir()
    (destination / "newer-diagnostic.json").write_text('{"keep":true}\n', encoding="utf-8")

    result = restore_runtime(
        manifest,
        destination,
        min_free_bytes=0,
        quarantine_incomplete_target=True,
    )

    assert result["status"] == "restored"
    quarantine = Path(result["quarantined_incomplete_target"])
    assert quarantine.is_relative_to(destination / "recovery" / "boot-partials")
    assert (quarantine / "newer-diagnostic.json").is_file()
    assert (destination / "current.json").is_file()


def test_restore_runtime_applies_newer_same_lane_critical_checkpoint_in_staging(tmp_path):
    runtime = tmp_path / "source-runtime"
    archive = tmp_path / "archive"
    checkpoint = tmp_path / "checkpoint"
    runtime.mkdir()
    (runtime / "current.json").write_text('{"as_of":"2026-08-24T18:00:00Z"}\n', encoding="utf-8")
    ledger = runtime / "prospective_predictions.jsonl"
    ledger.write_text('{"record":1}\n', encoding="utf-8")
    packed = pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    ledger.write_text('{"record":1}\n{"record":2}\n', encoding="utf-8")
    created = create_checkpoint(
        runtime,
        checkpoint,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )

    destination = tmp_path / "restored-runtime"
    result = restore_runtime(
        archive / "runtime-archive-manifest.json",
        destination,
        min_free_bytes=0,
        checkpoint_root=checkpoint,
    )

    assert packed["status"] == "archived"
    assert result["status"] == "restored"
    assert result["checkpoint_status"] == "applied"
    assert result["checkpoint_state_sha256"] == created["state_sha256"]
    assert ledger.read_bytes() == (destination / "prospective_predictions.jsonl").read_bytes()


def test_restore_runtime_skips_checkpoint_that_is_not_newer_than_full_archive(tmp_path):
    runtime = tmp_path / "source-runtime"
    archive = tmp_path / "archive"
    checkpoint = tmp_path / "checkpoint"
    runtime.mkdir()
    (runtime / "current.json").write_text('{"as_of":"2026-08-24T18:00:00Z"}\n', encoding="utf-8")
    ledger = runtime / "prospective_predictions.jsonl"
    ledger.write_text('{"record":1}\n', encoding="utf-8")
    pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    ledger.write_text('{"record":1}\n{"record":2}\n', encoding="utf-8")
    create_checkpoint(runtime, checkpoint, now=datetime(2020, 1, 1, tzinfo=UTC))

    destination = tmp_path / "restored-runtime"
    result = restore_runtime(
        archive / "runtime-archive-manifest.json",
        destination,
        min_free_bytes=0,
        checkpoint_root=checkpoint,
    )

    assert result["status"] == "restored"
    assert result["checkpoint_status"] == "skipped_not_newer"
    assert (destination / "prospective_predictions.jsonl").read_text(encoding="utf-8") == '{"record":1}\n'


def test_restore_runtime_rejects_newer_checkpoint_from_a_different_runtime_lane(tmp_path):
    runtime = tmp_path / "source-runtime"
    other_runtime = tmp_path / "other-runtime"
    archive = tmp_path / "archive"
    checkpoint = tmp_path / "checkpoint"
    for path in (runtime, other_runtime):
        path.mkdir()
        (path / "current.json").write_text('{"as_of":"2026-08-24T18:00:00Z"}\n', encoding="utf-8")
        (path / "prospective_predictions.jsonl").write_text('{"record":1}\n', encoding="utf-8")
    pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    create_checkpoint(other_runtime, checkpoint, now=datetime(2030, 1, 1, tzinfo=UTC))

    destination = tmp_path / "restored-runtime"
    result = restore_runtime(
        archive / "runtime-archive-manifest.json",
        destination,
        min_free_bytes=0,
        checkpoint_root=checkpoint,
    )

    assert result["status"] == "blocked_archive_content"
    assert "different runtime lane" in result["reason"]
    assert not destination.exists()


def test_restore_runtime_rejects_checkpoint_that_rewrites_append_only_prefix(tmp_path):
    runtime = tmp_path / "source-runtime"
    archive = tmp_path / "archive"
    checkpoint = tmp_path / "checkpoint"
    runtime.mkdir()
    (runtime / "current.json").write_text('{"as_of":"2026-08-24T18:00:00Z"}\n', encoding="utf-8")
    ledger = runtime / "prospective_predictions.jsonl"
    ledger.write_text('{"record":1}\n', encoding="utf-8")
    pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    ledger.write_text('{"record":9}\n{"record":2}\n', encoding="utf-8")
    create_checkpoint(runtime, checkpoint, now=datetime(2030, 1, 1, tzinfo=UTC))

    destination = tmp_path / "restored-runtime"
    result = restore_runtime(
        archive / "runtime-archive-manifest.json",
        destination,
        min_free_bytes=0,
        checkpoint_root=checkpoint,
    )

    assert result["status"] == "blocked_archive_content"
    assert "rewrite or shrink append-only ledger" in result["reason"]
    assert not destination.exists()


def test_restore_runtime_rejects_unsafe_archive_members(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archive = archive_dir / "runtime-snapshot-unsafe.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tar:
        payload = b"escape"
        member = tarfile.TarInfo("../escape.txt")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    manifest = archive_dir / "runtime-archive-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "matchline.runtime_archive.v1",
                "status": "archived",
                "archive_file": archive.name,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "compression": "gzip",
                "source": {
                    "file_count": 1,
                    "bytes": 6,
                    "state_sha256": "unsafe",
                    "files": [{"path": "escape.txt", "size": 6, "mtime_ns": 0}],
                },
            }
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "restored-runtime"

    result = restore_runtime(manifest, destination, min_free_bytes=0)

    assert result["status"] == "blocked_archive_verification"
    assert result["archive_status"] == "unsafe_members"
    assert not (tmp_path / "escape.txt").exists()


def test_restore_runtime_rejects_safe_path_symlink_even_after_hash_verification(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archive = archive_dir / "runtime-snapshot-link.tar.gz"
    current = b'{"as_of":"2026-08-22T00:00:00+00:00"}\n'
    with tarfile.open(archive, mode="w:gz") as tar:
        member = tarfile.TarInfo("current.json")
        member.size = len(current)
        tar.addfile(member, io.BytesIO(current))
        link = tarfile.TarInfo("latest.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    manifest = archive_dir / "runtime-archive-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "matchline.runtime_archive.v1",
                "status": "archived",
                "archive_file": archive.name,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "compression": "gzip",
                "source": {
                    "file_count": 1,
                    "bytes": len(current),
                    "state_sha256": "link-test",
                    "files": [
                        {"path": "current.json", "size": len(current), "mtime_ns": 0}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "restored-runtime"

    result = restore_runtime(manifest, destination, min_free_bytes=0)

    assert result["status"] == "blocked_archive_content"
    assert result["error"] == "UnsafeArchiveError"
    assert not destination.exists()
