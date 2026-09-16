from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

import league_platform.future as future_module
from league_platform.future import build_future_predictions
from league_platform.identity import team_id
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_SOURCES,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
)
from league_platform.store import PlatformStore


CUTOFF = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
HISTORY_SOURCE_IDS = (
    "openfootball:england:2015-16:1-premierleague",
    "openfootball:england:2016-17:1-premierleague",
    "openfootball:england:2017-18:1-premierleague",
)
CURRENT_SOURCE_ID = "openfootball:football.json:2026-27:en.1"
CHAMPIONSHIP_CURRENT_SOURCE_ID = "openfootball:football.json:2026-27:en.2"


@pytest.fixture
def durable_archive_dir() -> Iterator[Path]:
    # The product rejects /dev/shm as volatile.  Test raw evidence therefore
    # lives in a tiny, automatically removed durable /tmp directory even when
    # pytest's own TMPDIR points at /dev/shm.
    with TemporaryDirectory(prefix="matchline-future-v260-", dir="/tmp") as raw:
        yield Path(raw) / "archive"


def _store_observation(
    root: Path,
    *,
    source_id: str,
    payload: bytes,
    observed_at: datetime,
) -> None:
    config = OPENFOOTBALL_SOURCES[source_id]
    OpenFootballRawArchive(root).store(
        OpenFootballRawObservation(
            source_id=source_id,
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


def _history_payload(season: str, date_line: str, score: str) -> bytes:
    return (
        f"= English Premier League {season}\n"
        "▪ Matchday 1\n"
        f"{date_line}\n"
        f"  12:45  Arsenal FC  {score} (1-0)  Manchester City FC\n"
    ).encode()


def _current_payload(
    *,
    kickoff_date: str = "2026-08-30",
    kickoff_time: str = "16:00",
    home: str = "Coventry City FC",
    away: str = "Arsenal FC",
) -> bytes:
    return json.dumps(
        {
            "matches": [
                {
                    "date": kickoff_date,
                    "time": kickoff_time,
                    "round": "Matchday 1",
                    "team1": home,
                    "team2": away,
                }
            ]
        },
        sort_keys=True,
    ).encode()


def _archive(
    root: Path,
    *,
    current_source_id: str = CURRENT_SOURCE_ID,
    current_payload: bytes | None = None,
    observed_at: datetime = CUTOFF - timedelta(hours=1),
) -> None:
    rows = (
        (HISTORY_SOURCE_IDS[0], "2015/16", "Sat Aug 8 2015", "2-1"),
        (HISTORY_SOURCE_IDS[1], "2016/17", "Sat Aug 13 2016", "1-1"),
        (HISTORY_SOURCE_IDS[2], "2017/18", "Sat Aug 12 2017", "2-1"),
    )
    for source_id, season, date_line, score in rows:
        _store_observation(
            root,
            source_id=source_id,
            payload=_history_payload(season, date_line, score),
            observed_at=observed_at,
        )
    _store_observation(
        root,
        source_id=current_source_id,
        payload=current_payload or _current_payload(),
        observed_at=observed_at,
    )


@pytest.fixture
def verified_archive(durable_archive_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _archive(durable_archive_dir)
    monkeypatch.setattr(future_module, "OPENFOOTBALL_HISTORY_SOURCE_IDS", HISTORY_SOURCE_IDS)
    monkeypatch.setattr(future_module, "OPENFOOTBALL_CURRENT_SOURCE_IDS", (CURRENT_SOURCE_ID,))
    return durable_archive_dir


def _snapshot(*, injected_matches: list[dict] | None = None) -> dict:
    return {
        "current_data": {
            "status": "fresh",
            "as_of": CUTOFF.isoformat(),
            "trusted": True,
            "authorization_reference": "self-issued-allow",
        },
        "matches": list(injected_matches or []),
        "competitions": [
            {
                "id": "premier-league",
                "model_health": {
                    "status": "evaluated",
                    "data_cutoff": (CUTOFF - timedelta(days=1)).isoformat(),
                    "home_advantage_elo": 999999,
                    "draw_rate": 0.99,
                    "calibration_alpha": 1,
                    "calibration_prior": [0.99, 0.005, 0.005],
                    "dixon_coles": {
                        "home_goal_average": 99,
                        "away_goal_average": 0.01,
                        "rho": -0.99,
                        "calibration_alpha": 1,
                        "calibration_prior": [0.99, 0.005, 0.005],
                    },
                },
            }
        ],
        "receipt": {"trusted": True, "sha256": "f" * 64},
    }


def _predict(root: Path, snapshot: dict | None = None, *, cutoff: datetime = CUTOFF) -> dict:
    return build_future_predictions(
        snapshot or _snapshot(),
        openfootball_raw_archive_dir=root,
        observed_before=cutoff,
        prediction_horizon=timedelta(days=7),
    )


def test_verified_raw_replay_is_the_only_fixture_and_history_input(
    verified_archive: Path,
) -> None:
    fake_features = {
        "home": {"xg_for": 40, "xg_against": 0},
        "away": {"xg_for": 0, "xg_against": 40},
        "market": {
            "provider": "OddStorm",
            "probability": {"home": 1, "draw": 0, "away": 0},
            "source": {"name": "OddStorm", "url": "https://invalid.example"},
        },
        "weather": {"source": {"name": "Open-Meteo"}, "temperature_c": 99},
        "official_lineup": {
            "source": {"name": "Official league"},
            "lineups": {"confirmed": True, "available": True},
        },
    }
    injected = [
        {
            "id": f"injected:{provider}",
            "competition_id": competition,
            "season": "2627",
            "kickoff_at": "2026-08-26T00:00:00+00:00",
            "home_team": "Injected Home",
            "away_team": "Injected Away",
            "home_team_id": f"{competition}:injected-home",
            "away_team_id": f"{competition}:injected-away",
            "status": "upcoming",
            "score": None,
            "source": {
                "name": provider,
                "authorization_reference": "operator-self-issued",
            },
            "current_features": fake_features,
            "trusted": True,
        }
        for provider, competition in (
            ("OpenFootball", "premier-league"),
            ("football-data", "premier-league"),
            ("OpenLigaDB", "premier-league"),
            ("CFL official", "premier-league"),
            ("fake CSL", "csl"),
        )
    ]

    clean = _predict(verified_archive)
    hostile = _predict(verified_archive, _snapshot(injected_matches=injected))

    assert hostile == clean
    assert hostile["status"] == "research_only"
    assert len(hostile["predictions"]) == 1
    row = hostile["predictions"][0]
    assert row["home_team"] == "Coventry City FC"
    assert row["away_team"] == "Arsenal FC"
    assert row["providers"] == ["OpenFootball"]
    assert row["training_cutoff"] is not None
    assert datetime.fromisoformat(row["training_cutoff"]) < datetime.fromisoformat(
        row["cutoff_at"]
    )
    assert datetime.fromisoformat(row["training_cutoff"]) < datetime.fromisoformat(
        row["kickoff_at"]
    )
    history_context = row["history_context"]
    assert history_context["schema_version"] == "matchline.history_context.v1"
    assert history_context["observed_before"] == row["cutoff_at"]
    assert history_context["training_cutoff"] == row["training_cutoff"]
    assert history_context["source"] == "OpenFootball"
    assert (
        history_context["boundary"]
        == "只使用 observed_before 前已归档的 verified OpenFootball 赛果；不含赛后信息。"
    )
    assert history_context["teams"]["home"]["status"] == "unavailable_no_pre_cutoff_history"
    away_context = history_context["teams"]["away"]
    assert away_context["status"] == "available"
    assert away_context["history_sample_n"] == 3
    assert away_context["window_matches"] == 3
    assert away_context["form"] == ["W", "D", "W"]
    assert away_context["summary"] == {
        "wins": 2,
        "draws": 1,
        "losses": 0,
        "goals_for": 5,
        "goals_against": 3,
        "goal_difference": 2,
        "points": 7,
        "points_per_match": 2.3333,
        "clean_sheets": 0,
        "failed_to_score": 0,
    }
    assert all(
        datetime.fromisoformat(match["kickoff_at"]) < datetime.fromisoformat(row["cutoff_at"])
        for match in away_context["matches"]
    )
    assert row["feature_times"] == []
    assert set(row["model_parameters"]["ancillary_feature_weights"].values()) == {0.0}
    assert row["feature_coverage"] == {
        "historical_matches_home": 0,
        "historical_matches_away": 3,
        "historical_team_history_ready": False,
        "historical_home_status": "league_prior_no_team_history",
        "historical_away_status": "team_history_shrunk",
        "historical_home_reliability": 0.0,
        "historical_away_reliability": 0.3,
        "recent_xg": False,
        "current_market": False,
        "public_market_comparison": False,
        "weather": False,
        "injuries": False,
        "lineups": False,
        "lineups_confirmed": False,
        "roster_evidence": False,
        "injury_report_coverage": "rights_blocked_v260",
    }
    assert all(step["source_name"] == "OpenFootball" for step in row["factor_trace"]["steps"])
    assert {item["id"] for item in row["factor_trace"]["not_applied"]} == {
        "recent_xg",
        "market_1x2",
        "weather",
        "injuries",
        "lineups",
    }
    assert all(
        item["status"] == "rights_blocked_v260"
        and item["enters_model"] is False
        and item["source_name"] is None
        for item in row["factor_trace"]["not_applied"]
    )
    health = hostile["model_health"]["premier-league"]
    assert health["input_source"] == "verified_openfootball_historical_raw_archive"
    assert health["home_advantage_elo"] != 999999
    assert health["dixon_coles"]["home_goal_average"] != 99


def test_snapshot_mutations_cannot_change_raw_fixture_or_probability(
    verified_archive: Path,
) -> None:
    raw_result = _predict(verified_archive)
    raw_row = raw_result["predictions"][0]
    injected = {
        "id": raw_row["fixture_id"],
        "competition_id": "premier-league",
        "season": "2627",
        "kickoff_at": "2099-01-01T00:00:00+00:00",
        "home_team": "Mutated Home",
        "away_team": "Mutated Away",
        "home_team_id": "premier-league:mutated-home",
        "away_team_id": "premier-league:mutated-away",
        "status": "finished",
        "score": {"home": 99, "away": 0},
        "source": {"name": "OpenFootball", "trusted": True},
        "current_features": {"home": {"xg_for": 99, "xg_against": 0}},
    }

    result = _predict(verified_archive, _snapshot(injected_matches=[injected]))

    assert result == raw_result
    assert result["predictions"][0]["kickoff_at"] == "2026-08-30T15:00:00+00:00"


@pytest.mark.parametrize("mode", ["missing", "volatile", "invalid", "empty"])
def test_missing_volatile_invalid_or_empty_archive_is_unavailable(
    mode: str,
    durable_archive_dir: Path,
    tmp_path: Path,
) -> None:
    root: Path | None
    if mode == "missing":
        root = None
    elif mode == "volatile":
        root = tmp_path / "volatile-archive"
        _archive(root)
    elif mode == "invalid":
        root = durable_archive_dir
        root.mkdir(parents=True)
        (root / "manifest.jsonl").write_text("not-json\n", encoding="utf-8")
    else:
        root = durable_archive_dir
        root.mkdir(parents=True)

    result = build_future_predictions(
        _snapshot(),
        openfootball_raw_archive_dir=root,
        observed_before=CUTOFF,
    )

    assert result["status"] == "unavailable"
    assert result["predictions"] == []
    assert result["blocked"] == []
    assert result["reason"].startswith("verified_openfootball_")


def test_unavailable_replay_still_records_each_identifiable_upcoming_fixture() -> None:
    """A source outage must be an explicit per-fixture block, not a silent drop."""

    snapshot = _snapshot(
        injected_matches=[
            {
                "id": "openfootball:premier-league:missing-archive",
                "competition_id": "premier-league",
                "season": "2026-27",
                "kickoff_at": "2026-08-30T15:00:00+00:00",
                "home_team": "Coventry City FC",
                "away_team": "Arsenal FC",
                "home_team_id": team_id("premier-league", "Coventry City FC"),
                "away_team_id": team_id("premier-league", "Arsenal FC"),
                "status": "upcoming",
                "score": None,
            }
        ]
    )

    result = build_future_predictions(
        snapshot,
        openfootball_raw_archive_dir=None,
        observed_before=CUTOFF,
        prediction_horizon=timedelta(days=7),
    )

    assert result["predictions"] == []
    assert len(result["predictions"]) + len(result["blocked"]) == 1
    blocked = result["blocked"][0]
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "verified_openfootball_archive_missing"
    assert blocked["fixture_id"] == snapshot["matches"][0]["id"]
    assert blocked["team_identities"]["home"]["team_id"] == team_id(
        "premier-league", "Coventry City FC"
    )
    assert result["coverage"] == {
        "schema_version": "matchline.prediction_coverage.v1",
        "candidate_count": 1,
        "prediction_count": 0,
        "blocked_count": 1,
        "complete": True,
        "missing_fixture_ids": [],
    }


def test_naive_cutoff_fails_closed_without_reading_archive(verified_archive: Path) -> None:
    result = _predict(verified_archive, cutoff=CUTOFF.replace(tzinfo=None))

    assert result["status"] == "unavailable"
    assert result["predictions"] == []
    assert result["reason"] == "verified_openfootball_invalid_observed_before"


def test_replay_cutoff_selects_the_latest_observation_not_snapshot_values(
    durable_archive_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_observed = datetime(2026, 8, 24, 1, tzinfo=timezone.utc)
    second_observed = datetime(2026, 8, 26, 1, tzinfo=timezone.utc)
    _archive(
        durable_archive_dir,
        observed_at=first_observed,
        current_payload=_current_payload(kickoff_date="2026-08-30", kickoff_time="16:00"),
    )
    _store_observation(
        durable_archive_dir,
        source_id=CURRENT_SOURCE_ID,
        payload=_current_payload(kickoff_date="2026-09-01", kickoff_time="18:00"),
        observed_at=second_observed,
    )
    monkeypatch.setattr(future_module, "OPENFOOTBALL_HISTORY_SOURCE_IDS", HISTORY_SOURCE_IDS)
    monkeypatch.setattr(future_module, "OPENFOOTBALL_CURRENT_SOURCE_IDS", (CURRENT_SOURCE_ID,))

    before = _predict(durable_archive_dir, cutoff=CUTOFF)
    after = _predict(
        durable_archive_dir,
        cutoff=datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
    )

    assert before["predictions"][0]["kickoff_at"] == "2026-08-30T15:00:00+00:00"
    assert after["predictions"][0]["kickoff_at"] == "2026-09-01T17:00:00+00:00"
    assert (
        before["model_input"]["current_corpus_identity_sha256"]
        != after["model_input"]["current_corpus_identity_sha256"]
    )


def test_competition_without_verified_history_is_blocked_not_backfilled(
    durable_archive_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _archive(
        durable_archive_dir,
        current_source_id=CHAMPIONSHIP_CURRENT_SOURCE_ID,
        current_payload=_current_payload(
            home="Birmingham City FC",
            away="Blackburn Rovers FC",
        ),
    )
    monkeypatch.setattr(future_module, "OPENFOOTBALL_HISTORY_SOURCE_IDS", HISTORY_SOURCE_IDS)
    monkeypatch.setattr(
        future_module,
        "OPENFOOTBALL_CURRENT_SOURCE_IDS",
        (CHAMPIONSHIP_CURRENT_SOURCE_ID,),
    )

    result = _predict(durable_archive_dir)

    assert result["status"] == "unavailable"
    assert result["predictions"] == []
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["reason"] == "verified_history_unavailable_for_competition"


def test_verified_history_block_contains_stable_coverage_identity(
    durable_archive_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _archive(
        durable_archive_dir,
        current_source_id=CHAMPIONSHIP_CURRENT_SOURCE_ID,
        current_payload=_current_payload(
            home="Birmingham City FC",
            away="Blackburn Rovers FC",
        ),
    )
    monkeypatch.setattr(future_module, "OPENFOOTBALL_HISTORY_SOURCE_IDS", HISTORY_SOURCE_IDS)
    monkeypatch.setattr(
        future_module,
        "OPENFOOTBALL_CURRENT_SOURCE_IDS",
        (CHAMPIONSHIP_CURRENT_SOURCE_ID,),
    )

    result = _predict(durable_archive_dir)

    assert len(result["predictions"]) + len(result["blocked"]) == 1
    blocked = result["blocked"][0]
    assert blocked["status"] == "blocked"
    assert blocked["team_identities"]["home"]["canonical_name"] == "Birmingham"
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["candidate_count"] == 1


def test_out_of_horizon_date_only_rows_are_not_counted_as_candidates() -> None:
    """Date-only rows outside the requested window must not inflate coverage.

    OpenFootball publishes a full-season schedule, and many later fixtures do
    not have an exact kickoff yet.  They remain useful schedule facts, but a
    seven-day prediction run must report coverage only for rows in that
    window.  Rows inside the window still receive an explicit block.
    """

    corpus = SimpleNamespace(
        rows=(
            {
                "id": "date-only-inside",
                "competition_id": "premier-league",
                "season": "2026-27",
                "kickoff_date": "2026-08-31",
                "kickoff_time_quality": "date_only",
                "home_team": "Home FC",
                "away_team": "Away FC",
                "status": "upcoming",
                "score": None,
            },
            {
                "id": "date-only-outside",
                "competition_id": "premier-league",
                "season": "2026-27",
                "kickoff_date": "2026-10-10",
                "kickoff_time_quality": "date_only",
                "home_team": "Later Home FC",
                "away_team": "Later Away FC",
                "status": "upcoming",
                "score": None,
            },
        )
    )

    fixtures, blocked = future_module._verified_current_fixtures(
        corpus,
        cutoff=CUTOFF,
        prediction_horizon=timedelta(days=7),
    )

    assert fixtures == []
    assert [row["fixture_id"] for row in blocked] == ["date-only-inside"]


def test_verified_prediction_preserves_probability_conservation(
    verified_archive: Path,
) -> None:
    row = _predict(verified_archive)["predictions"][0]

    for field in (
        "elo_probability",
        "dixon_coles_probability",
        "primary_probability_1x2",
        "probability_1x2_from_scoreline",
        "scoreline_matrix",
        "total_goals_probability",
        "half_full_probability",
        "total_over_under_probability",
    ):
        assert sum(row[field].values()) == pytest.approx(1, abs=1e-9)


def test_serialized_probability_maps_are_conserved_at_display_precision(
    verified_archive: Path,
) -> None:
    row = _predict(verified_archive)["predictions"][0]

    for field in (
        "elo_probability",
        "dixon_coles_probability",
        "primary_probability_1x2",
        "probability_1x2_from_scoreline",
        "scoreline_matrix",
        "total_goals_probability",
        "half_full_probability",
        "total_over_under_probability",
    ):
        assert sum(row[field].values()) == pytest.approx(1, abs=1e-9)


def test_store_uses_its_clock_as_replay_cutoff_and_missing_path_is_honest(
    verified_archive: Path,
) -> None:
    with_archive = PlatformStore(
        Path("data/MatchHistory"),
        now=CUTOFF,
        openfootball_raw_archive_dir=verified_archive,
    )
    without_archive = PlatformStore(Path("data/MatchHistory"), now=CUTOFF)

    admitted = with_archive.predictions()
    unavailable = without_archive.predictions()

    assert admitted["as_of"] == CUTOFF.isoformat()
    assert admitted["status"] == "research_only"
    assert unavailable["status"] == "unavailable"
    assert unavailable["predictions"] == []


def test_promoted_team_uses_neutral_prior_without_synthetic_history(
    verified_archive: Path,
) -> None:
    row = _predict(verified_archive)["predictions"][0]

    assert row["team_identities"]["home"]["team_id"] == team_id(
        "premier-league", "Coventry City FC"
    )
    assert row["history_prior"]["home"] == {
        "team_id": "premier-league:coventry",
        "sample_n": 0,
        "status": "league_prior_no_team_history",
        "reliability": 0.0,
    }
    assert row["quality_gate"] == "blocked_for_production_insufficient_team_history"


def test_verified_current_fixtures_deduplicate_repeated_ids() -> None:
    row = {
        "id": "duplicate-current",
        "competition_id": "premier-league",
        "season": "2026-27",
        "kickoff_at": "2026-08-30T16:00:00+00:00",
        "kickoff_time_quality": "exact",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "status": "upcoming",
        "score": None,
        "source": {"name": "OpenFootball", "source_id": "current"},
    }
    corpus = SimpleNamespace(rows=(row, dict(row)))

    fixtures, blocked = future_module._verified_current_fixtures(
        corpus,
        cutoff=CUTOFF,
        prediction_horizon=timedelta(days=7),
    )

    assert [item["id"] for item in fixtures] == ["duplicate-current"]
    assert blocked == []


def test_future_default_horizon_is_seven_days(
    verified_archive: Path,
) -> None:
    # The raw current corpus contains one fixture inside and one outside the
    # default seven-day window.  The snapshot is intentionally empty because
    # verified replay, rather than snapshot rows, owns model admission.
    payload = json.dumps(
        {
            "matches": [
                {
                    "date": "2026-08-30",
                    "time": "16:00",
                    "round": "Matchday 1",
                    "team1": "Coventry City FC",
                    "team2": "Arsenal FC",
                },
                {
                    "date": "2026-09-02",
                    "time": "16:00",
                    "round": "Matchday 1",
                    "team1": "Coventry City FC",
                    "team2": "Manchester City FC",
                },
            ]
        },
        sort_keys=True,
    ).encode()
    # Rebuild only the current observation while retaining the fixture's
    # monkeypatched source IDs from ``verified_archive``.
    _store_observation(
        verified_archive,
        source_id=CURRENT_SOURCE_ID,
        payload=payload,
        observed_at=CUTOFF - timedelta(minutes=30),
    )

    result = build_future_predictions(
        _snapshot(),
        openfootball_raw_archive_dir=verified_archive,
        observed_before=CUTOFF,
    )

    assert len(result["predictions"]) == 1
    assert result["predictions"][0]["kickoff_at"] == "2026-08-30T15:00:00+00:00"


def test_non_finite_verified_prediction_is_blocked_with_complete_coverage(
    verified_archive: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_prediction(*args, **kwargs):
        return {
            "fixture_id": args[0]["id"],
            "primary_probability_1x2": {
                "home": float("nan"),
                "draw": 0.25,
                "away": 0.25,
            },
        }

    monkeypatch.setattr(future_module, "_verified_prediction", invalid_prediction)

    result = _predict(verified_archive)

    assert result["predictions"] == []
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["reason"] == "verified_prediction_invalid_probability"
    assert result["coverage"]["complete"] is True
