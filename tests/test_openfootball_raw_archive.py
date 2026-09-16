from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform.live_sources import openfootball_live
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES


PREMIER_LEAGUE_SOURCE_ID = "openfootball:football.json:2026-27:en.1"
OBSERVED_AT = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)


def _archive_api():
    try:
        return importlib.import_module("league_platform.openfootball_raw_archive")
    except ModuleNotFoundError as exc:
        pytest.fail(f"OpenFootball raw archive API is missing: {exc}")


def _observation(api, payload: bytes, *, observed_at: datetime = OBSERVED_AT):
    config = OPENFOOTBALL_SOURCES[PREMIER_LEAGUE_SOURCE_ID]
    return api.OpenFootballRawObservation(
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        url=config["url"],
        retrieved_at=observed_at,
        payload=payload,
        source_format=config["format"],
        competition_id=config["competition_id"],
        season=config["season"],
        timezone_name=config["timezone"],
        license="CC0-1.0",
    )


def test_store_is_content_addressed_fsynced_and_idempotent(tmp_path: Path):
    api = _archive_api()
    payload = json.dumps(
        {
            "matches": [
                {
                    "date": "2026-08-30",
                    "time": "16:00",
                    "team1": "Arsenal FC",
                    "team2": "Coventry City FC",
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    archive = api.OpenFootballRawArchive(tmp_path / "archive")

    first = archive.store(_observation(api, payload))
    second = archive.store(_observation(api, payload))

    digest = hashlib.sha256(payload).hexdigest()
    raw_relative = Path("raw") / "sha256" / digest[:2] / f"{digest}.raw"
    assert first["status"] == "raw_observation_archived"
    assert first["duplicate"] is False
    assert first["raw_sha256"] == digest
    assert first["size_bytes"] == len(payload)
    assert first["raw_path"] == raw_relative.as_posix()
    assert second == {**first, "duplicate": True}
    assert (tmp_path / "archive" / raw_relative).read_bytes() == payload

    manifest_lines = (tmp_path / "archive" / "manifest.jsonl").read_text().splitlines()
    assert len(manifest_lines) == 1
    assert json.loads(manifest_lines[0])["record_sha256"] == first["record_sha256"]


def test_store_keeps_cross_time_observations_but_rejects_same_time_hash_conflict(
    tmp_path: Path,
):
    api = _archive_api()
    archive_root = tmp_path / "archive"
    archive = api.OpenFootballRawArchive(archive_root)
    first_payload = b'{"matches":[]}'
    later = datetime(2026, 8, 25, 1, 7, 3, tzinfo=timezone.utc)

    first = archive.store(_observation(api, first_payload))
    later_receipt = archive.store(_observation(api, first_payload, observed_at=later))

    assert later_receipt["duplicate"] is False
    assert later_receipt["observation_id"] != first["observation_id"]
    assert later_receipt["raw_path"] == first["raw_path"]
    assert len((archive_root / "manifest.jsonl").read_text().splitlines()) == 2
    conflicting_payload = b'{"matches":[{"team1":"forged"}]}'

    with pytest.raises(api.OpenFootballRawArchiveError, match="same source and retrieved_at"):
        archive.store(_observation(api, conflicting_payload))

    assert len((archive_root / "manifest.jsonl").read_text().splitlines()) == 2
    conflicting_hash = hashlib.sha256(conflicting_payload).hexdigest()
    conflicting_path = archive_root / "raw" / "sha256" / conflicting_hash[:2]
    assert not (conflicting_path / f"{conflicting_hash}.raw").exists()


def test_loader_selects_latest_at_cutoff_and_future_append_does_not_change_admission(
    tmp_path: Path,
):
    api = _archive_api()
    archive = api.OpenFootballRawArchive(tmp_path / "archive")
    first_at = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
    selected_at = datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc)
    future_at = datetime(2026, 8, 25, 3, 0, tzinfo=timezone.utc)

    def payload(score: list[int] | None) -> bytes:
        match = {
            "date": "2026-08-30",
            "time": "16:00",
            "team1": "Arsenal FC",
            "team2": "Coventry City FC",
        }
        if score is not None:
            match["score"] = score
        return json.dumps({"matches": [match]}, separators=(",", ":")).encode()

    archive.store(_observation(api, payload(None), observed_at=first_at))
    selected_receipt = archive.store(_observation(api, payload([2, 1]), observed_at=selected_at))

    before_append = api.load_verified_openfootball_archive(
        tmp_path / "archive",
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        observed_before=selected_at,
    )

    assert before_append["status"] == "verified_raw_candidate"
    assert before_append["training_admitted"] is False
    assert before_append["observed_before"] == "2026-08-25T02:00:00+00:00"
    assert before_append["source_ids"] == [PREMIER_LEAGUE_SOURCE_ID]
    assert before_append["selected_records"] == [
        {
            "source_id": PREMIER_LEAGUE_SOURCE_ID,
            "retrieved_at": "2026-08-25T02:00:00+00:00",
            "record_sha256": selected_receipt["record_sha256"],
            "raw_sha256": selected_receipt["raw_sha256"],
        }
    ]
    assert before_append["rows"][0]["score"] == {"home": 2, "away": 1}
    assert before_append["rows"][0]["source"]["retrieved_at"] == ("2026-08-25T02:00:00+00:00")
    assert before_append["parser_contract"] == {
        "json": "openfootball_json_v1",
        "txt": "openfootball_football_txt_v1",
    }
    assert set(before_append["source_manifest"][0]["policy_decisions"]) == {
        "network_fetch",
        "serve_current",
        "model_input",
        "training",
        "redistribution",
    }
    for key in (
        "manifest_sha256",
        "source_manifest_sha256",
        "parser_contract_sha256",
        "rows_sha256",
        "admission_sha256",
    ):
        assert len(before_append[key]) == 64

    archive.store(_observation(api, payload([3, 1]), observed_at=future_at))
    after_append = api.load_verified_openfootball_archive(
        tmp_path / "archive",
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        observed_before=selected_at,
    )

    assert after_append["manifest_sha256"] != before_append["manifest_sha256"]
    assert after_append["admission_sha256"] == before_append["admission_sha256"]
    assert after_append["selected_records"] == before_append["selected_records"]
    assert after_append["rows"] == before_append["rows"]


def test_loader_rejects_duplicate_deterministic_fixture_ids(tmp_path: Path):
    api = _archive_api()
    archive = api.OpenFootballRawArchive(tmp_path / "archive")
    payload = json.dumps(
        {
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-30",
                    "time": "16:00",
                    "team1": "Repeated Home",
                    "team2": "Repeated Away",
                },
                {
                    "round": "Matchday 2",
                    "date": "2026-09-01",
                    "time": "16:00",
                    "team1": "Repeated Home",
                    "team2": "Repeated Away",
                },
            ]
        },
        separators=(",", ":"),
    ).encode()
    archive.store(_observation(api, payload))

    with pytest.raises(api.OpenFootballRawArchiveError, match="duplicate fixture id"):
        api.load_verified_openfootball_archive(
            tmp_path / "archive",
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
            observed_before=OBSERVED_AT,
        )


@pytest.mark.parametrize("tamper", ["raw", "manifest"])
def test_store_revalidates_existing_record_and_raw_before_dedupe(
    tmp_path: Path,
    tamper: str,
):
    api = _archive_api()
    archive_root = tmp_path / "archive"
    archive = api.OpenFootballRawArchive(archive_root)
    payload = b'{"matches":[]}'
    receipt = archive.store(_observation(api, payload))
    if tamper == "raw":
        (archive_root / receipt["raw_path"]).write_bytes(b"X" * len(payload))
    else:
        manifest_path = archive_root / "manifest.jsonl"
        record = json.loads(manifest_path.read_text())
        record["license"] = "MIT"
        manifest_path.write_text(json.dumps(record, separators=(",", ":")) + "\n")

    with pytest.raises(api.OpenFootballRawArchiveError):
        archive.store(_observation(api, payload))


@pytest.mark.parametrize("field", ["observation_id", "size_bytes", "raw_path", "manifest_sha256"])
def test_receipt_must_bind_to_manifest_and_content_addressed_raw_object(
    tmp_path: Path,
    field: str,
):
    api = _archive_api()
    archive_root = tmp_path / "archive"
    receipt = api.OpenFootballRawArchive(archive_root).store(
        _observation(api, b'{"matches":[]}')
    )
    forged = dict(receipt)
    if field == "observation_id":
        forged[field] = "f" * 64
    elif field == "size_bytes":
        forged[field] += 1
    elif field == "raw_path":
        forged[field] = "raw/forged.raw"
    else:
        forged[field] = "e" * 64

    with pytest.raises(api.OpenFootballRawArchiveError, match="receipt|manifest|observation"):
        api.verify_openfootball_raw_receipt(archive_root, forged)


def test_receipt_manifest_prefix_remains_valid_after_later_append(tmp_path: Path):
    api = _archive_api()
    archive_root = tmp_path / "archive"
    archive = api.OpenFootballRawArchive(archive_root)
    first = archive.store(_observation(api, b'{"matches":[]}'))
    archive.store(
        _observation(
            api,
            b'{"matches":[{"team1":"later","team2":"append"}]}',
            observed_at=datetime(2026, 8, 25, 1, 7, 3, tzinfo=timezone.utc),
        )
    )

    verified = api.verify_openfootball_raw_receipt(archive_root, first)
    assert verified["observation_id"] == first["observation_id"]


@pytest.mark.parametrize("target", ["manifest", "raw"])
def test_loader_rejects_symlinked_manifest_or_raw_object(tmp_path: Path, target: str):
    api = _archive_api()
    archive_root = tmp_path / "archive"
    archive = api.OpenFootballRawArchive(archive_root)
    payload = b'{"matches":[]}'
    receipt = archive.store(_observation(api, payload))
    if target == "manifest":
        protected = archive_root / "manifest.jsonl"
        external = tmp_path / "external-manifest.jsonl"
    else:
        protected = archive_root / receipt["raw_path"]
        external = tmp_path / "external.raw"
    external.write_bytes(protected.read_bytes())
    protected.unlink()
    protected.symlink_to(external)

    with pytest.raises(api.OpenFootballRawArchiveError, match="symlink"):
        api.load_verified_openfootball_archive(
            archive_root,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
            observed_before=OBSERVED_AT,
        )


@pytest.mark.parametrize("target", ["archive_root", "raw_directory"])
def test_loader_rejects_symlinked_archive_path_components(tmp_path: Path, target: str):
    api = _archive_api()
    real_root = tmp_path / "real-archive"
    api.OpenFootballRawArchive(real_root).store(_observation(api, b'{"matches":[]}'))
    if target == "archive_root":
        archive_root = tmp_path / "archive-link"
        archive_root.symlink_to(real_root, target_is_directory=True)
    else:
        external_raw = tmp_path / "external-raw"
        (real_root / "raw").rename(external_raw)
        (real_root / "raw").symlink_to(external_raw, target_is_directory=True)
        archive_root = real_root

    with pytest.raises(api.OpenFootballRawArchiveError, match="symlink"):
        api.load_verified_openfootball_archive(
            archive_root,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
            observed_before=OBSERVED_AT,
        )


def test_store_rejects_payload_over_live_adapter_limit_before_writing(tmp_path: Path):
    api = _archive_api()
    archive_root = tmp_path / "archive"

    with pytest.raises(api.OpenFootballRawArchiveError, match="4 MiB"):
        api.OpenFootballRawArchive(archive_root).store(
            _observation(api, b" " * (4 * 1024 * 1024 + 1))
        )

    assert not archive_root.exists()


@pytest.mark.parametrize("target", ["archive_root", "raw_directory"])
def test_store_rejects_symlinked_archive_directories_without_external_write(
    tmp_path: Path,
    target: str,
):
    api = _archive_api()
    archive_root = tmp_path / "archive"
    external = tmp_path / "external"
    external.mkdir()
    if target == "archive_root":
        archive_root.symlink_to(external, target_is_directory=True)
    else:
        archive_root.mkdir()
        (archive_root / "raw").symlink_to(external, target_is_directory=True)

    with pytest.raises(api.OpenFootballRawArchiveError, match="symlink"):
        api.OpenFootballRawArchive(archive_root).store(_observation(api, b'{"matches":[]}'))

    assert list(external.iterdir()) == []
    if target == "raw_directory":
        assert not (archive_root / "manifest.jsonl").exists()


def test_idempotent_retry_fsyncs_raw_and_manifest_after_manifest_fsync_failure(
    tmp_path: Path,
    monkeypatch,
):
    api = _archive_api()
    archive_root = tmp_path / "archive"
    archive = api.OpenFootballRawArchive(archive_root)
    observation = _observation(api, b'{"matches":[]}')
    real_fsync = os.fsync
    failed = False

    def fail_first_manifest_fsync(descriptor: int):
        nonlocal failed
        descriptor_path = os.readlink(f"/proc/self/fd/{descriptor}")
        if descriptor_path.endswith("/manifest.jsonl") and not failed:
            failed = True
            raise OSError("injected manifest fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(api.os, "fsync", fail_first_manifest_fsync)
    with pytest.raises(OSError, match="injected manifest fsync failure"):
        archive.store(observation)
    assert failed is True
    assert len((archive_root / "manifest.jsonl").read_text().splitlines()) == 1

    fsynced_paths: list[str] = []

    def observe_fsync(descriptor: int):
        fsynced_paths.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        return real_fsync(descriptor)

    monkeypatch.setattr(api.os, "fsync", observe_fsync)
    receipt = archive.store(observation)

    assert receipt["duplicate"] is True
    assert any(path.endswith("/manifest.jsonl") for path in fsynced_paths)
    assert any(path.endswith(".raw") for path in fsynced_paths)


@pytest.mark.parametrize("tamper", ["provider_fixture_id", "competition_id", "season"])
def test_loader_rejects_reparsed_row_identity_outside_allowlisted_config(
    tmp_path: Path,
    monkeypatch,
    tamper: str,
):
    api = _archive_api()
    archive = api.OpenFootballRawArchive(tmp_path / "archive")
    payload = b'{"matches":[{"date":"2026-08-30","time":"16:00","team1":"Identity Home","team2":"Identity Away"}]}'
    archive.store(_observation(api, payload))
    real_parser = openfootball_live.parse_openfootball_json

    def tampered_parser(*args, **kwargs):
        rows = real_parser(*args, **kwargs)
        if tamper == "provider_fixture_id":
            rows[0]["provider_fixture_ids"]["OpenFootball"] = "forged"
        else:
            rows[0][tamper] = "forged"
        return rows

    monkeypatch.setattr(openfootball_live, "parse_openfootball_json", tampered_parser)

    with pytest.raises(api.OpenFootballRawArchiveError, match="identity"):
        api.load_verified_openfootball_archive(
            tmp_path / "archive",
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
            observed_before=OBSERVED_AT,
        )


@pytest.mark.parametrize("tamper", ["source_row_id", "record_index", "line_number"])
def test_loader_rejects_reparsed_row_with_invalid_lineage_pointer(
    tmp_path: Path,
    monkeypatch,
    tamper: str,
):
    api = _archive_api()
    archive = api.OpenFootballRawArchive(tmp_path / "archive")
    payload = b'{"matches":[{"date":"2026-08-30","time":"16:00","team1":"Pointer Home","team2":"Pointer Away"}]}'
    archive.store(_observation(api, payload))
    real_parser = openfootball_live.parse_openfootball_json

    def tampered_parser(*args, **kwargs):
        rows = real_parser(*args, **kwargs)
        rows[0]["lineage"][tamper] = "forged" if tamper == "source_row_id" else -1
        return rows

    monkeypatch.setattr(openfootball_live, "parse_openfootball_json", tampered_parser)

    with pytest.raises(api.OpenFootballRawArchiveError, match="lineage"):
        api.load_verified_openfootball_archive(
            tmp_path / "archive",
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
            observed_before=OBSERVED_AT,
        )


def test_loader_verifies_raw_objects_for_manifest_records_after_cutoff(tmp_path: Path):
    api = _archive_api()
    archive_root = tmp_path / "archive"
    archive = api.OpenFootballRawArchive(archive_root)
    selected_payload = b'{"matches":[]}'
    future_payload = b'{"matches":[{"date":"2026-08-30","team1":"Future","team2":"Tamper"}]}'
    archive.store(_observation(api, selected_payload, observed_at=OBSERVED_AT))
    future_receipt = archive.store(
        _observation(
            api,
            future_payload,
            observed_at=datetime(2026, 8, 25, 2, 2, 3, tzinfo=timezone.utc),
        )
    )
    (archive_root / future_receipt["raw_path"]).write_bytes(b"X" * len(future_payload))

    with pytest.raises(api.OpenFootballRawArchiveError, match="hash"):
        api.load_verified_openfootball_archive(
            archive_root,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
            observed_before=OBSERVED_AT,
        )


def _publication_snapshot(api, archive_root: Path) -> dict:
    payload = json.dumps(
        {
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-30",
                    "time": "16:00",
                    "team1": "Arsenal FC",
                    "team2": "Coventry City FC",
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    archive = api.OpenFootballRawArchive(archive_root)
    archive.store(_observation(api, payload))
    verified = api.load_verified_openfootball_archive(
        archive_root,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        observed_before=OBSERVED_AT,
    )
    admission = {
        key: verified[key]
        for key in (
            "schema_version",
            "observed_before",
            "source_ids",
            "selected_records",
            "admission_sha256",
            "source_manifest_sha256",
            "parser_contract_sha256",
            "rows_sha256",
        )
    }
    admission.update(
        {
            "status": "verified_current_raw",
            "policy_version": "v260",
            "row_count": len(verified["rows"]),
        }
    )
    return {
        "schema_version": "1.0.0",
        "as_of": OBSERVED_AT.isoformat(),
        "fixture_feed": {
            "provider": "OpenFootball",
            "raw_archive_admission": admission,
            "fixtures": verified["rows"],
        },
    }


def test_publication_snapshot_admission_replays_durable_raw_archive():
    api = _archive_api()
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        archive_root = Path(directory) / "archive"
        snapshot = _publication_snapshot(api, archive_root)

        result = api.verify_openfootball_snapshot_admission(
            snapshot,
            archive_root=archive_root,
        )

        assert result["status"] == "verified_current_raw"
        assert result["published_fixture_count"] == 1
        assert result["admission_sha256"] == snapshot["fixture_feed"]["raw_archive_admission"]["admission_sha256"]

        from league_platform.publish_snapshot import require_openfootball_raw_producer_receipt

        assert require_openfootball_raw_producer_receipt(
            snapshot,
            dry_run=False,
            openfootball_raw_archive_dir=archive_root,
        ) == f"verified_current_raw:{result['admission_sha256']}"


@pytest.mark.parametrize("tamper_field", ["home_team", "record_index"])
def test_publication_snapshot_admission_rejects_mutated_fixture_or_lineage(tamper_field: str):
    api = _archive_api()
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        archive_root = Path(directory) / "archive"
        snapshot = _publication_snapshot(api, archive_root)
        row = snapshot["fixture_feed"]["fixtures"][0]
        if tamper_field == "home_team":
            row["home_team"] = "Forged FC"
            message = "fixture facts"
        else:
            row["lineage"]["record_index"] = 99
            message = "lineage"

        with pytest.raises(api.OpenFootballRawArchiveError, match=message):
            api.verify_openfootball_snapshot_admission(
                snapshot,
                archive_root=archive_root,
            )


@pytest.mark.parametrize("tamper_field", ["score", "kickoff_time_source", "kickoff_timezone"])
def test_publication_snapshot_admission_rejects_mutated_parser_owned_fixture_fields(
    tamper_field: str,
):
    api = _archive_api()
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        archive_root = Path(directory) / "archive"
        snapshot = _publication_snapshot(api, archive_root)
        row = snapshot["fixture_feed"]["fixtures"][0]
        if tamper_field == "score":
            row[tamper_field] = {"home": 9, "away": 9}
        else:
            row[tamper_field] = "forged"

        with pytest.raises(api.OpenFootballRawArchiveError, match="fixture facts"):
            api.verify_openfootball_snapshot_admission(
                snapshot,
                archive_root=archive_root,
            )


def test_publication_snapshot_admission_rejects_volatile_archive():
    api = _archive_api()
    archive_root = Path("/dev/shm") / "matchline-publication-raw-test"
    with pytest.raises(api.OpenFootballRawArchiveError, match="durable"):
        api.verify_openfootball_snapshot_admission(
            {
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "raw_archive_admission": {},
                    "fixtures": [],
                }
            },
            archive_root=archive_root,
        )
