from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from league_platform.domain import Match, Score
from league_platform.historical_xg import load_historical_xg_index


def _match(*, kickoff_at: datetime) -> Match:
    return Match(
        id="historical-xg-fixture",
        competition_id="premier-league",
        season="2025",
        kickoff_at=kickoff_at,
        home_team="Arsenal",
        away_team="Chelsea",
        home_team_id="premier-league:arsenal",
        away_team_id="premier-league:chelsea",
        status="finished",
        score=Score(1, 0),
        market_probability=None,
        source_file="demo.csv",
        source_sha256="a" * 64,
        provider_fixture_id="demo:1",
        source_name="test",
        source_license_status="test",
    )


def _write_xg_file(root: Path) -> None:
    cleaned = root / "cleaned"
    cleaned.mkdir(parents=True)
    (cleaned / "understat_ENG-Premier League_2024.csv").write_text(
        "date,home,away,home_score,away_score,home_xg,away_xg\n"
        "2025-01-01 12:00:00,Arsenal,Chelsea,1,0,1.8,0.6\n"
        "2025-02-01 12:00:00,Chelsea,Arsenal,0,2,0.7,1.9\n"
        "2025-03-01 12:00:00,Arsenal,Chelsea,2,1,2.1,0.9\n",
        encoding="utf-8",
    )


def test_historical_xg_uses_conservative_availability_bound(tmp_path: Path):
    _write_xg_file(tmp_path)
    index = load_historical_xg_index(tmp_path, league_ids=("premier-league",))
    match = _match(kickoff_at=datetime(2025, 3, 3, 12, tzinfo=timezone.utc))

    before_observation = index.snapshot_for_match(
        match,
        cutoff_at=datetime(2025, 1, 1, 12, tzinfo=timezone.utc),
        min_sample=1,
    )
    after_observation = index.snapshot_for_match(
        match,
        cutoff_at=datetime(2025, 3, 3, 12, tzinfo=timezone.utc),
        min_sample=1,
    )

    assert before_observation["status"] == "partial_or_missing"
    assert after_observation["status"] == "ready"
    assert after_observation["observed_at_exact"] is False
    assert after_observation["observed_at_basis"] == "effective_at_plus_1d_conservative"
    assert after_observation["home"]["sample_n"] == 3
    assert after_observation["away"]["sample_n"] == 3
    assert after_observation["home"]["source"]["archive_sha256"]


def test_historical_xg_does_not_accept_duplicate_fixture_rows(tmp_path: Path):
    _write_xg_file(tmp_path)
    path = tmp_path / "cleaned" / "understat_ENG-Premier League_2024.csv"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "2025-03-01 12:00:00,Arsenal,Chelsea,2,1,2.1,0.9\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate historical xG fixture"):
        load_historical_xg_index(tmp_path, league_ids=("premier-league",))


def test_historical_xg_index_diagnostics_identifies_execution_layer(tmp_path: Path):
    _write_xg_file(tmp_path)
    diagnostics = load_historical_xg_index(
        tmp_path,
        league_ids=("premier-league",),
        availability_lag=timedelta(days=2),
    ).diagnostics()
    assert diagnostics["status"] == "ready"
    assert diagnostics["source_role"] == "Understat historical fact source"
    assert "Crawl4AI" in diagnostics["execution_layer"]
    assert diagnostics["availability_lag_seconds"] == 172800
