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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode, urlparse


OPEN_METEO_HOST = "api.open-meteo.com"
OPEN_METEO_PATH = "/v1/forecast"
OPEN_METEO_SOURCE_NAME = "Open-Meteo"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_CONTENT_BYTES = 5 * 1024 * 1024
DEFAULT_HORIZON_DAYS = 16

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


def _safe_opener():
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            raise urllib.error.URLError("redirects are disabled for live source fetches")

    return urllib.request.build_opener(_NoRedirect())


def _read_limited(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
    *,
    timeout: float,
    max_bytes: int,
    expected_host: str,
    expected_path: str,
) -> bytes:
    open_method = getattr(opener, "open", None)
    if open_method is None:
        open_method = opener
    response = open_method(request, timeout=timeout)  # type: ignore[operator]
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        parsed = urlparse(final_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != expected_host
            or parsed.path != expected_path
        ):
            raise ValueError("live source redirected to a non-allowlisted URL")
        payload = response.read(max_bytes + 1)
    if not isinstance(payload, bytes):
        raise TypeError("live source response must be bytes")
    if len(payload) > max_bytes:
        raise ValueError("Open-Meteo response exceeded 5 MiB")
    return payload


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
    """Fetch weather for upcoming fixtures with per-fixture error isolation.

    Fixtures should contain ``kickoff_at`` and venue coordinates under
    ``latitude``/``longitude`` (or ``venue``).  Records outside the horizon or
    without coordinates are returned as explicit errors.  The adapter never
    falls back to a cached or synthetic weather value.
    """

    if not isinstance(horizon_days, int) or horizon_days < 0:
        raise ValueError("horizon_days must be a non-negative integer")
    if not isinstance(max_fixtures, int) or max_fixtures < 0:
        raise ValueError("max_fixtures must be a non-negative integer")
    timeout = _validated_timeout(timeout)
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_time = reference_time.astimezone(timezone.utc)
    cutoff = reference_time + timedelta(days=horizon_days)
    fetcher = opener if opener is not None else _safe_opener()
    weather: list[dict] = []
    errors: list[dict] = []
    observation_times: list[datetime] = []

    candidates = []
    for index, fixture in enumerate(fixtures):
        fixture_id = str(fixture.get("id", fixture.get("fixture_id", index)))
        try:
            kickoff = _utc_datetime(fixture["kickoff_at"], field="kickoff_at")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"fixture_id": fixture_id, "stage": "input", "error": str(exc)})
            continue
        if fixture.get("status") not in (None, "upcoming"):
            errors.append(
                {
                    "fixture_id": fixture_id,
                    "stage": "input",
                    "error": "weather is only requested for upcoming fixtures",
                }
            )
            continue
        if kickoff < reference_time or kickoff > cutoff:
            errors.append(
                {
                    "fixture_id": fixture_id,
                    "stage": "horizon",
                    "error": "fixture kickoff is outside the weather horizon",
                }
            )
            continue
        coordinates = _coordinate_pair(fixture)
        if coordinates is None:
            errors.append(
                {
                    "fixture_id": fixture_id,
                    "stage": "coordinates",
                    "error": "venue coordinates are unavailable or invalid",
                }
            )
            continue
        candidates.append((fixture_id, kickoff, *coordinates))

    for fixture_id, kickoff, latitude, longitude in candidates[:max_fixtures]:
        url = _weather_url(latitude, longitude, kickoff)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "Matchline/1.0"},
        )
        try:
            payload = _read_limited(
                fetcher,
                request,
                timeout=timeout,
                max_bytes=MAX_CONTENT_BYTES,
                expected_host=OPEN_METEO_HOST,
                expected_path=OPEN_METEO_PATH,
            )
            observed_at = reference_time if now is not None else datetime.now(timezone.utc)
            weather.append(
                parse_open_meteo_payload(
                    payload,
                    fixture_id=fixture_id,
                    kickoff_at=kickoff,
                    retrieved_at=observed_at,
                    url=url,
                )
            )
            observation_times.append(observed_at)
        except Exception as exc:  # source-level isolation is part of the contract
            errors.append({"fixture_id": fixture_id, "stage": "fetch", "url": url, "error": str(exc)})

    for fixture_id, *_ in candidates[max_fixtures:]:
        errors.append(
            {"fixture_id": fixture_id, "stage": "limit", "error": "fixture fetch limit exceeded"}
        )
    weather.sort(key=lambda item: item["kickoff_at"])
    return {
        "provider": OPEN_METEO_SOURCE_NAME,
        "retrieved_at": max(observation_times, default=reference_time).isoformat(),
        "horizon_days": horizon_days,
        "requested_fixtures": min(len(candidates), max_fixtures),
        "weather": weather,
        "errors": errors,
    }


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
