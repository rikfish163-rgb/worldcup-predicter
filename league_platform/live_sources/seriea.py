"""Public Lega Serie A match-centre lineup adapter.

The official Serie A site exposes the current match list in public HTML and
uses a public SDP API for match headers and lineups.  The API response does
not provide a trustworthy lineup publication timestamp, so ``retrieved_at``
is retained as the causal observation time.  Incomplete or post-kickoff
lineups stay in the audit/display layer and never become model input.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Callable, Iterable
from urllib.parse import quote, unquote, urlparse

from unidecode import unidecode

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


SERIE_A_WEB_HOST = "en.legaseriea.it"
SERIE_A_API_HOST = "api-sdp.legaseriea.it"
SERIE_A_HOME_PATH = re.compile(r"/(?:serie-a/?)?")
SERIE_A_MATCH_PATH = re.compile(
    r"/serie-a/match/[0-9a-f]{32}/[a-z0-9-]+(?:/(?:info|lineups?|formation|live|stats))?/?",
    re.IGNORECASE,
)
SERIE_A_API_PATH = re.compile(
    r"/v1/serie-a/football/seasons/serie-a::Football_Season::[0-9a-f]{32}/"
    r"matches/serie-a::Football_Match::[0-9a-f]{32}/(?:header|lineups)",
    re.IGNORECASE,
)
SERIE_A_CRAWL_DELAY_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_BYTES = 12 * 1024 * 1024
API_MAX_BYTES = 4 * 1024 * 1024


class _AllowlistedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow only the official Serie A hosts and their allowlisted paths.

    The public match centre occasionally returns a temporary redirect while
    its edge cache is refreshing.  Refusing every redirect turns that
    transient transport state into a whole-provider outage.  The redirect
    target is still checked before it is followed and the final response is
    validated again by ``_read_limited``; no cross-host or path discovery is
    permitted.
    """

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        parsed = urlparse(newurl)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.hostname not in {SERIE_A_WEB_HOST, SERIE_A_API_HOST}
        ):
            raise urllib.error.URLError("Serie A redirect target is outside the official hosts")
        path = unquote(parsed.path)
        allowed = (
            parsed.hostname == SERIE_A_WEB_HOST
            and (path == "/serie-a" or path.startswith("/serie-a/"))
        ) or (parsed.hostname == SERIE_A_API_HOST and path.startswith("/v1/serie-a/"))
        if not allowed:
            raise urllib.error.URLError("Serie A redirect target is outside the allowlisted paths")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_opener():
    return urllib.request.build_opener(_AllowlistedRedirect())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_url(url: str, *, kind: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Serie A URL is not allowlisted")
    if kind == "home":
        if parsed.hostname != SERIE_A_WEB_HOST or not SERIE_A_HOME_PATH.fullmatch(parsed.path):
            raise ValueError("Serie A home URL is not allowlisted")
        return
    if kind == "match":
        if parsed.hostname != SERIE_A_WEB_HOST or not SERIE_A_MATCH_PATH.fullmatch(parsed.path):
            raise ValueError("Serie A match URL is not allowlisted")
        return
    if parsed.hostname != SERIE_A_API_HOST or not SERIE_A_API_PATH.fullmatch(unquote(parsed.path)):
        raise ValueError("Serie A API URL is not allowlisted")


def _read_limited(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
    *,
    kind: str,
    max_bytes: int,
) -> bytes:
    open_method = getattr(opener, "open", None) or opener
    response = open_method(request, timeout=DEFAULT_TIMEOUT_SECONDS)  # type: ignore[operator]
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        _validate_url(str(final_url), kind=kind)
        payload = response.read(max_bytes + 1)
    if not isinstance(payload, bytes):
        raise TypeError("Serie A response must be bytes")
    if len(payload) > max_bytes:
        raise ValueError(f"Serie A response exceeded {max_bytes // (1024 * 1024)} MiB")
    return payload


def _read_json(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
) -> tuple[dict, bytes]:
    payload = _read_limited(
        opener,
        request,
        kind="api",
        max_bytes=API_MAX_BYTES,
    )
    try:
        data = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Serie A API response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Serie A API response root is not an object")
    return data, payload


def _normalise(value: object) -> str:
    return "".join(ch for ch in unidecode(str(value or "")).lower() if ch.isalnum())


_GENERIC_TEAM_TOKENS = {
    "ac",
    "as",
    "calcio",
    "club",
    "fc",
    "ss",
}

_TEAM_ALIASES = {
    "internazionale": "inter",
    "intermilan": "inter",
    "acmilan": "milan",
    "asroma": "roma",
}


def _team_matches(expected: object, official: object, *, slug: str = "") -> bool:
    expected_text = _normalise(expected)
    official_text = _normalise(official)
    slug_text = _normalise(slug)
    if not expected_text or not official_text:
        return False
    if expected_text in official_text or official_text in expected_text:
        return True
    alias = _TEAM_ALIASES.get(expected_text)
    if alias and (alias in official_text or alias in slug_text):
        return True
    expected_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", unidecode(str(expected or "")).lower())
        if token not in _GENERIC_TEAM_TOKENS and len(token) >= 3
    }
    official_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", unidecode(str(official or "")).lower())
        if token not in _GENERIC_TEAM_TOKENS and len(token) >= 3
    }
    return bool(expected_tokens & official_tokens) and bool(
        any(token in slug_text for token in expected_tokens)
    )


def discover_match_urls(payload: bytes, *, url: str) -> list[str]:
    """Extract only public official Serie A match routes from the home page."""

    _validate_url(url, kind="home")
    text = payload.decode("utf-8", errors="strict")
    candidates = re.findall(
        r'href=["\']([^"\']*?/serie-a/match/[0-9a-f]{32}/[a-z0-9-]+[^"\']*)["\']',
        text,
        flags=re.IGNORECASE,
    )
    urls: list[str] = []
    for candidate in candidates:
        candidate = unescape(candidate)
        if candidate.startswith("/"):
            candidate = f"https://{SERIE_A_WEB_HOST}{candidate}"
        try:
            _validate_url(candidate, kind="match")
        except ValueError:
            continue
        if candidate not in urls:
            urls.append(candidate)
    return urls


def _season_id(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="strict")
    match = re.search(r"serie-a::Football_Season::([0-9a-f]{32})", text, flags=re.IGNORECASE)
    if match is None:
        raise ValueError("Serie A home page is missing the current season id")
    return f"serie-a::Football_Season::{match.group(1)}"


def _match_id(url: str) -> str:
    _validate_url(url, kind="match")
    match = re.search(r"/match/([0-9a-f]{32})/", urlparse(url).path, flags=re.IGNORECASE)
    if match is None:
        raise ValueError("Serie A match URL is missing its native match id")
    return match.group(1)


def _api_url(season_id: str, match_id: str, endpoint: str) -> str:
    if not re.fullmatch(r"serie-a::Football_Season::[0-9a-f]{32}", season_id, re.IGNORECASE):
        raise ValueError("Serie A season id is invalid")
    if not re.fullmatch(r"[0-9a-f]{32}", match_id, re.IGNORECASE):
        raise ValueError("Serie A match id is invalid")
    if endpoint not in {"header", "lineups"}:
        raise ValueError("Serie A API endpoint is invalid")
    return (
        f"https://{SERIE_A_API_HOST}/v1/serie-a/football/seasons/"
        f"{quote(season_id, safe='')}/matches/"
        f"{quote(f'serie-a::Football_Match::{match_id}', safe='')}/{endpoint}"
    )


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Serie A {field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Serie A {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Serie A {field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def parse_serie_a_header(
    data: dict,
    *,
    fixture_id: str,
    match_id: str,
    kickoff_at: str,
    retrieved_at: datetime,
    url: str,
    raw_payload: bytes,
    expected_home: str | None = None,
    expected_away: str | None = None,
) -> dict:
    """Validate the official header against the canonical fixture."""

    _validate_url(url, kind="api")
    observed = _utc(retrieved_at)
    official_kickoff = _parse_timestamp(data.get("matchDateUtc"), field="matchDateUtc")
    canonical_kickoff = _parse_timestamp(kickoff_at, field="kickoff_at")
    if abs((official_kickoff - canonical_kickoff).total_seconds()) > 90:
        raise ValueError("Serie A official kickoff does not match canonical fixture")
    native_match = data.get("matchId")
    expected_native = f"serie-a::Football_Match::{match_id}"
    if native_match != expected_native:
        raise ValueError("Serie A official match id does not match requested route")
    home = data.get("home") if isinstance(data.get("home"), dict) else {}
    away = data.get("away") if isinstance(data.get("away"), dict) else {}
    home_name = home.get("shortName") or home.get("officialName")
    away_name = away.get("shortName") or away.get("officialName")
    if expected_home and not _team_matches(expected_home, home_name):
        raise ValueError("Serie A official home team does not match canonical fixture")
    if expected_away and not _team_matches(expected_away, away_name):
        raise ValueError("Serie A official away team does not match canonical fixture")
    status = str(data.get("status") or "").casefold()
    normalized_status = (
        "finished" if status in {"finished", "full_time", "fulltime"} else "upcoming"
    )
    source = {
        "name": "Serie A official",
        "url": url,
        "retrieved_at": observed.isoformat(),
        "raw_sha256": hashlib.sha256(raw_payload).hexdigest(),
        "native_match_id": match_id,
        "native_fixture_id": match_id,
        "source_kind": "official_fixture",
        "time_basis": "observed_at_no_published_at",
        "effective_at": None,
    }
    row = {
        "id": fixture_id,
        "competition_id": "serie-a",
        "kickoff_at": official_kickoff.isoformat(),
        "home_team": home_name,
        "away_team": away_name,
        "status": normalized_status,
        "source": source,
    }
    stadium_name = data.get("stadiumName")
    if isinstance(stadium_name, str) and stadium_name.strip():
        venue: dict[str, str] = {"name": stadium_name.strip()}
        city_name = data.get("cityName")
        if isinstance(city_name, str) and city_name.strip():
            venue["city"] = city_name.strip()
        stadium_id = data.get("stadiumId")
        if isinstance(stadium_id, str) and stadium_id.strip():
            venue["native_venue_id"] = stadium_id.strip()
        row["venue"] = venue
    return row


def _player(row: object, *, status: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    provider_id = row.get("playerId") or row.get("providerId")
    if not provider_id:
        return None
    name = row.get("displayName") or row.get("shortName") or row.get("shirtName")
    if not name:
        first = str(row.get("mediaFirstName") or "").strip()
        last = str(row.get("mediaLastName") or "").strip()
        name = " ".join(part for part in (first, last) if part)
    if not name:
        return None
    return {
        "player_id": f"seriea:{provider_id}",
        "provider_player_id": str(provider_id),
        "name": str(name),
        "position": row.get("roleLabel"),
        "shirt_number": row.get("bibNumber"),
        "captain": bool(row.get("isCaptain")),
        "starter": status == "starter",
        "substitute": status == "substitute",
        "status": status,
        "expected_minutes": None,
    }


def _lineup_side(value: object) -> tuple[dict, bool]:
    if not isinstance(value, dict):
        return {"players": [], "available": False, "missing_players": []}, False
    fielded = value.get("fielded") if isinstance(value.get("fielded"), list) else []
    benched = value.get("benched") if isinstance(value.get("benched"), list) else []
    players: list[dict] = []
    for row in fielded:
        item = _player(row, status="starter")
        if item is not None:
            players.append(item)
    for row in benched:
        item = _player(row, status="substitute")
        if item is not None:
            players.append(item)
    starter_ids = {item["provider_player_id"] for item in players if item.get("starter")}
    complete = len(fielded) == 11 and len(starter_ids) == 11
    return {
        "players": players,
        "available": bool(players),
        "formation": value.get("tacticalFormation") or None,
        "missing_players": [],
    }, complete


def parse_serie_a_lineups(
    data: dict,
    *,
    fixture_id: str,
    match_id: str,
    kickoff_at: str,
    retrieved_at: datetime,
    url: str,
    raw_payload: bytes,
    header_source: dict,
) -> dict:
    """Normalize the official SDP lineup payload with strict causal checks."""

    _validate_url(url, kind="api")
    observed = _utc(retrieved_at)
    kickoff = _parse_timestamp(kickoff_at, field="kickoff_at")
    expected_native = f"serie-a::Football_Match::{match_id}"
    if data.get("matchId") != expected_native:
        raise ValueError("Serie A lineup match id does not match requested route")
    home, home_complete = _lineup_side(data.get("home"))
    away, away_complete = _lineup_side(data.get("away"))
    confirmed = bool(home_complete and away_complete)
    source = {
        "name": "Serie A official",
        "url": url,
        "retrieved_at": observed.isoformat(),
        "raw_sha256": hashlib.sha256(raw_payload).hexdigest(),
        "header_raw_sha256": header_source.get("raw_sha256"),
        "native_match_id": match_id,
        "source_kind": "official_lineup_api",
        "time_basis": "observed_at_no_published_at",
        "effective_at": None,
    }
    return {
        "fixture_id": fixture_id,
        "match_id": match_id,
        "lineups": {
            "confirmed": confirmed,
            "home": home,
            "away": away,
            "available": bool(home["available"] or away["available"]),
            "model_eligible": bool(confirmed and observed < kickoff),
            "missing_fields": ["expected_minutes", "replacement_value"],
        },
        "source": source,
    }


def fetch_serie_a_lineups(
    fixtures: Iterable[dict],
    *,
    now: datetime | None = None,
    horizon_hours: int = 48,
    max_matches: int = 4,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    """Fetch bounded official Serie A API observations for near-term fixtures."""

    return rights_blocked_fetch_envelope(
        SourceId.OFFICIAL_SERIE_A_LINEUPS,
        provider="Serie A official",
        now=now,
        empty_fields=("fixtures", "lineups"),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    reference = _utc(now or datetime.now(timezone.utc))
    candidates: list[dict] = []
    for fixture in fixtures:
        if fixture.get("competition_id") != "serie-a" or fixture.get("status") != "upcoming":
            continue
        try:
            kickoff = _parse_timestamp(fixture["kickoff_at"], field="kickoff_at")
        except (KeyError, TypeError, ValueError):
            continue
        if reference <= kickoff <= reference + timedelta(hours=horizon_hours):
            candidates.append(fixture)
    candidates.sort(key=lambda row: (str(row.get("kickoff_at", "")), str(row.get("id", ""))))
    # The bare host currently answers with a Next.js 307 without a Location
    # header.  There is no safe redirect target to infer from that response;
    # use the provider's canonical public match-centre path directly.
    home_url = f"https://{SERIE_A_WEB_HOST}/serie-a"
    result = {
        "provider": "Serie A official",
        "retrieved_at": reference.isoformat(),
        "home_url": home_url,
        "fixtures": [],
        "lineups": [],
        "errors": [],
        "status": "not_requested" if not candidates else "ok",
    }
    if not candidates:
        return result
    real_opener = opener is None
    fetcher = opener or _safe_opener()
    try:
        home_request = urllib.request.Request(
            home_url,
            headers={"Accept": "text/html", "User-Agent": "Matchline/1.0"},
        )
        home_payload = _read_limited(
            fetcher,
            home_request,
            kind="home",
            max_bytes=MAX_BYTES,
        )
        urls = discover_match_urls(home_payload, url=home_url)
        season_id = _season_id(home_payload)
    except Exception as exc:
        result["status"] = "unavailable"
        result["errors"].append({"stage": "home", "error": str(exc)})
        return result
    selected: list[tuple[dict, str]] = []
    for fixture in candidates:
        matching = [
            url
            for url in urls
            if _team_matches(fixture.get("home_team"), urlparse(url).path, slug=urlparse(url).path)
            and _team_matches(
                fixture.get("away_team"), urlparse(url).path, slug=urlparse(url).path
            )
        ]
        if len(matching) == 1:
            selected.append((fixture, matching[0]))
        elif len(matching) > 1:
            result["errors"].append(
                {
                    "fixture_id": fixture.get("id"),
                    "stage": "match_url_join",
                    "reason": "ambiguous",
                    "urls": matching,
                }
            )
        else:
            result["errors"].append(
                {"fixture_id": fixture.get("id"), "stage": "match_url_join", "reason": "not_found"}
            )
    last_request = 0.0
    for fixture, match_url in selected[:max_matches]:
        native_id = _match_id(match_url)
        try:
            header_url = _api_url(season_id, native_id, "header")
            if real_opener and last_request:
                time.sleep(
                    max(0.0, SERIE_A_CRAWL_DELAY_SECONDS - (time.monotonic() - last_request))
                )
            header_request = urllib.request.Request(
                header_url,
                headers={"Accept": "text/plain; x-api-version=1.0", "User-Agent": "Matchline/1.0"},
            )
            header_data, header_payload = _read_json(fetcher, header_request)
            last_request = time.monotonic()
            official_fixture = parse_serie_a_header(
                header_data,
                fixture_id=str(fixture["id"]),
                match_id=native_id,
                kickoff_at=str(fixture["kickoff_at"]),
                retrieved_at=reference,
                url=header_url,
                raw_payload=header_payload,
                expected_home=str(fixture.get("home_team") or ""),
                expected_away=str(fixture.get("away_team") or ""),
            )
            lineup_url = _api_url(season_id, native_id, "lineups")
            if real_opener:
                time.sleep(
                    max(0.0, SERIE_A_CRAWL_DELAY_SECONDS - (time.monotonic() - last_request))
                )
            lineup_request = urllib.request.Request(
                lineup_url,
                headers={"Accept": "text/plain; x-api-version=1.0", "User-Agent": "Matchline/1.0"},
            )
            lineup_data, lineup_payload = _read_json(fetcher, lineup_request)
            last_request = time.monotonic()
            row = parse_serie_a_lineups(
                lineup_data,
                fixture_id=str(fixture["id"]),
                match_id=native_id,
                kickoff_at=official_fixture["kickoff_at"],
                retrieved_at=reference,
                url=lineup_url,
                raw_payload=lineup_payload,
                header_source=official_fixture["source"],
            )
            result["fixtures"].append(official_fixture)
            result["lineups"].append(row)
        except Exception as exc:  # noqa: BLE001 - isolate one public fixture
            result["errors"].append(
                {
                    "fixture_id": fixture.get("id"),
                    "stage": "api",
                    "match_url": match_url,
                    "error": str(exc),
                }
            )
    result["status"] = "degraded" if result["errors"] else "ok"
    return result


__all__ = [
    "SERIE_A_API_HOST",
    "SERIE_A_API_PATH",
    "SERIE_A_HOME_PATH",
    "SERIE_A_MATCH_PATH",
    "SERIE_A_WEB_HOST",
    "discover_match_urls",
    "fetch_serie_a_lineups",
    "parse_serie_a_header",
    "parse_serie_a_lineups",
]
