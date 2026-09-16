"""Bounded Wikidata entity/venue join for current fixture context.

Only the narrow, auditable chain ``fixture home team -> one club entity ->
one preferred home venue -> one coordinate`` is admitted.  Search results,
ambiguous entities, missing preferred venues and malformed claims are kept as
diagnostics; none of them are guessed into a fixture.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from league_platform.live_sources.http_utils import read_response_bounded
from league_platform.source_rights import SourceId, UseCase, require_source_rights


WIKIDATA_HOST = "www.wikidata.org"
WIKIDATA_PATH = "/w/api.php"
WIKIDATA_SOURCE_ID = SourceId.WIKIDATA_ENTITIES.value
WIKIDATA_SOURCE_NAME = "Wikidata structured data"
WIKIDATA_LICENSE = "CC0-1.0"
WIKIDATA_LICENSE_URL = "https://www.wikidata.org/wiki/Wikidata:Licensing"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_FIXTURES = 120
DEFAULT_MAX_REQUESTS = 16
MAX_REQUESTS = 96
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


def _normal_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[.'’`’]", "", text)
    text = re.sub(r"\b(?:f\s*c|fc|a\s*f\s*c|afc|football club|club)\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _valid_entity_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"Q[1-9][0-9]*", value) is not None


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != WIKIDATA_HOST
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != WIKIDATA_PATH
        or parsed.fragment
    ):
        raise ValueError("Wikidata API URL is not allowlisted")


def _api_url(params: dict[str, str]) -> str:
    url = f"https://{WIKIDATA_HOST}{WIKIDATA_PATH}?{urlencode(params)}"
    _validate_url(url)
    return url


def _label(payload: dict[str, Any]) -> str | None:
    labels = payload.get("labels")
    if not isinstance(labels, dict):
        return None
    for language in ("en", "zh", "zh-hans"):
        value = labels.get(language)
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            return value["value"]
    return None


def _entity_from_claim(claim: object) -> str | None:
    if not isinstance(claim, dict):
        return None
    mainsnak = claim.get("mainsnak")
    datavalue = mainsnak.get("datavalue") if isinstance(mainsnak, dict) else None
    value = datavalue.get("value") if isinstance(datavalue, dict) else None
    entity_id = value.get("id") if isinstance(value, dict) else None
    return entity_id if _valid_entity_id(entity_id) else None


def _preferred_entity_claims(entity: dict[str, Any], property_id: str) -> list[str]:
    claims = entity.get("claims")
    rows = claims.get(property_id) if isinstance(claims, dict) else None
    if not isinstance(rows, list):
        return []
    preferred = [
        entity_id
        for row in rows
        if isinstance(row, dict)
        and row.get("rank") == "preferred"
        and (entity_id := _entity_from_claim(row)) is not None
    ]
    return sorted(set(preferred))


def _entity_claims(entity: dict[str, Any], property_id: str) -> list[str]:
    claims = entity.get("claims")
    rows = claims.get(property_id) if isinstance(claims, dict) else None
    if not isinstance(rows, list):
        return []
    return sorted(
        {entity_id for row in rows if (entity_id := _entity_from_claim(row)) is not None}
    )


def _coordinates(entity: dict[str, Any]) -> tuple[float, float] | None:
    claims = entity.get("claims")
    rows = claims.get("P625") if isinstance(claims, dict) else None
    if not isinstance(rows, list):
        return None
    ranked = sorted(
        (row for row in rows if isinstance(row, dict)),
        key=lambda row: 0 if row.get("rank") == "preferred" else 1,
    )
    for row in ranked:
        mainsnak = row.get("mainsnak")
        datavalue = mainsnak.get("datavalue") if isinstance(mainsnak, dict) else None
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if not isinstance(value, dict):
            continue
        try:
            latitude = float(value["latitude"])
            longitude = float(value["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            return latitude, longitude
    return None


def _search_candidate(payload: dict[str, Any], team_name: str) -> str:
    rows = payload.get("search")
    if not isinstance(rows, list):
        raise ValueError("team_entity_search_malformed")
    candidates = [row for row in rows if isinstance(row, dict) and _valid_entity_id(row.get("id"))]
    if not candidates:
        raise ValueError("team_entity_not_found")
    wanted = _normal_name(team_name)
    exact = [
        row
        for row in candidates
        if _normal_name(row.get("label")) == wanted
        or _normal_name(row.get("label")).startswith(wanted)
    ]
    football = [
        row
        for row in candidates
        if any(
            marker in str(row.get("description") or "").casefold()
            for marker in (
                "association football club",
                "football club",
                "professional football club",
                "football team",
            )
        )
        and not any(
            marker
            in (
                str(row.get("label") or "").casefold()
                + " "
                + str(row.get("description") or "").casefold()
            )
            for marker in (
                " women",
                " women's",
                " femen",
                " ii",
                " reserve",
                " junior",
                " youth",
                "juvenil",
                "academy",
                "u23",
                "u-23",
            )
        )
    ]
    # The API search is deliberately broad (a city, station, company, or
    # academy often shares the club's name).  Prefer a unique senior-club
    # candidate, but only when its description is unambiguously stronger than
    # the alternatives.  A tie remains quarantined instead of guessing a
    # venue for a similarly named club.
    pool = football or exact or candidates
    wanted = _normal_name(team_name)

    def score(row: dict[str, Any]) -> tuple[int, str]:
        label = _normal_name(row.get("label"))
        description = str(row.get("description") or "").casefold()
        value = 0
        if label == wanted:
            value += 4
        elif label.startswith(wanted) or wanted.startswith(label):
            value += 2
        if "association football club" in description:
            value += 3
        elif "professional football club" in description:
            value += 2
        elif "football club" in description or "football team" in description:
            value += 1
        if "former" in description or "defunct" in description:
            value -= 4
        return value, str(row["id"])

    ranked = sorted(pool, key=score, reverse=True)
    if not ranked:
        raise ValueError("team_entity_not_found")
    top_score = score(ranked[0])[0]
    top_ids = sorted({str(row["id"]) for row in ranked if score(row)[0] == top_score})
    if len(top_ids) != 1:
        raise ValueError("team_entity_ambiguous")
    return top_ids[0]


def _open_json(
    open_method: Callable[..., Any],
    url: str,
    *,
    user_agent: str,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": user_agent},
    )
    with open_method(request, timeout=timeout) as response:
        payload = read_response_bounded(response, MAX_CONTENT_BYTES)
        final_url = str(getattr(response, "geturl", lambda: url)())
    _validate_url(final_url)
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("wikidata_response_invalid_json") from exc
    if not isinstance(decoded, dict):
        raise ValueError("wikidata_response_root_not_object")
    return decoded, hashlib.sha256(payload).hexdigest()


def fetch_wikidata_venues(
    fixtures: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    max_fixtures: int = MAX_FIXTURES,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str,
    opener: Callable[..., Any] | Any | None = None,
) -> dict[str, Any]:
    """Fetch exact club-to-venue coordinates for a bounded fixture set."""

    require_source_rights(WIKIDATA_SOURCE_ID, UseCase.NETWORK_FETCH)
    if not isinstance(user_agent, str) or len(user_agent.strip()) < 8:
        raise ValueError("Wikidata requires a descriptive User-Agent")
    if (
        not isinstance(max_fixtures, int)
        or isinstance(max_fixtures, bool)
        or not 1 <= max_fixtures <= MAX_FIXTURES
    ):
        raise ValueError(f"max_fixtures must be between 1 and {MAX_FIXTURES}")
    if (
        not isinstance(max_requests, int)
        or isinstance(max_requests, bool)
        or not 1 <= max_requests <= MAX_REQUESTS
    ):
        raise ValueError(f"max_requests must be between 1 and {MAX_REQUESTS}")
    timeout_value = float(timeout)
    if (
        not math.isfinite(timeout_value)
        or timeout_value <= 0
        or timeout_value > MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT_SECONDS:g} seconds")
    reference = _utc(now or datetime.now(timezone.utc), field="now")
    open_method = urllib.request.urlopen if opener is None else getattr(opener, "open", None)
    if not callable(open_method):
        raise TypeError("opener must provide open(request, timeout=...)")

    venues: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    error_counts: dict[str, int] = {}
    network_opened = False
    request_count = 0
    budget_exhausted = False
    team_cache: dict[str, tuple[Any, ...]] = {}
    venue_cache: dict[str, tuple[dict[str, Any], str, str]] = {}

    def record(reason: str) -> None:
        error_counts[reason] = error_counts.get(reason, 0) + 1

    def fetch_json(url: str) -> tuple[dict[str, Any], str]:
        nonlocal network_opened, request_count, budget_exhausted
        if request_count >= max_requests:
            budget_exhausted = True
            raise ValueError("wikidata_request_budget_exhausted")
        request_count += 1
        network_opened = True
        return _open_json(open_method, url, user_agent=user_agent, timeout=timeout_value)

    for fixture in fixtures[:max_fixtures]:
        if budget_exhausted:
            break
        if (
            not isinstance(fixture, dict)
            or not isinstance(fixture.get("id"), str)
            or not fixture["id"].strip()
        ):
            record("fixture_id_missing")
            continue
        fixture_id = fixture["id"]
        home_team = fixture.get("home_team")
        if not isinstance(home_team, str) or not home_team.strip():
            record("home_team_missing")
            continue
        cache_key = _normal_name(home_team)
        try:
            if cache_key not in team_cache:
                search_url = _api_url(
                    {
                        "action": "wbsearchentities",
                        "search": home_team.strip(),
                        "language": "en",
                        "uselang": "en",
                        "format": "json",
                        "limit": "5",
                    }
                )
                search_payload, search_hash = fetch_json(search_url)
                team_qid = _search_candidate(search_payload, home_team)
                club_url = _api_url(
                    {
                        "action": "wbgetentities",
                        "ids": team_qid,
                        "props": "claims|labels|descriptions",
                        "languages": "en",
                        "format": "json",
                    }
                )
                club_payload, club_hash = fetch_json(club_url)
                entities = club_payload.get("entities")
                club = entities.get(team_qid) if isinstance(entities, dict) else None
                if not isinstance(club, dict):
                    raise ValueError("team_entity_missing")
                preferred_venues = _preferred_entity_claims(club, "P115")
                all_venues = _entity_claims(club, "P115")
                if len(preferred_venues) == 1:
                    venue_claim_rank = "preferred"
                    selected_venue = preferred_venues[0]
                    row_model_eligible = True
                elif not preferred_venues and len(all_venues) == 1:
                    # A single normal-rank P115 claim is useful for display,
                    # but it is not strong enough to enter weather/model
                    # features.  Preserve the distinction explicitly.
                    venue_claim_rank = "normal"
                    selected_venue = all_venues[0]
                    row_model_eligible = False
                else:
                    raise ValueError(
                        "preferred_home_venue_ambiguous"
                        if preferred_venues or len(all_venues) > 1
                        else "preferred_home_venue_missing"
                    )
                team_cache[cache_key] = (
                    selected_venue,
                    club,
                    club_hash,
                    venue_claim_rank,
                    row_model_eligible,
                )
                # Keep the search digest in the cache tuple's metadata via a
                # separate deterministic map, avoiding extra requests while
                # retaining provenance on each projected row.
                team_search_hash = search_hash
            else:
                team_qid = team_cache[cache_key][0]
                club = team_cache[cache_key][1]
                club_hash = team_cache[cache_key][2]
                venue_claim_rank = team_cache[cache_key][3]
                row_model_eligible = team_cache[cache_key][4]
                team_search_hash = ""
            venue_qid = team_qid
            if cache_key in team_cache:
                venue_qid = team_cache[cache_key][0]
            if venue_qid not in venue_cache:
                venue_url = _api_url(
                    {
                        "action": "wbgetentities",
                        "ids": venue_qid,
                        "props": "claims|labels|descriptions",
                        "languages": "en",
                        "format": "json",
                    }
                )
                venue_payload, venue_hash = fetch_json(venue_url)
                entities = venue_payload.get("entities")
                venue_entity = entities.get(venue_qid) if isinstance(entities, dict) else None
                if not isinstance(venue_entity, dict):
                    raise ValueError("home_venue_entity_missing")
                coordinates = _coordinates(venue_entity)
                if coordinates is None:
                    raise ValueError("venue_coordinates_missing")
                venue_cache[venue_qid] = (venue_entity, venue_hash, venue_url)
            venue_entity, venue_hash, venue_url = venue_cache[venue_qid]
            coordinates = _coordinates(venue_entity)
            if coordinates is None:
                raise ValueError("venue_coordinates_missing")
            latitude, longitude = coordinates
            observed_at = _utc(fixture.get("observed_at") or reference, field="observed_at")
            venues.append(
                {
                    "fixture_id": fixture_id,
                    "venue": {
                        "name": _label(venue_entity) or venue_qid,
                        "wikidata_id": venue_qid,
                        "latitude": latitude,
                        "longitude": longitude,
                        "claim_rank": venue_claim_rank,
                        "confidence": "high" if row_model_eligible else "medium",
                        "model_eligible": row_model_eligible,
                        "source": {
                            "name": WIKIDATA_SOURCE_NAME,
                            "source_id": WIKIDATA_SOURCE_ID,
                            "url": venue_url,
                            "retrieved_at": observed_at.isoformat(),
                            "raw_sha256": venue_hash,
                            "license": WIKIDATA_LICENSE,
                            "license_url": WIKIDATA_LICENSE_URL,
                            "attribution_required": False,
                        },
                        "lineage": {
                            "team_name": home_team,
                            "team_wikidata_id": _entity_id_from_cache(club, cache_key, team_cache),
                            "venue_wikidata_id": venue_qid,
                            "search_raw_sha256": team_search_hash,
                            "club_raw_sha256": club_hash,
                            "venue_raw_sha256": venue_hash,
                        },
                    },
                }
            )
        except (OSError, TimeoutError, TypeError, ValueError, KeyError) as exc:
            reason = (
                str(exc)
                if str(exc).startswith(
                    ("team_entity_", "preferred_", "home_venue_", "venue_", "wikidata_")
                )
                else "source_unavailable"
            )
            record(reason)

    for reason in sorted(error_counts):
        errors.append({"reason": reason, "count": error_counts[reason]})
    if venues and errors:
        status = "partial"
    elif venues:
        status = "ok"
    else:
        status = "unavailable"
    return {
        "provider": WIKIDATA_SOURCE_NAME,
        "source_id": WIKIDATA_SOURCE_ID,
        "license": WIKIDATA_LICENSE,
        "license_url": WIKIDATA_LICENSE_URL,
        "attribution_required": False,
        "retrieved_at": reference.isoformat(),
        "network_opened": network_opened,
        "status": status,
        "venues": venues,
        "errors": errors,
        "error_count": sum(error_counts.values()),
        "record_count": len(venues),
        "request_count": request_count,
        "request_budget": max_requests,
        "model_eligible": any(
            isinstance(item.get("venue"), dict) and item["venue"].get("model_eligible") is True
            for item in venues
        ),
    }


def _entity_id_from_cache(
    club: dict[str, Any], cache_key: str, cache: dict[str, tuple[Any, ...]]
) -> str | None:
    for team_qid, (_venue_qid, entity, *_metadata) in cache.items():
        if team_qid == cache_key and entity is club:
            value = entity.get("id")
            return value if isinstance(value, str) else None
    return None


__all__ = [
    "WIKIDATA_HOST",
    "WIKIDATA_PATH",
    "WIKIDATA_SOURCE_ID",
    "WIKIDATA_SOURCE_NAME",
    "WIKIDATA_LICENSE",
    "WIKIDATA_LICENSE_URL",
    "DEFAULT_MAX_REQUESTS",
    "MAX_REQUESTS",
    "fetch_wikidata_venues",
]
