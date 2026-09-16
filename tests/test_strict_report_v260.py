from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform import strict_report
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCES,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
)
from league_platform.source_rights import POLICY_VERSION
from league_platform.strict_report import build_report


OBSERVED_BEFORE = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)
PREMIER_LEAGUE_SOURCE_ID = "openfootball:england:2015-16:1-premierleague"


def _store_history(archive_root: Path) -> dict:
    config = OPENFOOTBALL_HISTORY_SOURCES[PREMIER_LEAGUE_SOURCE_ID]
    payload = (
        "= Premier League\n\u25aa Matchday 1\nSat Aug 8 2015\n  12:45  Home  1-0 (1-0)  Away\n"
    ).encode()
    return OpenFootballRawArchive(archive_root).store(
        OpenFootballRawObservation(
            source_id=PREMIER_LEAGUE_SOURCE_ID,
            url=config["url"],
            retrieved_at=OBSERVED_BEFORE,
            payload=payload,
            source_format=config["format"],
            competition_id=config["competition_id"],
            season=config["season"],
            timezone_name=config["timezone"],
            license="CC0-1.0",
        )
    )


def test_formal_report_is_bound_to_verified_raw_admission_and_blocks_missing_lanes(
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "raw-archive"
    receipt = _store_history(archive_root)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    report = build_report(
        openfootball_raw_archive=archive_root,
        observed_before=OBSERVED_BEFORE,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        candidate_evidence_dir=evidence_dir,
        prospective_lock_path=None,
    )

    assert report["schema_version"] == "matchline.strict_report.v260"
    assert report["report_lane"] == "formal_v260"
    assert report["training_source_contract"] == {
        "provider": "OpenFootball",
        "raw_archive_required": True,
        "training_admission_sha256": report["training_admission"]["admission_sha256"],
        "policy_version": POLICY_VERSION,
        "observed_before": "2026-08-25T01:02:03+00:00",
        "source_ids": [PREMIER_LEAGUE_SOURCE_ID],
        "prohibited_formal_inputs": [
            "football_data_csv",
            "legacy_csl_files",
            "snapshot_self_report",
        ],
    }
    admission = report["training_admission"]
    assert admission["status"] == "training_admitted"
    assert admission["training_admitted"] is True
    assert admission["selected_records"][0]["raw_sha256"] == receipt["raw_sha256"]
    assert admission["counts"]["finished_admitted"] == 1

    premier_league = report["leagues"]["premier-league"]
    assert premier_league["status"] == "not_evaluated"
    assert premier_league["sample_n"] == 0
    assert all(gate["status"] == "blocked" for gate in premier_league["gates"].values())
    assert all(
        "insufficient_sample" in gate["failures"] for gate in premier_league["gates"].values()
    )
    assert report["leagues"]["bundesliga"]["status"] == "unavailable"
    assert report["combined"]["scoreline_sample_n"] == 0
    assert report["combined"]["scoreline_gate"]["status"] == "blocked"
    assert report["overall"]["sample_threshold_gate"] == ("partial_with_explicit_blocks")

    csl = report["leagues"]["csl"]
    assert csl["status"] == "unavailable"
    assert csl["reason"] == "no_declared_verified_raw_history_source"
    assert csl["sample_n"] == 0
    assert all(gate["status"] == "blocked" for gate in csl["gates"].values())
    assert report["overall"]["production_allowed"] is False
    assert report["overall"]["market_gate"] == ("unavailable_no_independent_market_baseline")
    assert report["overall"]["market_comparison_targets"] == []
    assert any("独立市场基线不可用" in reason for reason in report["overall"]["blocked_reasons"])
    assert (
        "csl:no_declared_verified_raw_history_source" in report["overall"]["availability_blocks"]
    )


def test_formal_cli_missing_raw_archive_exits_before_build_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "strict.json"

    def forbidden_build(*_args, **_kwargs):
        raise AssertionError("formal builder must not run without a raw archive")

    monkeypatch.setattr(strict_report, "build_report", forbidden_build)
    monkeypatch.setattr(
        sys,
        "argv",
        ["strict_report", "--output", str(output)],
    )

    with pytest.raises(SystemExit) as raised:
        strict_report.main()

    assert raised.value.code != 0
    assert not output.exists()


@pytest.mark.parametrize(
    "legacy_arguments",
    [
        ["--data-dir", "legacy-data"],
        ["--kickoff-enrichment", "snapshot-derived-kickoff.json"],
    ],
)
def test_formal_cli_rejects_legacy_file_and_snapshot_inputs_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_arguments: list[str],
) -> None:
    output = tmp_path / "strict.json"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("formal input validation must precede all loading")

    monkeypatch.setattr(strict_report, "build_input_fingerprint", forbidden)
    monkeypatch.setattr(strict_report, "build_report", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "strict_report",
            "--openfootball-raw-archive",
            str(tmp_path / "raw-archive"),
            "--observed-before",
            OBSERVED_BEFORE.isoformat(),
            "--output",
            str(output),
            *legacy_arguments,
        ],
    )

    with pytest.raises(SystemExit) as raised:
        strict_report.main()

    assert raised.value.code != 0
    assert not output.exists()


def test_legacy_builder_is_explicitly_research_only_even_if_body_claims_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        strict_report,
        "_build_legacy_report_body",
        lambda *_args, **_kwargs: {
            "generated_at": "2026-08-25T01:02:03+00:00",
            "leagues": {},
            "combined": {"sample_n": 0},
            "overall": {
                "production_allowed": True,
                "blocked_reasons": [],
            },
        },
    )

    report = strict_report.build_legacy_research_report(tmp_path / "legacy-data")

    assert report["schema_version"] == ("matchline.strict_report.legacy_research_only.v1")
    assert report["report_lane"] == "legacy_research_only"
    assert report["formal_maturity_eligible"] is False
    assert report["overall"]["production_allowed"] is False
    assert report["overall"]["legacy_source_gate"] == "blocked"
    assert "legacy_research_only" in report["overall"]["blocked_reasons"]


def test_formal_fingerprint_binds_raw_admission_and_ignores_snapshot_self_reports(
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "raw-archive"
    _store_history(archive_root)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    candidate = evidence_dir / "model-candidate-demo.json"
    candidate.write_text('{"candidate": 1}\n', encoding="utf-8")
    lock = evidence_dir / "prospective-lock.json"
    lock.write_text('{"model_version_sha256": "demo"}\n', encoding="utf-8")

    first = strict_report.build_input_fingerprint(
        openfootball_raw_archive=archive_root,
        observed_before=OBSERVED_BEFORE,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        candidate_evidence_dir=evidence_dir,
        prospective_lock_path=lock,
    )

    assert first["report_lane"] == "formal_v260"
    assert first["inputs"]["training_admission"]["training_admitted"] is True
    assert first["inputs"]["training_admission"]["observed_before"] == (
        "2026-08-25T01:02:03+00:00"
    )
    assert "data_files" not in first["inputs"]
    snapshot = tmp_path / "current.json"
    snapshot.write_text('{"self_reported": "all_green"}\n', encoding="utf-8")
    after_snapshot = strict_report.build_input_fingerprint(
        openfootball_raw_archive=archive_root,
        observed_before=OBSERVED_BEFORE,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        candidate_evidence_dir=evidence_dir,
        prospective_lock_path=lock,
    )
    assert after_snapshot["sha256"] == first["sha256"]

    candidate.write_text('{"candidate": 2}\n', encoding="utf-8")
    after_candidate = strict_report.build_input_fingerprint(
        openfootball_raw_archive=archive_root,
        observed_before=OBSERVED_BEFORE,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        candidate_evidence_dir=evidence_dir,
        prospective_lock_path=lock,
    )
    assert after_candidate["sha256"] != first["sha256"]


def test_formal_cache_rejects_a_content_addressed_legacy_report(tmp_path: Path) -> None:
    archive_root = tmp_path / "raw-archive"
    _store_history(archive_root)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    fingerprint = strict_report.build_input_fingerprint(
        openfootball_raw_archive=archive_root,
        observed_before=OBSERVED_BEFORE,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        candidate_evidence_dir=evidence_dir,
        prospective_lock_path=None,
    )
    output = tmp_path / "strict.json"
    output.write_text(
        json.dumps(
            {
                "schema_version": ("matchline.strict_report.legacy_research_only.v1"),
                "report_lane": "legacy_research_only",
                "formal_maturity_eligible": False,
                "combined": {"sample_n": 999999},
                "overall": {"production_allowed": False},
            }
        ),
        encoding="utf-8",
    )
    cache = tmp_path / "cache.json"
    strict_report.write_report_cache(cache, output, fingerprint)

    assert strict_report.load_cached_report(cache, output, fingerprint) is None


def test_formal_cache_rejects_missing_league_gates_and_forged_production_status(
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "raw-archive"
    _store_history(archive_root)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    fingerprint = strict_report.build_input_fingerprint(
        openfootball_raw_archive=archive_root,
        observed_before=OBSERVED_BEFORE,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        candidate_evidence_dir=evidence_dir,
        prospective_lock_path=None,
    )
    report = build_report(
        openfootball_raw_archive=archive_root,
        observed_before=OBSERVED_BEFORE,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        candidate_evidence_dir=evidence_dir,
        prospective_lock_path=None,
    )
    report["leagues"] = {}
    report["overall"] = {"production_allowed": True}
    output = tmp_path / "strict.json"
    output.write_text(json.dumps(report), encoding="utf-8")
    cache = tmp_path / "cache.json"
    strict_report.write_report_cache(cache, output, fingerprint)

    assert strict_report.load_cached_report(cache, output, fingerprint) is None


def test_formal_cli_generates_then_reuses_only_a_verified_raw_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive_root = tmp_path / "raw-archive"
    _store_history(archive_root)
    manifest_path = archive_root / "manifest.jsonl"
    manifest_before = manifest_path.read_bytes()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    output = tmp_path / "strict.json"
    cache = tmp_path / "strict.cache.json"
    argv = [
        "strict_report",
        "--openfootball-raw-archive",
        str(archive_root),
        "--observed-before",
        OBSERVED_BEFORE.isoformat(),
        "--openfootball-source-id",
        PREMIER_LEAGUE_SOURCE_ID,
        "--candidate-evidence-dir",
        str(evidence_dir),
        "--prospective-lock",
        str(tmp_path / "missing-lock.json"),
        "--cache-state",
        str(cache),
        "--output",
        str(output),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    strict_report.main()
    first_status = json.loads(capsys.readouterr().out)
    first_report = json.loads(output.read_text(encoding="utf-8"))
    strict_report.main()
    second_status = json.loads(capsys.readouterr().out)

    assert first_status["status"] == "generated"
    assert first_status["report_lane"] == "formal_v260"
    assert first_report["schema_version"] == "matchline.strict_report.v260"
    assert first_report["report_lane"] == "formal_v260"
    assert second_status["status"] == "skipped_unchanged"
    assert second_status["report_lane"] == "formal_v260"
    assert manifest_path.read_bytes() == manifest_before
