"""Commercially reusable MET Norway weather adapter.

This module is deliberately independent from the non-commercial Open-Meteo
adapter.  It only requests the official Locationforecast 2.0 compact endpoint,
uses a unique identifying User-Agent, truncates coordinates to four decimals,
and keeps the raw response digest and CC BY attribution on every observation.
No venue or coordinate is inferred from a team name.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

from league_platform.live_sources.http_utils import read_response_bounded
from league_platform.source_rights import SourceId, UseCase, require_source_rights


MET_HOST = "api.met.no"
MET_PATH = "/weatherapi/locationforecast/2.0/compact"
MET_SOURCE_ID = SourceId.MET_NORWAY_WEATHER.value
MET_SOURCE_NAME = "MET Norway Locationforecast"
MET_LICENSE = "CC-BY-4.0"
MET_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
MET_TERMS_URL = "https://api.met.no/doc/TermsOfService"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_CONTENT_BYTES = 5 * 1024 * 1024
MAX_FIXTURES = 120
MAX_FORECAST_DAYS = 9
MAX_ERROR_ROWS = 64


def _utc(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} is not ISO-8601") from exc
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _coordinate_selection(
    fixture: dict[str, Any], *, allow_display_only_coordinates: bool = False
) -> tuple[float, float, str, bool] | None:
    """Return coordinates plus the evidence boundary attached to them.

    Preferred Wikidata venues are high-confidence and may be marked as
    coordinate-model-eligible.  Normal-rank venues are useful for a weather
    display, but remain medium-confidence and never become model input.  A
    bare coordinate on a fixture has no independently verified venue lineage,
    so it is retained only as an explicitly unverified display observation.
    """

    venue = fixture.get("venue")
    venue = venue if isinstance(venue, dict) else {}
    venue_model_eligible = venue.get("model_eligible")
    if venue_model_eligible is False and not allow_display_only_coordinates:
        return None
    candidates: tuple[tuple[Any, Any], ...]
    if venue_model_eligible is False:
        # A display-only override is deliberately narrower than the normal
        # path: use only the coordinates attached to the explicitly retained
        # venue record, never an unrelated top-level coordinate.
        candidates = (
            (venue.get("latitude"), venue.get("longitude")),
            (venue.get("lat"), venue.get("lon")),
        )
    else:
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
            and -90.0 <= latitude <= 90.0
            and -180.0 <= longitude <= 180.0
        ):
            if venue_model_eligible is True:
                confidence = venue.get("confidence")
                if confidence == "high":
                    return latitude, longitude, "high", True
                return latitude, longitude, "unverified", False
            if venue_model_eligible is False:
                confidence = venue.get("confidence")
                return (
                    latitude,
                    longitude,
                    confidence if confidence == "medium" else "unverified",
                    False,
                )
            return latitude, longitude, "unverified", False
    return None


def _coordinate_pair(fixture: dict[str, Any]) -> tuple[float, float] | None:
    """Backward-compatible coordinate-only helper for local callers."""

    selection = _coordinate_selection(fixture)
    return selection[:2] if selection is not None else None


def _four_decimals(value: float) -> str:
    # MET requires no more than four decimal places.  Formatting rounds at
    # the boundary while keeping a stable, compact URL for cache keys.
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _forecast_url(latitude: float, longitude: float) -> str:
    return f"https://{MET_HOST}{MET_PATH}?" + urlencode(
        {"lat": _four_decimals(latitude), "lon": _four_decimals(longitude)}
    )


def _validate_url(url: str) -> tuple[float, float]:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != MET_HOST
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != MET_PATH
        or parsed.fragment
    ):
        raise ValueError("MET Norway URL is not allowlisted")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) != {"lat", "lon"} or any(len(values) != 1 for values in query.values()):
        raise ValueError("MET Norway URL coordinates are invalid")
    try:
        latitude = float(query["lat"][0])
        longitude = float(query["lon"][0])
    except (TypeError, ValueError) as exc:
        raise ValueError("MET Norway URL coordinates are invalid") from exc
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    ):
        raise ValueError("MET Norway URL coordinates are invalid")
    return latitude, longitude


def _numeric(details: dict[str, Any], key: str) -> float | None:
    value = details.get(key)
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MET Norway field {key!r} is not numeric") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"MET Norway field {key!r} is not finite")
    return numeric


def parse_met_norway_payload(
    payload: bytes,
    *,
    fixture_id: str,
    kickoff_at: datetime | str,
    latitude: float,
    longitude: float,
    retrieved_at: datetime,
    url: str,
) -> dict[str, Any]:
    """Parse the exact forecast hour for one fixture."""

    if not isinstance(payload, bytes):
        raise TypeError("MET Norway response must be bytes")
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("MET Norway response exceeded 5 MiB")
    url_latitude, url_longitude = _validate_url(url)
    expected_latitude = float(_four_decimals(float(latitude)))
    expected_longitude = float(_four_decimals(float(longitude)))
    if (url_latitude, url_longitude) != (expected_latitude, expected_longitude):
        raise ValueError("MET Norway URL coordinates do not match fixture")
    target = _utc(kickoff_at, field="kickoff_at").replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    observed = _utc(retrieved_at, field="retrieved_at")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("MET Norway response is not valid JSON") from exc
    properties = data.get("properties") if isinstance(data, dict) else None
    timeseries = properties.get("timeseries") if isinstance(properties, dict) else None
    if not isinstance(timeseries, list):
        raise ValueError("MET Norway response is missing timeseries")
    selected: dict[str, Any] | None = None
    for item in timeseries:
        if not isinstance(item, dict):
            continue
        try:
            item_time = _utc(item.get("time"), field="forecast time")
        except (TypeError, ValueError):
            continue
        if item_time == target:
            selected = item
            break
    if selected is None:
        raise ValueError(f"MET Norway response has no exact forecast hour {target.isoformat()}")
    data_block = selected.get("data")
    instant = data_block.get("instant") if isinstance(data_block, dict) else None
    instant_details = instant.get("details") if isinstance(instant, dict) else None
    if not isinstance(instant_details, dict):
        raise ValueError("MET Norway forecast hour is missing instant details")
    hourly = None
    if isinstance(data_block, dict):
        hourly = data_block.get("next_1_hours") or data_block.get("next_6_hours")
    hourly_details = hourly.get("details") if isinstance(hourly, dict) else {}
    hourly_summary = hourly.get("summary") if isinstance(hourly, dict) else {}
    symbol_code = hourly_summary.get("symbol_code") if isinstance(hourly_summary, dict) else None
    if symbol_code is not None and not isinstance(symbol_code, str):
        raise ValueError("MET Norway symbol_code is invalid")
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "fixture_id": str(fixture_id),
        "forecast_at": target.isoformat(),
        "latitude": expected_latitude,
        "longitude": expected_longitude,
        "temperature_c": _numeric(instant_details, "air_temperature"),
        "humidity_percent": _numeric(instant_details, "relative_humidity"),
        "wind_speed_mps": _numeric(instant_details, "wind_speed"),
        "wind_direction_deg": _numeric(instant_details, "wind_from_direction"),
        "precipitation_mm": _numeric(hourly_details, "precipitation_amount")
        if isinstance(hourly_details, dict)
        else None,
        "symbol_code": symbol_code,
        "source": {
            "name": MET_SOURCE_NAME,
            "source_id": MET_SOURCE_ID,
            "url": url,
            "retrieved_at": observed.isoformat(),
            "raw_sha256": digest,
            "hash": f"sha256:{digest}",
            "license": MET_LICENSE,
            "license_url": MET_LICENSE_URL,
            "terms_url": MET_TERMS_URL,
            "attribution_required": True,
        },
    }


def fetch_met_norway_weather(
    fixtures: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    max_fixtures: int = MAX_FIXTURES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str,
    opener: Callable[..., Any] | Any | None = None,
    allow_display_only_coordinates: bool = False,
) -> dict[str, Any]:
    """Fetch bounded forecasts; display-only coordinates never enter the model."""

    require_source_rights(MET_SOURCE_ID, UseCase.NETWORK_FETCH)
    if not isinstance(user_agent, str) or len(user_agent.strip()) < 8:
        raise ValueError("MET Norway requires a descriptive User-Agent")
    if not isinstance(allow_display_only_coordinates, bool):
        raise TypeError("allow_display_only_coordinates must be a bool")
    if (
        not isinstance(max_fixtures, int)
        or isinstance(max_fixtures, bool)
        or not 1 <= max_fixtures <= MAX_FIXTURES
    ):
        raise ValueError(f"max_fixtures must be between 1 and {MAX_FIXTURES}")
    timeout_value = float(timeout)
    if (
        not math.isfinite(timeout_value)
        or timeout_value <= 0
        or timeout_value > MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT_SECONDS:g} seconds")
    reference = _utc(now or datetime.now(timezone.utc), field="now")
    open_method: Callable[..., Any] | None
    if opener is None:
        open_method = urllib.request.urlopen
    else:
        open_method = getattr(opener, "open", None)
    if not callable(open_method):
        raise TypeError("opener must provide open(request, timeout=...)")
    weather: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    error_count = 0

    def record_error(error: dict[str, str]) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_ERROR_ROWS - 1:
            errors.append(error)

    network_opened = False
    for fixture in fixtures[:max_fixtures]:
        fixture_id = fixture.get("id") if isinstance(fixture, dict) else None
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            record_error({"fixture_id": "", "reason": "fixture_id_missing"})
            continue
        coordinate_selection = _coordinate_selection(
            fixture,
            allow_display_only_coordinates=allow_display_only_coordinates,
        )
        if coordinate_selection is None:
            record_error({"fixture_id": fixture_id, "reason": "venue_coordinates_missing"})
            continue
        try:
            kickoff = _utc(fixture.get("kickoff_at"), field="kickoff_at")
        except (TypeError, ValueError):
            record_error({"fixture_id": fixture_id, "reason": "kickoff_at_invalid"})
            continue
        if kickoff < reference or kickoff > reference + timedelta(days=MAX_FORECAST_DAYS):
            record_error({"fixture_id": fixture_id, "reason": "forecast_outside_9_day_horizon"})
            continue
        latitude, longitude, coordinate_confidence, coordinate_model_eligible = (
            coordinate_selection
        )
        url = _forecast_url(latitude, longitude)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent.strip(),
            },
        )
        try:
            network_opened = True
            with open_method(request, timeout=timeout_value) as response:
                payload = read_response_bounded(response, MAX_CONTENT_BYTES)
            observation = parse_met_norway_payload(
                payload,
                fixture_id=fixture_id,
                kickoff_at=kickoff,
                latitude=latitude,
                longitude=longitude,
                retrieved_at=reference,
                url=url,
            )
            observation["coordinate_confidence"] = coordinate_confidence
            observation["coordinate_model_eligible"] = coordinate_model_eligible
            weather.append(observation)
        except (OSError, TimeoutError, ValueError, TypeError) as exc:
            record_error({"fixture_id": fixture_id, "reason": str(exc)[:240]})
    if error_count > len(errors):
        errors.append(
            {
                "fixture_id": "",
                "reason": f"errors_truncated:{error_count - len(errors)}_additional",
            }
        )
    if weather and errors:
        status = "partial"
    elif weather:
        status = "ok"
    else:
        status = "unavailable"
    return {
        "provider": MET_SOURCE_NAME,
        "source_id": MET_SOURCE_ID,
        "license": MET_LICENSE,
        "license_url": MET_LICENSE_URL,
        "terms_url": MET_TERMS_URL,
        "attribution_required": True,
        "retrieved_at": reference.isoformat(),
        "network_opened": network_opened,
        "status": status,
        "weather": weather,
        "errors": errors,
        "error_count": error_count,
    }


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_CONTENT_BYTES",
    "MET_HOST",
    "MET_LICENSE",
    "MET_LICENSE_URL",
    "MET_PATH",
    "MET_SOURCE_ID",
    "MET_SOURCE_NAME",
    "fetch_met_norway_weather",
    "parse_met_norway_payload",
]
