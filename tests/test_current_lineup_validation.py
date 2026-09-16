from __future__ import annotations

from datetime import datetime, timezone

import pytest

from league_platform.current import (
    _official_fixture_join_diagnostics,
    _validate_lineup_poll_diagnostics,
    _validate_live_snapshot,
)


AS_OF = datetime(2026, 8, 14, 2, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def test_official_fixture_join_diagnostics_separates_exact_and_time_mismatch():
    canonical = [
        {
            "id": "espn:exact",
            "competition_id": "la-liga",
            "status": "upcoming",
            "kickoff_at": "2026-08-22T15:00:00+00:00",
            "home_team": "Athletic Club",
            "away_team": "Sevilla FC",
        },
        {
            "id": "espn:mismatch",
            "competition_id": "la-liga",
            "status": "upcoming",
            "kickoff_at": "2026-08-22T17:00:00+00:00",
            "home_team": "Valencia CF",
            "away_team": "Celta",
        },
    ]
    official = [
        {
            "id": "official:exact",
            "competition_id": "la-liga",
            "kickoff_at": "2026-08-22T15:00:00+00:00",
            "home_team": "Athletic Club",
            "away_team": "Sevilla",
        },
        {
            "id": "official:shifted",
            "competition_id": "la-liga",
            "kickoff_at": "2026-08-22T18:00:00+00:00",
            "home_team": "Valencia",
            "away_team": "Celta Vigo",
        },
    ]
    result = _official_fixture_join_diagnostics(
        canonical,
        official,
        competition_id="la-liga",
        reference_time=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "partial"
    assert result["matched_count"] == 1
    assert result["team_pair_time_mismatch_count"] == 1
    assert result["unmatched_canonical_count"] == 0
    assert result["samples"][0]["outcome"] == "team_pair_time_mismatch"


def test_official_fixture_join_diagnostics_does_not_call_source_health_match_without_candidates():
    result = _official_fixture_join_diagnostics(
        [],
        [{"id": "official:1", "kickoff_at": "2026-08-22T15:00:00+00:00"}],
        competition_id="premier-league",
        reference_time=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "not_due"
    assert result["matched_count"] == 0
    assert result["unmatched_official_count"] == 1


def test_official_fixture_join_diagnostics_uses_explicit_premier_league_display_aliases():
    canonical = [
        {
            "id": "espn:brighton",
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-08-23T13:00:00+00:00",
            "home_team": "Brighton & Hove Albion",
            "away_team": "AFC Bournemouth",
        }
    ]
    official = [
        {
            "id": "official:brighton",
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-08-23T13:00:00+00:00",
            "home_team": "Brighton and Hove Albion",
            "away_team": "Bournemouth",
        }
    ]

    result = _official_fixture_join_diagnostics(
        canonical,
        official,
        competition_id="premier-league",
        reference_time=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "exact"
    assert result["matched_count"] == 1
    assert result["unmatched_canonical_count"] == 0


def _source(url: str, retrieved_at: str) -> dict:
    return {
        "name": "Premier League official",
        "url": url,
        "retrieved_at": retrieved_at,
        "raw_sha256": SHA,
    }


def _snapshot(*, retrieved_at: str, model_eligible: bool = True, confirmed: bool = True) -> dict:
    fixture = {
        "id": "premierleague:1",
        "competition_id": "premier-league",
        "status": "upcoming",
        "kickoff_at": "2026-08-14T01:00:00+00:00",
        "source": _source(
            "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v1/competitions/8/seasons/2026/matchweeks/1/matches",
            AS_OF.isoformat(),
        ),
    }
    return {
        "espn": {
            "fixtures": [
                {
                    "id": "espn:1",
                    "competition_id": "premier-league",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-14T01:00:00+00:00",
                    "home_provider_team_id": "h",
                    "away_provider_team_id": "a",
                    "source": {
                        "name": "ESPN",
                        "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
                        "retrieved_at": AS_OF.isoformat(),
                        "raw_sha256": SHA,
                        "native_fixture_id": "1",
                    },
                }
            ]
        },
        "premier_league_official": {
            "provider": "Premier League official",
            "fixtures": [fixture],
            "lineups": [
                {
                    "match_id": "1",
                    "fixture_id": fixture["id"],
                    "lineups": {
                        "confirmed": confirmed,
                        "model_eligible": model_eligible,
                    },
                    "source": _source(
                        "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v3/matches/1/lineups",
                        retrieved_at,
                    ),
                }
            ],
            "errors": [],
        },
    }


def test_rejects_post_kickoff_official_lineup_at_snapshot_boundary():
    with pytest.raises(ValueError, match="must precede kickoff"):
        _validate_live_snapshot(
            _snapshot(retrieved_at="2026-08-14T01:00:00+00:00"),
            AS_OF,
            {"premier-league"},
        )


def test_rejects_unconfirmed_lineup_marked_model_eligible():
    with pytest.raises(ValueError, match="requires confirmed lineup"):
        _validate_live_snapshot(
            _snapshot(
                retrieved_at="2026-08-14T00:30:00+00:00",
                confirmed=False,
            ),
            AS_OF,
            {"premier-league"},
        )


def test_accepts_confirmed_pre_kickoff_official_lineup():
    _validate_live_snapshot(
        _snapshot(retrieved_at="2026-08-14T00:30:00+00:00"),
        AS_OF,
        {"premier-league"},
    )


def _espn_injury_payload(*, enters_model: bool = False, url: str | None = None) -> dict:
    return {
        "provider": "ESPN injury report",
        "status": "fresh",
        "retrieved_at": "2026-08-14T00:30:00+00:00",
        "reports": [
            {
                "competition_id": "premier-league",
                "provider_team_id": "359",
                "team_name": "Arsenal",
                "report_status": "published",
                "injuries": [
                    {
                        "provider_player_id": "123",
                        "name": "Example Player",
                        "status": "Out",
                        "status_type": "injury_report",
                    }
                ],
                "reported_player_count": 1,
                "retrieved_at": "2026-08-14T00:30:00+00:00",
                "model_eligible": False,
                "enters_model": enters_model,
                "source": {
                    "name": "ESPN injury report",
                    "url": url
                    or "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries",
                    "retrieved_at": "2026-08-14T00:30:00+00:00",
                    "raw_sha256": SHA,
                    "policy": {"allow_model": False},
                },
            }
        ],
        "competition_reports": [],
        "errors": [],
        "model_eligible": False,
        "enters_model": enters_model,
        "coverage_semantics": "missing_team_bucket_is_unknown_not_healthy",
    }


def test_accepts_display_only_espn_injury_report_contract():
    snapshot = _snapshot(retrieved_at="2026-08-14T00:30:00+00:00")
    snapshot["espn"]["fixtures"][0].update(
        {"home_provider_team_id": "359", "away_provider_team_id": "370"}
    )
    snapshot["espn_injuries"] = _espn_injury_payload()

    _validate_live_snapshot(snapshot, AS_OF, {"premier-league"})


def test_rejects_espn_injury_report_marked_model_eligible_or_foreign_url():
    snapshot = _snapshot(retrieved_at="2026-08-14T00:30:00+00:00")
    snapshot["espn_injuries"] = _espn_injury_payload(enters_model=True)
    with pytest.raises(ValueError, match="must remain display-only"):
        _validate_live_snapshot(snapshot, AS_OF, {"premier-league"})

    snapshot["espn_injuries"] = _espn_injury_payload(
        url="https://example.com/apis/site/v2/sports/soccer/eng.1/injuries"
    )
    with pytest.raises(ValueError, match="source"):
        _validate_live_snapshot(snapshot, AS_OF, {"premier-league"})


def test_lineup_poll_diagnostics_is_bounded_audit_metadata():
    _validate_lineup_poll_diagnostics(
        {
            "csl": {
                "state": "not_configured",
                "requested": False,
                "candidate_count": 1,
                "invalid_kickoff_count": 0,
                "source_lineup_count": 0,
                "confirmed_lineup_count": 0,
                "model_eligible_lineup_count": 0,
                "error_count": 0,
                "fixture_plans": [
                    {
                        "fixture_id": "espn:fixture-1",
                        "kickoff_at": "2026-08-21T19:00:00+00:00",
                        "stage_plan": [
                            {
                                "stage": "t_minus_24h",
                                "cutoff_at": "2026-08-20T19:00:00+00:00",
                                "state": "upcoming",
                            }
                        ],
                        "source_lineup_count": 0,
                        "available_lineup_count": 0,
                        "confirmed_lineup_count": 0,
                        "model_eligible_lineup_count": 0,
                        "poll_due": False,
                    }
                ],
            }
        }
    )
    with pytest.raises(ValueError, match="state is invalid"):
        _validate_lineup_poll_diagnostics(
            {
                "csl": {
                    "state": "confirmed_by_guess",
                    "requested": False,
                    "candidate_count": 1,
                    "invalid_kickoff_count": 0,
                    "source_lineup_count": 0,
                    "confirmed_lineup_count": 0,
                    "model_eligible_lineup_count": 0,
                    "error_count": 0,
                }
            }
        )


def test_accepts_confirmed_pre_kickoff_espn_public_provider_lineup():
    snapshot = _snapshot(retrieved_at="2026-08-14T01:30:00+00:00")
    snapshot["espn"]["fixtures"][0]["kickoff_at"] = "2026-08-14T03:00:00+00:00"
    snapshot["premier_league_official"]["fixtures"][0]["kickoff_at"] = "2026-08-14T03:00:00+00:00"
    snapshot["espn_markets"] = {
        "provider": "ESPN event summary",
        "markets": [],
        "errors": [],
        "team_status": [
            {
                "fixture_id": "espn:1",
                "retrieved_at": "2026-08-14T01:30:00+00:00",
                "provider": "ESPN event summary",
                "confirmed": True,
                "model_eligible": True,
                "status": "confirmed_lineup",
                "teams": {
                    side: {
                        "players": [
                            {
                                "player_id": f"p-{side}-{index}",
                                "name": f"Player {side} {index}",
                                "starter": True,
                            }
                            for index in range(11)
                        ],
                        "starter_count": 11,
                    }
                    for side in ("home", "away")
                },
                "source": {
                    "name": "ESPN event summary",
                    "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=1",
                    "retrieved_at": "2026-08-14T01:30:00+00:00",
                    "raw_sha256": SHA,
                },
            }
        ],
    }
    # The provider response keeps the causal timestamp on the team-status
    # record; validation must not require a duplicated nested timestamp.
    snapshot["espn_markets"]["team_status"][0]["source"].pop("retrieved_at")
    _validate_live_snapshot(snapshot, AS_OF, {"premier-league"})


def test_accepts_confirmed_pre_kickoff_laliga_official_lineup():
    snapshot = _snapshot(retrieved_at="2026-08-14T00:30:00+00:00")
    snapshot["espn"]["fixtures"][0].update(
        {
            "id": "espn:la-1",
            "competition_id": "la-liga",
            "kickoff_at": "2026-08-14T01:00:00+00:00",
        }
    )
    snapshot.pop("premier_league_official")
    snapshot["laliga_official"] = {
        "provider": "LaLiga official",
        "fixtures": [
            {
                "id": "espn:la-1",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": "2026-08-14T01:00:00+00:00",
                "home_team": "Alavés",
                "away_team": "Getafe",
                "source": {
                    "name": "LaLiga official",
                    "url": "https://www.laliga.com/en-GB/match/temporada-2026-2027-laliga-ea-sports-deportivo-alaves-getafe-cf-1",
                    "retrieved_at": AS_OF.isoformat(),
                    "raw_sha256": SHA,
                },
            }
        ],
        "lineups": [
            {
                "fixture_id": "espn:la-1",
                "match_id": "98797",
                "lineups": {"confirmed": True, "model_eligible": True},
                "source": {
                    "name": "LaLiga official",
                    "url": "https://www.laliga.com/en-GB/match/temporada-2026-2027-laliga-ea-sports-deportivo-alaves-getafe-cf-1",
                    "retrieved_at": "2026-08-14T00:30:00+00:00",
                    "raw_sha256": SHA,
                },
            }
        ],
        "errors": [],
    }
    _validate_live_snapshot(snapshot, AS_OF, {"la-liga"})


def test_accepts_confirmed_pre_kickoff_bundesliga_official_lineup():
    snapshot = _snapshot(retrieved_at="2026-08-14T00:30:00+00:00")
    snapshot["espn"]["fixtures"][0].update(
        {
            "id": "espn:bundesliga-1",
            "competition_id": "bundesliga",
            "kickoff_at": "2026-08-14T01:00:00+00:00",
        }
    )
    snapshot.pop("premier_league_official")
    source = {
        "name": "Bundesliga official",
        "url": "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1/fc-bayern-muenchen-vs-vfb-stuttgart/lineup",
        "retrieved_at": "2026-08-14T00:30:00+00:00",
        "raw_sha256": SHA,
    }
    players = [
        {
            "player_id": f"bundesliga:p-{side}-{index}",
            "name": f"Player {side} {index}",
            "starter": True,
        }
        for side in ("home", "away")
        for index in range(11)
    ]
    snapshot["bundesliga_official"] = {
        "provider": "Bundesliga official",
        "fixtures": [
            {
                "id": "espn:bundesliga-1",
                "competition_id": "bundesliga",
                "status": "upcoming",
                "kickoff_at": "2026-08-14T01:00:00+00:00",
                "home_team": "Bayern Munich",
                "away_team": "VfB Stuttgart",
                "source": source,
            }
        ],
        "lineups": [
            {
                "fixture_id": "espn:bundesliga-1",
                "match_id": None,
                "lineups": {
                    "confirmed": True,
                    "model_eligible": True,
                    "home": {"players": players[:11]},
                    "away": {"players": players[11:]},
                },
                "source": source,
            }
        ],
        "errors": [],
    }
    _validate_live_snapshot(snapshot, AS_OF, {"bundesliga"})
