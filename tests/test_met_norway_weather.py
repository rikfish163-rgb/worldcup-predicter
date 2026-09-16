from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from league_platform.source_rights import (
    DecisionValue,
    SourceId,
    UseCase,
    decide_source_rights,
)


def test_met_norway_weather_is_allowed_only_for_current_weather_use_cases() -> None:
    allowed = {
        UseCase.NETWORK_FETCH,
        UseCase.SERVE_CURRENT,
        UseCase.MODEL_INPUT,
        UseCase.REDISTRIBUTION,
    }
    for use_case in UseCase:
        decision = decide_source_rights(SourceId.MET_NORWAY_WEATHER, use_case)
        if use_case in allowed:
            assert decision.decision is DecisionValue.ALLOW
            assert decision.commercial_reuse_verified is True
            assert decision.model_eligible is True
            assert decision.attribution_required is True
        else:
            assert decision.decision is DecisionValue.BLOCK
            assert decision.model_eligible is False


def _payload() -> bytes:
    return json.dumps(
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-0.28, 51.5, 24]},
            "properties": {
                "meta": {"updated_at": "2026-08-28T12:00:00Z"},
                "timeseries": [
                    {
                        "time": "2026-08-28T18:00:00Z",
                        "data": {
                            "instant": {
                                "details": {
                                    "air_temperature": 18.2,
                                    "relative_humidity": 72.1,
                                    "wind_speed": 4.5,
                                    "wind_from_direction": 220.0,
                                }
                            },
                            "next_1_hours": {
                                "summary": {"symbol_code": "partlycloudy_day"},
                                "details": {"precipitation_amount": 0.4},
                            },
                        },
                    }
                ],
            },
        }
    ).encode()


def test_met_norway_parser_requires_exact_kickoff_hour_and_preserves_lineage() -> None:
    from league_platform.live_sources.met_norway import parse_met_norway_payload

    url = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=51.5&lon=-0.28"
    row = parse_met_norway_payload(
        _payload(),
        fixture_id="openfootball:premier-league:1001",
        kickoff_at="2026-08-28T18:30:00Z",
        latitude=51.5,
        longitude=-0.28,
        retrieved_at=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        url=url,
    )

    assert row["fixture_id"] == "openfootball:premier-league:1001"
    assert row["temperature_c"] == 18.2
    assert row["humidity_percent"] == 72.1
    assert row["wind_speed_mps"] == 4.5
    assert row["wind_direction_deg"] == 220.0
    assert row["precipitation_mm"] == 0.4
    assert row["symbol_code"] == "partlycloudy_day"
    assert row["source"]["source_id"] == "met_norway_weather"
    assert row["source"]["license"] == "CC-BY-4.0"
    assert len(row["source"]["raw_sha256"]) == 64

    with pytest.raises(ValueError, match="no exact forecast hour"):
        parse_met_norway_payload(
            _payload(),
            fixture_id="openfootball:premier-league:1001",
            kickoff_at="2026-08-28T19:30:00Z",
            latitude=51.5,
            longitude=-0.28,
            retrieved_at=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
            url=url,
        )


def test_met_norway_fetch_uses_identifying_user_agent_and_quarantines_missing_coordinates() -> (
    None
):
    from league_platform.live_sources.met_norway import fetch_met_norway_weather

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self, _size: int = -1) -> bytes:
            value, self.payload = self.payload, b""
            return value

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def __init__(self) -> None:
            self.request = None

        def open(self, request: object, timeout: float) -> Response:
            self.request = (request, timeout)
            return Response(_payload())

    opener = Opener()
    result = fetch_met_norway_weather(
        [
            {
                "id": "openfootball:premier-league:1001",
                "kickoff_at": "2026-08-28T18:00:00Z",
                "latitude": 51.5,
                "longitude": -0.28009,
            },
            {
                "id": "openfootball:premier-league:1002",
                "kickoff_at": "2026-08-28T18:00:00Z",
            },
        ],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        user_agent="Matchline/1.0 support@example.invalid",
        opener=opener,
    )

    assert result["status"] == "partial"
    assert len(result["weather"]) == 1
    assert result["errors"] == [
        {
            "fixture_id": "openfootball:premier-league:1002",
            "reason": "venue_coordinates_missing",
        }
    ]
    request, timeout = opener.request
    assert timeout == 10.0
    assert request.get_header("User-agent") == "Matchline/1.0 support@example.invalid"
    query = parse_qs(urlparse(request.full_url).query)
    assert query["lat"] == ["51.5"]
    assert query["lon"] == ["-0.2801"]


def test_met_norway_display_mode_uses_medium_venue_coordinates_without_model_admission() -> None:
    from league_platform.live_sources.met_norway import fetch_met_norway_weather

    class Response:
        def __init__(self) -> None:
            self.payload = _payload()

        def read(self, _size: int = -1) -> bytes:
            value, self.payload = self.payload, b""
            return value

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def __init__(self) -> None:
            self.calls = 0

        def open(self, _request: object, timeout: float) -> Response:
            assert timeout == 10.0
            self.calls += 1
            return Response()

    fixture = {
        "id": "openfootball:premier-league:normal-venue",
        "kickoff_at": "2026-08-28T18:00:00Z",
        "venue": {
            "name": "Normal-rank ground",
            "latitude": 51.5,
            "longitude": -0.28,
            "confidence": "medium",
            "model_eligible": False,
        },
    }

    default_opener = Opener()
    default_result = fetch_met_norway_weather(
        [fixture],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        user_agent="Matchline/1.0 support@example.invalid",
        opener=default_opener,
    )
    assert default_opener.calls == 0
    assert default_result["weather"] == []
    assert default_result["errors"] == [
        {"fixture_id": fixture["id"], "reason": "venue_coordinates_missing"}
    ]

    display_opener = Opener()
    display_result = fetch_met_norway_weather(
        [fixture],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        user_agent="Matchline/1.0 support@example.invalid",
        opener=display_opener,
        allow_display_only_coordinates=True,
    )
    assert display_opener.calls == 1
    assert display_result["status"] == "ok"
    assert display_result["weather"][0]["coordinate_confidence"] == "medium"
    assert display_result["weather"][0]["coordinate_model_eligible"] is False

    self_attested = {
        **fixture,
        "id": "openfootball:premier-league:self-attested-venue",
        "venue": {**fixture["venue"], "model_eligible": True},
    }
    self_attested_result = fetch_met_norway_weather(
        [self_attested],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        user_agent="Matchline/1.0 support@example.invalid",
        opener=Opener(),
        allow_display_only_coordinates=True,
    )
    assert self_attested_result["weather"][0]["coordinate_confidence"] == "unverified"
    assert self_attested_result["weather"][0]["coordinate_model_eligible"] is False


def test_met_norway_default_transport_uses_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    from league_platform.live_sources import met_norway

    class Response:
        def __init__(self) -> None:
            self.payload = _payload()

        def read(self, _size: int = -1) -> bytes:
            value, self.payload = self.payload, b""
            return value

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    calls: list[tuple[object, float]] = []

    def fake_urlopen(request: object, timeout: float) -> Response:
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr(met_norway.urllib.request, "urlopen", fake_urlopen)
    result = met_norway.fetch_met_norway_weather(
        [
            {
                "id": "openfootball:premier-league:1001",
                "kickoff_at": "2026-08-28T18:00:00Z",
                "latitude": 51.5,
                "longitude": -0.28,
            }
        ],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        user_agent="Matchline/1.0 support@example.invalid",
    )

    assert result["status"] == "ok"
    assert len(result["weather"]) == 1
    assert len(calls) == 1
    assert calls[0][1] == 10.0


def test_met_norway_error_diagnostics_are_bounded() -> None:
    from league_platform.live_sources.met_norway import fetch_met_norway_weather

    result = fetch_met_norway_weather(
        [
            {
                "id": f"openfootball:premier-league:{index}",
                "kickoff_at": "2026-08-28T18:00:00Z",
            }
            for index in range(120)
        ],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        user_agent="Matchline/1.0 support@example.invalid",
    )

    assert result["status"] == "unavailable"
    assert len(result["errors"]) <= 64
    assert result["error_count"] == 120
    assert result["errors"][-1]["reason"] == "errors_truncated:57_additional"
