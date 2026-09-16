from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from league_platform import publish_met_weather_cache as publisher


def _snapshot(now: datetime) -> dict:
    kickoff = (now + timedelta(hours=24)).isoformat()
    return {
        "fixture_feed": {
            "fixtures": [
                {
                    "id": "openfootball:premier-league:weather-1",
                    "competition_id": "premier-league",
                    "kickoff_at": kickoff,
                    "home_team": "West Bromwich Albion FC",
                    "away_team": "Millwall FC",
                    "status": "upcoming",
                },
                {
                    "id": "openfootball:premier-league:finished",
                    "competition_id": "premier-league",
                    "kickoff_at": (now - timedelta(hours=1)).isoformat(),
                    "home_team": "Finished FC",
                    "away_team": "Other FC",
                    "status": "finished",
                },
            ]
        }
    }


def test_select_upcoming_fixtures_is_bounded_and_ignores_finished_rows() -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    rows = publisher.select_upcoming_fixtures(_snapshot(now), now=now, max_fixtures=24)
    assert [row["id"] for row in rows] == ["openfootball:premier-league:weather-1"]
    assert rows[0]["kickoff_at"] == now + timedelta(hours=24)


def test_build_cache_payload_keeps_only_display_weather_and_no_model_values(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    venue = {
        "name": "The Hawthorns",
        "wikidata_id": "Q31678",
        "latitude": 52.509166,
        "longitude": -1.963888,
        "confidence": "medium",
        "model_eligible": False,
    }
    monkeypatch.setattr(
        publisher,
        "fetch_wikidata_venues",
        lambda fixtures, **kwargs: {"venues": [{"fixture_id": fixtures[0]["id"], "venue": venue}]},
    )
    monkeypatch.setattr(
        publisher,
        "fetch_met_norway_weather",
        lambda fixtures, **kwargs: {
            "retrieved_at": now.isoformat(),
            "weather": [{
                "fixture_id": fixtures[0]["id"],
                "forecast_at": (now + timedelta(hours=24)).replace(minute=0, second=0, microsecond=0).isoformat(),
                "latitude": 52.5092,
                "longitude": -1.9639,
                "temperature_c": 19.7,
                "humidity_percent": 72.0,
                "wind_speed_mps": 3.5,
                "wind_direction_deg": 220.0,
                "precipitation_mm": 0.2,
                "symbol_code": "partlycloudy_day",
                "coordinate_confidence": "medium",
                "coordinate_model_eligible": False,
                "source": {
                    "name": publisher.MET_SOURCE_NAME,
                    "source_id": publisher.MET_SOURCE_ID,
                    "url": "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=52.5092&lon=-1.9639",
                    "retrieved_at": now.isoformat(),
                    "raw_sha256": "a" * 64,
                    "license": publisher.MET_LICENSE,
                    "license_url": publisher.MET_LICENSE_URL,
                    "terms_url": publisher.MET_TERMS_URL,
                    "attribution_required": True,
                },
            }],
        },
    )
    body = publisher.build_cache_payload(_snapshot(now), now=now)
    payload = json.loads(body)
    assert payload["schema"] == publisher.CACHE_SCHEMA
    assert len(payload["observations"]) == 1
    row = payload["observations"][0]
    assert row["venueName"] == "The Hawthorns"
    assert row["coordinateModelEligible"] is False
    assert "prediction" not in row
    assert "market" not in row
    assert row["source"]["license"] == "CC-BY-4.0"


def test_build_cache_payload_fails_without_a_real_weather_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        publisher,
        "fetch_wikidata_venues",
        lambda fixtures, **kwargs: {"venues": []},
    )
    with pytest.raises(ValueError, match="weather_venue_join_unavailable"):
        publisher.build_cache_payload(_snapshot(now), now=now)


def test_weather_endpoint_is_fixed_and_cannot_redirect_or_carry_query() -> None:
    assert publisher.normalized_endpoint(publisher.DEFAULT_ENDPOINT) == publisher.DEFAULT_ENDPOINT
    for value in (
        "https://evil.example/api/v1/weather",
        "https://matchline-intelligence.willif57kbkd.chatgpt.site/api/v1/weather?token=leak",
        "http://matchline-intelligence.willif57kbkd.chatgpt.site/api/v1/weather",
        "https://matchline-intelligence.willif57kbkd.chatgpt.site/api/v1/weather/other",
    ):
        with pytest.raises(ValueError):
            publisher.normalized_endpoint(value)


def test_weather_timer_uses_server_cache_and_a_bounded_runtime_input() -> None:
    root = publisher.Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/matchline-met-weather-cache.service").read_text(encoding="utf-8")
    timer = (root / "deploy/systemd/matchline-met-weather-cache.timer").read_text(encoding="utf-8")
    assert "publish_met_weather_cache" in service
    assert "--max-fixtures 24" in service
    assert "MATCHLINE_INGEST_TOKEN" not in service
    assert "MATCHLINE_MET_WEATHER_CACHE_ENDPOINT" in service
    assert "/dev/shm/matchline-live-runtime/current.json" in service
    assert "OnUnitActiveSec=60min" in timer
