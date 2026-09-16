from datetime import datetime, timezone

from league_platform.future import (
    _freeze_stage,
    _freeze_versions,
    _player_layer,
    _provider_lineup_feature,
)


def test_freeze_versions_never_fills_future_stages_with_old_predictions():
    kickoff = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    as_of = datetime(2026, 8, 12, 7, tzinfo=timezone.utc)
    versions = _freeze_versions(
        kickoff,
        as_of,
        fixture_id="espn:fixture-1",
        lineup_available=False,
        lineup_observed_at=None,
        model_version="dc-v1",
    )
    assert [item["status"] for item in versions] == ["available", "available", "pending", "pending"]
    assert versions[0]["model_version"] == "dc-v1"
    assert len({item["version_id"] for item in versions}) == 4


def test_freeze_stage_keeps_unconfirmed_lineup_at_90_minute_stage():
    kickoff = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 12, 10, 30, tzinfo=timezone.utc)
    assert _freeze_stage(kickoff, cutoff) == "t_minus_90m"
    assert _freeze_stage(
        kickoff,
        cutoff,
        lineup_available=True,
        lineup_observed_at="2026-08-12T10:31:00+00:00",
    ) == "t_minus_90m"


def test_freeze_stage_uses_lineup_confirmation_only_after_observation():
    kickoff = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 12, 10, 35, tzinfo=timezone.utc)
    assert _freeze_stage(
        kickoff,
        cutoff,
        lineup_available=True,
        lineup_confirmed=True,
        lineup_observed_at="2026-08-12T10:31:00+00:00",
    ) == "lineup_confirmation"


def test_freeze_stage_rejects_predicted_lineup_even_when_observed_before_cutoff():
    kickoff = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 12, 10, 35, tzinfo=timezone.utc)
    assert _freeze_stage(
        kickoff,
        cutoff,
        lineup_available=True,
        lineup_confirmed=False,
        lineup_observed_at="2026-08-12T10:31:00+00:00",
    ) == "t_minus_90m"


def test_freeze_versions_quarantines_malformed_lineup_timestamp():
    kickoff = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    as_of = datetime(2026, 8, 12, 10, 45, tzinfo=timezone.utc)
    versions = _freeze_versions(
        kickoff,
        as_of,
        fixture_id="espn:fixture-2",
        lineup_available=True,
        lineup_observed_at="not-a-timestamp",
        model_version="dc-v1",
    )
    assert versions[-1]["status"] == "pending"
    assert versions[-1]["cutoff_at"] is None


def test_freeze_versions_keeps_predicted_lineup_confirmation_pending():
    kickoff = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    as_of = datetime(2026, 8, 12, 10, 35, tzinfo=timezone.utc)
    versions = _freeze_versions(
        kickoff,
        as_of,
        fixture_id="espn:fixture-predicted-lineup",
        lineup_available=True,
        lineup_confirmed=False,
        lineup_observed_at="2026-08-12T10:31:00+00:00",
        model_version="dc-v1",
    )
    assert versions[-1]["status"] == "pending"


def test_player_layer_keeps_unvalidated_replacement_value_missing():
    layer = _player_layer(
        {
            "lineups": {
                "confirmed": True,
                "home": {
                    "players": [
                        {
                            "player_id": "1",
                            "name": "Keeper",
                            "position": "GK",
                            "status": "starter",
                            "starter": True,
                            "expected_minutes": 90,
                        }
                    ]
                },
                "away": {"players": []},
            },
            "injuries": {"home": [], "away": []},
        }
    )
    assert layer["status"] == "partial"
    assert layer["players"][0]["expected_minutes"] == 90
    assert layer["players"][0]["replacement_value"] is None
    assert layer["model_eligible"] is False


def test_player_layer_can_display_espn_roster_evidence_without_confirming_lineup():
    layer = _player_layer(
        None,
        {
            "confirmed": False,
            "model_eligible": False,
            "teams": {
                "home": {
                    "full_roster": False,
                    "players": [
                        {
                            "player_id": "p1",
                            "name": "Keeper",
                            "position": "Goalkeeper",
                            "status": "active_roster",
                        }
                    ],
                }
            },
        },
    )
    assert layer["status"] == "partial"
    assert layer["confirmed"] is False
    assert layer["players"][0]["name"] == "Keeper"
    assert layer["model_eligible"] is False


def test_provider_lineup_feature_preserves_top_level_observation_clock():
    feature = _provider_lineup_feature(
        {
            "confirmed": True,
            "model_eligible": True,
            "retrieved_at": "2026-08-12T10:31:00+00:00",
            "source": {"name": "ESPN event summary", "raw_sha256": "a" * 64},
            "teams": {"home": {"players": []}, "away": {"players": []}},
        }
    )

    assert feature is not None
    assert feature["source"]["retrieved_at"] == "2026-08-12T10:31:00+00:00"
    assert feature["lineups"]["confirmed"] is True
