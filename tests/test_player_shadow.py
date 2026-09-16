from league_platform.player_shadow import (
    build_player_availability_shadow,
    build_player_availability_shadow_from_feature_snapshot,
)


def _layer():
    return {
        "players": [
            {
                "team_side": "home",
                "player_id": "home-1",
                "status": "starter",
                "starter": True,
                "expected_minutes": 90,
            },
            {
                "team_side": "home",
                "player_id": "home-1",
                "status": "injured",
                "starter": False,
                "expected_minutes": None,
            },
            {
                "team_side": "away",
                "player_id": "away-1",
                "status": "available",
                "starter": False,
                "expected_minutes": 30,
            },
        ]
    }


def test_shadow_deduplicates_player_rows_and_keeps_impact_out_of_model():
    result = build_player_availability_shadow(
        _layer(),
        observed_at="2026-08-22T10:00:00+00:00",
        as_of="2026-08-22T10:30:00+00:00",
        kickoff_at="2026-08-22T12:00:00+00:00",
    )

    assert result["status"] == "shadow_ready"
    assert result["model_boundary"] == "validation_only"
    assert result["model_eligible"] is False
    assert result["enters_model"] is False
    assert result["impact_delta_probability_1x2"] is None
    assert result["deduplicated_player_count"] == 2
    assert result["duplicate_conflict_count"] == 1
    assert result["teams"]["home"]["unavailable_count"] == 1
    assert result["teams"]["home"]["starter_count"] == 1
    assert result["feature_vector"]["home_unavailable_count"] == 1
    assert "validated_impact_coefficient" in result["missing_fields"]


def test_shadow_rejects_observation_after_as_of_without_discarding_audit_counts():
    result = build_player_availability_shadow(
        _layer(),
        observed_at="2026-08-22T11:00:00+00:00",
        as_of="2026-08-22T10:30:00+00:00",
        kickoff_at="2026-08-22T12:00:00+00:00",
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "observation_after_cutoff"
    assert result["feature_vector"] is None
    assert result["teams"]["home"]["roster_count"] == 1
    assert "observation_after_cutoff" in result["missing_fields"]
    assert result["enters_model"] is False


def test_shadow_requires_an_observation_timestamp():
    result = build_player_availability_shadow(
        _layer(),
        observed_at=None,
        as_of="2026-08-22T10:30:00+00:00",
        kickoff_at="2026-08-22T12:00:00+00:00",
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "missing_or_invalid_observed_at"
    assert result["feature_vector"] is None
    assert result["impact_status"] == "not_estimated_no_validated_baseline"


def test_shadow_projects_team_status_from_a_stage_snapshot_with_time_gate():
    result = build_player_availability_shadow_from_feature_snapshot(
        {
            "team_status": {
                "source": {"retrieved_at": "2026-08-22T10:00:00+00:00"},
                "teams": {
                    "home": {"players": [{"player_id": "h1", "status": "available", "starter": True}]},
                    "away": {"players": [{"player_id": "a1", "status": "injured"}]},
                },
            }
        },
        as_of="2026-08-22T10:30:00+00:00",
        kickoff_at="2026-08-22T12:00:00+00:00",
    )

    assert result["status"] == "shadow_ready"
    assert result["teams"]["home"]["roster_count"] == 1
    assert result["teams"]["away"]["unavailable_count"] == 1
    assert result["model_eligible"] is False
    assert result["impact_delta_probability_1x2"] is None


def test_shadow_prefers_later_provider_with_players_over_playerless_confirmed_envelope():
    result = build_player_availability_shadow_from_feature_snapshot(
        {
            "official_lineup": {
                "source": {"retrieved_at": "2026-08-22T09:55:00+00:00"},
                "lineups": {"confirmed": True, "model_eligible": True},
            },
            "team_status": {
                "source": {"retrieved_at": "2026-08-22T10:00:00+00:00"},
                "confirmed": True,
                "model_eligible": True,
                "teams": {
                    "home": {
                        "players": [
                            {"player_id": f"h{index}", "status": "starter", "starter": True}
                            for index in range(11)
                        ]
                    },
                    "away": {
                        "players": [
                            {"player_id": f"a{index}", "status": "starter", "starter": True}
                            for index in range(11)
                        ]
                    },
                },
            },
        },
        as_of="2026-08-22T10:30:00+00:00",
        kickoff_at="2026-08-22T12:00:00+00:00",
    )

    assert result["status"] == "shadow_ready"
    assert result["deduplicated_player_count"] == 22
    assert result["teams"]["home"]["starter_count"] == 11
    assert result["teams"]["away"]["starter_count"] == 11


def test_shadow_merges_pre_cutoff_espn_injuries_without_hiding_lineup_players():
    result = build_player_availability_shadow_from_feature_snapshot(
        {
            "official_lineup": {
                "source": {"retrieved_at": "2026-08-22T10:00:00+00:00"},
                "lineups": {
                    "home": {"players": [{"player_id": "h1", "status": "starter", "starter": True}]},
                    "away": {"players": [{"player_id": "a1", "status": "starter", "starter": True}]},
                },
            },
            "espn_injuries": {
                "source": {"retrieved_at": "2026-08-22T10:05:00+00:00"},
                "injuries": {
                    "home": [{"provider_player_id": "h2", "status": "Out"}],
                    "away": [],
                    "available": False,
                    "partial": True,
                },
            },
        },
        as_of="2026-08-22T10:30:00+00:00",
        kickoff_at="2026-08-22T12:00:00+00:00",
    )

    assert result["status"] == "shadow_ready"
    assert result["deduplicated_player_count"] == 3
    assert result["teams"]["home"]["starter_count"] == 1
    assert result["teams"]["home"]["unavailable_count"] == 1
    assert result["observed_at"] == "2026-08-22T10:05:00+00:00"


def test_shadow_ignores_espn_injury_report_observed_after_cutoff():
    result = build_player_availability_shadow_from_feature_snapshot(
        {
            "team_status": {
                "source": {"retrieved_at": "2026-08-22T10:00:00+00:00"},
                "teams": {
                    "home": {"players": [{"player_id": "h1", "status": "available"}]},
                    "away": {"players": []},
                },
            },
            "espn_injuries": {
                "source": {"retrieved_at": "2026-08-22T10:45:00+00:00"},
                "injuries": {
                    "home": [{"provider_player_id": "h2", "status": "Out"}],
                    "away": [],
                },
            },
        },
        as_of="2026-08-22T10:30:00+00:00",
        kickoff_at="2026-08-22T12:00:00+00:00",
    )

    assert result["status"] == "shadow_ready"
    assert result["deduplicated_player_count"] == 1
    assert result["teams"]["home"]["unavailable_count"] == 0
    assert result["observed_at"] == "2026-08-22T10:00:00+00:00"
