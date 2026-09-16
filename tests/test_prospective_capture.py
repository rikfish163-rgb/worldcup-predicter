from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import league_platform.capture_prospective as capture_module
from league_platform.capture_prospective import (
    _blocked_diagnostics,
    _canonical_fixture_ids,
    _confirmed_lineup_observed_at,
    _current_match_to_domain,
    _evaluation_window_start,
    _within_prediction_horizon,
    _prune_replay_candidates,
    _select_archived_features,
    capture,
)
from league_platform.domain import Match
from league_platform.player_shadow import (
    build_player_availability_shadow_from_feature_snapshot,
)
from league_platform.strict_backtest import _lineup_confirmation_fingerprint


@pytest.mark.parametrize("mode", ["volatile", "missing", "malformed"])
def test_capture_rejects_invalid_raw_archive_before_read_or_append(
    tmp_path: Path,
    mode: str,
) -> None:
    output = tmp_path / "predictions.jsonl"
    output.write_bytes(b"existing-predictions\n")

    with TemporaryDirectory(prefix="matchline-invalid-raw-", dir="/tmp") as directory:
        raw_archive_dir = (
            Path("/dev/shm/forged-openfootball-raw")
            if mode == "volatile"
            else Path(directory) / mode
        )
        if mode == "malformed":
            raw_archive_dir.mkdir()
            (raw_archive_dir / "manifest.jsonl").write_text("{", encoding="utf-8")
        with pytest.raises(ValueError, match="durable"):
            capture(
                live_path=tmp_path / "missing-current.json",
                output=output,
                openfootball_raw_archive_dir=raw_archive_dir,
            )

    assert output.read_bytes() == b"existing-predictions\n"


def test_capture_cli_raw_archive_precedence_is_flag_then_env_then_runtime_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[Path] = []

    def fake_capture(**kwargs):
        captured.append(kwargs["openfootball_raw_archive_dir"])
        return {"predictions": 0}

    monkeypatch.setattr(capture_module, "capture", fake_capture)
    monkeypatch.setenv(
        "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR",
        str(tmp_path / "from-env"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture-prospective",
            "--archive-dir",
            str(tmp_path / "runtime" / "archive"),
            "--openfootball-raw-archive-dir",
            str(tmp_path / "from-flag"),
        ],
    )
    capture_module.main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture-prospective",
            "--archive-dir",
            str(tmp_path / "runtime" / "archive"),
        ],
    )
    capture_module.main()
    monkeypatch.delenv("MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR")
    capture_module.main()

    assert captured == [
        tmp_path / "from-flag",
        tmp_path / "from-env",
        tmp_path / "runtime" / "openfootball-raw",
    ]
    assert len(capsys.readouterr().out.splitlines()) == 3


def test_capture_forwards_verified_raw_archive_and_snapshot_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    as_of = datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc)
    raw_archive_dir = Path("/tmp/matchline-verified-openfootball-raw")
    live_path = tmp_path / "current.json"
    live_path.write_text(
        json.dumps(
            {"as_of": as_of.isoformat(), "expected_competitions": []}
        ),
        encoding="utf-8",
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        capture_module,
        "require_durable_openfootball_raw_archive",
        lambda path: Path(path),
    )
    monkeypatch.setattr(capture_module, "_load_lock", lambda _path: {})
    class FakeVerifiedHistory:
        def __init__(self, archive_dir, *, observed_before, source_ids):
            assert Path(archive_dir) == raw_archive_dir
            assert observed_before == as_of
            assert source_ids

        def admission_manifest(self):
            return {"competition_ids": []}

    monkeypatch.setattr(capture_module, "VerifiedOpenFootballHistorySource", FakeVerifiedHistory)
    monkeypatch.setattr(
        capture_module,
        "attach_current_data",
        lambda *_args, **_kwargs: {
            "competitions": [],
            "matches": [],
            "current_data": {"status": "ok"},
        },
    )

    def fake_live_results(**kwargs):
        observed.update(kwargs)
        return {"matches": [], "diagnostics": {"admitted_rows": 0}}

    monkeypatch.setattr(capture_module, "load_live_results", fake_live_results)
    monkeypatch.setattr(
        capture_module,
        "_select_archived_features",
        lambda *_args, **_kwargs: ({}, {}, {}),
    )
    monkeypatch.setattr(
        capture_module,
        "build_strict_prospective_predictions",
        lambda *_args, **_kwargs: {"predictions": [], "blocked": []},
    )
    monkeypatch.setattr(
        capture_module,
        "append_predictions",
        lambda *_args, **_kwargs: {"appended": 0},
    )

    result = capture(
        live_path=live_path,
        archive_dir=tmp_path / "archive",
        output=tmp_path / "predictions.jsonl",
        openfootball_raw_archive_dir=raw_archive_dir,
    )

    assert observed["openfootball_raw_archive_dir"] == raw_archive_dir
    assert observed["reference"] == as_of
    assert result["predictions"] == 0


def test_formal_capture_never_reads_legacy_matchhistory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    as_of = datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc)
    live_path = tmp_path / "current.json"
    live_path.write_text(
        json.dumps(
            {
                "as_of": as_of.isoformat(),
                "expected_competitions": ["premier-league"],
            }
        ),
        encoding="utf-8",
    )
    raw_archive_dir = Path("/tmp/matchline-verified-openfootball-raw")
    legacy_dir = tmp_path / "legacy-matchhistory"

    class FakeVerifiedHistory:
        def __init__(self, archive_dir, *, observed_before, source_ids):
            assert Path(archive_dir) == raw_archive_dir
            assert observed_before == as_of
            assert source_ids

        def admission_manifest(self):
            return {"competition_ids": ["premier-league"]}

        def load(self, _competition_id):
            return type("Result", (), {"matches": []})()

    monkeypatch.setattr(
        capture_module,
        "require_durable_openfootball_raw_archive",
        lambda path: Path(path),
    )
    monkeypatch.setattr(capture_module, "_load_lock", lambda _path: {})
    monkeypatch.setattr(capture_module, "VerifiedOpenFootballHistorySource", FakeVerifiedHistory)
    monkeypatch.setattr(
        capture_module,
        "attach_current_data",
        lambda *_args, **_kwargs: {
            "competitions": [{"id": "premier-league"}],
            "matches": [],
            "current_data": {"status": "ok"},
        },
    )
    monkeypatch.setattr(
        capture_module,
        "load_live_results",
        lambda **_kwargs: {"matches": [], "diagnostics": {}},
    )
    monkeypatch.setattr(
        capture_module,
        "_select_archived_features",
        lambda *_args, **_kwargs: ({}, {}, {}),
    )
    monkeypatch.setattr(
        capture_module,
        "build_strict_prospective_predictions",
        lambda *_args, **_kwargs: {"predictions": [], "blocked": []},
    )
    monkeypatch.setattr(
        capture_module,
        "append_predictions",
        lambda *_args, **_kwargs: {"appended": 0},
    )

    result = capture(
        data_dir=legacy_dir,
        live_path=live_path,
        archive_dir=tmp_path / "archive",
        output=tmp_path / "predictions.jsonl",
        openfootball_raw_archive_dir=raw_archive_dir,
    )

    assert result["predictions"] == 0


def test_prediction_capture_window_is_strictly_bounded_to_next_seven_days():
    as_of = datetime(2026, 8, 24, tzinfo=timezone.utc)
    assert _within_prediction_horizon(as_of + timedelta(days=7), as_of=as_of) is True
    assert (
        _within_prediction_horizon(
            as_of + timedelta(days=7, seconds=1),
            as_of=as_of,
        )
        is False
    )


def test_openfootball_fixture_is_a_provider_neutral_capture_candidate(tmp_path: Path):
    fixture_id = "openfootball:premier-league:abc123"
    live = {
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [{"id": fixture_id}],
            "errors": [],
        },
        # A blocked legacy envelope may coexist with the canonical feed, but
        # must not control which fixtures are frozen.
        "espn": {"provider": "ESPN", "fixtures": [], "errors": []},
    }
    assert _canonical_fixture_ids(live) == {fixture_id}

    match = {
        "id": fixture_id,
        "competition_id": "premier-league",
        "season": "2026-27",
        "kickoff_at": "2026-08-24T19:00:00+00:00",
        "home_team": "Home",
        "away_team": "Away",
        "home_team_id": "premier-league:home",
        "away_team_id": "premier-league:away",
        "status": "upcoming",
        "score": None,
        "kickoff_time_quality": "exact",
        "kickoff_time_source": "Premier League official",
        "kickoff_time_observed_at": "2026-08-24T00:30:00+00:00",
        "source": {
            "name": "OpenFootball",
            "source_id": "openfootball_en1_json_2026_27",
            "retrieved_at": "2026-08-24T01:00:00+00:00",
            "raw_sha256": "a" * 64,
            "license": "CC0-1.0",
        },
    }
    domain = _current_match_to_domain(match, live_path=tmp_path / "current.json")

    assert domain.id == fixture_id
    assert domain.source_name == "OpenFootball"
    assert domain.source_license_status == "CC0-1.0"
    assert domain.kickoff_time_source == "Premier League official"
    assert domain.kickoff_time_observed_at == "2026-08-24T00:30:00+00:00"


def _complete_team_status(
    retrieved_at: str,
    *,
    player_prefix: str = "event",
) -> dict:
    return {
        "confirmed": True,
        "model_eligible": True,
        "status": "confirmed_lineup",
        "source": {"retrieved_at": retrieved_at},
        "teams": {
            side: {
                "players": [
                    {
                        "player_id": f"{player_prefix}-{side}-{index}",
                        "status": "starter",
                        "starter": True,
                    }
                    for index in range(11)
                ]
            }
            for side in ("home", "away")
        },
    }


def test_archive_feature_selection_is_cutoff_bounded_and_latest_wins(tmp_path: Path, monkeypatch):
    archive = tmp_path / "archive"
    raw = archive / "raw"
    raw.mkdir(parents=True)
    live = tmp_path / "current.json"
    kickoff = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    reference = datetime(2026, 8, 14, 11, 30, tzinfo=timezone.utc)
    match = Match(
        id="espn:archive-selection",
        competition_id="premier-league",
        season="2026",
        kickoff_at=kickoff,
        home_team="Home",
        away_team="Away",
        home_team_id="home",
        away_team_id="away",
        status="upcoming",
        score=None,
        market_probability=None,
        source_file=str(live),
        source_sha256="a" * 64,
        provider_fixture_id="archive-selection",
        source_name="ESPN",
        source_license_status="test",
    )
    snapshots = {
        "old.json": "2026-08-12T23:00:00+00:00",
        "mid.json": "2026-08-14T05:00:00+00:00",
        "new.json": "2026-08-14T11:00:00+00:00",
    }
    for name, as_of in snapshots.items():
        (raw / name).write_text(json.dumps({"as_of": as_of}), encoding="utf-8")
    live.write_text(json.dumps({"as_of": reference.isoformat()}), encoding="utf-8")

    def fake_attach(_base: dict, path: Path, *, now: datetime):
        assert _base["matches"] == []
        assert _base["competitions"] == [{"id": "premier-league"}]
        return {
            "matches": [
                {
                    "id": match.id,
                    "kickoff_at": match.kickoff_at.isoformat(),
                    "kickoff_time_quality": "exact",
                    "kickoff_time_source": "fixture source",
                    "kickoff_time_observed_at": now.isoformat(),
                    "current_features": {"marker": path.name, "observed_at": now.isoformat()},
                }
            ]
        }

    monkeypatch.setattr("league_platform.capture_prospective.attach_current_data", fake_attach)
    selected, lineups, diagnostics = _select_archived_features(
        {
            "schema_version": "1.1.0",
            "generated_at": reference.isoformat(),
            "timezone": "Asia/Shanghai",
            "summary": {"finished_matches": 99999},
            "competitions": [
                {"id": "premier-league", "model_health": {"large": "payload"}}
            ],
            "matches": [{"id": "historic-match"}],
        },
        [match],
        live_path=live,
        archive_dir=archive,
        reference=reference,
    )

    assert lineups == {}
    assert diagnostics["candidate_count"] == 4
    assert diagnostics["attached_count"] == 4
    assert diagnostics["selected_stage_features"] == 3
    by_stage = selected[match.id]["by_stage"]
    assert by_stage["t_minus_24h"]["marker"] == "old.json"
    assert by_stage["t_minus_6h"]["marker"] == "mid.json"
    assert by_stage["t_minus_90m"]["marker"] == "mid.json"
    assert selected[match.id]["fixture_observed_at_by_stage"] == {
        "t_minus_24h": "2026-08-12T23:00:00+00:00",
        "t_minus_6h": "2026-08-14T05:00:00+00:00",
        "t_minus_90m": "2026-08-14T05:00:00+00:00",
    }
    assert selected[match.id]["fixture_by_stage"] == {
        "t_minus_24h": {
            "kickoff_at": kickoff.isoformat(),
            "kickoff_time_quality": "exact",
            "kickoff_time_source": "fixture source",
            "kickoff_time_observed_at": "2026-08-12T23:00:00+00:00",
        },
        "t_minus_6h": {
            "kickoff_at": kickoff.isoformat(),
            "kickoff_time_quality": "exact",
            "kickoff_time_source": "fixture source",
            "kickoff_time_observed_at": "2026-08-14T05:00:00+00:00",
        },
        "t_minus_90m": {
            "kickoff_at": kickoff.isoformat(),
            "kickoff_time_quality": "exact",
            "kickoff_time_source": "fixture source",
            "kickoff_time_observed_at": "2026-08-14T05:00:00+00:00",
        },
    }


def test_archive_feature_selection_replays_lineup_stage_at_observation_boundary(
    tmp_path: Path, monkeypatch
):
    archive = tmp_path / "archive"
    raw = archive / "raw"
    raw.mkdir(parents=True)
    live = tmp_path / "current.json"
    kickoff = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    reference = datetime(2026, 8, 14, 11, 30, tzinfo=timezone.utc)
    match = Match(
        id="espn:lineup-selection",
        competition_id="premier-league",
        season="2026",
        kickoff_at=kickoff,
        home_team="Home",
        away_team="Away",
        home_team_id="home",
        away_team_id="away",
        status="upcoming",
        score=None,
        market_probability=None,
        source_file=str(live),
        source_sha256="b" * 64,
        provider_fixture_id="lineup-selection",
        source_name="ESPN",
        source_license_status="test",
    )
    snapshots = {
        "old.json": "2026-08-12T23:00:00+00:00",
        "lineup.json": "2026-08-14T05:00:00+00:00",
        "late.json": "2026-08-14T11:00:00+00:00",
    }
    for name, as_of in snapshots.items():
        (raw / name).write_text(json.dumps({"as_of": as_of}), encoding="utf-8")
    live.write_text(json.dumps({"as_of": reference.isoformat()}), encoding="utf-8")

    lineup_observed_at = "2026-08-14T05:30:00+00:00"

    def fake_attach(_base: dict, path: Path, *, now: datetime):
        assert _base["matches"] == []
        assert _base["competitions"] == [{"id": "premier-league"}]
        features = {"marker": path.name, "observed_at": now.isoformat()}
        if path.name != "old.json":
            features["team_status"] = _complete_team_status(lineup_observed_at)
        return {"matches": [{"id": match.id, "current_features": features}]}

    monkeypatch.setattr("league_platform.capture_prospective.attach_current_data", fake_attach)
    selected, lineups, diagnostics = _select_archived_features(
        {
            "schema_version": "1.1.0",
            "generated_at": reference.isoformat(),
            "timezone": "Asia/Shanghai",
            "summary": {},
            "competitions": [{"id": "premier-league"}],
            "matches": [],
        },
        [match],
        live_path=live,
        archive_dir=archive,
        reference=reference,
    )

    assert lineups[match.id] == datetime(2026, 8, 14, 5, 30, tzinfo=timezone.utc)
    assert diagnostics["candidate_count"] == 4
    by_stage = selected[match.id]["by_stage"]
    assert by_stage["lineup_confirmation"]["marker"] == "lineup.json"
    assert (
        selected[match.id]["source_as_of_by_stage"]["lineup_confirmation"]
        == "2026-08-14T05:00:00+00:00"
    )


def test_archive_feature_selection_does_not_borrow_snapshot_after_lineup_observation(
    tmp_path: Path, monkeypatch
):
    archive = tmp_path / "archive"
    raw = archive / "raw"
    raw.mkdir(parents=True)
    live = tmp_path / "current.json"
    kickoff = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    reference = datetime(2026, 8, 14, 11, 30, tzinfo=timezone.utc)
    match = Match(
        id="espn:lineup-after-snapshot",
        competition_id="premier-league",
        season="2026",
        kickoff_at=kickoff,
        home_team="Home",
        away_team="Away",
        home_team_id="home",
        away_team_id="away",
        status="upcoming",
        score=None,
        market_probability=None,
        source_file=str(live),
        source_sha256="c" * 64,
        provider_fixture_id="lineup-after-snapshot",
        source_name="ESPN",
        source_license_status="test",
    )
    snapshots = {
        "old.json": "2026-08-13T23:00:00+00:00",
        "prelineup.json": "2026-08-14T05:00:00+00:00",
        # The provider timestamp says the lineup was observed at 05:30, but
        # the poll carrying that response was archived at 06:00.
        "lineup.json": "2026-08-14T06:00:00+00:00",
        "late.json": "2026-08-14T11:00:00+00:00",
    }
    for name, as_of in snapshots.items():
        (raw / name).write_text(json.dumps({"as_of": as_of}), encoding="utf-8")
    live.write_text(json.dumps({"as_of": reference.isoformat()}), encoding="utf-8")

    lineup_observed_at = "2026-08-14T05:30:00+00:00"

    def fake_attach(_base: dict, path: Path, *, now: datetime):
        assert _base["matches"] == []
        assert _base["competitions"] == [{"id": "premier-league"}]
        features = {"marker": path.name, "observed_at": now.isoformat()}
        if path.name in {"lineup.json", "late.json"}:
            features["team_status"] = {
                "confirmed": True,
                "model_eligible": True,
                "source": {"retrieved_at": lineup_observed_at},
                "teams": {
                    "home": {
                        "players": [
                            {"player_id": f"home-{index}", "starter": True}
                            for index in range(11)
                        ]
                    },
                    "away": {
                        "players": [
                            {"player_id": f"away-{index}", "starter": True}
                            for index in range(11)
                        ]
                    },
                },
            }
        return {"matches": [{"id": match.id, "current_features": features}]}

    monkeypatch.setattr("league_platform.capture_prospective.attach_current_data", fake_attach)
    selected, lineups, diagnostics = _select_archived_features(
        {
            "schema_version": "1.1.0",
            "generated_at": reference.isoformat(),
            "timezone": "Asia/Shanghai",
            "summary": {},
            "competitions": [{"id": "premier-league"}],
            "matches": [],
        },
        [match],
        live_path=live,
        archive_dir=archive,
        reference=reference,
    )

    assert lineups[match.id] == datetime(2026, 8, 14, 5, 30, tzinfo=timezone.utc)
    assert diagnostics["lineup_replay_attached_count"] == 2
    assert selected[match.id]["by_stage"]["t_minus_6h"]["marker"] == "lineup.json"
    lineup_features = selected[match.id]["by_stage"]["lineup_confirmation"]
    assert lineup_features["marker"] == "prelineup.json"
    assert lineup_features["team_status"]["source"]["retrieved_at"] == lineup_observed_at
    assert len(lineup_features["team_status"]["teams"]["home"]["players"]) == 11
    assert len(lineup_features["team_status"]["teams"]["away"]["players"]) == 11
    assert lineup_features["lineup_confirmation_event"] == {
        "observed_at": lineup_observed_at,
        "provider_key": "team_status",
    }
    assert diagnostics["lineup_event_overlay_count"] == 1
    assert (
        selected[match.id]["source_as_of_by_stage"]["lineup_confirmation"]
        == "2026-08-14T05:00:00+00:00"
    )


def test_lineup_event_can_freeze_from_its_own_clock_without_later_snapshot_fields(
    tmp_path: Path, monkeypatch
):
    archive = tmp_path / "archive"
    (archive / "raw").mkdir(parents=True)
    live = tmp_path / "current.json"
    reference = datetime(2026, 8, 14, 6, 0, tzinfo=timezone.utc)
    kickoff = datetime(2026, 8, 14, 7, 0, tzinfo=timezone.utc)
    lineup_observed_at = "2026-08-14T05:30:00+00:00"
    live.write_text(json.dumps({"as_of": reference.isoformat()}), encoding="utf-8")
    match = Match(
        id="openfootball:event-only-lineup",
        competition_id="premier-league",
        season="2026",
        kickoff_at=kickoff,
        home_team="Home",
        away_team="Away",
        home_team_id="home",
        away_team_id="away",
        status="upcoming",
        score=None,
        market_probability=None,
        source_file=str(live),
        source_sha256="d" * 64,
        provider_fixture_id="event-only-lineup",
        source_name="OpenFootball",
        source_license_status="CC0-1.0",
    )

    def fake_attach(_base: dict, path: Path, *, now: datetime):
        assert path == live
        return {
            "matches": [
                {
                    "id": match.id,
                    "current_features": {
                        "late_marker": now.isoformat(),
                        "team_status": _complete_team_status(lineup_observed_at),
                    },
                }
            ]
        }

    monkeypatch.setattr("league_platform.capture_prospective.attach_current_data", fake_attach)
    selected, lineups, diagnostics = _select_archived_features(
        {
            "schema_version": "1.1.0",
            "generated_at": reference.isoformat(),
            "timezone": "Asia/Shanghai",
            "summary": {},
            "competitions": [{"id": "premier-league"}],
            "matches": [],
        },
        [match],
        live_path=live,
        archive_dir=archive,
        reference=reference,
    )

    stage = selected[match.id]["by_stage"]["lineup_confirmation"]
    assert lineups[match.id] == datetime(2026, 8, 14, 5, 30, tzinfo=timezone.utc)
    assert "late_marker" not in stage
    assert stage["team_status"]["source"]["retrieved_at"] == lineup_observed_at
    assert stage["lineup_confirmation_event"] == {
        "observed_at": lineup_observed_at,
        "provider_key": "team_status",
    }
    assert _lineup_confirmation_fingerprint(stage) is not None
    assert diagnostics["lineup_replay_attached_count"] == 0
    assert diagnostics["lineup_event_overlay_count"] == 1


def test_capture_lock_window_parser_is_timezone_aware():
    start = _evaluation_window_start({"evaluation_window_started_at": "2026-08-22T00:00:00+00:00"})
    assert start == datetime(2026, 8, 22, tzinfo=timezone.utc)


def test_blocked_diagnostics_preserve_reasons_and_counts():
    result = _blocked_diagnostics(
        [
            {"reason": "no_freeze_cutoff_observed_before_as_of", "competition_id": "la-liga"},
            {"reason": "no_freeze_cutoff_observed_before_as_of", "competition_id": "serie-a"},
            {"reason": "not_enough_historical_seasons", "competition_id": "new-league"},
        ]
    )
    assert result["count"] == 3
    assert result["by_reason"] == {
        "no_freeze_cutoff_observed_before_as_of": 2,
        "not_enough_historical_seasons": 1,
    }
    assert result["items"][0]["competition_id"] == "la-liga"


def test_large_archive_replay_keeps_latest_predecessor_per_due_cutoff():
    reference = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    kickoff = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
    match = Match(
        id="espn:prune",
        competition_id="premier-league",
        season="2026",
        kickoff_at=kickoff,
        home_team="Home",
        away_team="Away",
        home_team_id="home",
        away_team_id="away",
        status="upcoming",
        score=None,
        market_probability=None,
        source_file="current.json",
        source_sha256="d" * 64,
        provider_fixture_id="prune",
        source_name="ESPN",
        source_license_status="test",
    )
    candidates = [
        (datetime(2026, 8, 18, tzinfo=timezone.utc) + timedelta(minutes=15 * index), Path(f"{index}.json"))
        for index in range(240)
    ]

    replay, pruned = _prune_replay_candidates(
        candidates,
        [match],
        reference=reference,
    )

    assert pruned is True
    assert len(replay) == 2
    assert replay[-1] == candidates[-1]
    # The 24-hour predecessor is retained independently of the latest
    # snapshot; this is the only candidate that can win the due clock stage.
    cutoff = kickoff - timedelta(hours=24)
    expected = next(candidate for candidate in reversed(candidates) if candidate[0] <= cutoff)
    assert expected in replay


def test_confirmed_espn_public_provider_lineup_is_causal_only_before_kickoff():
    kickoff = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    reference = datetime(2026, 8, 14, 11, 30, tzinfo=timezone.utc)
    features = {
        "team_status": _complete_team_status("2026-08-14T11:00:00+00:00")
    }
    assert _confirmed_lineup_observed_at(
        features,
        kickoff_at=kickoff,
        reference=reference,
    ) == datetime(2026, 8, 14, 11, 0, tzinfo=timezone.utc)

    features["team_status"]["model_eligible"] = False
    assert _confirmed_lineup_observed_at(
        features,
        kickoff_at=kickoff,
        reference=reference,
    ) is None


def test_confirmed_espn_lineup_accepts_normalized_row_observation_clock():
    kickoff = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    reference = datetime(2026, 8, 14, 11, 30, tzinfo=timezone.utc)
    team_status = _complete_team_status("2026-08-14T11:00:00+00:00")
    team_status["retrieved_at"] = team_status["source"].pop("retrieved_at")
    team_status["source"]["raw_sha256"] = "a" * 64
    features = {"team_status": team_status}
    assert _confirmed_lineup_observed_at(
        features,
        kickoff_at=kickoff,
        reference=reference,
    ) == datetime(2026, 8, 14, 11, 0, tzinfo=timezone.utc)


def test_confirmed_marker_without_complete_bilateral_xi_is_not_a_freeze_event():
    kickoff = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    reference = datetime(2026, 8, 14, 11, 30, tzinfo=timezone.utc)
    incomplete = _complete_team_status("2026-08-14T11:00:00+00:00")
    incomplete["teams"]["away"]["players"].pop()

    assert _confirmed_lineup_observed_at(
        {"team_status": incomplete},
        kickoff_at=kickoff,
        reference=reference,
    ) is None


def test_lineup_stage_derivations_bind_to_the_exact_confirmation_event():
    observed_at = "2026-08-14T11:00:00+00:00"
    event = _complete_team_status(observed_at, player_prefix="espn")
    competing_provider = {
        "source": {"retrieved_at": "2026-08-14T10:59:00+00:00"},
        "lineups": {
            "confirmed": True,
            "model_eligible": True,
            "home": {
                "players": [
                    {"player_id": f"official-home-{index}", "starter": True}
                    for index in range(11)
                ]
            },
            "away": {
                "players": [
                    {"player_id": f"official-away-{index}", "starter": True}
                    for index in range(11)
                ]
            },
        },
    }
    features = {
        "official_lineup": competing_provider,
        "team_status": event,
        "lineup_confirmation_event": {
            "provider_key": "team_status",
            "observed_at": observed_at,
        },
    }

    assert _lineup_confirmation_fingerprint(features) == _lineup_confirmation_fingerprint(
        {"team_status": event}
    )
    shadow = build_player_availability_shadow_from_feature_snapshot(
        features,
        as_of=observed_at,
        kickoff_at="2026-08-14T12:00:00+00:00",
    )
    assert shadow["observed_at"] == observed_at
    assert shadow["teams"]["home"]["starter_count"] == 11
    assert shadow["teams"]["away"]["starter_count"] == 11


def test_lineup_fingerprint_is_cross_provider_when_shirt_numbers_prove_same_xi():
    observed_at = "2026-08-23T14:04:15+00:00"
    official_sides = {
        side: {
            "players": [
                {
                    "player_id": f"laliga-{side}-{index}",
                    "provider_player_id": f"official-{side}-{index}",
                    "name": (
                        "Jorge Resurreccion Merodio"
                        if side == "home" and index == 5
                        else f"Official {side} player {index}"
                    ),
                    "shirt_number": index + 1,
                    "starter": True,
                }
                for index in range(11)
            ]
        }
        for side in ("home", "away")
    }
    official = {
        "lineups": {
            "confirmed": True,
            "model_eligible": True,
            **official_sides,
        },
        "source": {"retrieved_at": observed_at},
    }
    fallback_sides = {
        side: {
            "players": list(
                reversed(
                    [
                        {
                            "player_id": f"espn-{side}-{index}",
                            "name": (
                                "Koke"
                                if side == "home" and index == 5
                                else f"ESPN {side} player {index}"
                            ),
                            "shirt_number": str(index + 1),
                            "starter": True,
                        }
                        for index in range(11)
                    ]
                )
            )
        }
        for side in ("home", "away")
    }
    fallback = {
        "confirmed": True,
        "model_eligible": True,
        "source": {"retrieved_at": "2026-08-23T14:30:23+00:00"},
        "teams": fallback_sides,
    }

    official_fingerprint = _lineup_confirmation_fingerprint(
        {
            "official_lineup": official,
            "lineup_confirmation_event": {
                "provider_key": "official_lineup",
                "observed_at": observed_at,
            },
        }
    )
    fallback_fingerprint = _lineup_confirmation_fingerprint(
        {
            "team_status": fallback,
            "lineup_confirmation_event": {
                "provider_key": "team_status",
                "observed_at": "2026-08-23T14:30:23+00:00",
            },
        }
    )

    assert official_fingerprint == fallback_fingerprint

    fallback["teams"]["home"]["players"][0]["shirt_number"] = "99"
    assert _lineup_confirmation_fingerprint(
        {
            "team_status": fallback,
            "lineup_confirmation_event": {
                "provider_key": "team_status",
                "observed_at": "2026-08-23T14:30:23+00:00",
            },
        }
    ) != official_fingerprint
