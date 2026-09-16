from __future__ import annotations

import hashlib
import io
import json
import fcntl
import shutil
import tarfile

import pytest

from league_platform.runtime_archive import SourceChangedError, main, pack_runtime, verify_archive


def test_pack_runtime_is_atomic_verifiable_and_idempotent(tmp_path):
    runtime = tmp_path / "runtime"
    archive = tmp_path / "archive"
    (runtime / "intelligence").mkdir(parents=True)
    (runtime / "archive" / "raw").mkdir(parents=True)
    (runtime / "current.json").write_text('{"as_of":"2026-08-22T00:00:00Z"}\n', encoding="utf-8")
    (runtime / "intelligence" / "observations.jsonl").write_text('{"kind":"news"}\n', encoding="utf-8")
    (runtime / "archive" / "raw" / "row.json").write_text('{"raw":true}\n', encoding="utf-8")
    (runtime / "ignored.lock").write_text("lock", encoding="utf-8")
    (runtime / ".hidden").write_text("hidden", encoding="utf-8")

    first = pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    assert first["status"] == "archived"
    assert first["source"]["file_count"] == 3
    assert first["archive_bytes"] > 0
    assert (archive / first["archive_file"]).is_file()
    manifest = json.loads((archive / "runtime-archive-manifest.json").read_text(encoding="utf-8"))
    assert manifest["runtime_persistence"] == "durable_packed_archive"

    verified = verify_archive(archive / "runtime-archive-manifest.json")
    assert verified["status"] == "verified"
    assert verified["archive_member_files"] == 3

    second = pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    assert second["status"] == "unchanged"
    assert second["archive_file"] == first["archive_file"]


def test_pack_runtime_fails_closed_before_writing_when_reserve_is_unsafe(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    archive = tmp_path / "archive"
    runtime.mkdir()
    (runtime / "current.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("league_platform.runtime_archive._free_bytes", lambda _path: 1)

    result = pack_runtime(runtime, archive, min_free_bytes=1024, compression="gzip")

    assert result["status"] == "blocked_storage"
    assert not list(archive.glob("runtime-snapshot-*"))


def test_zstd_archive_verification_uses_streaming_decoder(tmp_path):
    if shutil.which("zstd") is None:
        pytest.skip("zstd is not installed")
    runtime = tmp_path / "runtime"
    archive = tmp_path / "archive"
    runtime.mkdir()
    (runtime / "current.json").write_text("{}\n", encoding="utf-8")

    packed = pack_runtime(runtime, archive, min_free_bytes=0, compression="zstd")

    assert packed["status"] == "archived"
    verified = verify_archive(archive / "runtime-archive-manifest.json")
    assert verified["status"] == "verified"
    assert verified["archive_member_files"] == 1


def test_zstd_verification_retries_one_empty_decoder_failure(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archive = archive_dir / "runtime-snapshot-test.tar.zst"
    archive.write_bytes(b"test-zstd-envelope")

    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        payload = b'{}\n'
        member = tarfile.TarInfo("./current.json")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    tar_bytes = tar_stream.getvalue()

    manifest = archive_dir / "runtime-archive-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "matchline.runtime_archive.v1",
        "status": "archived",
        "archive_file": archive.name,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "compression": "zstd",
        "source": {"state_sha256": "a" * 64},
    }), encoding="utf-8")

    return_codes = iter((1, 0))
    calls = 0

    class FakeDecoder:
        def __init__(self):
            self.stdout = io.BytesIO(tar_bytes)
            self.stderr = io.BytesIO()
            self.return_code = next(return_codes)

        def wait(self):
            return self.return_code

        def poll(self):
            return self.return_code

        def kill(self):
            self.return_code = -9

    def fake_popen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeDecoder()

    monkeypatch.setattr("league_platform.runtime_archive.subprocess.Popen", fake_popen)

    verified = verify_archive(manifest)

    assert calls == 2
    assert verified["status"] == "verified"
    assert verified["archive_member_files"] == 1


def test_rotation_records_free_space_after_pruning_previous_snapshot(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    archive = tmp_path / "archive"
    runtime.mkdir()
    current = runtime / "current.json"
    current.write_text('{"version":1}\n', encoding="utf-8")
    first = pack_runtime(runtime, archive, min_free_bytes=0, keep=1, compression="gzip")
    assert first["status"] == "archived"

    current.write_text('{"version":2}\n', encoding="utf-8")
    free_space_samples = iter((128 * 1024 * 1024, 112 * 1024 * 1024, 120 * 1024 * 1024))
    monkeypatch.setattr(
        "league_platform.runtime_archive._free_bytes",
        lambda _path: next(free_space_samples),
    )
    second = pack_runtime(runtime, archive, min_free_bytes=0, keep=1, compression="gzip")

    assert second["status"] == "archived"
    assert second["free_bytes_before"] == 128 * 1024 * 1024
    assert second["free_bytes_before_prune"] == 112 * 1024 * 1024
    assert second["free_bytes_after"] == 120 * 1024 * 1024
    assert second["pruned_archives"] == [first["archive_file"]]


def test_failed_rotation_keeps_last_good_manifest_and_writes_failure_receipt(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    archive = tmp_path / "archive"
    runtime.mkdir()
    (runtime / "current.json").write_text('{"version":1}\n', encoding="utf-8")
    first = pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    assert first["status"] == "archived"

    (runtime / "current.json").write_text('{"version":2}\n', encoding="utf-8")
    monkeypatch.setattr(
        "league_platform.runtime_archive._estimate",
        lambda *_args: (_ for _ in ()).throw(SourceChangedError("changed during estimate")),
    )

    result = pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")

    assert result["status"] == "source_changed"
    manifest = json.loads((archive / "runtime-archive-manifest.json").read_text(encoding="utf-8"))
    assert manifest["archive_file"] == first["archive_file"]
    assert verify_archive(archive / "runtime-archive-manifest.json")["status"] == "verified"
    failure = json.loads((archive / "runtime-archive-last-failure.json").read_text(encoding="utf-8"))
    assert failure["status"] == "source_changed"


def test_pack_runtime_skips_without_reading_when_runtime_mutation_lock_is_busy(tmp_path):
    runtime = tmp_path / "runtime"
    archive = tmp_path / "archive"
    runtime.mkdir()
    (runtime / "current.json").write_text('{}\n', encoding="utf-8")
    lock_path = runtime / "prospective-cycle.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")

    assert result["status"] == "source_busy"
    assert result["reason"] == "runtime_mutation_lock_is_held"
    assert not list(archive.glob("runtime-snapshot-*"))


def test_verify_cli_writes_bounded_receipt_bound_to_exact_archive(tmp_path, capsys):
    runtime = tmp_path / "runtime"
    archive = tmp_path / "archive"
    runtime.mkdir()
    (runtime / "current.json").write_text(
        '{"as_of":"2026-08-23T00:00:00+00:00"}\n', encoding="utf-8"
    )
    packed = pack_runtime(runtime, archive, min_free_bytes=0, compression="gzip")
    receipt = archive / "runtime-archive-verification.json"

    exit_code = main([
        "--verify",
        "--manifest",
        str(archive / "runtime-archive-manifest.json"),
        "--verification-receipt",
        str(receipt),
    ])

    assert exit_code == 0
    verified = json.loads(receipt.read_text(encoding="utf-8"))
    assert verified == {
        "schema_version": "matchline.runtime_archive_verification.v1",
        "status": "verified",
        "verified_at": verified["verified_at"],
        "manifest_file": "runtime-archive-manifest.json",
        "archive_file": packed["archive_file"],
        "archive_sha256": packed["archive_sha256"],
        "source_state_sha256": packed["source"]["state_sha256"],
        "archive_member_files": 1,
    }
    printed = json.loads(capsys.readouterr().out)
    assert "files" not in printed["source"]
