import json
from datetime import datetime, timezone

import pytest

from league_platform.domain import Match, Score
from league_platform.sources.espn_kickoff import (
    apply_kickoff_enrichment,
    load_kickoff_enrichment,
)


def _match(*, quality="date_only"):
    return Match(
        id="fixture-1",
        competition_id="premier-league",
        season="1516",
        kickoff_at=datetime(2015, 8, 8, tzinfo=timezone.utc),
        home_team="Bournemouth",
        away_team="Aston Villa",
        home_team_id="premier-league:bournemouth",
        away_team_id="premier-league:aston-villa",
        status="finished",
        score=Score(home=0, away=1),
        market_probability=None,
        source_file="E0_1516.csv",
        source_sha256="a" * 64,
        provider_fixture_id="E0_1516.csv:2",
        source_name="football-data.co.uk",
        source_license_status="provider_terms_require_review",
        kickoff_time_quality=quality,
    )


def _record(**overrides):
    value = {
        "competition_id": "premier-league",
        "season": "1516",
        "source_file": "E0_1516.csv",
        "source_row": 2,
        "provider_fixture_id": "E0_1516.csv:2",
        "kickoff_at": "2015-08-08T11:45:00+00:00",
        "home_team": "Bournemouth",
        "away_team": "Aston Villa",
        "source_name": "ESPN",
        "source_url": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
        "source_raw_sha256": "b" * 64,
        "source_retrieved_at": "2026-08-13T00:00:00+00:00",
        "source_row_sha256": "a" * 64,
        "match_confidence": 1.0,
        "join_score": 2.5,
        "provider_fixture_native_id": "422655",
        "role": "temporal_order_only",
    }
    value.update(overrides)
    return value


def test_load_and_apply_enrichment_is_temporal_only(tmp_path):
    path = tmp_path / "enrichment.json"
    path.write_text(json.dumps({"generated_at": "2026-08-13T00:00:00+00:00", "records": [_record()]}))
    records = load_kickoff_enrichment(path)
    matches, stats = apply_kickoff_enrichment([_match()], records)
    assert stats["applied"] == 1
    assert stats["remaining_date_only"] == 0
    assert matches[0].kickoff_at.isoformat() == "2015-08-08T11:45:00+00:00"
    assert matches[0].kickoff_time_quality == "exact"
    assert matches[0].kickoff_time_source == "ESPN"
    assert matches[0].kickoff_time_observed_at == "2026-08-13T00:00:00+00:00"


def test_exact_provider_time_is_never_overwritten(tmp_path):
    path = tmp_path / "enrichment.json"
    path.write_text(json.dumps({"records": [_record()]}))
    matches, stats = apply_kickoff_enrichment([_match(quality="exact")], load_kickoff_enrichment(path))
    assert stats["skipped_exact"] == 1
    assert matches[0].kickoff_at.hour == 0


def test_apply_accepts_one_shot_iterable(tmp_path):
    path = tmp_path / "enrichment.json"
    path.write_text(json.dumps({"records": [_record()]}))
    matches, stats = apply_kickoff_enrichment(
        (item for item in [_match()]), load_kickoff_enrichment(path)
    )
    assert len(matches) == 1
    assert stats["applied"] == 1
    assert matches[0].kickoff_time_quality == "exact"


def test_invalid_source_or_confidence_fails_closed(tmp_path):
    path = tmp_path / "enrichment.json"
    path.write_text(json.dumps({"records": [_record(source_url="https://example.com/x")]}))
    with pytest.raises(ValueError, match="not allowlisted"):
        load_kickoff_enrichment(path)

    path.write_text(json.dumps({"records": [_record(match_confidence=0.89)]}))
    with pytest.raises(ValueError, match="below threshold"):
        load_kickoff_enrichment(path)
