from __future__ import annotations

import json
from datetime import datetime, timezone

from league_platform.live_sources.espn import fetch_espn_fixtures
from league_platform.live_sources.espn_market import (
    fetch_espn_markets,
    parse_espn_incidents,
    parse_espn_event_update,
    parse_espn_match_stats,
)
from league_platform.sync_live import _apply_espn_event_updates


def test_espn_fixture_window_is_reported_without_opening_blocked_network():
    urls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"events": []}'

    def opener(request, timeout):
        assert timeout == 30
        urls.append(request.full_url)
        return Response()

    result = fetch_espn_fixtures(
        now=datetime(2026, 8, 11, tzinfo=timezone.utc),
        horizon_days=45,
        lookback_days=45,
        opener=opener,
        authorization_reference="test-fixture",
    )

    assert result["lookback_days"] == 45
    assert result["horizon_days"] == 45
    assert result["status"] == "rights_blocked"
    assert urls == []


def test_espn_fixture_fetch_blocks_before_an_unallowlisted_redirect_can_occur():
    class RedirectedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://evil.example/scoreboard"

        def read(self, _limit):
            return b'{"events": []}'

    def opener(_request, timeout):
        assert timeout == 30
        return RedirectedResponse()

    result = fetch_espn_fixtures(
        now=datetime(2026, 8, 11, tzinfo=timezone.utc),
        opener=opener,
        authorization_reference="test-fixture",
    )

    assert result["fixtures"] == []
    assert result["status"] == "rights_blocked"
    assert result["errors"] == []
    assert result["network_opened"] is False


def test_espn_market_payload_authorization_cannot_enable_fallback_hosts():
    payload = json.dumps(
        {
            "pickcenter": [
                {
                    "provider": {"name": "ExampleBook"},
                    "homeTeamOdds": {"moneyLine": -120},
                    "drawOdds": {"moneyLine": 250},
                    "awayTeamOdds": {"moneyLine": 330},
                }
            ]
        }
    ).encode()

    class Response:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, _limit):
            return payload

    seen = []

    def opener(request, timeout):
        assert timeout == 30
        seen.append(request.full_url)
        if request.full_url.startswith("https://site.api.espn.com/"):
            raise OSError("primary host unavailable")
        return Response(request.full_url)

    result = fetch_espn_markets(
        [
            {
                "id": "espn:1",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-20T19:00:00+00:00",
                "source": {"native_fixture_id": "1"},
            }
        ],
        now=datetime(2026, 8, 11, tzinfo=timezone.utc),
        opener=opener,
        authorization_reference="test-fixture",
    )

    assert seen == []
    assert result["status"] == "rights_blocked"
    assert result["errors"] == []
    assert result["fallbacks"] == []
    assert result["markets"] == []


def test_espn_summary_refreshes_lagging_scoreboard_status_and_score():
    payload = json.dumps(
        {
            "header": {
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "2",
                                "linescores": [{"displayValue": "0"}, {"displayValue": "2"}],
                            },
                            {
                                "homeAway": "away",
                                "score": "1",
                                "linescores": [{"displayValue": "1"}, {"displayValue": "0"}],
                            },
                        ],
                        "status": {"type": {"name": "STATUS_FULL_TIME"}},
                    }
                ]
            }
        }
    ).encode()
    observed_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    update = parse_espn_event_update(
        payload,
        fixture_id="espn:401861447",
        retrieved_at=observed_at,
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=401861447",
        kickoff_at="2026-08-14T11:35:00+00:00",
    )
    assert update is not None
    assert update["status"] == "finished"
    assert update["result_scope"] == "regulation_90"
    assert update["score"] == {"home": 2, "away": 1}
    assert update["halftime_score"] == {"home": 0, "away": 1}
    espn = {
        "fixtures": [
            {
                "id": "espn:401861447",
                "status": "upcoming",
                "score": None,
                "source": {"name": "ESPN", "raw_sha256": "a" * 64},
            }
        ]
    }
    assert _apply_espn_event_updates(espn, {"fixture_updates": [update]}) == 1
    assert espn["fixtures"][0]["status"] == "finished"
    assert espn["fixtures"][0]["score"] == {"home": 2, "away": 1}
    assert espn["fixtures"][0]["halftime_score"] == {"home": 0, "away": 1}
    assert espn["fixtures"][0]["source"]["name"] == "ESPN"
    assert espn["fixtures"][0]["source"]["source_kind"] == "event_summary_status"


def test_espn_recent_finished_replay_is_display_only():
    payload = json.dumps(
        {
            "header": {
                "competitions": [
                    {
                        "status": {"type": {"name": "STATUS_FULL_TIME"}},
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "2",
                                "linescores": [{"displayValue": "1"}],
                            },
                            {
                                "homeAway": "away",
                                "score": "1",
                                "linescores": [{"displayValue": "0"}],
                            },
                        ],
                    }
                ]
            },
            "plays": [
                {
                    "type": "goal",
                    "text": "Goal",
                    "clock": {"displayValue": "35'"},
                    "homeScore": 1,
                    "awayScore": 0,
                }
            ],
            "boxscore": {
                "teams": [
                    {
                        "homeAway": "home",
                        "team": {"id": "1", "displayName": "Home"},
                        "statistics": [{"name": "possessionPct", "displayValue": "55"}],
                    },
                    {
                        "homeAway": "away",
                        "team": {"id": "2", "displayName": "Away"},
                        "statistics": [{"name": "possessionPct", "displayValue": "45"}],
                    },
                ]
            },
            "pickcenter": [
                {
                    "provider": {"name": "DoNotUseAfterKickoff"},
                    "homeTeamOdds": {"moneyLine": -120},
                    "drawOdds": {"moneyLine": 250},
                    "awayTeamOdds": {"moneyLine": 330},
                }
            ],
        }
    ).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=1"

        def read(self, _limit):
            return payload

    def opener(_request, timeout):
        assert timeout == 30
        return Response()

    result = fetch_espn_markets(
        [
            {
                "id": "espn:1",
                "competition_id": "csl",
                "status": "finished",
                "kickoff_at": "2026-08-14T11:35:00+00:00",
                "source": {"native_fixture_id": "1"},
            }
        ],
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        opener=opener,
        authorization_reference="test-fixture",
    )
    assert result["status"] == "rights_blocked"
    assert result["requested_fixtures"] == 0
    assert result["active_fixtures"] == 0
    assert result["postmatch_fixtures"] == 0
    assert result["markets"] == []
    assert result["team_status"] == []
    assert result["fixture_updates"] == []
    assert result["incidents"] == []
    assert result["match_stats"] == []


def test_espn_summary_live_score_never_becomes_finished_result():
    payload = json.dumps(
        {
            "header": {
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "score": "2"},
                            {"homeAway": "away", "score": "1"},
                        ],
                        "status": {
                            "type": {"name": "STATUS_IN_PROGRESS", "shortDetail": "2nd Half"},
                            "displayClock": "67:14",
                            "period": 2,
                        },
                    }
                ]
            }
        }
    ).encode()
    update = parse_espn_event_update(
        payload,
        fixture_id="espn:401861447",
        retrieved_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=401861447",
        kickoff_at="2026-08-14T11:35:00+00:00",
    )
    assert update is not None
    assert update["status"] == "live"
    assert update["clock"] == "67:14"
    assert update["period"] == 2
    assert update["status_text"] == "2nd Half"
    espn = {
        "fixtures": [
            {
                "id": "espn:401861447",
                "status": "upcoming",
                "score": None,
                "source": {"name": "ESPN", "raw_sha256": "a" * 64},
            }
        ]
    }
    assert _apply_espn_event_updates(espn, {"fixture_updates": [update]}) == 1
    assert espn["fixtures"][0]["status"] == "live"
    assert espn["fixtures"][0]["score"] == {"home": 2, "away": 1}
    assert espn["fixtures"][0]["clock"] == "67:14"
    assert espn["fixtures"][0]["period"] == 2
    assert espn["fixtures"][0]["status_text"] == "2nd Half"


def test_espn_incidents_are_bounded_and_display_only():
    payload = json.dumps(
        {
            "plays": [
                {
                    "type": {"text": "Goal"},
                    "text": "Forward scores",
                    "clock": {"displayValue": "45+2"},
                    "team": {"id": "10", "displayName": "Home"},
                    "participants": [{"athlete": {"id": "100", "displayName": "Forward"}}],
                    "homeScore": 1,
                    "awayScore": 0,
                    "id": "goal-1",
                    "period": {"number": 2},
                    "scoringPlay": True,
                    "wallclock": "2026-08-14T12:45:00Z",
                    "fieldPositionX": 96.7,
                    "fieldPositionY": 50.0,
                    "fieldPosition2X": 100.0,
                    "fieldPosition2Y": 49.0,
                    "goalPositionY": 49.0,
                },
                {
                    "type": {"text": "Shot"},
                    "text": "Malformed coordinate event",
                    "fieldPositionX": 101,
                    "fieldPositionY": 40,
                    "fieldPosition2X": "100",
                    "fieldPosition2Y": -1,
                },
            ]
        }
    ).encode()
    incident = parse_espn_incidents(
        payload,
        fixture_id="espn:1",
        retrieved_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=1",
    )
    assert incident is not None
    assert incident["events"][0]["minute"] == "45+2"
    assert incident["events"][0]["participants"] == ["Forward"]
    assert incident["events"][0]["participant_ids"] == ["100"]
    assert incident["events"][0]["score"] == {"home": 1, "away": 0}
    assert incident["events"][0]["location"] == {
        "coordinate_system": "espn_field_percent",
        "source": "provider_declared",
        "provider_declared": True,
        "origin": {"x": 96.7, "y": 50.0},
        "target": {"x": 100.0, "y": 49.0},
        "goal_y": 49.0,
        "model_eligible": False,
    }
    assert "location" not in incident["events"][1]
    assert incident["events"][0]["scoring_play"] is True
    assert incident["events"][0]["period"] == 2
    assert incident["events"][0]["wallclock"] == "2026-08-14T12:45:00Z"
    assert incident["enters_model"] is False
    assert incident["model_exclusion_reason"] == "live_or_postmatch_event"


def test_espn_boxscore_stats_are_bounded_and_display_only():
    payload = json.dumps(
        {
            "boxscore": {
                "teams": [
                    {
                        "homeAway": "home",
                        "team": {"id": "10", "displayName": "Home"},
                        "statistics": [
                            {
                                "name": "possessionPct",
                                "label": "Possession",
                                "displayValue": "53.7",
                            },
                            {"name": "totalShots", "label": "SHOTS", "displayValue": "16"},
                            {"name": "unknownMetric", "displayValue": "999"},
                        ],
                    },
                    {
                        "homeAway": "away",
                        "team": {"id": "11", "displayName": "Away"},
                        "statistics": [
                            {
                                "name": "possessionPct",
                                "label": "Possession",
                                "displayValue": "46.3",
                            }
                        ],
                    },
                ]
            }
        }
    ).encode()
    stats = parse_espn_match_stats(
        payload,
        fixture_id="espn:1",
        retrieved_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=1",
    )
    assert stats is not None
    assert stats["teams"]["home"]["statistics"][0]["value"] == 53.7
    assert len(stats["teams"]["home"]["statistics"]) == 2
    assert stats["enters_model"] is False
    assert stats["model_exclusion_reason"] == "live_or_postmatch_event"


def test_espn_player_boxscore_stats_are_bounded_and_display_only():
    payload = json.dumps(
        {
            "boxscore": {
                "teams": [
                    {
                        "homeAway": "home",
                        "team": {"id": "10", "displayName": "Home"},
                        "statistics": [],
                    },
                    {
                        "homeAway": "away",
                        "team": {"id": "11", "displayName": "Away"},
                        "statistics": [],
                    },
                ],
                "players": [
                    {
                        "homeAway": "home",
                        "team": {"id": "10", "displayName": "Home"},
                        "statistics": [
                            {
                                "keys": [
                                    "minutesPlayed",
                                    "goals",
                                    "assists",
                                    "providerPrivateMetric",
                                ],
                                "athletes": [
                                    {
                                        "athlete": {
                                            "id": "100",
                                            "displayName": "Forward",
                                            "position": {"displayName": "FW"},
                                        },
                                        "stats": ["90", "1", "0", "999"],
                                        "displayValues": ["90", "1", "0", "999"],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        }
    ).encode()
    stats = parse_espn_match_stats(
        payload,
        fixture_id="espn:1",
        retrieved_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=1",
    )
    assert stats is not None
    assert stats["players"][0]["player_id"] == "100"
    assert stats["players"][0]["team_side"] == "home"
    assert [row["name"] for row in stats["players"][0]["statistics"]] == [
        "minutesPlayed",
        "goals",
        "assists",
    ]
    assert stats["enters_model"] is False
    assert stats["model_exclusion_reason"] == "live_or_postmatch_event"


def test_espn_player_only_boxscore_is_kept_without_inventing_team_stats():
    payload = json.dumps(
        {
            "boxscore": {
                "players": [
                    {
                        "homeAway": "away",
                        "team": {"id": "11", "displayName": "Away"},
                        "statistics": [
                            {
                                "keys": ["minutesPlayed", "goals"],
                                "athletes": [
                                    {
                                        "athlete": {"id": "101", "displayName": "Keeper"},
                                        "stats": ["90", "0"],
                                        "displayValues": ["90", "0"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        }
    ).encode()
    stats = parse_espn_match_stats(
        payload,
        fixture_id="espn:1",
        retrieved_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=1",
    )
    assert stats is not None
    assert stats["teams"] == {}
    assert stats["players"][0]["name"] == "Keeper"
    assert stats["players"][0]["team_side"] == "away"
    assert stats["players"][0]["statistics"][0]["name"] == "minutesPlayed"
    assert stats["enters_model"] is False


def test_espn_roster_stats_schema_is_projected_to_player_stats():
    """The live ESPN web host publishes player stats under rosters, not boxscore.players."""

    payload = json.dumps(
        {
            "boxscore": {
                "teams": [
                    {
                        "homeAway": "home",
                        "team": {"id": "10", "displayName": "Home"},
                        "statistics": [],
                    },
                    {
                        "homeAway": "away",
                        "team": {"id": "11", "displayName": "Away"},
                        "statistics": [],
                    },
                ]
            },
            "rosters": [
                {
                    "homeAway": "home",
                    "team": {"id": "10", "displayName": "Home"},
                    "roster": [
                        {
                            "starter": True,
                            "athlete": {
                                "id": "100",
                                "displayName": "Forward",
                            },
                            "position": {"displayName": "Forward", "abbreviation": "F"},
                            "stats": [
                                {
                                    "name": "totalGoals",
                                    "displayName": "Total Goals",
                                    "value": 1,
                                    "displayValue": "1",
                                },
                                {
                                    "name": "goalAssists",
                                    "displayName": "Assists",
                                    "value": 0,
                                    "displayValue": "0",
                                },
                                {
                                    "name": "totalShots",
                                    "displayName": "Shots",
                                    "value": 4,
                                    "displayValue": "4",
                                },
                                {
                                    "name": "providerPrivateMetric",
                                    "value": 999,
                                    "displayValue": "999",
                                },
                            ],
                        }
                    ],
                },
                {
                    "homeAway": "away",
                    "team": {"id": "11", "displayName": "Away"},
                    "roster": [
                        {
                            "starter": False,
                            "athlete": {"id": "101", "displayName": "Substitute"},
                            "position": {"displayName": "Midfielder"},
                            "stats": [
                                {
                                    "name": "subIns",
                                    "displayName": "Substitute Appearances",
                                    "value": 1,
                                    "displayValue": "1",
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    ).encode()
    stats = parse_espn_match_stats(
        payload,
        fixture_id="espn:roster-stats",
        retrieved_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        url="https://site.web.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=1",
    )
    assert stats is not None
    assert stats["teams"] == {}
    assert [row["name"] for row in stats["players"][0]["statistics"]] == [
        "goals",
        "assists",
        "shots",
    ]
    assert stats["players"][0]["statistics"][0]["provider_name"] == "totalGoals"
    assert stats["players"][0]["team_side"] == "home"
    assert stats["players"][1]["statistics"][0]["name"] == "subIns"
    assert stats["players"][1]["starter"] is False
    assert all(
        "providerPrivateMetric" not in row["name"]
        for player in stats["players"]
        for row in player["statistics"]
    )
    assert stats["enters_model"] is False
    assert stats["model_exclusion_reason"] == "live_or_postmatch_event"


def test_espn_summary_first_half_status_is_live():
    payload = json.dumps(
        {
            "header": {
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "score": "2"},
                            {"homeAway": "away", "score": "1"},
                        ],
                        "status": {"type": {"name": "STATUS_FIRST_HALF"}},
                    }
                ]
            }
        }
    ).encode()
    update = parse_espn_event_update(
        payload,
        fixture_id="espn:401861447",
        retrieved_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=401861447",
    )
    assert update is not None
    assert update["status"] == "live"
    assert update["score"] == {"home": 2, "away": 1}


def test_espn_first_half_linescore_is_not_published_as_halftime_score():
    payload = json.dumps(
        {
            "header": {
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "1",
                                "linescores": [{"displayValue": "1"}],
                            },
                            {
                                "homeAway": "away",
                                "score": "0",
                                "linescores": [{"displayValue": "0"}],
                            },
                        ],
                        "status": {"type": {"name": "STATUS_FIRST_HALF"}},
                    }
                ]
            }
        }
    ).encode()
    update = parse_espn_event_update(
        payload,
        fixture_id="espn:401861447",
        retrieved_at=datetime(2026, 8, 14, 11, 20, tzinfo=timezone.utc),
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=401861447",
    )
    assert update is not None
    assert "halftime_score" not in update


def test_espn_live_fixture_is_polled_for_status_but_not_prematch_market():
    payload = json.dumps(
        {
            "header": {
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "score": "2"},
                            {"homeAway": "away", "score": "1"},
                        ],
                        "status": {"type": {"name": "STATUS_FIRST_HALF"}},
                    }
                ]
            },
            "pickcenter": [
                {
                    "provider": {"name": "DraftKings"},
                    "homeTeamOdds": {"moneyLine": -200},
                    "drawOdds": {"moneyLine": 300},
                    "awayTeamOdds": {"moneyLine": 500},
                }
            ],
        }
    ).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1/summary?event=1"

        def read(self, _limit):
            return payload

    def opener(request, timeout):
        assert timeout == 30
        return Response()

    result = fetch_espn_markets(
        [
            {
                "id": "espn:1",
                "competition_id": "csl",
                "status": "live",
                "kickoff_at": "2026-08-14T11:35:00+00:00",
                "source": {"native_fixture_id": "1"},
            }
        ],
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        opener=opener,
        authorization_reference="test-fixture",
    )
    assert result["status"] == "rights_blocked"
    assert len(result["markets"]) == 0
    assert result["fixture_updates"] == []
