from league_platform.ingestion import snapshot_observations
from league_platform.player_evidence import snapshot_player_observations


SOURCE = {
    "name": "ESPN event summary",
    "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/summary?event=1",
    "raw_sha256": "a" * 64,
    "retrieved_at": "2026-08-18T10:00:00+00:00",
}


def _snapshot():
    return {
        "as_of": "2026-08-18T10:01:00+00:00",
        "espn": {"fixtures": []},
        "espn_markets": {
            "markets": [],
            "team_status": [
                {
                    "fixture_id": "espn:1",
                    "retrieved_at": "2026-08-18T10:00:00+00:00",
                    "confirmed": False,
                    "model_eligible": False,
                    "status": "roster_evidence_only",
                    "source": SOURCE,
                    "teams": {
                        "home": {
                            "provider_team_id": "10",
                            "name": "Home FC",
                            "players": [
                                {
                                    "player_id": "101",
                                    "name": "A Keeper",
                                    "position": "GK",
                                    "starter": False,
                                    "status": "active_roster",
                                },
                                {"name": "missing id"},
                            ],
                        },
                        "away": {
                            "provider_team_id": "20",
                            "name": "Away FC",
                            "players": [{"player_id": "202", "name": "B Striker", "starter": True}],
                        },
                    },
                },
            ],
        },
    }


def test_snapshot_player_observations_keep_provider_identity_unbound():
    rows = snapshot_player_observations(_snapshot(), fallback="2026-08-18T10:01:00+00:00")

    assert [row.entity_id for row in rows] == ["espn:101", "espn:202"]
    assert all(row.entity_type == "player" for row in rows)
    assert all(row.kind == "player_roster" for row in rows)
    assert all(row.enters_model is False for row in rows)
    assert all(row.model_exclusion_reason == "player_identity_not_canonical" for row in rows)
    assert rows[0].payload["team_provider_id"] == "10"
    assert rows[1].payload["team_side"] == "away"
    assert "player_id" not in rows[0].as_dict()


def test_confirmed_official_lineup_preserves_minutes_as_unknown_and_deduplicates():
    snapshot = _snapshot()
    snapshot["premier_league_official"] = {
        "lineups": [
            {
                "fixture_id": "premierleague:9",
                "lineups": {
                    "confirmed": True,
                    "model_eligible": True,
                    "home": {
                        "players": [{
                            "player_id": "premierleague:501",
                            "name": "Official Player",
                            "status": "starter",
                            "starter": True,
                            "expected_minutes": 0,
                        }],
                    },
                    "away": {"players": []},
                },
                "source": {
                    "name": "Premier League official",
                    "url": "https://example.com/lineups/9",
                    "raw_sha256": "b" * 64,
                    "retrieved_at": "2026-08-18T10:00:00+00:00",
                },
            },
        ],
    }

    rows = snapshot_player_observations(snapshot, fallback="2026-08-18T10:01:00+00:00")
    official = next(row for row in rows if row.entity_id == "premierleague:501")

    assert official.kind == "player_lineup"
    assert official.source_tier == "official"
    assert official.payload["expected_minutes"] == 0
    assert official.enters_model is False
    assert official.model_exclusion_reason == "player_identity_not_canonical"


def test_invalid_source_isolated_and_player_rows_enter_fixture_ledger():
    snapshot = _snapshot()
    snapshot["espn_markets"]["team_status"][0]["source"] = {
        **SOURCE,
        "url": "http://not-allowed.example/summary",
    }
    assert snapshot_player_observations(snapshot, fallback=snapshot["as_of"]) == []

    snapshot = _snapshot()
    rows = snapshot_observations(snapshot)
    player_rows = [row for row in rows if row.entity_type == "player"]
    assert {row.entity_id for row in player_rows} == {"espn:101", "espn:202"}


def test_explicit_canonical_binding_is_the_only_path_to_model_entry():
    snapshot = _snapshot()
    status = snapshot["espn_markets"]["team_status"][0]
    status["confirmed"] = True
    status["model_eligible"] = True
    status["status"] = "confirmed_lineup"
    status["canonical_fixture_id"] = 7
    status["teams"]["home"]["canonical_team_id"] = 8
    status["teams"]["home"]["players"][0]["canonical_player_id"] = 9

    row = snapshot_player_observations(snapshot, fallback=snapshot["as_of"])[0]
    assert row.fixture_id == 7
    assert row.team_id == 8
    assert row.player_id == 9
    assert row.enters_model is True
    assert row.model_exclusion_reason is None
    assert row.as_dict()["player_id"] == 9


def test_espn_injury_report_creates_player_availability_not_roster_or_lineup():
    snapshot = _snapshot()
    snapshot["espn_injuries"] = {
        "reports": [
            {
                "competition_id": "premier-league",
                "provider_team_id": "10",
                "team_name": "Home FC",
                "retrieved_at": "2026-08-18T10:00:00+00:00",
                "injuries": [
                    {
                        "provider_player_id": "303",
                        "name": "Unavailable Player",
                        "position": "DF",
                        "status": "Out",
                        "status_type": "injury_report",
                        "reason": "Hamstring",
                        "return_date": "2026-09-01T00:00:00Z",
                        "expected_minutes": None,
                        "replacement_value": None,
                    }
                ],
                "model_eligible": False,
                "enters_model": False,
                "source": {
                    "name": "ESPN injury report",
                    "url": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries",
                    "retrieved_at": "2026-08-18T10:00:00+00:00",
                    "raw_sha256": "c" * 64,
                    "policy": {"allow_model": False},
                },
            }
        ]
    }

    rows = snapshot_player_observations(snapshot, fallback=snapshot["as_of"])
    availability = next(row for row in rows if row.entity_id == "espn:303")

    assert availability.kind == "player_availability"
    assert availability.payload["fixture_provider_id"] == "espn-injuries:premier-league:10"
    assert availability.payload["team_provider_id"] == "10"
    assert availability.payload["reason"] == "Hamstring"
    assert availability.payload["expected_minutes"] is None
    assert availability.enters_model is False
    assert availability.model_exclusion_reason == (
        "league_report_not_fixture_complete_and_provider_terms_require_review"
    )
