"""Offline contract tests for the Open-Meteo and SofaScore adapters."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.open_meteo import (
    fetch_open_meteo_weather,
    parse_open_meteo_payload,
)
from league_platform.live_sources.sofascore import (
    fetch_sofascore_prematch,
    parse_sofascore_lineups_payload,
    parse_sofascore_scheduled_events_payload,
)


AS_OF = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, _limit):
        return self.payload


class _Routes:
    def __init__(self, routes: dict[str, bytes | Exception]):
        self.routes = routes
        self.calls: list[tuple[str, float]] = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        route = next((value for suffix, value in self.routes.items() if request.full_url.endswith(suffix)), None)
        if isinstance(route, Exception):
            raise route
        if route is None:
            raise AssertionError(f"unexpected URL: {request.full_url}")
        return _Response(route, request.full_url)


def _weather_payload() -> bytes:
    return json.dumps(
        {
            "latitude": 51.5,
            "longitude": -0.1,
            "hourly": {
                "time": ["2026-08-11T12:00", "2026-08-11T13:00"],
                "temperature_2m": [20.5, 21.0],
                "relative_humidity_2m": [65, 63],
                "precipitation_probability": [10, 20],
                "precipitation": [0.0, 0.2],
                "wind_speed_10m": [12.0, 13.0],
            },
        }
    ).encode()


def test_open_meteo_parser_keeps_exact_hour_and_raw_hash():
    payload = _weather_payload()
    url = (
        "https://api.open-meteo.com/v1/forecast?latitude=51.500000&longitude=-0.100000"
        "&hourly=temperature_2m%2Crelative_humidity_2m%2Cprecipitation_probability%2C"
        "precipitation%2Cwind_speed_10m&start_date=2026-08-11&end_date=2026-08-11&timezone=UTC"
    )

    observation = parse_open_meteo_payload(
        payload,
        fixture_id="espn:weather-1",
        kickoff_at="2026-08-11T12:00:00+00:00",
        retrieved_at=AS_OF,
        url=url,
    )

    assert observation["temperature_c"] == 20.5
    assert observation["wind_speed_kmh"] == 12.0
    assert observation["source"]["retrieved_at"] == AS_OF.isoformat()
    assert observation["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()


def test_open_meteo_fetch_isolates_missing_coordinates_and_network_errors():
    routes = _Routes({"/v1/forecast?": OSError("offline")})
    fixtures = [
        {
            "id": "missing-location",
            "kickoff_at": "2026-08-11T12:00:00+00:00",
            "status": "upcoming",
        },
        {
            "id": "network-failure",
            "kickoff_at": "2026-08-11T13:00:00+00:00",
            "status": "upcoming",
            "latitude": 51.5,
            "longitude": -0.1,
        },
    ]

    result = fetch_open_meteo_weather(fixtures, now=AS_OF, opener=routes)

    assert result["weather"] == []
    assert {error["stage"] for error in result["errors"]} == {"coordinates", "fetch"}
    assert routes.calls and routes.calls[0][1] == 10.0


def test_open_meteo_rejects_non_allowlisted_source_url():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_open_meteo_payload(
            _weather_payload(),
            fixture_id="bad",
            kickoff_at="2026-08-11T12:00:00+00:00",
            retrieved_at=AS_OF,
            url="https://example.invalid/v1/forecast",
        )


def _event_payload(event_id: int = 777) -> bytes:
    return json.dumps(
        {
            "event": {
                "id": event_id,
                "startTimestamp": 1786449600,
                "status": {"type": "notstarted"},
                "homeTeam": {"id": 10, "name": "Arsenal"},
                "awayTeam": {"id": 20, "name": "Coventry City"},
            }
        }
    ).encode()


def _lineups_payload() -> bytes:
    return json.dumps(
        {
            "confirmed": True,
            "home": {
                "formation": "4-3-3",
                "players": [
                    {"player": {"id": 1, "name": "Keeper"}, "starter": True},
                    {"player": {"id": 2, "name": "Bench"}, "substitute": True},
                ],
                "missingPlayers": [
                    {"player": {"id": 3, "name": "Unavailable"}, "reason": "injury"}
                ],
            },
            "away": {"players": [], "missingPlayers": []},
        }
    ).encode()


def test_sofascore_lineups_parser_distinguishes_missing_players_from_empty_data():
    payload = _lineups_payload()
    observation = parse_sofascore_lineups_payload(
        payload,
        event_id="777",
        fixture_id="espn:sofa-1",
        retrieved_at=AS_OF,
        url="https://api.sofascore.com/api/v1/event/777/lineups",
    )

    assert observation["lineups"]["confirmed"] is True
    assert observation["lineups"]["home"]["players"][0]["name"] == "Keeper"
    assert observation["injuries"]["home"][0]["reason"] == "injury"
    assert observation["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()


def test_sofascore_schedule_parser_hashes_original_schedule_response():
    payload = json.dumps(
        {
            "events": [
                {
                    "id": 777,
                    "startTimestamp": 1786449600,
                    "status": {"type": "notstarted"},
                    "homeTeam": {"id": 10, "name": "Arsenal"},
                    "awayTeam": {"id": 20, "name": "Coventry City"},
                }
            ]
        }
    ).encode()
    events = parse_sofascore_scheduled_events_payload(
        payload,
        retrieved_at=AS_OF,
        url="https://api.sofascore.com/api/v1/sport/football/scheduled-events/2026-08-11",
    )

    assert events[0]["event_id"] == "777"
    assert events[0]["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()


def test_sofascore_fetch_returns_event_when_lineups_are_unavailable():
    event = _event_payload()
    routes = _Routes({"/event/777": event, "/event/777/lineups": OSError("not published")})
    fixture = {
        "id": "espn:sofa-1",
        "sofascore_event_id": "777",
        "kickoff_at": "2026-08-11T12:00:00+00:00",
        "status": "upcoming",
        "home_team": "Arsenal",
        "away_team": "Coventry City",
    }

    result = fetch_sofascore_prematch([fixture], now=AS_OF, opener=routes)

    assert len(result["events"]) == 1
    assert result["events"][0]["event"]["event_id"] == "777"
    assert result["events"][0]["lineups"] is None
    assert result["events"][0]["injuries"] is None
    assert result["status"] == "degraded"
    assert any(error["stage"] == "lineups" for error in result["errors"])


def test_sofascore_fetch_maps_schedule_event_and_keeps_lineups():
    schedule = json.dumps(
        {
            "events": [
                {
                    "id": 777,
                    "startTimestamp": 1786449600,
                    "status": {"type": "notstarted"},
                    "homeTeam": {"id": 10, "name": "Arsenal"},
                    "awayTeam": {"id": 20, "name": "Coventry City"},
                }
            ]
        }
    ).encode()
    routes = _Routes(
        {
            "/sport/football/scheduled-events/2026-08-11": schedule,
            "/event/777/lineups": _lineups_payload(),
        }
    )
    fixture = {
        "id": "espn:sofa-1",
        "kickoff_at": "2026-08-11T12:00:00+00:00",
        "status": "upcoming",
        "home_team": "Arsenal",
        "away_team": "Coventry City",
    }

    result = fetch_sofascore_prematch([fixture], now=AS_OF, opener=routes)

    assert result["status"] == "available"
    assert result["events"][0]["event"]["event_id"] == "777"
    assert result["events"][0]["lineups"]["confirmed"] is True
    assert result["events"][0]["source"]["raw_sha256"] == hashlib.sha256(schedule).hexdigest()
    assert any("scheduled-events/2026-08-11" in call[0] for call in routes.calls)
