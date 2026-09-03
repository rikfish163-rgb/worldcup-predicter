#!/usr/bin/env python3
"""Collect small, facts-only source snapshots on the Matchline VPS.

This collector is intentionally independent from the legacy ``wc-predict``
service.  It stores one bounded JSON document and never writes predictions,
odds, model fields, or raw provider payloads.  The output is suitable for a
later authenticated import into the Matchline Sites read model, but this
script itself performs no remote POST.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


CACHE_SCHEMA = "matchline.remote.facts.v1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_OPENLIGADB_ROWS = 2_000
MAX_WIKIDATA_ENTITIES = 12
USER_AGENT = "MatchlineFactsCollector/1.0 (+facts-only; no-model-data)"

OPENLIGADB_LICENSE_URL = "https://www.openligadb.de/lizenz"
WIKIDATA_LICENSE_URL = "https://www.wikidata.org/wiki/Wikidata:Licensing"
MET_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
MET_TERMS_URL = "https://api.met.no/doc/TermsOfService"


class NoRedirect(urllib.request.HTTPRedirectHandler):
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


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or now_utc()).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def text(value: object, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def current_season(reference: datetime | None = None) -> int:
    value = reference or now_utc()
    return value.year if value.month >= 7 else value.year - 1


def parse_datetime(value: object) -> str | None:
    raw = text(value, 100)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _http_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[int, bytes, object | None]:
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, method="GET", headers=request_headers)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            declared = response.headers.get("Content-Length")
            if declared and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
                return status, b"", None
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_RESPONSE_BYTES + 1)
        return int(exc.code), body, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b"", None
    if len(body) > MAX_RESPONSE_BYTES:
        return status, body, None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return status, body, parsed


def _source_status(
    *,
    source_id: str,
    name: str,
    url: str,
    retrieved_at: str,
    status: str,
    record_count: int | None,
    license_url: str | None,
    terms_url: str | None = None,
    http_status: int | None = None,
    error_code: str | None = None,
    body: bytes = b"",
) -> dict[str, object]:
    result: dict[str, object] = {
        "sourceId": source_id,
        "name": name,
        "status": status,
        "retrievedAt": retrieved_at,
        "recordCount": record_count,
        "licenseUrl": license_url,
        "termsUrl": terms_url,
        "httpStatus": http_status,
        "errorCode": error_code,
        "rawSha256": sha256_hex(body) if body else None,
        "modelEligible": False,
        "attributionRequired": bool(license_url),
    }
    return result


def collect_openligadb(season: int, retrieved_at: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    url = f"https://api.openligadb.de/getmatchdata/bl1/{season}"
    status, body, payload = _http_json(url)
    if status != 200 or not isinstance(payload, list):
        return (
            _source_status(
                source_id="openligadb_secondary_results",
                name="OpenLigaDB Bundesliga",
                url=url,
                retrieved_at=retrieved_at,
                status="unavailable" if status == 0 else "failed",
                record_count=None,
                license_url=OPENLIGADB_LICENSE_URL,
                http_status=status or None,
                error_code="http_error" if status else "transport_error",
                body=body,
            ),
            [],
        )

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in payload[:MAX_OPENLIGADB_ROWS]:
        if not isinstance(value, Mapping):
            continue
        match_id = value.get("matchID", value.get("MatchID"))
        if isinstance(match_id, bool) or not isinstance(match_id, (int, str)):
            continue
        match_token = str(match_id).strip()
        if not match_token or match_token in seen:
            continue
        kickoff = parse_datetime(value.get("matchDateTimeUTC", value.get("MatchDateTimeUTC")))
        team_one_value = value.get("team1", value.get("Team1"))
        team_two_value = value.get("team2", value.get("Team2"))
        team_one = team_one_value if isinstance(team_one_value, Mapping) else {}
        team_two = team_two_value if isinstance(team_two_value, Mapping) else {}
        home = text(team_one.get("teamName", team_one.get("TeamName")))
        away = text(team_two.get("teamName", team_two.get("TeamName")))
        if not kickoff or not home or not away:
            continue
        finished = value.get("matchIsFinished", value.get("MatchIsFinished")) is True
        row: dict[str, object] = {
            "id": f"openligadb:{match_token}",
            "provider": "OpenLigaDB",
            "competitionId": "bundesliga",
            "kickoffAt": kickoff,
            "homeTeam": home,
            "awayTeam": away,
            "status": "finished" if finished else "scheduled",
        }
        results = value.get("matchResults", value.get("MatchResults"))
        if finished and isinstance(results, list):
            score: dict[str, int] | None = None
            for result in results:
                if not isinstance(result, Mapping) or result.get("resultName", result.get("ResultName")) != "Endergebnis":
                    continue
                home_score = result.get("pointsTeam1", result.get("PointsTeam1"))
                away_score = result.get("pointsTeam2", result.get("PointsTeam2"))
                if isinstance(home_score, int) and not isinstance(home_score, bool) and isinstance(away_score, int) and not isinstance(away_score, bool):
                    score = {"home": home_score, "away": away_score}
                    break
            if score is not None:
                row["score"] = score
        rows.append(row)
        seen.add(match_token)
    rows.sort(key=lambda item: (str(item["kickoffAt"]), str(item["id"])))
    source = _source_status(
        source_id="openligadb_secondary_results",
        name="OpenLigaDB Bundesliga",
        url=url,
        retrieved_at=retrieved_at,
        status="fresh" if rows else "observed_empty",
        record_count=len(rows),
        license_url=OPENLIGADB_LICENSE_URL,
        http_status=status,
        error_code=None if rows else "no_valid_matches",
        body=body,
    )
    return source, rows


def collect_wikidata(retrieved_at: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    query = urllib.parse.urlencode({
        "action": "wbsearchentities",
        "search": "Bundesliga",
        "language": "en",
        "format": "json",
        "limit": "4",
    })
    url = f"https://www.wikidata.org/w/api.php?{query}"
    status, body, payload = _http_json(url)
    entities: list[dict[str, object]] = []
    if status == 200 and isinstance(payload, Mapping) and isinstance(payload.get("search"), list):
        for value in payload["search"][:MAX_WIKIDATA_ENTITIES]:
            if not isinstance(value, Mapping):
                continue
            entity_id = text(value.get("id"), 32)
            label = text(value.get("label"), 160)
            description = text(value.get("description"), 240)
            if not entity_id or not label:
                continue
            entities.append({
                "id": entity_id,
                "label": label,
                "description": description,
                "url": f"https://www.wikidata.org/entity/{entity_id}",
            })
    source = _source_status(
        source_id="wikidata_entities",
        name="Wikidata structured entities",
        url=url,
        retrieved_at=retrieved_at,
        status="fresh" if entities else (
            "unavailable" if status == 0 else
            "blocked" if status in {401, 403, 451} else
            "observed_empty"
        ),
        record_count=len(entities) if entities else None,
        license_url=WIKIDATA_LICENSE_URL,
        http_status=status or None,
        error_code=None if entities else (
            "provider_policy_or_access_block" if status in {401, 403, 451} else "no_entity_results"
        ),
        body=body,
    )
    return source, entities


def collect_met(retrieved_at: str, coordinates: Mapping[str, object] | None) -> tuple[dict[str, object], list[dict[str, object]]]:
    if not coordinates:
        return (
            _source_status(
                source_id="met_norway_weather",
                name="MET Norway Locationforecast",
                url="https://api.met.no/weatherapi/locationforecast/2.0/compact",
                retrieved_at=retrieved_at,
                status="not_configured",
                record_count=None,
                license_url=MET_LICENSE_URL,
                terms_url=MET_TERMS_URL,
                error_code="coordinates_not_configured",
            ),
            [],
        )
    observations: list[dict[str, object]] = []
    first_url = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
    last_status = 0
    last_body = b""
    for name, coordinate in list(coordinates.items())[:12]:
        if not isinstance(name, str) or not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
            continue
        latitude, longitude = coordinate
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        query = urllib.parse.urlencode({"lat": f"{latitude:.4f}", "lon": f"{longitude:.4f}"})
        url = f"{first_url}?{query}"
        status, body, payload = _http_json(url, headers={"User-Agent": f"{USER_AGENT} contact=matchline"})
        last_status, last_body = status, body
        if status != 200 or not isinstance(payload, Mapping):
            continue
        properties = payload.get("properties") if isinstance(payload.get("properties"), Mapping) else {}
        timeseries = properties.get("timeseries") if isinstance(properties.get("timeseries"), list) else []
        first = timeseries[0] if timeseries and isinstance(timeseries[0], Mapping) else None
        if not first:
            continue
        details = first.get("data") if isinstance(first.get("data"), Mapping) else {}
        instant = details.get("instant") if isinstance(details.get("instant"), Mapping) else {}
        details_values = instant.get("details") if isinstance(instant.get("details"), Mapping) else {}
        observations.append({
            "location": name[:120],
            "latitude": round(float(latitude), 4),
            "longitude": round(float(longitude), 4),
            "forecastAt": parse_datetime(first.get("time")),
            "temperatureC": details_values.get("air_temperature"),
            "windSpeedMps": details_values.get("wind_speed"),
            "precipitationMm": (first.get("data") or {}).get("next_1_hours", {}).get("details", {}).get("precipitation_amount") if isinstance(first.get("data"), Mapping) and isinstance(first.get("data").get("next_1_hours"), Mapping) else None,
        })
    source = _source_status(
        source_id="met_norway_weather",
        name="MET Norway Locationforecast",
        url=first_url,
        retrieved_at=retrieved_at,
        status="fresh" if observations else ("unavailable" if last_status == 0 else "observed_empty"),
        record_count=len(observations) if observations else None,
        license_url=MET_LICENSE_URL,
        terms_url=MET_TERMS_URL,
        http_status=last_status or None,
        error_code=None if observations else "no_valid_forecasts",
        body=last_body,
    )
    return source, observations


def probe_provider(source_id: str, name: str, url: str, retrieved_at: str) -> dict[str, object]:
    status, body, _ = _http_json(url)
    if status in {401, 403, 451}:
        state = "blocked"
        error = "provider_policy_or_access_block"
    elif status == 200:
        state = "reachable_not_admitted"
        error = "adapter_not_enabled_in_this_collector"
    elif status == 0:
        state = "unavailable"
        error = "transport_error"
    else:
        state = "failed"
        error = "http_error"
    return _source_status(
        source_id=source_id,
        name=name,
        url=url,
        retrieved_at=retrieved_at,
        status=state,
        record_count=None,
        license_url=None,
        http_status=status or None,
        error_code=error,
        body=body,
    )


def load_coordinates(raw: str | None) -> Mapping[str, object] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, Mapping) else None


def collect(*, season: int, coordinates: Mapping[str, object] | None) -> dict[str, object]:
    retrieved_at = iso()
    openligadb_source, matches = collect_openligadb(season, retrieved_at)
    wikidata_source, entities = collect_wikidata(retrieved_at)
    met_source, weather = collect_met(retrieved_at, coordinates)
    sources = [
        openligadb_source,
        wikidata_source,
        met_source,
        probe_provider(
            "espn_public_api",
            "ESPN public API (diagnostic only)",
            "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?limit=1",
            retrieved_at,
        ),
        probe_provider(
            "sofascore_public_api",
            "SofaScore public API (diagnostic only)",
            "https://api.sofascore.com/api/v1/sport/football/scheduled-events/2026-09-03",
            retrieved_at,
        ),
    ]
    return {
        "schema": CACHE_SCHEMA,
        "retrievedAt": retrieved_at,
        "hostname": socket.gethostname()[:120],
        "factsOnly": True,
        "predictions": [],
        "odds": [],
        "sources": sources,
        "openligadb": {"season": season, "matches": matches},
        "wikidata": {"entities": entities},
        "metNorway": {"observations": weather},
    }


def atomic_write(path: Path, value: Mapping[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = canonical_json_bytes(value) + b"\n"
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return sha256_hex(body)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=os.environ.get("MATCHLINE_FACTS_OUTPUT", "/home/ubuntu/matchline-facts/current.json"))
    parser.add_argument("--season", type=int, default=current_season())
    parser.add_argument("--coordinates-json", default=os.environ.get("MATCHLINE_MET_COORDINATES"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    value = collect(season=args.season, coordinates=load_coordinates(args.coordinates_json))
    digest = atomic_write(Path(args.output), value)
    print(json.dumps({
        "status": "ok",
        "schema": CACHE_SCHEMA,
        "output": str(args.output),
        "sha256": digest,
        "openligadb": len(value["openligadb"]["matches"]),
        "wikidata": len(value["wikidata"]["entities"]),
        "metNorway": len(value["metNorway"]["observations"]),
        "factsOnly": value["factsOnly"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
