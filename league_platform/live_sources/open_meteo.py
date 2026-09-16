"""Small, read-only Open-Meteo adapter for pre-match weather observations.

The adapter intentionally accepts fixture coordinates from the caller.  The
current ESPN fixture contract does not contain venue coordinates, so a fixture
without coordinates is reported as unavailable instead of guessing a stadium
or a city.  URLs are built from a fixed HTTPS host and path; callers cannot
provide an arbitrary endpoint.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode, urlparse

from league_platform.source_rights import SourceId, rights_blocked_envelope


OPEN_METEO_HOST = "api.open-meteo.com"
OPEN_METEO_PATH = "/v1/forecast"
OPEN_METEO_SOURCE_NAME = "Open-Meteo"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_CONTENT_BYTES = 5 * 1024 * 1024
# The public forecast endpoint's practical range is today plus the next
# fifteen days.  Keeping one day of margin avoids issuing requests at the
# provider's inclusive boundary, which otherwise returns HTTP 400 and looks
# like a source failure rather than an unavailable forecast.
DEFAULT_HORIZON_DAYS = 15
MAX_CONCURRENT_REQUESTS = 8

HOURLY_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
)


def _utc_datetime(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _validated_timeout(timeout: float) -> float:
    value = float(timeout)
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT_SECONDS:g} seconds")
    return value


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OPEN_METEO_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path != OPEN_METEO_PATH
    ):
        raise ValueError("Open-Meteo URL is not allowlisted")




def _coordinate_pair(fixture: dict) -> tuple[float, float] | None:
    venue = fixture.get("venue")
    if not isinstance(venue, dict):
        venue = {}
    candidates = (
        (fixture.get("latitude"), fixture.get("longitude")),
        (fixture.get("venue_latitude"), fixture.get("venue_longitude")),
        (venue.get("latitude"), venue.get("longitude")),
        (venue.get("lat"), venue.get("lon")),
    )
    for raw_latitude, raw_longitude in candidates:
        if raw_latitude is None or raw_longitude is None:
            continue
        try:
            latitude = float(raw_latitude)
            longitude = float(raw_longitude)
        except (TypeError, ValueError):
            continue
        if (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            return latitude, longitude
    return None


def _weather_url(latitude: float, longitude: float, target: datetime) -> str:
    query = urlencode(
        {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "hourly": ",".join(HOURLY_FIELDS),
            "start_date": target.strftime("%Y-%m-%d"),
            "end_date": target.strftime("%Y-%m-%d"),
            "timezone": "UTC",
        }
    )
    return f"https://{OPEN_METEO_HOST}{OPEN_METEO_PATH}?{query}"


def _hour_key(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def parse_open_meteo_payload(
    payload: bytes,
    *,
    fixture_id: str,
    kickoff_at: datetime | str,
    retrieved_at: datetime,
    url: str,
) -> dict:
    """Parse one Open-Meteo hourly response at the fixture kickoff hour.

    A missing target hour or malformed/short hourly series raises a
    ``ValueError``.  The caller records that error as source-level
    unavailability; no nearest-hour value is silently substituted.
    """

    _validate_url(url)
    target = _utc_datetime(kickoff_at, field="kickoff_at").replace(
        minute=0, second=0, microsecond=0
    )
    observed = _utc_datetime(retrieved_at, field="retrieved_at")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Open-Meteo response is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("hourly"), dict):
        raise ValueError("Open-Meteo response is missing hourly data")
    hourly = data["hourly"]
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        raise ValueError("Open-Meteo response is missing hourly timestamps")
    try:
        indices = [_hour_key(value) for value in times]
    except (TypeError, ValueError) as exc:
        raise ValueError("Open-Meteo hourly timestamps are invalid") from exc
    try:
        index = indices.index(target)
    except ValueError as exc:
        raise ValueError(f"Open-Meteo response has no exact kickoff hour {target.isoformat()}") from exc

    values: dict[str, object] = {}
    output_fields = {
        "temperature_2m": "temperature_c",
        "relative_humidity_2m": "humidity_percent",
        "precipitation_probability": "precipitation_probability_percent",
        "precipitation": "precipitation_mm",
        "wind_speed_10m": "wind_speed_kmh",
    }
    for field, output_name in output_fields.items():
        series = hourly.get(field)
        if not isinstance(series, list) or len(series) != len(times):
            raise ValueError(f"Open-Meteo hourly field {field!r} is missing or mis-sized")
        value = series[index]
        if value is not None:
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Open-Meteo hourly field {field!r} is not numeric") from exc
            if not math.isfinite(numeric):
                raise ValueError(f"Open-Meteo hourly field {field!r} is not finite")
            value = numeric
        values[output_name] = value

    return {
        "fixture_id": str(fixture_id),
        "kickoff_at": _utc_datetime(kickoff_at, field="kickoff_at").isoformat(),
        **values,
        "source": {
            "name": OPEN_METEO_SOURCE_NAME,
            "url": url,
            "retrieved_at": observed.isoformat(),
            "raw_sha256": hashlib.sha256(payload).hexdigest(),
        },
    }


def fetch_open_meteo_weather(
    fixtures: list[dict],
    *,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    max_fixtures: int = 120,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] | object | None = None,
) -> dict:
    """Return the v260 rights block before inspecting fixtures or the opener."""

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_time = reference_time.astimezone(timezone.utc)
    return rights_blocked_envelope(
        SourceId.OPEN_METEO_WEATHER,
        provider=OPEN_METEO_SOURCE_NAME,
        checked_at=reference_time.isoformat(),
        empty_fields=("weather",),
    )


# A descriptive alias for callers that prefer the API's forecast terminology.
fetch_open_meteo_forecasts = fetch_open_meteo_weather

__all__ = [
    "DEFAULT_HORIZON_DAYS",
    "MAX_CONTENT_BYTES",
    "OPEN_METEO_HOST",
    "fetch_open_meteo_forecasts",
    "fetch_open_meteo_weather",
    "parse_open_meteo_payload",
]
