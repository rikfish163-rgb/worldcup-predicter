from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from league_platform import runtime_evidence
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCES,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
    load_verified_openfootball_archive,
)


OBSERVED_AT = datetime(2026, 8, 25, 2, 30, tzinfo=timezone.utc)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_receipt(raw_root: Path, pointer: Path, receipt: dict) -> None:
    payload = _canonical_bytes(receipt) + b"\n"
    digest = hashlib.sha256(payload).hexdigest()
    immutable = (
        raw_root
        / "history-refresh-receipts"
        / "sha256"
        / digest[:2]
        / f"{digest}.json"
    )
    immutable.parent.mkdir(parents=True, exist_ok=True)
    immutable.write_bytes(payload)
    pointer.write_bytes(payload)


def _durable_history_fixture(root: Path) -> tuple[Path, Path, dict]:
    raw_root = root / "openfootball-raw"
    archive = OpenFootballRawArchive(raw_root)
    for index, source_id in enumerate(OPENFOOTBALL_HISTORY_SOURCE_IDS, start=1):
        config = OPENFOOTBALL_HISTORY_SOURCES[source_id]
        year = int(config["season"].split("-", maxsplit=1)[0])
        payload = (
            "= OpenFootball deterministic history fixture\n"
            "▪ Matchday 1\n"
            f"Sat Aug 8 {year}\n"
            f"  12:45  History Home {index}  1-0 (1-0)  History Away {index}\n"
        ).encode()
        archive.store(
            OpenFootballRawObservation(
                source_id=source_id,
                url=config["url"],
                retrieved_at=OBSERVED_AT,
                payload=payload,
                source_format=config["format"],
                competition_id=config["competition_id"],
                season=config["season"],
                timezone_name=config["timezone"],
                license="CC0-1.0",
            )
        )
    replay = load_verified_openfootball_archive(
        raw_root,
        source_ids=OPENFOOTBALL_HISTORY_SOURCE_IDS,
        observed_before=OBSERVED_AT,
    )
    rows = replay["rows"]
    receipt = {
        "schema_version": "matchline.openfootball_history_refresh.v1",
        "status": "verified_raw_history",
        "observed_at": OBSERVED_AT.isoformat(),
        "source_count": len(OPENFOOTBALL_HISTORY_SOURCE_IDS),
        "source_ids": sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS),
        "parsed_fixture_count": len(rows),
        "finished_fixture_count": sum(row["status"] == "finished" for row in rows),
        "unfinished_fixture_count": sum(row["status"] != "finished" for row in rows),
        "admission_sha256": replay["admission_sha256"],
        "rows_sha256": replay["rows_sha256"],
        "source_manifest_sha256": replay["source_manifest_sha256"],
        "parser_contract_sha256": replay["parser_contract_sha256"],
        "errors": [],
    }
    pointer = root / "openfootball-history-refresh-current.json"
    _write_receipt(raw_root, pointer, receipt)
    return raw_root, pointer, receipt


def _verify_history_input(**kwargs):
    verifier = getattr(runtime_evidence, "verify_openfootball_history_input")
    return verifier(**kwargs)


def _run_strict_report(**kwargs):
    runner = getattr(runtime_evidence, "run_strict_report_from_verified_history")
    return runner(**kwargs)


def test_verified_history_input_replays_raw_bytes_and_drives_formal_report() -> None:
    with tempfile.TemporaryDirectory(prefix="matchline-v260-runtime-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        raw_root, pointer, receipt = _durable_history_fixture(runtime_root)

        verified = _verify_history_input(
            runtime_root=runtime_root,
            raw_archive_dir=raw_root,
            receipt_path=pointer,
            checked_at=OBSERVED_AT + timedelta(hours=1),
        )

        assert verified == {
            "schema_version": "matchline.verified_strict_history_input.v1",
            "status": "verified",
            "raw_archive_dir": str(raw_root.resolve()),
            "receipt_path": str(pointer.resolve()),
            "receipt_raw_sha256": hashlib.sha256(pointer.read_bytes()).hexdigest(),
            "observed_before": OBSERVED_AT.isoformat(),
            "source_ids": sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS),
            "admission_sha256": receipt["admission_sha256"],
            "rows_sha256": receipt["rows_sha256"],
            "source_manifest_sha256": receipt["source_manifest_sha256"],
            "parser_contract_sha256": receipt["parser_contract_sha256"],
        }

        evidence_dir = runtime_root / "candidate-evidence"
        evidence_dir.mkdir()
        output = runtime_root / "strict-backtest-current.json"
        cache = runtime_root / "strict-report-cache.json"
        result = _run_strict_report(
            runtime_root=runtime_root,
            raw_archive_dir=raw_root,
            receipt_path=pointer,
            output_path=output,
            cache_path=cache,
            candidate_evidence_dir=evidence_dir,
            prospective_lock_path=runtime_root / "missing-lock.json",
            checked_at=OBSERVED_AT + timedelta(hours=1),
            python_executable=Path(sys.executable),
        )

        report = json.loads(output.read_text(encoding="utf-8"))
        assert result["status"] == "pass"
        assert result["strict_report_status"] == "generated"
        assert report["schema_version"] == "matchline.strict_report.v260"
        assert report["report_lane"] == "formal_v260"
        assert report["training_source_contract"]["observed_before"] == OBSERVED_AT.isoformat()
        assert report["training_source_contract"]["source_ids"] == sorted(
            OPENFOOTBALL_HISTORY_SOURCE_IDS
        )


def test_tampered_or_expired_history_receipt_is_not_a_trusted_cutoff() -> None:
    with tempfile.TemporaryDirectory(prefix="matchline-v260-runtime-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        raw_root, pointer, receipt = _durable_history_fixture(runtime_root)
        error_type = getattr(runtime_evidence, "VerifiedHistoryInputError")

        with pytest.raises(error_type, match="expired"):
            _verify_history_input(
                runtime_root=runtime_root,
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                checked_at=OBSERVED_AT + timedelta(days=3),
            )

        receipt["admission_sha256"] = "f" * 64
        _write_receipt(raw_root, pointer, receipt)
        with pytest.raises(error_type, match="raw replay"):
            _verify_history_input(
                runtime_root=runtime_root,
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                checked_at=OBSERVED_AT + timedelta(hours=1),
            )


def test_noncanonical_history_pointer_cannot_create_an_ambiguous_receipt_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="matchline-v260-runtime-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        raw_root, pointer, receipt = _durable_history_fixture(runtime_root)
        payload = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        immutable = (
            raw_root
            / "history-refresh-receipts"
            / "sha256"
            / digest[:2]
            / f"{digest}.json"
        )
        immutable.parent.mkdir(parents=True, exist_ok=True)
        immutable.write_bytes(payload)
        pointer.write_bytes(payload)
        error_type = getattr(runtime_evidence, "VerifiedHistoryInputError")

        with pytest.raises(error_type, match="canonical"):
            _verify_history_input(
                runtime_root=runtime_root,
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                checked_at=OBSERVED_AT + timedelta(hours=1),
            )


def test_missing_history_receipt_preserves_previous_strict_pointer() -> None:
    with tempfile.TemporaryDirectory(prefix="matchline-v260-runtime-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        raw_root = runtime_root / "openfootball-raw"
        raw_root.mkdir()
        output = runtime_root / "strict-backtest-current.json"
        previous = b'{"status":"previous-verified"}\n'
        output.write_bytes(previous)
        error_type = getattr(runtime_evidence, "VerifiedHistoryInputError")

        with pytest.raises(error_type, match="receipt"):
            _run_strict_report(
                runtime_root=runtime_root,
                raw_archive_dir=raw_root,
                receipt_path=runtime_root / "openfootball-history-refresh-current.json",
                output_path=output,
                cache_path=runtime_root / "strict-report-cache.json",
                candidate_evidence_dir=runtime_root / "evidence",
                prospective_lock_path=runtime_root / "missing-lock.json",
                checked_at=OBSERVED_AT,
                python_executable=Path(sys.executable),
            )

        assert output.read_bytes() == previous


def test_history_input_rejects_path_escape_and_symlinked_archive() -> None:
    with tempfile.TemporaryDirectory(prefix="matchline-v260-runtime-", dir="/tmp") as directory:
        base = Path(directory)
        runtime_root = base / "runtime"
        runtime_root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        linked = runtime_root / "openfootball-raw"
        linked.symlink_to(outside, target_is_directory=True)
        error_type = getattr(runtime_evidence, "VerifiedHistoryInputError")

        with pytest.raises(error_type, match="archive"):
            _verify_history_input(
                runtime_root=runtime_root,
                raw_archive_dir=linked,
                receipt_path=runtime_root / "openfootball-history-refresh-current.json",
                checked_at=OBSERVED_AT,
            )


def test_runtime_cli_history_mode_fails_closed_before_replacing_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with tempfile.TemporaryDirectory(prefix="matchline-v260-runtime-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        raw_root = runtime_root / "openfootball-raw"
        raw_root.mkdir()
        output = runtime_root / "strict-backtest-current.json"
        previous = b'{"status":"previous-verified"}\n'
        output.write_bytes(previous)

        exit_code = runtime_evidence.main(
            [
                "--strict-report-from-openfootball-history",
                "--runtime-dir",
                str(runtime_root),
                "--openfootball-history-receipt",
                str(runtime_root / "openfootball-history-refresh-current.json"),
                "--openfootball-raw-archive",
                str(raw_root),
                "--strict-report-output",
                str(output),
                "--strict-report-cache",
                str(runtime_root / "strict-report-cache.json"),
                "--candidate-evidence-dir",
                str(runtime_root / "evidence"),
                "--strict-report-prospective-lock",
                str(runtime_root / "missing-lock.json"),
            ]
        )

        assert exit_code == 2
        error = json.loads(capsys.readouterr().err)
        assert error["status"] == "blocked"
        assert "history receipt" in error["error"]
        assert output.read_bytes() == previous


def test_strict_report_wrapper_consumes_fixed_history_pointer_and_preserves_old_output() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    wrapper = repository_root / "deploy/systemd/run-matchline-strict-report.sh"
    with tempfile.TemporaryDirectory(prefix="matchline-v260-runtime-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        raw_root = runtime_root / "openfootball-raw"
        raw_root.mkdir()
        output = runtime_root / "strict-backtest-current.json"
        previous = b'{"status":"previous-verified"}\n'
        output.write_bytes(previous)
        environment = os.environ.copy()
        environment.update(
            {
                "MATCHLINE_RUNTIME_DIR": str(runtime_root),
                "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR": str(raw_root),
                "TMPDIR": "/dev/shm",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

        completed = subprocess.run(
            ["/bin/bash", str(wrapper)],
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode != 0
        assert "history receipt" in completed.stderr
        assert output.read_bytes() == previous


def test_runtime_atomic_write_does_not_follow_a_predictable_temporary_symlink(
    tmp_path: Path,
) -> None:
    output = tmp_path / "strict-backtest-current.json"
    outside = tmp_path / "outside.json"
    outside.write_text("keep\n", encoding="utf-8")
    predictable = output.with_name(f".{output.name}.runtime.{os.getpid()}.tmp")
    predictable.symlink_to(outside)

    runtime_evidence._write_atomic(output, {"status": "safe"})

    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "safe"}
