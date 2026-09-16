"""Allowlisted Open-Meteo geocoding for venue cities.

Geocoding is deliberately separate from weather.  A venue without a reliable
city is unavailable; the adapter never turns a team name into a guessed
coordinate.  Results are accepted only when the provider returns the same
normalized city and, when supplied, the same country code.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

from league_platform.source_rights import SourceId, rights_blocked_envelope


GEOCODING_HOST = "geocoding-api.open-meteo.com"
GEOCODING_PATH = "/v1/search"
GEOCODING_SOURCE_NAME = "Open-Meteo Geocoding"
MAX_CONTENT_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_CONCURRENT_REQUESTS = 8

# ESPN sometimes uses a local-language or historical city spelling that the
# geocoder does not index, while the canonical city is unambiguous once the
# country is part of the query.  Keep this list deliberately small and
# explicit; unknown names still fail closed instead of being guessed.
# Values are canonical geocoder query keys, not identity rewrites.  The
# original ESPN venue label remains in every returned record and the selected
# query is retained for auditability.  Keep this list explicit and small:
# unknown or ambiguous venue labels still fail closed.
_CITY_QUERY_ALIASES = {
    ("sevilla", "spain"): "Seville",
    ("lacoruna", "spain"): "A Coruña",
    ("milano", "italy"): "Milan",
    ("genova", "italy"): "Genoa",
    ("roma", "italy"): "Rome",
    ("firenze", "italy"): "Florence",
    ("napoli", "italy"): "Naples",
    ("newcastleupontyne", "england"): "Newcastle upon Tyne",
    ("hamburgnorderstedt", "germany"): "Hamburg",
}

# ESPN's current fixture summaries contain two known venue-label defects.  A
# city/country pair alone is not enough for the Liaoning correction, so it is
# keyed by the explicit stadium name as well.  These are query normalizations
# for weather only; they never change the fixture/team identity.
_VENUE_QUERY_ALIASES = {
    (
        "tiexinewdistrictsportscentre",
        "liaoning",
        "chinapr",
    ): ("Shenyang", "China", "CN"),
    (
        "stadelouisii",
        "monaco",
        "france",
    ): ("Monaco", "Monaco", "MC"),
}


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != GEOCODING_HOST
        or parsed.port not in (None, 443)
        or parsed.path != GEOCODING_PATH
    ):
        raise ValueError("Open-Meteo geocoding URL is not allowlisted")


def _normalise(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _country_matches(expected: str, observed: str) -> bool:
    expected_key = _normalise(expected)
    observed_key = _normalise(observed)
    if not expected_key or not observed_key:
        return True
    aliases = {
        "england": "unitedkingdom",
        "scotland": "unitedkingdom",
        "wales": "unitedkingdom",
        "northernireland": "unitedkingdom",
        "uk": "unitedkingdom",
        "usa": "unitedstates",
        "us": "unitedstates",
        "unitedstatesofamerica": "unitedstates",
        "chinapr": "china",
    }
    return aliases.get(expected_key, expected_key) == aliases.get(observed_key, observed_key)


def _venue_key(fixture: dict) -> tuple[str, str, str] | None:
    venue = fixture.get("venue")
    if not isinstance(venue, dict):
        return None
    city = str(venue.get("city") or "").strip()
    country = str(venue.get("country") or "").strip()
    country_code = str(venue.get("country_code") or "").strip().upper()
    return (city, country, country_code) if city else None


def _query_key(key: tuple[str, str, str], *, venue_name: str = "") -> tuple[str, str, str]:
    city, country, country_code = key
    venue_alias = _VENUE_QUERY_ALIASES.get(
        (_normalise(venue_name), _normalise(city), _normalise(country))
    )
    if venue_alias:
        return venue_alias
    alias = _CITY_QUERY_ALIASES.get((_normalise(city), _normalise(country)))
    return (alias, country, country_code) if alias else key


def _url(key: tuple[str, str, str]) -> str:
    city, _country, _country_code = key
    query = urllib.parse.urlencode(
        {"name": city, "count": 5, "language": "en", "format": "json"}
    )
    return f"https://{GEOCODING_HOST}{GEOCODING_PATH}?{query}"


def _source(*, url: str, retrieved_at: datetime, payload: bytes) -> dict:
    return {
        "name": GEOCODING_SOURCE_NAME,
        "url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }


def parse_geocoding_payload(
    payload: bytes,
    *,
    fixture_id: str,
    venue_key: tuple[str, str, str],
    retrieved_at: datetime,
    url: str,
) -> dict:
    _validate_url(url)
    observed = _utc(retrieved_at, field="retrieved_at")
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("Open-Meteo geocoding response exceeded 2 MiB")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Open-Meteo geocoding response is not valid JSON") from exc
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ValueError("Open-Meteo geocoding response is missing results")
    city, country, country_code = venue_key
    city_key = _normalise(city)
    selected = None
    for item in results:
        if not isinstance(item, dict):
            continue
        candidate_city = item.get("city") or item.get("name")
        candidate_code = str(item.get("country_code") or "").upper()
        if _normalise(candidate_city) != city_key:
            continue
        if country_code and candidate_code and candidate_code != country_code:
            continue
        if country and not _country_matches(country, str(item.get("country") or "")):
            continue
        selected = item
        break
    if selected is None:
        raise ValueError("Open-Meteo geocoding returned no exact venue city")
    try:
        latitude = float(selected["latitude"])
        longitude = float(selected["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Open-Meteo geocoding result has invalid coordinates") from exc
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        raise ValueError("Open-Meteo geocoding coordinates are out of range")
    return {
        "fixture_id": str(fixture_id),
        "city": city,
        "requested_country": country,
        "country": country or selected.get("country"),
        "country_code": country_code or selected.get("country_code"),
        "latitude": latitude,
        "longitude": longitude,
        "timezone": selected.get("timezone"),
        "source": _source(url=url, retrieved_at=observed, payload=payload),
    }




def fetch_open_meteo_geocoding(
    fixtures: list[dict],
    *,
    now: datetime | None = None,
    max_fixtures: int = 120,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] | object | None = None,
) -> dict:
    """Return the v260 rights block before inspecting venues or the opener."""

    reference = _utc(now or datetime.now(timezone.utc), field="now")
    return rights_blocked_envelope(
        SourceId.OPEN_METEO_GEOCODING,
        provider=GEOCODING_SOURCE_NAME,
        checked_at=reference.isoformat(),
        empty_fields=("geocodes",),
    )


__all__ = ["fetch_open_meteo_geocoding", "parse_geocoding_payload"]
