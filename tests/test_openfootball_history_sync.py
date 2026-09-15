from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCES,
)


AS_OF = datetime(2026, 8, 25, 2, 30, tzinfo=timezone.utc)


def _api():
    try:
        return importlib.import_module("league_platform.openfootball_history_sync")
    except ModuleNotFoundError as exc:
        pytest.fail(f"OpenFootball history producer API is missing: {exc}")


class _Response:
    def __init__(self, *, url: str, payload: bytes) -> None:
        self._url = url
        self._payload = payload
        self._read = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, _size: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._payload


class _PayloadOpener:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads

    def open(self, request, timeout: float):
        assert timeout > 0
        url = request.full_url
        if url not in self._payloads:
            raise AssertionError(f"unexpected network URL: {url}")
        return _Response(url=url, payload=self._payloads[url])


def _history_payloads(*, unfinished_source_id: str | None = None) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for index, source_id in enumerate(OPENFOOTBALL_HISTORY_SOURCE_IDS, start=1):
        config = OPENFOOTBALL_HISTORY_SOURCES[source_id]
        year = int(config["season"].split("-", maxsplit=1)[0])
        match = (
            f"    12:45  History Home {index}  v  History Away {index}"
            if source_id == unfinished_source_id
            else f"  12:45  History Home {index}  1-0 (1-0)  History Away {index}"
        )
        payloads[config["url"]] = (
            "= OpenFootball deterministic history fixture\n"
            "▪ Matchday 1\n"
            f"Sat Aug 8 {year}\n"
            f"{match}\n"
        ).encode()
    return payloads


def test_refresh_replays_all_66_sources_before_publishing_atomic_receipt(tmp_path: Path):
    api = _api()
    unfinished_source_id = OPENFOOTBALL_HISTORY_SOURCE_IDS[-1]
    opener = _PayloadOpener(_history_payloads(unfinished_source_id=unfinished_source_id))
    pointer = tmp_path / "openfootball-history-refresh-current.json"

    with tempfile.TemporaryDirectory(prefix="matchline-history-raw-", dir="/tmp") as directory:
        raw_root = Path(directory) / "openfootball-raw"
        receipt = api.refresh_openfootball_history(
            raw_archive_dir=raw_root,
            receipt_path=pointer,
            now=AS_OF,
            opener=opener,
        )

        pointer_bytes = pointer.read_bytes()
        receipt_sha256 = hashlib.sha256(pointer_bytes).hexdigest()
        immutable_receipt = (
            raw_root
            / "history-refresh-receipts"
            / "sha256"
            / receipt_sha256[:2]
            / f"{receipt_sha256}.json"
        )
        assert immutable_receipt.read_bytes() == pointer_bytes
        assert len((raw_root / "manifest.jsonl").read_text().splitlines()) == 66

    assert json.loads(pointer_bytes) == receipt
    assert set(receipt) == {
        "schema_version",
        "status",
        "observed_at",
        "source_count",
        "source_ids",
        "parsed_fixture_count",
        "finished_fixture_count",
        "unfinished_fixture_count",
        "admission_sha256",
        "rows_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
        "errors",
    }
    assert receipt["schema_version"] == "matchline.openfootball_history_refresh.v1"
    assert receipt["status"] == "verified_raw_history"
    assert receipt["observed_at"] == AS_OF.isoformat()
    assert receipt["source_count"] == 66
    assert receipt["source_ids"] == sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    assert receipt["parsed_fixture_count"] == 66
    assert receipt["finished_fixture_count"] == 65
    assert receipt["unfinished_fixture_count"] == 1
    assert receipt["errors"] == []
    for field in (
        "admission_sha256",
        "rows_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
    ):
        assert len(receipt[field]) == 64
    serialized = json.dumps(receipt, sort_keys=True)
    assert "training_admitted" not in serialized
    assert "raw_archive_receipts" not in serialized
    assert "raw_path" not in serialized
    assert "payload" not in serialized
    assert "authorization" not in serialized


def test_partial_source_failure_keeps_raw_appends_but_preserves_previous_pointer(
    tmp_path: Path,
):
    api = _api()
    failed_source_id = OPENFOOTBALL_HISTORY_SOURCE_IDS[-1]
    failed_url = OPENFOOTBALL_HISTORY_SOURCES[failed_source_id]["url"]
    payloads = _history_payloads()
    payloads.pop(failed_url)
    pointer = tmp_path / "openfootball-history-refresh-current.json"
    previous = b'{"status":"previous-verified"}\n'
    pointer.write_bytes(previous)

    with tempfile.TemporaryDirectory(prefix="matchline-history-raw-", dir="/tmp") as directory:
        raw_root = Path(directory) / "openfootball-raw"
        with pytest.raises(api.OpenFootballHistorySyncError) as caught:
            api.refresh_openfootball_history(
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                now=AS_OF,
                opener=_PayloadOpener(payloads),
            )

        assert len((raw_root / "manifest.jsonl").read_text().splitlines()) == 65
        assert not (raw_root / "history-refresh-receipts").exists()

    assert pointer.read_bytes() == previous
    assert any(
        error.get("source_id") == failed_source_id and error.get("stage") == "fetch_or_parse"
        for error in caught.value.errors
    )


def test_raw_tamper_after_fetch_blocks_verified_replay_and_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    api = _api()
    pointer = tmp_path / "openfootball-history-refresh-current.json"
    previous = b'{"status":"previous-verified"}\n'
    pointer.write_bytes(previous)

    with tempfile.TemporaryDirectory(prefix="matchline-history-raw-", dir="/tmp") as directory:
        raw_root = Path(directory) / "openfootball-raw"
        real_fetch = api.fetch_openfootball_history

        def fetch_then_tamper(**kwargs):
            result = real_fetch(**kwargs)
            archived = result["raw_archive_receipts"][0]
            raw_path = raw_root / archived["raw_path"]
            raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
            return result

        monkeypatch.setattr(api, "fetch_openfootball_history", fetch_then_tamper)
        with pytest.raises(api.OpenFootballHistorySyncError) as caught:
            api.refresh_openfootball_history(
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                now=AS_OF,
                opener=_PayloadOpener(_history_payloads()),
            )

        assert len((raw_root / "manifest.jsonl").read_text().splitlines()) == 66
        assert not (raw_root / "history-refresh-receipts").exists()

    assert pointer.read_bytes() == previous
    assert caught.value.errors == [
        {
            "stage": "verified_replay",
            "error": "verified OpenFootball raw archive replay failed",
        }
    ]


def test_fetch_rows_must_exactly_match_the_verified_raw_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    api = _api()
    pointer = tmp_path / "openfootball-history-refresh-current.json"
    previous = b'{"status":"previous-verified"}\n'
    pointer.write_bytes(previous)

    with tempfile.TemporaryDirectory(prefix="matchline-history-raw-", dir="/tmp") as directory:
        raw_root = Path(directory) / "openfootball-raw"
        real_fetch = api.fetch_openfootball_history

        def fetch_then_change_a_row(**kwargs):
            result = real_fetch(**kwargs)
            result["history"][0] = {
                **result["history"][0],
                "home_team": "not-the-reparsed-team",
            }
            return result

        monkeypatch.setattr(api, "fetch_openfootball_history", fetch_then_change_a_row)
        with pytest.raises(api.OpenFootballHistorySyncError) as caught:
            api.refresh_openfootball_history(
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                now=AS_OF,
                opener=_PayloadOpener(_history_payloads()),
            )

        assert len((raw_root / "manifest.jsonl").read_text().splitlines()) == 66
        assert not (raw_root / "history-refresh-receipts").exists()

    assert pointer.read_bytes() == previous
    assert caught.value.errors == [
        {
            "stage": "fetch_replay_comparison",
            "error": "fetch rows do not match verified raw replay rows",
        }
    ]


def test_volatile_raw_root_is_rejected_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    api = _api()
    calls = 0

    def forbidden_fetch(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("fetch must not start for a volatile raw root")

    monkeypatch.setattr(api, "fetch_openfootball_history", forbidden_fetch)
    pointer = tmp_path / "current.json"
    previous = b'{"status":"previous-verified"}\n'
    pointer.write_bytes(previous)

    with tempfile.TemporaryDirectory(prefix="matchline-history-volatile-", dir="/dev/shm") as directory:
        raw_root = Path(directory) / "openfootball-raw"
        with pytest.raises(api.OpenFootballHistorySyncError) as caught:
            api.refresh_openfootball_history(
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                now=AS_OF,
            )

    assert calls == 0
    assert pointer.read_bytes() == previous
    assert caught.value.errors == [
        {
            "stage": "raw_archive_path",
            "error": "OpenFootball raw archive must not be under /dev/shm",
        }
    ]


def test_symlinked_or_non_directory_raw_root_is_rejected_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    api = _api()
    calls = 0

    def forbidden_fetch(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("fetch must not start for an invalid raw root")

    monkeypatch.setattr(api, "fetch_openfootball_history", forbidden_fetch)
    pointer = tmp_path / "current.json"
    previous = b'{"status":"previous-verified"}\n'
    pointer.write_bytes(previous)

    with tempfile.TemporaryDirectory(prefix="matchline-history-path-", dir="/tmp") as directory:
        base = Path(directory)
        real_root = base / "real-root"
        real_root.mkdir()
        symlink_root = base / "linked-root"
        symlink_root.symlink_to(real_root, target_is_directory=True)
        regular_file = base / "not-a-directory"
        regular_file.write_text("not a root")

        for invalid_root in (symlink_root, regular_file):
            with pytest.raises(api.OpenFootballHistorySyncError) as caught:
                api.refresh_openfootball_history(
                    raw_archive_dir=invalid_root,
                    receipt_path=pointer,
                    now=AS_OF,
                )
            assert caught.value.errors[0]["stage"] == "raw_archive_path"

    assert calls == 0
    assert pointer.read_bytes() == previous


def test_cli_requires_explicit_raw_root_and_flag_overrides_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    api = _api()
    pointer = tmp_path / "current.json"
    calls = []

    def fake_refresh(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": "matchline.openfootball_history_refresh.v1",
            "status": "verified_raw_history",
            "errors": [],
        }

    monkeypatch.setattr(api, "refresh_openfootball_history", fake_refresh)
    monkeypatch.delenv("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR", raising=False)

    assert api.main(["--receipt", str(pointer)]) == 2
    assert calls == []
    missing_output = json.loads(capsys.readouterr().err)
    assert missing_output["status"] == "blocked"
    assert missing_output["errors"][0]["stage"] == "raw_archive_path"

    environment_root = Path("/tmp/openfootball-history-from-environment")
    flag_root = Path("/tmp/openfootball-history-from-flag")
    monkeypatch.setenv("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR", str(environment_root))
    assert api.main(["--receipt", str(pointer)]) == 0
    assert calls[-1]["raw_archive_dir"] == environment_root
    assert json.loads(capsys.readouterr().out)["status"] == "verified_raw_history"

    assert (
        api.main(
            [
                "--raw-archive-dir",
                str(flag_root),
                "--receipt",
                str(pointer),
            ]
        )
        == 0
    )
    assert calls[-1]["raw_archive_dir"] == flag_root


def test_pointer_replace_failure_keeps_previous_pointer_and_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    api = _api()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    pointer = runtime_dir / "current.json"
    previous = b'{"status":"previous-verified"}\n'
    pointer.write_bytes(previous)
    real_replace = api.os.replace

    def fail_pointer_replace(source, destination):
        if Path(destination) == pointer:
            raise OSError("injected pointer replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(api.os, "replace", fail_pointer_replace)
    with tempfile.TemporaryDirectory(prefix="matchline-history-raw-", dir="/tmp") as directory:
        raw_root = Path(directory) / "openfootball-raw"
        with pytest.raises(api.OpenFootballHistorySyncError) as caught:
            api.refresh_openfootball_history(
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                now=AS_OF,
                opener=_PayloadOpener(_history_payloads()),
            )
        immutable_receipts = list((raw_root / "history-refresh-receipts").glob("sha256/*/*.json"))

    assert len(immutable_receipts) == 1
    assert pointer.read_bytes() == previous
    assert list(runtime_dir.iterdir()) == [pointer]
    assert caught.value.errors == [
        {
            "stage": "receipt_pointer",
            "error": "history receipt pointer update failed",
        }
    ]


def test_daily_history_refresh_systemd_templates_use_durable_raw_environment():
    project_root = Path(__file__).resolve().parents[1]
    service_path = project_root / "deploy/systemd/matchline-openfootball-history-refresh.service"
    timer_path = project_root / "deploy/systemd/matchline-openfootball-history-refresh.timer"
    environment_path = project_root / "deploy/systemd/matchline-runtime.env.example"

    assert service_path.is_file()
    assert timer_path.is_file()
    verified = subprocess.run(
        ["systemd-analyze", "verify", str(service_path), str(timer_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr

    service = service_path.read_text()
    timer = timer_path.read_text()
    environment = environment_path.read_text()
    assert "EnvironmentFile=-/etc/matchline/matchline-runtime.env" in service
    assert "-m league_platform.openfootball_history_sync" in service
    assert (
        "--receipt=${MATCHLINE_RUNTIME_DIR}/openfootball-history-refresh-current.json" in service
    )
    assert "--raw-archive-dir=${MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR}" in service
    assert "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR=/dev/shm" not in service
    assert "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR=" in environment
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer
    assert "Unit=matchline-openfootball-history-refresh.service" in timer
    assert "systemctl start" not in service + timer
    assert "systemctl enable" not in service + timer
    assert "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR=" in environment
    assert "current and historical" in environment


def test_receipt_archive_symlink_cannot_escape_durable_raw_root(
    tmp_path: Path,
):
    api = _api()
    pointer = tmp_path / "current.json"
    previous = b'{"status":"previous-verified"}\n'
    pointer.write_bytes(previous)

    with tempfile.TemporaryDirectory(prefix="matchline-history-raw-", dir="/tmp") as directory:
        base = Path(directory)
        raw_root = base / "openfootball-raw"
        raw_root.mkdir()
        external = base / "external"
        external.mkdir()
        (raw_root / "history-refresh-receipts").symlink_to(external, target_is_directory=True)

        with pytest.raises(api.OpenFootballHistorySyncError) as caught:
            api.refresh_openfootball_history(
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                now=AS_OF,
                opener=_PayloadOpener(_history_payloads()),
            )

        assert list(external.iterdir()) == []
        assert len((raw_root / "manifest.jsonl").read_text().splitlines()) == 66

    assert pointer.read_bytes() == previous
    assert caught.value.errors == [
        {
            "stage": "receipt_archive",
            "error": "immutable history receipt write failed",
        }
    ]


def test_raw_sink_failure_isolated_for_all_sources_and_never_updates_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    api = _api()
    pointer = tmp_path / "current.json"
    previous = b'{"status":"previous-verified"}\n'
    pointer.write_bytes(previous)
    sink_calls = []

    class FailingSink:
        def __init__(self, root):
            self.root = Path(root)

        def __call__(self, observation):
            sink_calls.append(observation.source_id)
            raise OSError("injected archive sink failure")

    monkeypatch.setattr(api, "OpenFootballRawArchive", FailingSink)
    with tempfile.TemporaryDirectory(prefix="matchline-history-raw-", dir="/tmp") as directory:
        raw_root = Path(directory) / "openfootball-raw"
        with pytest.raises(api.OpenFootballHistorySyncError) as caught:
            api.refresh_openfootball_history(
                raw_archive_dir=raw_root,
                receipt_path=pointer,
                now=AS_OF,
                opener=_PayloadOpener(_history_payloads()),
            )
        assert not raw_root.exists()

    assert sorted(sink_calls) == sorted(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    assert len(caught.value.errors) == 66
    assert all(error["stage"] == "raw_archive" for error in caught.value.errors)
    assert pointer.read_bytes() == previous
