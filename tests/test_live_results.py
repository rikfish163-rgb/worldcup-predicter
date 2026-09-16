from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import league_platform.sources.live_results as live_results_module
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
    load_verified_openfootball_archive,
)
from league_platform.sources.live_results import load_live_results
from league_platform.source_rights import UseCase


OPENFOOTBALL_SOURCE_ID = "openfootball:football.json:2026-27:en.1"
RAW_OBSERVED_AT = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)
SNAPSHOT_AS_OF = "2026-08-30T18:05:00+00:00"
REFERENCE = datetime(2026, 8, 30, 19, 0, tzinfo=timezone.utc)


def _verified_openfootball_row(
    raw_archive_dir: Path,
    *,
    observed_at: datetime = RAW_OBSERVED_AT,
    score: tuple[int, int] = (2, 1),
    halftime: tuple[int, int] | None = (1, 0),
) -> dict:
    config = OPENFOOTBALL_SOURCES[OPENFOOTBALL_SOURCE_ID]
    score_payload: dict[str, list[int]] = {"ft": list(score)}
    if halftime is not None:
        score_payload["ht"] = list(halftime)
    payload = json.dumps(
        {
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-30",
                    "time": "16:00",
                    "team1": "Arsenal FC",
                    "team2": "Coventry City FC",
                    "score": score_payload,
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    OpenFootballRawArchive(raw_archive_dir).store(
        OpenFootballRawObservation(
            source_id=OPENFOOTBALL_SOURCE_ID,
            url=config["url"],
            retrieved_at=observed_at,
            payload=payload,
            source_format=config["format"],
            competition_id=config["competition_id"],
            season=config["season"],
            timezone_name=config["timezone"],
            license="CC0-1.0",
        )
    )
    candidate = load_verified_openfootball_archive(
        raw_archive_dir,
        source_ids=[OPENFOOTBALL_SOURCE_ID],
        observed_before=observed_at + timedelta(minutes=5),
    )
    return candidate["rows"][0]


def _write_fixture_snapshot(path: Path, fixture: dict, *, as_of: str = SNAPSHOT_AS_OF) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "as_of": as_of,
                "fixture_feed": {
                    "provider": str(fixture.get("source", {}).get("name") or "unknown"),
                    "fixtures": [fixture],
                    "errors": [],
                },
            }
        ),
        encoding="utf-8",
    )


def test_verified_openfootball_result_is_replayed_from_raw_without_mutating_archives(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    row = _verified_openfootball_row(raw_archive_dir)
    snapshot_path = tmp_path / "snapshots" / "raw" / "verified.json"
    _write_fixture_snapshot(snapshot_path, row)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=REFERENCE,
    )

    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match.id == row["id"]
    assert (match.score.home, match.score.away) == (2, 1)
    assert match.result_observed_at == RAW_OBSERVED_AT.isoformat()
    assert match.source_name == "OpenFootball"
    assert result["diagnostics"]["admission"] == {
        "snapshots_checked": 1,
        "candidate_rows": 1,
        "admitted_rows": 1,
        "quarantined_rows": 0,
        "excluded_display_only_rows": 0,
        "verified_archive_replays": 1,
        "quarantine_reasons": {},
        "exclusion_reasons": {},
    }
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    ("provider", "source_id"),
    [
        ("ESPN", "espn_schedule_summary"),
        ("Chinese Professional Football League official", "cfl_official_current"),
        ("football-data.co.uk China", "football_data_china_public_csv"),
        ("Operator self-declared feed", "operator_claimed_source"),
    ],
)
def test_blocked_or_unknown_provider_cannot_self_authorize_a_formal_result(
    tmp_path: Path,
    provider: str,
    source_id: str,
) -> None:
    fixture = {
        "id": f"blocked:{source_id}",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-30T15:00:00+00:00",
        "home_team": "Home",
        "away_team": "Away",
        "status": "finished",
        "score": {"home": 9, "away": 0},
        "source": {
            "name": provider,
            "source_id": source_id,
            "retrieved_at": RAW_OBSERVED_AT.isoformat(),
            "raw_sha256": "a" * 64,
            "authorization_reference": "operator-self-issued",
            "commercial_reuse_verified": True,
            "training_allowed": True,
        },
    }
    snapshot_path = tmp_path / "snapshots" / "raw" / "blocked.json"
    _write_fixture_snapshot(snapshot_path, fixture)

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=tmp_path / "missing-openfootball-raw",
        reference=REFERENCE,
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"] == {
        "snapshots_checked": 1,
        "candidate_rows": 1,
        "admitted_rows": 0,
        "quarantined_rows": 1,
        "excluded_display_only_rows": 0,
        "verified_archive_replays": 0,
        "quarantine_reasons": {"provider_not_openfootball": 1},
        "exclusion_reasons": {},
    }


def test_openligadb_result_remains_diagnostic_only_even_when_it_is_finished(
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / "snapshots" / "raw" / "openligadb.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": SNAPSHOT_AS_OF,
                "fixture_feed": {"provider": "OpenFootball", "fixtures": [], "errors": []},
                "openligadb": {
                    "provider": "OpenLigaDB",
                    "display_lane": "isolated_current_only",
                    "fixture_feed_eligible": False,
                    "material_feature_eligible": False,
                    "model_eligible": False,
                    "redistribution_allowed": False,
                    "training_eligible": False,
                    "model_rights": {"decision": "block"},
                    "training_rights": {"decision": "block"},
                    "redistribution_rights": {"decision": "block"},
                    "matches": [
                        {
                            "id": "openligadb:finished-1",
                            "competition_id": "bundesliga",
                            "kickoff_at": "2026-08-30T15:00:00+00:00",
                            "home_team": "Home",
                            "away_team": "Away",
                            "status": "finished",
                            "score": {"home": 8, "away": 0},
                            "display_lane": "isolated_current_only",
                            "enters_model": False,
                            "model_eligible": False,
                            "redistribution_allowed": False,
                            "training_eligible": False,
                            "source": {
                                "name": "OpenLigaDB",
                                "source_id": "openligadb_secondary_results",
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=tmp_path / "missing-openfootball-raw",
        reference=REFERENCE,
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"]["admitted_rows"] == 0
    assert result["diagnostics"]["admission"]["quarantined_rows"] == 0
    assert result["diagnostics"]["admission"]["excluded_display_only_rows"] == 1
    assert result["diagnostics"]["admission"]["exclusion_reasons"] == {
        "openligadb_display_only": 1
    }


def test_openligadb_injected_into_canonical_fixture_feed_is_quarantined(
    tmp_path: Path,
) -> None:
    fixture = {
        "id": "openligadb:injected",
        "competition_id": "bundesliga",
        "kickoff_at": "2026-08-30T15:00:00+00:00",
        "home_team": "Home",
        "away_team": "Away",
        "status": "finished",
        "score": {"home": 8, "away": 0},
        "source": {
            "name": "OpenLigaDB",
            "source_id": "openligadb_secondary_results",
            "commercial_reuse_verified": True,
            "training_allowed": True,
        },
    }
    _write_fixture_snapshot(tmp_path / "snapshots" / "raw" / "injected.json", fixture)

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=tmp_path / "missing-openfootball-raw",
        reference=REFERENCE,
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"]["quarantined_rows"] == 1
    assert result["diagnostics"]["admission"]["excluded_display_only_rows"] == 0
    assert result["diagnostics"]["admission"]["quarantine_reasons"] == {
        "provider_not_openfootball": 1
    }


def test_missing_or_tampered_raw_archive_fails_closed_without_repairing_it(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    row = _verified_openfootball_row(raw_archive_dir)
    snapshot_path = tmp_path / "snapshots" / "raw" / "result.json"
    _write_fixture_snapshot(snapshot_path, row)
    manifest = json.loads((raw_archive_dir / "manifest.jsonl").read_text())
    raw_path = raw_archive_dir / manifest["raw_path"]
    raw_path.write_bytes(b"X" * len(raw_path.read_bytes()))
    tampered = raw_path.read_bytes()

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=REFERENCE,
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"]["quarantine_reasons"] == {
        "openfootball_archive_verification_failed": 1
    }
    assert raw_path.read_bytes() == tampered

    missing = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=tmp_path / "absent-openfootball-raw",
        reference=REFERENCE,
    )
    assert missing["matches"] == []
    assert missing["diagnostics"]["admission"]["quarantine_reasons"] == {
        "openfootball_archive_verification_failed": 1
    }


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("score", "verified_openfootball_row_mismatch"),
        ("self_authorization", "verified_openfootball_row_mismatch"),
        ("source_time", "source_timestamp_mismatch"),
        ("pre_kickoff_source", "source_timestamp_before_kickoff"),
        ("source_after_snapshot", "source_timestamp_after_snapshot"),
        ("future_snapshot", "snapshot_observed_after_reference"),
    ],
)
def test_verified_result_mismatch_or_clock_conflict_is_quarantined(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    raw_observed_at = (
        datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)
        if mutation == "pre_kickoff_source"
        else RAW_OBSERVED_AT
    )
    row = _verified_openfootball_row(raw_archive_dir, observed_at=raw_observed_at)
    fixture = json.loads(json.dumps(row))
    as_of = SNAPSHOT_AS_OF
    reference = REFERENCE
    if mutation == "score":
        fixture["score"] = {"home": 3, "away": 1}
    elif mutation == "self_authorization":
        fixture["source"]["commercial_reuse_verified"] = True
        fixture["source"]["training_allowed"] = True
    elif mutation == "source_time":
        fixture["source"]["retrieved_at"] = "2026-08-30T18:01:00+00:00"
    elif mutation == "source_after_snapshot":
        as_of = "2026-08-30T17:00:00+00:00"
    elif mutation == "future_snapshot":
        as_of = "2026-08-30T20:00:00+00:00"
        reference = datetime(2026, 8, 30, 19, 0, tzinfo=timezone.utc)
    snapshot_path = tmp_path / "snapshots" / "raw" / f"{mutation}.json"
    _write_fixture_snapshot(snapshot_path, fixture, as_of=as_of)

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=reference,
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"]["admitted_rows"] == 0
    assert result["diagnostics"]["admission"]["quarantine_reasons"] == {reason: 1}


def test_load_live_results_is_deduplicated_and_causal(tmp_path: Path) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    row = _verified_openfootball_row(raw_archive_dir)
    snapshot_dir = tmp_path / "archive" / "raw"
    _write_fixture_snapshot(snapshot_dir / "early.json", row)
    _write_fixture_snapshot(
        snapshot_dir / "later.json",
        row,
        as_of="2026-08-30T18:10:00+00:00",
    )

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=REFERENCE,
    )

    assert result["diagnostics"]["candidate_rows"] == 2
    assert result["diagnostics"]["deduplicated_matches"] == 1
    match = result["matches"][0]
    assert match.result_observed_at == RAW_OBSERVED_AT.isoformat()
    assert match.score.home == 2


def test_self_declared_openfootball_result_without_raw_evidence_is_blocked(
    tmp_path: Path,
) -> None:
    fixture = {
        "id": "openfootball:self-declared",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-30T15:00:00+00:00",
        "home_team": "Home",
        "away_team": "Away",
        "status": "finished",
        "score": {"home": 2, "away": 1},
        "source": {
            "name": "OpenFootball",
            "source_id": OPENFOOTBALL_SOURCE_ID,
            "retrieved_at": RAW_OBSERVED_AT.isoformat(),
            "license": "CC0-1.0",
            "raw_sha256": "b" * 64,
        },
    }
    _write_fixture_snapshot(tmp_path / "archive" / "raw" / "self-declared.json", fixture)

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "archive",
        openfootball_raw_archive_dir=tmp_path / "missing-openfootball-raw",
        reference=REFERENCE,
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"]["quarantine_reasons"] == {
        "openfootball_archive_verification_failed": 1
    }


def test_load_live_results_quarantines_conflicting_verified_scores(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    first = _verified_openfootball_row(
        raw_archive_dir,
        observed_at=RAW_OBSERVED_AT,
        score=(2, 1),
    )
    corrected_observed_at = RAW_OBSERVED_AT + timedelta(days=1)
    corrected = _verified_openfootball_row(
        raw_archive_dir,
        observed_at=corrected_observed_at,
        score=(1, 1),
    )
    assert corrected["id"] == first["id"]
    snapshot_dir = tmp_path / "archive" / "raw"
    _write_fixture_snapshot(snapshot_dir / "first.json", first)
    _write_fixture_snapshot(
        snapshot_dir / "corrected.json",
        corrected,
        as_of=(corrected_observed_at + timedelta(minutes=5)).isoformat(),
    )

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=corrected_observed_at + timedelta(hours=1),
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"]["admitted_rows"] == 2
    assert result["diagnostics"]["conflict_count"] == 1


def test_load_live_results_preserves_verified_halftime_metadata(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    first = _verified_openfootball_row(
        raw_archive_dir,
        observed_at=RAW_OBSERVED_AT,
        halftime=None,
    )
    enriched_observed_at = RAW_OBSERVED_AT + timedelta(hours=2)
    enriched = _verified_openfootball_row(
        raw_archive_dir,
        observed_at=enriched_observed_at,
        halftime=(1, 0),
    )
    assert enriched["id"] == first["id"]
    snapshot_dir = tmp_path / "archive" / "raw"
    _write_fixture_snapshot(snapshot_dir / "early.json", first)
    _write_fixture_snapshot(
        snapshot_dir / "later.json",
        enriched,
        as_of=(enriched_observed_at + timedelta(minutes=5)).isoformat(),
    )

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=enriched_observed_at + timedelta(hours=1),
    )

    assert result["diagnostics"]["conflict_count"] == 0
    assert result["matches"][0].score.halftime_home == 1
    assert result["matches"][0].score.halftime_away == 0


def test_load_live_results_quarantines_snapshot_after_reference(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    row = _verified_openfootball_row(raw_archive_dir)
    _write_fixture_snapshot(tmp_path / "archive" / "raw" / "future.json", row)

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "archive",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=RAW_OBSERVED_AT - timedelta(minutes=1),
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"]["quarantine_reasons"] == {
        "snapshot_observed_after_reference": 1
    }


def test_same_kickoff_results_keep_one_verified_observation_batch(
    tmp_path: Path,
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    config = OPENFOOTBALL_SOURCES[OPENFOOTBALL_SOURCE_ID]
    payload = json.dumps(
        {
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-30",
                    "time": "16:00",
                    "team1": "Arsenal FC",
                    "team2": "Coventry City FC",
                    "score": {"ft": [2, 1], "ht": [1, 0]},
                },
                {
                    "round": "Matchday 1",
                    "date": "2026-08-30",
                    "time": "16:00",
                    "team1": "Liverpool FC",
                    "team2": "Everton FC",
                    "score": {"ft": [1, 1], "ht": [0, 0]},
                },
            ]
        },
        separators=(",", ":"),
    ).encode()
    OpenFootballRawArchive(raw_archive_dir).store(
        OpenFootballRawObservation(
            source_id=OPENFOOTBALL_SOURCE_ID,
            url=config["url"],
            retrieved_at=RAW_OBSERVED_AT,
            payload=payload,
            source_format=config["format"],
            competition_id=config["competition_id"],
            season=config["season"],
            timezone_name=config["timezone"],
            license="CC0-1.0",
        )
    )
    rows = load_verified_openfootball_archive(
        raw_archive_dir,
        source_ids=[OPENFOOTBALL_SOURCE_ID],
        observed_before=datetime.fromisoformat(SNAPSHOT_AS_OF),
    )["rows"]
    snapshot_path = tmp_path / "snapshots" / "raw" / "same-kickoff.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": SNAPSHOT_AS_OF,
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "fixtures": rows,
                    "errors": [],
                },
            }
        ),
        encoding="utf-8",
    )

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=REFERENCE,
    )

    assert result["diagnostics"]["admission"]["admitted_rows"] == 2
    assert len(result["matches"]) == 2
    assert len({match.kickoff_at for match in result["matches"]}) == 1
    assert {match.result_observed_at for match in result["matches"]} == {
        RAW_OBSERVED_AT.isoformat()
    }


def test_verified_replay_rechecks_training_and_redistribution_rights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    row = _verified_openfootball_row(raw_archive_dir)
    _write_fixture_snapshot(tmp_path / "snapshots" / "raw" / "result.json", row)
    observed_use_cases: list[UseCase] = []
    original = live_results_module.require_source_rights

    def recording_require(source_id: object, use_case: UseCase):
        observed_use_cases.append(use_case)
        return original(source_id, use_case)

    monkeypatch.setattr(live_results_module, "require_source_rights", recording_require)

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=REFERENCE,
    )

    assert len(result["matches"]) == 1
    assert observed_use_cases == [UseCase.TRAINING, UseCase.REDISTRIBUTION]


def test_any_central_rights_denial_quarantines_verified_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_archive_dir = tmp_path / "openfootball-raw"
    row = _verified_openfootball_row(raw_archive_dir)
    _write_fixture_snapshot(tmp_path / "snapshots" / "raw" / "result.json", row)
    original = live_results_module.require_source_rights

    def deny_redistribution(source_id: object, use_case: UseCase):
        if use_case is UseCase.REDISTRIBUTION:
            raise PermissionError("test rights denial")
        return original(source_id, use_case)

    monkeypatch.setattr(live_results_module, "require_source_rights", deny_redistribution)

    result = load_live_results(
        live_path=tmp_path / "missing-current.json",
        archive_dir=tmp_path / "snapshots",
        openfootball_raw_archive_dir=raw_archive_dir,
        reference=REFERENCE,
    )

    assert result["matches"] == []
    assert result["diagnostics"]["admission"]["quarantine_reasons"] == {
        "source_rights_not_allowed": 1
    }
