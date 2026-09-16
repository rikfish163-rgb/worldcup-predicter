"""Publish a bounded MET Norway weather cache to the Sites server.

The producer reads the current fixture feed, resolves a small set of home
venues through Wikidata, fetches the exact MET Norway forecast hour, and sends
only the resulting display-only observations to one authenticated server
endpoint.  It never writes a local snapshot and never creates model fields.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from league_platform.live_sources.met_norway import (
    MET_LICENSE,
    MET_LICENSE_URL,
    MET_SOURCE_ID,
    MET_SOURCE_NAME,
    MET_TERMS_URL,
    fetch_met_norway_weather,
)
from league_platform.live_sources.wikidata import fetch_wikidata_venues


CACHE_SCHEMA = "matchline.met_norway.weather.cache.v1"
DEFAULT_ENDPOINT = "https://matchline-intelligence.willif57kbkd.chatgpt.site/api/v1/weather"
DEFAULT_RUNTIME_PATH = "/dev/shm/matchline-live-runtime/current.json"
TOKEN_ENV = "MATCHLINE_INGEST_TOKEN"
ENDPOINT_ENV = "MATCHLINE_MET_WEATHER_CACHE_ENDPOINT"
MAX_FIXTURES = 24
MAX_FORECAST_DAYS = 9
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BODY_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalized_endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "matchline-intelligence.willif57kbkd.chatgpt.site"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
        or parsed.path.rstrip("/") != "/api/v1/weather"
    ):
        raise ValueError("MET Norway weather endpoint must be the fixed Sites HTTPS route")
    return urllib.parse.urlunsplit(parsed)


def _utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field} must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: object, field: str) -> str:
    return _utc(value, field).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_runtime_snapshot(path: Path = Path(DEFAULT_RUNTIME_PATH)) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"runtime snapshot is unreadable: {path}") from exc
    if not raw or len(raw) > 32 * 1024 * 1024:
        raise ValueError("runtime snapshot exceeds its byte limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime snapshot is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("runtime snapshot must be an object")
    return value


def select_upcoming_fixtures(
    snapshot: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_fixtures: int = MAX_FIXTURES,
) -> list[dict[str, Any]]:
    if not isinstance(max_fixtures, int) or isinstance(max_fixtures, bool) or not 1 <= max_fixtures <= MAX_FIXTURES:
        raise ValueError(f"max_fixtures must be between 1 and {MAX_FIXTURES}")
    reference = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    fixture_feed = snapshot.get("fixture_feed")
    rows = fixture_feed.get("fixtures") if isinstance(fixture_feed, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("runtime snapshot has no fixture feed")
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in rows:
        if not isinstance(value, Mapping):
            continue
        fixture_id = value.get("id")
        kickoff = value.get("kickoff_at")
        if not isinstance(fixture_id, str) or not fixture_id.strip() or fixture_id in seen:
            continue
        try:
            kickoff_at = _utc(kickoff, "fixture kickoff")
        except ValueError:
            continue
        if kickoff_at < reference or kickoff_at > reference + timedelta(days=MAX_FORECAST_DAYS):
            continue
        if value.get("status") not in ("upcoming", "scheduled", None):
            continue
        home_team = value.get("home_team")
        away_team = value.get("away_team")
        competition_id = value.get("competition_id")
        if not all(isinstance(item, str) and item.strip() for item in (home_team, away_team, competition_id)):
            continue
        selected.append({
            "id": fixture_id,
            "competition_id": competition_id,
            "kickoff_at": kickoff_at,
            "home_team": home_team,
            "away_team": away_team,
            "status": "upcoming",
        })
        seen.add(fixture_id)
        if len(selected) >= max_fixtures:
            break
    selected.sort(key=lambda row: (row["kickoff_at"], row["id"]))
    return selected


def build_cache_payload(
    snapshot: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_fixtures: int = MAX_FIXTURES,
    user_agent: str = "MatchlineMetWeatherCache/1.0 (+server-cache)",
) -> bytes:
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fixtures = select_upcoming_fixtures(snapshot, now=reference, max_fixtures=max_fixtures)
    if not fixtures:
        raise ValueError("no_upcoming_fixtures_for_weather")
    venue_result = fetch_wikidata_venues(
        fixtures,
        now=reference,
        max_fixtures=max_fixtures,
        max_requests=96,
        user_agent=user_agent,
    )
    venues = {
        row.get("fixture_id"): row.get("venue")
        for row in venue_result.get("venues", [])
        if isinstance(row, Mapping)
        and isinstance(row.get("fixture_id"), str)
        and isinstance(row.get("venue"), Mapping)
    }
    fixtures_with_venues: list[dict[str, Any]] = []
    for fixture in fixtures:
        venue = venues.get(fixture["id"])
        if isinstance(venue, Mapping):
            item = dict(fixture)
            item["venue"] = dict(venue)
            fixtures_with_venues.append(item)
    if not fixtures_with_venues:
        raise ValueError("weather_venue_join_unavailable")
    weather_result = fetch_met_norway_weather(
        fixtures_with_venues,
        now=reference,
        max_fixtures=max_fixtures,
        user_agent=user_agent,
        allow_display_only_coordinates=True,
    )
    weather_rows = weather_result.get("weather")
    if not isinstance(weather_rows, list) or not weather_rows:
        raise ValueError("met_norway_weather_unavailable")
    by_id = {fixture["id"]: fixture for fixture in fixtures_with_venues}
    observations: list[dict[str, Any]] = []
    retrieved_at = _iso(weather_result.get("retrieved_at"), "weather retrieved_at")
    for weather in weather_rows:
        if not isinstance(weather, Mapping):
            continue
        fixture_id = weather.get("fixture_id")
        fixture = by_id.get(fixture_id) if isinstance(fixture_id, str) else None
        source = weather.get("source")
        venue = fixture.get("venue") if isinstance(fixture, Mapping) else None
        if not isinstance(fixture, Mapping) or not isinstance(source, Mapping) or not isinstance(venue, Mapping):
            continue
        source_retrieved_at = _iso(source.get("retrieved_at"), "weather source retrieved_at")
        observations.append({
            "fixtureId": fixture_id,
            "competitionId": fixture["competition_id"],
            "kickoffAt": _iso(fixture["kickoff_at"], "fixture kickoff"),
            "homeTeam": fixture["home_team"],
            "awayTeam": fixture["away_team"],
            "venueName": venue.get("name") or "场地待确认",
            "forecastAt": _iso(weather.get("forecast_at"), "forecast_at"),
            "latitude": weather.get("latitude"),
            "longitude": weather.get("longitude"),
            "temperatureC": weather.get("temperature_c"),
            "humidityPercent": weather.get("humidity_percent"),
            "windSpeedMps": weather.get("wind_speed_mps"),
            "windDirectionDeg": weather.get("wind_direction_deg"),
            "precipitationMm": weather.get("precipitation_mm"),
            "symbolCode": weather.get("symbol_code"),
            "coordinateConfidence": weather.get("coordinate_confidence", "unverified"),
            "coordinateModelEligible": False,
            "source": {
                "name": source.get("name", MET_SOURCE_NAME),
                "sourceId": source.get("source_id", MET_SOURCE_ID),
                "url": source.get("url"),
                "retrievedAt": source_retrieved_at,
                "rawSha256": source.get("raw_sha256"),
                "license": source.get("license", MET_LICENSE),
                "licenseUrl": source.get("license_url", MET_LICENSE_URL),
                "termsUrl": source.get("terms_url", MET_TERMS_URL),
                "attributionRequired": True,
            },
        })
    if not observations:
        raise ValueError("met_norway_weather_projection_empty")
    if retrieved_at != observations[0]["source"]["retrievedAt"]:
        raise ValueError("met_norway_weather_retrieval_epoch_mismatch")
    body = canonical_json_bytes({
        "schema": CACHE_SCHEMA,
        "retrievedAt": retrieved_at,
        "observations": observations,
    })
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("MET Norway weather cache upload exceeds its byte limit")
    return body


def upload_cache(
    body: bytes,
    *,
    endpoint: str,
    token: str,
    timeout: float = 60.0,
) -> Mapping[str, object]:
    if not token:
        raise ValueError(f"{TOKEN_ENV} is required")
    request = urllib.request.Request(
        normalized_endpoint(endpoint),
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "MatchlineMetWeatherCache/1.0",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            response_body = response.read(MAX_RESPONSE_BODY_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        raise ValueError(f"MET Norway cache endpoint returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError("MET Norway cache endpoint transport failed") from exc
    if len(response_body) > MAX_RESPONSE_BODY_BYTES:
        raise ValueError("MET Norway cache acknowledgement exceeds its byte limit")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("MET Norway cache acknowledgement is not JSON") from exc
    if not isinstance(result, Mapping) or status != 201 or result.get("status") != "ok":
        raise ValueError("MET Norway cache acknowledgement is invalid")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-snapshot", type=Path, default=Path(os.environ.get("MATCHLINE_RUNTIME_SNAPSHOT", DEFAULT_RUNTIME_PATH)))
    parser.add_argument("--endpoint", default=os.environ.get(ENDPOINT_ENV, DEFAULT_ENDPOINT))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-fixtures", type=int, default=MAX_FIXTURES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshot = load_runtime_snapshot(args.runtime_snapshot)
    body = build_cache_payload(snapshot, max_fixtures=args.max_fixtures)
    result = upload_cache(
        body,
        endpoint=args.endpoint,
        token=os.environ.get(TOKEN_ENV, ""),
        timeout=args.timeout,
    )
    print(json.dumps({
        "status": result.get("status"),
        "schema": result.get("schema"),
        "retrievedAt": result.get("retrievedAt"),
        "rowCount": result.get("rowCount"),
        "sizeBytes": result.get("sizeBytes"),
        "sha256": result.get("sha256"),
        "key": result.get("key"),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CACHE_SCHEMA",
    "DEFAULT_ENDPOINT",
    "build_cache_payload",
    "canonical_json_bytes",
    "load_runtime_snapshot",
    "normalized_endpoint",
    "select_upcoming_fixtures",
    "upload_cache",
]
