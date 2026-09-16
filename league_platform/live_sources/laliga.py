"""Public LaLiga Match Centre lineup adapter.

The LaLiga website exposes the current round's match pages as public HTML.
Those pages contain a server-rendered ``__NEXT_DATA__`` payload with the
official starting XI and substitutes once the teams publish them.  The
adapter deliberately uses the page as an observed source: LaLiga does not
publish a reliable per-match lineup timestamp in this payload, so
``retrieved_at`` is the causal time basis.  A page observed after kickoff is
kept for audit only.
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
from urllib.parse import urlencode, urlparse

from unidecode import unidecode

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


LALIGA_HOST = "www.laliga.com"
LALIGA_API_HOST = "apim.laliga.com"
LALIGA_HOME_PATH = re.compile(r"/en-GB/?")
LALIGA_MATCH_PATH = re.compile(
    r"/[a-z]{2}-[A-Z]{2}/(?:match|partido)/"
    r"temporada-\d{4}-\d{4}-laliga-ea-sports-[a-z0-9-]+-\d+/?"
)
LALIGA_WEBVIEW_LINEUP_PATH = re.compile(r"/webview/api/web/matches/\d+/lineups")
LALIGA_PUBLIC_MATCHES_PATH = re.compile(r"/public-service/api/v1/matches")
LALIGA_COMPETITION_OPTA_ID = "23"
LALIGA_COMPETITION_SLUG = "primera-division"
LALIGA_MATCH_PAGE_SIZE = 100
LALIGA_MATCH_PAGE_LIMIT = 4
LALIGA_CRAWL_DELAY_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_BYTES = 12 * 1024 * 1024
API_MAX_BYTES = 4 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise urllib.error.URLError("LaLiga redirected; redirects are disabled")


def _safe_opener():
    return urllib.request.build_opener(_NoRedirect())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_url(url: str, *, kind: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != LALIGA_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("LaLiga URL is not allowlisted")
    pattern = LALIGA_HOME_PATH if kind == "home" else LALIGA_MATCH_PATH
    if not pattern.fullmatch(parsed.path):
        raise ValueError("LaLiga URL path is not allowlisted")


def _read_html(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
    *,
    kind: str,
) -> bytes:
    open_method = getattr(opener, "open", None) or opener
    response = open_method(request, timeout=DEFAULT_TIMEOUT_SECONDS)  # type: ignore[operator]
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        _validate_url(str(final_url), kind=kind)
        payload = response.read(MAX_BYTES + 1)
    if not isinstance(payload, bytes):
        raise TypeError("LaLiga response must be bytes")
    if len(payload) > MAX_BYTES:
        raise ValueError("LaLiga response exceeded 12 MiB")
    return payload


def _next_data(payload: bytes) -> dict:
    match = re.search(
        rb'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        payload,
        flags=re.DOTALL,
    )
    if match is None:
        raise ValueError("LaLiga page is missing __NEXT_DATA__")
    try:
        data = json.loads(unescape(match.group(1).decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("LaLiga __NEXT_DATA__ is invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("LaLiga __NEXT_DATA__ root is not an object")
    return data


def _next_page_props(payload: bytes) -> dict:
    data = _next_data(payload)
    props = data.get("props", {}).get("pageProps") if isinstance(data, dict) else None
    if not isinstance(props, dict):
        raise ValueError("LaLiga pageProps are missing")
    return props


def _webview_subscription(payload: bytes) -> str | None:
    """Read the public web-client subscription key without hardcoding it."""

    try:
        runtime = _next_data(payload).get("runtimeConfig")
    except ValueError:
        return None
    if not isinstance(runtime, dict):
        return None
    value = runtime.get("webviewSubscription")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _public_service_subscription(payload: bytes) -> str | None:
    """Read the public Azure API key shipped in the official page runtime.

    The key is a browser configuration value, not a project secret.  Reading
    it from the source page avoids hard-coding a value that the site may
    rotate.  Missing or malformed configuration fails closed.
    """

    try:
        runtime = _next_data(payload).get("runtimeConfig")
    except ValueError:
        return None
    if not isinstance(runtime, dict):
        return None
    value = runtime.get("backendSubscription")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _competition_subscription_id(payload: bytes) -> str | None:
    """Extract the current-season LaLiga competition subscription id.

    ``COMPETITION_CONFIG`` is embedded in the official page shell rather than
    in ``__NEXT_DATA__``.  Keep the scan bounded and require the exact
    ``primera-division`` object so a random number in page content cannot be
    used as an API subscription.
    """

    text = payload.decode("utf-8", errors="strict")
    match = re.search(
        r'"primera-division":\{[^{}]{0,1200}?"subscription_id":"(\d+)"',
        text,
    )
    return match.group(1) if match else None


def _validate_api_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != LALIGA_API_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not LALIGA_WEBVIEW_LINEUP_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("LaLiga lineup API URL is not allowlisted")


def _validate_public_service_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != LALIGA_API_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not LALIGA_PUBLIC_MATCHES_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("LaLiga public match API URL is not allowlisted")


def _read_api_json(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
) -> tuple[dict, bytes]:
    open_method = getattr(opener, "open", None) or opener
    response = open_method(request, timeout=DEFAULT_TIMEOUT_SECONDS)  # type: ignore[operator]
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        _validate_api_url(str(final_url).split("?", 1)[0])
        payload = response.read(API_MAX_BYTES + 1)
    if not isinstance(payload, bytes):
        raise TypeError("LaLiga lineup API response must be bytes")
    if len(payload) > API_MAX_BYTES:
        raise ValueError("LaLiga lineup API response exceeded 4 MiB")
    try:
        data = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("LaLiga lineup API response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("LaLiga lineup API response root is not an object")
    return data, payload


def _read_public_service_json(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
) -> tuple[dict, bytes]:
    """Read one bounded response from the official public match directory."""

    open_method = getattr(opener, "open", None) or opener
    response = open_method(request, timeout=DEFAULT_TIMEOUT_SECONDS)  # type: ignore[operator]
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        _validate_public_service_url(str(final_url).split("?", 1)[0])
        payload = response.read(API_MAX_BYTES + 1)
    if not isinstance(payload, bytes):
        raise TypeError("LaLiga public match API response must be bytes")
    if len(payload) > API_MAX_BYTES:
        raise ValueError("LaLiga public match API response exceeded 4 MiB")
    try:
        data = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("LaLiga public match API response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("LaLiga public match API response root is not an object")
    return data, payload


def _normalise(value: object) -> str:
    return "".join(ch for ch in unidecode(str(value or "")).lower() if ch.isalnum())


_GENERIC_TEAM_TOKENS = {
    "afc",
    "club",
    "cf",
    "cd",
    "de",
    "del",
    "deportivo",
    "fc",
    "la",
    "rc",
    "racing",
    "real",
    "sd",
    "ud",
}

# LaLiga's public site does not consistently use the same display name as
# ESPN's canonical fixture feed.  Keep this mapping deliberately narrow: it
# is only used for joining a canonical fixture to the official page and does
# not rewrite the project's global team identity table.
_TEAM_NAME_ALIASES = {
    "racingsantander": ("racingclub",),
}


def _team_matches(expected: object, official: object, *, slug: str = "") -> bool:
    expected_text = _normalise(expected)
    official_text = _normalise(official)
    slug_text = _normalise(slug)
    if not expected_text or not official_text:
        return False
    if expected_text in official_text or official_text in expected_text:
        return True
    aliases = _TEAM_NAME_ALIASES.get(expected_text, ())
    if any(alias in official_text or alias in slug_text for alias in aliases):
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


def _provider_player_id(row: dict) -> str:
    """Prefer the stable player id embedded in the official photo path.

    The webview lineup API exposes a per-match lineup-row id rather than a
    stable player id.  Its public photo path carries the stable ``p####``
    identifier; when a page fixture omits photos, retain the row id as an
    explicitly weaker but still traceable fallback.
    """

    photos = row.get("photos")
    if isinstance(photos, dict):
        match = re.search(r"/p(\d+)(?:/|$)", json.dumps(photos, ensure_ascii=False))
        if match is not None:
            return match.group(1)
    return str(row["id"])


def _player(row: object, *, status: str) -> dict | None:
    if not isinstance(row, dict) or row.get("id") is None:
        return None
    person = row.get("person") if isinstance(row.get("person"), dict) else {}
    aliases = list(
        dict.fromkeys(
            str(value).strip()
            for value in (person.get("name"), person.get("nickname"))
            if isinstance(value, str) and value.strip()
        )
    )
    name = aliases[0] if aliases else None
    if not name:
        return None
    provider_player_id = _provider_player_id(row)
    return {
        "player_id": f"laliga:{provider_player_id}",
        "provider_player_id": provider_player_id,
        "name": str(name),
        "aliases": aliases,
        "position": row.get("position"),
        "shirt_number": row.get("shirt_number"),
        "captain": bool(row.get("captain")),
        "starter": status == "starter",
        "substitute": status == "substitute",
        "status": status,
        "expected_minutes": None,
    }


def _lineup_side(value: object) -> tuple[dict, bool]:
    if not isinstance(value, dict):
        return {"players": [], "available": False, "missing_players": []}, False
    players: list[dict] = []
    starts = value.get("starts") if isinstance(value.get("starts"), list) else []
    subs = value.get("subs") if isinstance(value.get("subs"), list) else []
    for row in starts:
        item = _player(row, status="starter")
        if item is not None:
            players.append(item)
    for row in subs:
        item = _player(row, status="substitute")
        if item is not None:
            players.append(item)
    starter_ids = {row["player_id"] for row in players if row.get("starter")}
    complete = len(starts) == 11 and len(starter_ids) == 11
    return {
        "players": players,
        "available": bool(players),
        "formation": value.get("formation"),
        "missing_players": [],
    }, complete


def _api_lineup_side(value: object) -> tuple[dict, bool]:
    """Normalize the public LaLiga webview API's flat lineup rows."""

    if not isinstance(value, list):
        return {"players": [], "available": False, "missing_players": []}, False
    starters = [
        row
        for row in value
        if isinstance(row, dict)
        and str(row.get("status") or "").casefold() in {"start", "starter"}
    ]
    substitutes = [
        row
        for row in value
        if isinstance(row, dict)
        and str(row.get("status") or "").casefold() in {"sub", "substitute"}
    ]
    players = [
        item
        for row, status in [
            *((row, "starter") for row in starters),
            *((row, "substitute") for row in substitutes),
        ]
        if (item := _player(row, status=status)) is not None
    ]
    valid_starters = sum(1 for item in players if item.get("starter") is True)
    complete = len(starters) == 11 and valid_starters == 11
    return {
        "players": players,
        "available": bool(players),
        "formation": None,
        "missing_players": [],
    }, complete


def _lineup_diagnostic(
    home: dict,
    away: dict,
    *,
    confirmed: bool,
    before_kickoff: bool,
) -> dict:
    """Explain why a public lineup observation is or is not usable.

    A successful official page/API response with two empty arrays is a normal
    pre-match state (the club has not published the XI yet), not a healthy
    lineup feed.  Keeping that distinction explicit prevents the source
    registry and UI from turning an observed page into false coverage.
    """

    home_players = home.get("players") if isinstance(home.get("players"), list) else []
    away_players = away.get("players") if isinstance(away.get("players"), list) else []
    home_starters = sum(
        1 for item in home_players if isinstance(item, dict) and item.get("starter") is True
    )
    away_starters = sum(
        1 for item in away_players if isinstance(item, dict) and item.get("starter") is True
    )
    if not before_kickoff:
        status = "post_kickoff_observation"
        reason = "official_page_observed_after_kickoff"
    elif confirmed:
        status = "confirmed"
        reason = "both_sides_have_11_starters"
    elif not home_players and not away_players:
        status = "not_published"
        reason = "official_feed_returned_no_lineup_rows"
    else:
        status = "partial"
        reason = "official_feed_has_incomplete_lineup_rows"
    return {
        "status": status,
        "reason": reason,
        "home_player_count": len(home_players),
        "away_player_count": len(away_players),
        "home_starter_count": home_starters,
        "away_starter_count": away_starters,
    }


def _parse_api_lineups(data: dict, *, before_kickoff: bool) -> dict:
    home, home_complete = _api_lineup_side(data.get("home_team_lineups"))
    away, away_complete = _api_lineup_side(data.get("away_team_lineups"))
    confirmed = bool(home_complete and away_complete)
    return {
        "confirmed": confirmed,
        "home": home,
        "away": away,
        "available": bool(home["available"] or away["available"]),
        "model_eligible": bool(confirmed and before_kickoff),
        "missing_fields": ["expected_minutes", "replacement_value"],
        "diagnostic": _lineup_diagnostic(
            home,
            away,
            confirmed=confirmed,
            before_kickoff=before_kickoff,
        ),
    }


def discover_match_urls(payload: bytes, *, url: str) -> list[str]:
    """Extract allowlisted match pages from the public LaLiga home page."""

    _validate_url(url, kind="home")
    text = payload.decode("utf-8", errors="strict")
    candidates = re.findall(
        r'href=["\']([^"\']*?/[a-z]{2}-[A-Z]{2}/(?:match|partido)/[^"\']+)["\']',
        text,
        flags=re.I,
    )
    urls: list[str] = []
    for candidate in candidates:
        candidate = unescape(candidate)
        if candidate.startswith("/"):
            candidate = f"https://{LALIGA_HOST}{candidate}"
        try:
            _validate_url(candidate, kind="match")
        except ValueError:
            continue
        if candidate not in urls:
            urls.append(candidate)
    return urls


def _discover_match_urls_from_public_service(
    payload: bytes,
    fixtures: Iterable[dict],
    *,
    opener: Callable[..., object] | object,
) -> tuple[dict[str, str], dict]:
    """Resolve missing near-term pages through LaLiga's public match index.

    The public home page only renders a bounded set of current-round cards.
    The official page shell exposes a public, paginated match directory; use
    it solely to discover source-declared match slugs for candidate fixtures.
    Every final lineup still comes from the allowlisted match page and is
    rechecked by :func:`parse_laliga_match_page`.
    """

    targets: list[tuple[dict, datetime]] = []
    for fixture in fixtures:
        try:
            kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]).replace("Z", "+00:00"))
            kickoff = _utc(kickoff)
        except (KeyError, TypeError, ValueError):
            continue
        if fixture.get("id"):
            targets.append((fixture, kickoff))
    details = {
        "status": "not_requested" if not targets else "unavailable",
        "endpoint": f"https://{LALIGA_API_HOST}/public-service/api/v1/matches",
        "competition": LALIGA_COMPETITION_SLUG,
        "pages": 0,
        "records_seen": 0,
        "matched_count": 0,
        "truncated": False,
        "errors": [],
    }
    if not targets:
        return {}, details
    subscription = _public_service_subscription(payload)
    competition_subscription = _competition_subscription_id(payload)
    if not subscription or not competition_subscription:
        details["errors"] = [
            {
                "reason": "official_public_match_directory_configuration_missing",
                "missing": [
                    name
                    for name, value in (
                        ("backendSubscription", subscription),
                        ("competition_subscription_id", competition_subscription),
                    )
                    if not value
                ],
            }
        ]
        return {}, details
    digest = hashlib.sha256()
    matches_by_fixture: dict[str, str] = {}
    target_by_id = {str(fixture["id"]): (fixture, kickoff) for fixture, kickoff in targets}
    try:
        for page_number in range(LALIGA_MATCH_PAGE_LIMIT):
            offset = page_number * LALIGA_MATCH_PAGE_SIZE
            query = urlencode(
                {
                    "subscription": competition_subscription,
                    "competition": LALIGA_COMPETITION_SLUG,
                    "limit": LALIGA_MATCH_PAGE_SIZE,
                    "offset": offset,
                }
            )
            api_url = f"{details['endpoint']}?{query}"
            request = urllib.request.Request(
                api_url,
                headers={
                    "Accept": "application/json",
                    "Ocp-Apim-Subscription-Key": subscription,
                    "User-Agent": "Matchline/1.0",
                },
            )
            data, raw_payload = _read_public_service_json(opener, request)
            digest.update(raw_payload)
            details["pages"] += 1
            matches = data.get("matches")
            if not isinstance(matches, list):
                raise ValueError("LaLiga public match API response is missing matches")
            details["records_seen"] += len(matches)
            for match in matches:
                if not isinstance(match, dict):
                    continue
                competition = match.get("competition")
                if (
                    not isinstance(competition, dict)
                    or str(competition.get("opta_id")) != LALIGA_COMPETITION_OPTA_ID
                ):
                    continue
                slug = str(match.get("slug") or "").strip()
                if not re.fullmatch(
                    r"temporada-\d{4}-\d{4}-laliga-ea-sports-[a-z0-9-]+-\d+",
                    slug,
                ):
                    continue
                match_time_value = match.get("date") or match.get("time")
                try:
                    match_time = _utc(
                        datetime.fromisoformat(str(match_time_value).replace("Z", "+00:00"))
                    )
                except (TypeError, ValueError):
                    continue
                home = match.get("home_team") if isinstance(match.get("home_team"), dict) else {}
                away = match.get("away_team") if isinstance(match.get("away_team"), dict) else {}
                url = f"https://{LALIGA_HOST}/en-GB/match/{slug}"
                try:
                    _validate_url(url, kind="match")
                except ValueError:
                    continue
                for fixture_id, (fixture, kickoff) in target_by_id.items():
                    if fixture_id in matches_by_fixture:
                        continue
                    if abs((match_time - kickoff).total_seconds()) > 90:
                        continue
                    if not _team_matches(
                        fixture.get("home_team"),
                        home.get("nickname") or home.get("boundname") or home.get("name"),
                        slug=slug,
                    ) or not _team_matches(
                        fixture.get("away_team"),
                        away.get("nickname") or away.get("boundname") or away.get("name"),
                        slug=slug,
                    ):
                        continue
                    matches_by_fixture[fixture_id] = url
            total = data.get("total")
            try:
                total_count = int(total)
            except (TypeError, ValueError):
                total_count = None
            if len(matches) < LALIGA_MATCH_PAGE_SIZE or (
                total_count is not None and offset + len(matches) >= total_count
            ):
                break
        else:
            details["truncated"] = True
    except Exception as exc:
        details["errors"] = [
            {"reason": "official_public_match_directory_error", "error": str(exc)}
        ]
    details["matched_count"] = len(matches_by_fixture)
    details["status"] = (
        "ok"
        if matches_by_fixture and not details["errors"]
        else "degraded"
        if matches_by_fixture
        else "unavailable"
    )
    if details["pages"]:
        details["raw_sha256"] = digest.hexdigest()
    return matches_by_fixture, details


def parse_laliga_match_page(
    payload: bytes,
    *,
    fixture_id: str,
    kickoff_at: str,
    retrieved_at: datetime,
    url: str,
    expected_home: str | None = None,
    expected_away: str | None = None,
) -> dict:
    """Parse one official page and enforce strict causal/team identity."""

    _validate_url(url, kind="match")
    observed = _utc(retrieved_at)
    props = _next_page_props(payload)
    match = props.get("match")
    data = props.get("data")
    if not isinstance(match, dict) or not isinstance(data, dict):
        raise ValueError("LaLiga match payload is incomplete")
    competition = match.get("competition") if isinstance(match.get("competition"), dict) else {}
    if str(competition.get("opta_id")) != LALIGA_COMPETITION_OPTA_ID:
        raise ValueError("LaLiga page is not a LaLiga EA SPORTS match")
    try:
        kickoff = datetime.fromisoformat(str(kickoff_at).replace("Z", "+00:00"))
        official_kickoff = datetime.fromisoformat(
            str(match.get("date") or match.get("time")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("LaLiga kickoff timestamp is invalid") from exc
    if kickoff.tzinfo is None or official_kickoff.tzinfo is None:
        raise ValueError("LaLiga kickoff timestamps must be timezone-aware")
    kickoff = kickoff.astimezone(timezone.utc)
    official_kickoff = official_kickoff.astimezone(timezone.utc)
    if abs((official_kickoff - kickoff).total_seconds()) > 90:
        raise ValueError("LaLiga official kickoff does not match canonical fixture")
    home_team = match.get("home_team") if isinstance(match.get("home_team"), dict) else {}
    away_team = match.get("away_team") if isinstance(match.get("away_team"), dict) else {}
    home_name = home_team.get("nickname") or home_team.get("boundname") or home_team.get("name")
    away_name = away_team.get("nickname") or away_team.get("boundname") or away_team.get("name")
    slug = str(match.get("slug") or urlparse(url).path.rsplit("/", 1)[-1])
    if expected_home and not _team_matches(expected_home, home_name, slug=slug):
        raise ValueError("LaLiga official home team does not match canonical fixture")
    if expected_away and not _team_matches(expected_away, away_name, slug=slug):
        raise ValueError("LaLiga official away team does not match canonical fixture")
    raw_lineups = data.get("lineups") if isinstance(data.get("lineups"), dict) else {}
    home, home_complete = _lineup_side(raw_lineups.get("home"))
    away, away_complete = _lineup_side(raw_lineups.get("away"))
    confirmed = bool(home_complete and away_complete)
    before_kickoff = observed < kickoff
    source = {
        "name": "LaLiga official",
        "url": url,
        "retrieved_at": observed.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "native_match_id": str(match.get("id")) if match.get("id") is not None else None,
        "source_kind": "official_lineup",
        "time_basis": "observed_at_no_published_at",
        "effective_at": None,
    }
    source = {key: value for key, value in source.items() if value is not None}
    return {
        "fixture_id": fixture_id,
        "match_id": str(match.get("id")) if match.get("id") is not None else None,
        "official_fixture": {
            "id": fixture_id,
            "competition_id": "la-liga",
            "kickoff_at": kickoff.isoformat(),
            "home_team": home_name,
            "away_team": away_name,
            "status": "finished" if str(match.get("status")) == "FullTime" else "upcoming",
            "source": source,
        },
        "lineups": {
            "confirmed": confirmed,
            "home": home,
            "away": away,
            "available": bool(home["available"] or away["available"]),
            "model_eligible": bool(confirmed and before_kickoff),
            "missing_fields": ["expected_minutes", "replacement_value"],
            "diagnostic": _lineup_diagnostic(
                home,
                away,
                confirmed=confirmed,
                before_kickoff=before_kickoff,
            ),
        },
        "injuries": {"home": [], "away": [], "available": False},
        "source": source,
    }


def fetch_laliga_lineups(
    fixtures: Iterable[dict],
    *,
    now: datetime | None = None,
    horizon_hours: int = 48,
    max_pages: int = 6,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    """Fetch only near-term LaLiga official pages from public page discovery.

    The home page is tried first.  If it does not expose a candidate match,
    the official public match directory is used to discover the exact page
    slug; the page itself remains the authoritative lineup observation.
    Requests are serial and respect the public site's 30-second crawl delay.
    Tests may inject an opener; the delay is then skipped so contract tests
    remain deterministic and fast.
    """

    return rights_blocked_fetch_envelope(
        SourceId.OFFICIAL_LALIGA_LINEUPS,
        provider="LaLiga official",
        now=now,
        empty_fields=("fixtures", "lineups"),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    reference = _utc(now or datetime.now(timezone.utc))
    candidates = []
    for fixture in fixtures:
        if fixture.get("competition_id") != "la-liga" or fixture.get("status") != "upcoming":
            continue
        try:
            kickoff = datetime.fromisoformat(
                str(fixture["kickoff_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if reference <= kickoff <= reference + timedelta(hours=horizon_hours):
            candidates.append(fixture)
    candidates.sort(key=lambda item: (item.get("kickoff_at", ""), item.get("id", "")))
    if not candidates:
        return {
            "provider": "LaLiga official",
            "retrieved_at": reference.isoformat(),
            "home_url": f"https://{LALIGA_HOST}/en-GB",
            "fixtures": [],
            "lineups": [],
            "errors": [],
            "match_discovery": {"status": "not_requested"},
            "status": "not_requested",
        }
    fetcher = opener if opener is not None else _safe_opener()
    home_url = f"https://{LALIGA_HOST}/en-GB"
    errors: list[dict] = []
    lineups: list[dict] = []
    official_fixtures: list[dict] = []
    try:
        home_request = urllib.request.Request(
            home_url,
            headers={"Accept": "text/html", "User-Agent": "Matchline/1.0"},
        )
        home_payload = _read_html(fetcher, home_request, kind="home")
        urls = discover_match_urls(home_payload, url=home_url)
    except Exception as exc:
        return {
            "provider": "LaLiga official",
            "retrieved_at": reference.isoformat(),
            "home_url": home_url,
            "fixtures": [],
            "lineups": [],
            "errors": [{"stage": "home", "error": str(exc)}],
            "match_discovery": {"status": "not_requested"},
            "status": "unavailable",
        }
    home_matches: dict[str, str] = {}
    home_ambiguous: dict[str, list[str]] = {}
    missing_fixtures: list[dict] = []
    for fixture in candidates:
        matching = [
            url
            for url in urls
            if _team_matches(fixture.get("home_team"), url, slug=urlparse(url).path)
            and _team_matches(fixture.get("away_team"), url, slug=urlparse(url).path)
        ]
        fixture_id = str(fixture.get("id") or "")
        if len(matching) == 1 and fixture_id:
            home_matches[fixture_id] = matching[0]
        elif len(matching) > 1 and fixture_id:
            home_ambiguous[fixture_id] = matching
            missing_fixtures.append(fixture)
        else:
            missing_fixtures.append(fixture)
    schedule_urls: dict[str, str] = {}
    if missing_fixtures:
        schedule_urls, match_discovery = _discover_match_urls_from_public_service(
            home_payload,
            missing_fixtures,
            opener=fetcher,
        )
        for detail in match_discovery.get("errors", []):
            errors.append({"stage": "schedule_api", **detail})
    else:
        match_discovery = {
            "status": "not_requested",
            "reason": "home_page_resolved_all_candidates",
            "pages": 0,
            "records_seen": 0,
            "matched_count": 0,
            "errors": [],
        }
    selected: list[tuple[dict, str]] = []
    for fixture in candidates:
        fixture_id = str(fixture.get("id") or "")
        resolved_url = schedule_urls.get(fixture_id) or home_matches.get(fixture_id)
        if resolved_url:
            selected.append((fixture, resolved_url))
        elif fixture_id in home_ambiguous:
            errors.append(
                {
                    "fixture_id": fixture.get("id"),
                    "stage": "match_url_join",
                    "reason": "ambiguous",
                    "urls": home_ambiguous[fixture_id],
                }
            )
        else:
            errors.append(
                {"fixture_id": fixture.get("id"), "stage": "match_url_join", "reason": "not_found"}
            )
    selected = selected[:max_pages]
    last_request = 0.0
    for fixture, url in selected:
        if opener is None and last_request:
            delay = LALIGA_CRAWL_DELAY_SECONDS - (time.monotonic() - last_request)
            if delay > 0:
                time.sleep(delay)
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "text/html", "User-Agent": "Matchline/1.0"},
            )
            payload = _read_html(fetcher, request, kind="match")
            last_request = time.monotonic()
            row = parse_laliga_match_page(
                payload,
                fixture_id=str(fixture["id"]),
                kickoff_at=str(fixture["kickoff_at"]),
                retrieved_at=reference,
                url=url,
                expected_home=str(fixture.get("home_team") or ""),
                expected_away=str(fixture.get("away_team") or ""),
            )
            if not row["lineups"]["confirmed"]:
                subscription = _webview_subscription(home_payload)
                match_id = str(row.get("match_id") or "")
                if subscription is None:
                    errors.append(
                        {
                            "fixture_id": fixture.get("id"),
                            "stage": "lineup_api",
                            "reason": "missing_public_subscription_key",
                        }
                    )
                elif not re.fullmatch(r"\d+", match_id):
                    errors.append(
                        {
                            "fixture_id": fixture.get("id"),
                            "stage": "lineup_api",
                            "reason": "missing_numeric_match_id",
                        }
                    )
                else:
                    api_path = f"/webview/api/web/matches/{match_id}/lineups"
                    api_url = f"https://{LALIGA_API_HOST}{api_path}"
                    api_query = urlencode(
                        {
                            "contentLanguage": "en",
                            "countryCode": "GB",
                            "subscription-key": subscription,
                        }
                    )
                    api_request = urllib.request.Request(
                        f"{api_url}?{api_query}",
                        headers={
                            "Accept": "application/json",
                            "Content-Language": "en",
                            "Country-Code": "GB",
                            "User-Agent": "Matchline/1.0",
                        },
                    )
                    try:
                        api_data, api_payload = _read_api_json(fetcher, api_request)
                        api_lineups = _parse_api_lineups(
                            api_data,
                            before_kickoff=reference
                            < datetime.fromisoformat(
                                str(fixture["kickoff_at"]).replace("Z", "+00:00")
                            ).astimezone(timezone.utc),
                        )
                    except Exception as exc:
                        # Preserve the page observation and isolate a transient
                        # API failure from the rest of the source batch.
                        errors.append(
                            {
                                "fixture_id": fixture.get("id"),
                                "stage": "lineup_api",
                                "url": api_url,
                                "error": str(exc),
                            }
                        )
                    else:
                        page_source = dict(row["source"])
                        page_hash = page_source.get("raw_sha256")
                        api_hash = hashlib.sha256(api_payload).hexdigest()
                        page_source.update(
                            {
                                "api_url": api_url,
                                "api_raw_sha256": api_hash,
                            }
                        )
                        if api_lineups["available"]:
                            page_source.update(
                                {
                                    "raw_sha256": api_hash,
                                    "page_raw_sha256": page_hash,
                                    "source_kind": "official_lineup_api",
                                }
                            )
                            row["lineups"] = api_lineups
                        row["source"] = page_source
                        row["official_fixture"]["source"] = page_source
                        last_request = time.monotonic()
            official_fixtures.append(row["official_fixture"])
            lineups.append(row)
        except Exception as exc:
            errors.append(
                {"fixture_id": fixture.get("id"), "stage": "match", "url": url, "error": str(exc)}
            )
    return {
        "provider": "LaLiga official",
        "retrieved_at": reference.isoformat(),
        "home_url": home_url,
        "fixtures": official_fixtures,
        "lineups": lineups,
        "errors": errors,
        "match_discovery": match_discovery,
        "status": "ok" if lineups and not errors else "degraded" if lineups else "unavailable",
    }


__all__ = [
    "LALIGA_HOST",
    "LALIGA_HOME_PATH",
    "LALIGA_MATCH_PATH",
    "discover_match_urls",
    "fetch_laliga_lineups",
    "parse_laliga_match_page",
]
