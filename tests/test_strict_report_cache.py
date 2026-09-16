from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from league_platform import strict_report
from league_platform.strict_report import (
    build_legacy_input_fingerprint,
    _independent_three_way_metrics,
    _scoreline_summary,
    load_cached_report,
    write_report_cache,
)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "E0_2526.csv").write_text(
        "Date,HomeTeam,AwayTeam\n01/01/25,A,B\n", encoding="utf-8"
    )
    kickoff = tmp_path / "kickoff.json"
    kickoff.write_text('{"rows": []}\n', encoding="utf-8")
    candidate_dir = tmp_path / "evidence"
    candidate_dir.mkdir()
    (candidate_dir / "model-candidate-test.json").write_text(
        '{"candidate": 1}\n', encoding="utf-8"
    )
    lock = tmp_path / "lock.json"
    lock.write_text('{"model_version_sha256": "a"}\n', encoding="utf-8")
    return data_dir, kickoff, candidate_dir, lock


def test_scoreline_summary_separates_frequency_result_from_market_availability() -> None:
    summary = _scoreline_summary(
        {
            "demo": {
                "sample_n": 2,
                "three_way": {
                    "independent_scoreline": {"market_sample_n": 0},
                },
                "targets": {
                    "scoreline": {
                        "sample_n": 2,
                        "log_loss": 0.5,
                        "frequency_log_loss": 0.7,
                    }
                },
            }
        }
    )

    assert summary["frequency_comparison"]["status"] == "beats_frequency"
    assert summary["frequency_comparison"]["model_minus_frequency_log_loss"] == pytest.approx(-0.2)
    assert summary["market_baseline"] == {
        "status": "unavailable_no_independent_market_baseline",
        "comparison_sample_n": 0,
    }


def test_legacy_input_fingerprint_changes_when_used_input_changes(
    tmp_path: Path,
) -> None:
    data_dir, kickoff, candidate_dir, lock = _inputs(tmp_path)
    first = build_legacy_input_fingerprint(
        data_dir,
        kickoff_enrichment_path=kickoff,
        candidate_evidence_dir=candidate_dir,
        prospective_lock_path=lock,
    )
    (data_dir / "E0_2526.csv").write_text(
        "Date,HomeTeam,AwayTeam\n01/01/25,A,B\n02/01/25,C,D\n", encoding="utf-8"
    )
    second = build_legacy_input_fingerprint(
        data_dir,
        kickoff_enrichment_path=kickoff,
        candidate_evidence_dir=candidate_dir,
        prospective_lock_path=lock,
    )
    assert first["sha256"] != second["sha256"]


def test_cache_requires_matching_output_hash_and_fingerprint(tmp_path: Path) -> None:
    data_dir, kickoff, candidate_dir, lock = _inputs(tmp_path)
    fingerprint = build_legacy_input_fingerprint(
        data_dir,
        kickoff_enrichment_path=kickoff,
        candidate_evidence_dir=candidate_dir,
        prospective_lock_path=lock,
    )
    output = tmp_path / "strict.json"
    cache = tmp_path / "strict-cache.json"
    report = {
        "schema_version": "matchline.strict_report.legacy_research_only.v1",
        "report_lane": "legacy_research_only",
        "formal_maturity_eligible": False,
        "combined": {"sample_n": 42},
        "leagues": {},
        "overall": {
            "production_allowed": False,
            "legacy_source_gate": "blocked",
        },
    }
    output.write_text(json.dumps(report) + "\n", encoding="utf-8")
    write_report_cache(cache, output, fingerprint)
    assert load_cached_report(cache, output, fingerprint) == report

    output.write_text(json.dumps({**report, "changed": True}) + "\n", encoding="utf-8")
    assert load_cached_report(cache, output, fingerprint) is None


def test_report_keeps_independent_scoreline_probability_separate_from_market_blend() -> None:
    rows = [
        {
            "outcome_1x2": "home",
            "independent_scoreline_probability": {"home": 0.60, "draw": 0.20, "away": 0.20},
            "market_probability": {"home": 0.50, "draw": 0.25, "away": 0.25},
        },
        {
            "outcome_1x2": "away",
            "independent_scoreline_probability": {"home": 0.25, "draw": 0.25, "away": 0.50},
            "market_probability": {"home": 0.30, "draw": 0.25, "away": 0.45},
        },
    ]

    result = _independent_three_way_metrics(rows)

    assert result["status"] == "evaluated"
    assert result["sample_n"] == 2
    assert result["market_sample_n"] == 2
    assert result["model"]["sample_n"] == 2
    assert result["model_on_market_sample"]["sample_n"] == 2
    assert result["model_minus_market_brier_ci"]["mean"] < 0


def test_legacy_cli_does_not_replay_optional_kickoff_enrichment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[Path | None] = []

    def fake_fingerprint(
        _data_dir: Path,
        *,
        kickoff_enrichment_path: Path | None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed.append(kickoff_enrichment_path)
        return {"sha256": "a" * 64}

    def fake_report(
        _data_dir: Path,
        *,
        kickoff_enrichment_path: Path | None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed.append(kickoff_enrichment_path)
        return {"combined": {"sample_n": 0}, "leagues": {}, "overall": {}}

    monkeypatch.setattr(strict_report, "build_legacy_input_fingerprint", fake_fingerprint)
    monkeypatch.setattr(strict_report, "build_legacy_research_report", fake_report)
    monkeypatch.setattr(strict_report, "_write_json_atomic", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "strict_report",
            "--legacy-research-only",
            "--data-dir",
            str(tmp_path / "data"),
            "--output",
            str(tmp_path / "strict.json"),
        ],
    )

    strict_report.main()

    assert observed == [None, None]
