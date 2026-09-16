"""Public Bundesliga Match Centre lineup adapter.

The Bundesliga Match Centre exposes matchday pages and a public ``/lineup``
route.  The page is treated as an observed source: there is no trustworthy
published timestamp in the rendered payload, so ``retrieved_at`` is the
causal time basis.  Only a complete eleven-player XI observed before the
canonical kickoff can enter the model; all other pages remain audit-only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from unidecode import unidecode

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


BUNDESLIGA_HOST = "www.bundesliga.com"
BUNDESLIGA_MATCHDAY_PATH = re.compile(r"/en/bundesliga/matchday/\d{4}-\d{4}/(?:\d+)?/?")
BUNDESLIGA_MATCH_PATH = re.compile(
    r"/en/bundesliga/matchday/\d{4}-\d{4}/\d+/[a-z0-9-]+-vs-[a-z0-9-]+/(?:lineup|liveticker)/?"
)
BUNDESLIGA_CRAWL_DELAY_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_BYTES = 12 * 1024 * 1024
_BERLIN = ZoneInfo("Europe/Berlin")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise urllib.error.URLError("Bundesliga redirected; redirects are disabled")


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
        or parsed.hostname != BUNDESLIGA_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Bundesliga URL is not allowlisted")
    pattern = BUNDESLIGA_MATCHDAY_PATH if kind == "matchday" else BUNDESLIGA_MATCH_PATH
    if not pattern.fullmatch(parsed.path):
        raise ValueError("Bundesliga URL path is not allowlisted")


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
        raise TypeError("Bundesliga response must be bytes")
    if len(payload) > MAX_BYTES:
        raise ValueError("Bundesliga response exceeded 12 MiB")
    return payload


_GERMAN_TRANSLITERATION = str.maketrans(
    {
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
)


def _german_ascii(value: object) -> str:
    return unidecode(str(value or "").translate(_GERMAN_TRANSLITERATION)).lower()


def _normalise(value: object) -> str:
    return "".join(ch for ch in _german_ascii(value) if ch.isalnum())


_GENERIC_TEAM_TOKENS = {
    "afc",
    "club",
    "fc",
    "fsv",
    "sc",
    "sv",
    "tsg",
    "vfb",
}


def _team_matches(expected: object, slug: object) -> bool:
    expected_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", _german_ascii(expected))
        if token not in _GENERIC_TEAM_TOKENS and len(token) >= 3
    }
    slug_text = _normalise(slug)
    expected_text = _normalise(expected)
    return bool(
        expected_text
        and (expected_text in slug_text or any(token in slug_text for token in expected_tokens))
    )


class _LineupParser(HTMLParser):
    """Collect player names from the two starter columns only."""

    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.tactical_depth: int | None = None
        self.side_depth: int | None = None
        self.side_index = -1
        self.capture_depth: int | None = None
        self.capture: list[str] = []
        self.players: list[list[str]] = [[], []]

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        value = next((item for key, item in attrs if key == "class"), "") or ""
        return set(value.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag in self._VOID_TAGS:
            return
        self.depth += 1
        if tag == "div" and "tactical-lineup" in classes:
            self.tactical_depth = self.depth
        if (
            tag == "div"
            and self.tactical_depth is not None
            and self.side_depth is None
            and "col-md-6" in classes
        ):
            self.side_index += 1
            self.side_depth = self.depth
        if (
            tag == "div"
            and self.side_depth is not None
            and "player-name" in classes
            and self.side_index in (0, 1)
        ):
            self.capture_depth = self.depth
            self.capture = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_data(self, data: str) -> None:
        if self.capture_depth is not None:
            self.capture.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.capture_depth == self.depth:
            value = " ".join("".join(self.capture).split())
            if value and self.side_index in (0, 1):
                self.players[self.side_index].append(value)
            self.capture_depth = None
            self.capture = []
        if tag == "div" and self.side_depth == self.depth:
            self.side_depth = None
        if tag == "div" and self.tactical_depth == self.depth:
            self.tactical_depth = None
        self.depth = max(0, self.depth - 1)


def _provider_last_updated_at(payload: bytes) -> str | None:
    """Read the official page's embedded lineup-feed status timestamp.

    The Angular transfer state is server-rendered into the public lineup page.
    It may contain only ``lastUpdateDateTime`` before either club publishes a
    player row.  That is useful publication-state evidence, but it is not a
    trustworthy lineup effective time and therefore never replaces
    ``retrieved_at`` as the causal boundary.
    """

    match = re.search(
        rb'<script id="ng-state" type="application/json">(.*?)</script>',
        payload,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    try:
        state = json.loads(match.group(1).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    for key, value in state.items():
        if (
            isinstance(key, str)
            and key.startswith("_getDataFromFirebase-")
            and key.rstrip("/").endswith("/lineup")
            and isinstance(value, dict)
        ):
            timestamp = value.get("lastUpdateDateTime")
            if isinstance(timestamp, str) and timestamp.strip():
                return timestamp.strip()
    return None


def _lineup_diagnostic(
    home_players: list[dict],
    away_players: list[dict],
    *,
    confirmed: bool,
    before_kickoff: bool,
    provider_last_updated_at: str | None,
) -> dict:
    if not before_kickoff:
        status, reason = "post_kickoff_observation", "official_page_observed_after_kickoff"
    elif confirmed:
        status, reason = "confirmed", "both_sides_have_11_starters"
    elif not home_players and not away_players:
        status, reason = "not_published", "official_feed_returned_no_lineup_rows"
    else:
        status, reason = "partial", "official_feed_has_incomplete_lineup_rows"
    return {
        "status": status,
        "reason": reason,
        "home_player_count": len(home_players),
        "away_player_count": len(away_players),
        "home_starter_count": sum(1 for item in home_players if item.get("starter") is True),
        "away_starter_count": sum(1 for item in away_players if item.get("starter") is True),
        "provider_last_updated_at": provider_last_updated_at,
    }


def discover_match_urls(payload: bytes, *, url: str) -> list[str]:
    """Extract allowlisted match routes from an official matchday page."""

    _validate_url(url, kind="matchday")
    text = payload.decode("utf-8", errors="strict")
    candidates = re.findall(
        r'href=["\']([^"\']*?/en/bundesliga/matchday/[^"\']+)["\']', text, flags=re.I
    )
    urls: list[str] = []
    for candidate in candidates:
        if candidate.startswith("/"):
            candidate = f"https://{BUNDESLIGA_HOST}{candidate}"
        parsed = urlparse(candidate)
        if parsed.path.endswith("/lineup"):
            candidate = candidate[: -len("/lineup")] + "/liveticker"
        try:
            _validate_url(candidate, kind="match")
        except ValueError:
            continue
        if candidate not in urls:
            urls.append(candidate)
    return urls


def _official_kickoff(payload: bytes) -> datetime:
    text = payload.decode("utf-8", errors="ignore")
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\b", text)
    if iso_match is not None:
        return datetime.strptime(iso_match.group(1), "%Y-%m-%dT%H:%M:%S%z").astimezone(
            timezone.utc
        )
    match = re.search(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})", text
    )
    if match is None:
        raise ValueError("Bundesliga page is missing canonical kickoff text")
    return (
        datetime.strptime(f"{match.group(1)} {match.group(2)}", "%d.%m.%Y %H:%M")
        .replace(tzinfo=_BERLIN)
        .astimezone(timezone.utc)
    )


def _slug_parts(url: str) -> tuple[str, str]:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if slug in {"liveticker", "lineup"}:
        slug = urlparse(url).path.rstrip("/").split("/")[-2]
    home, separator, away = slug.partition("-vs-")
    if not separator:
        raise ValueError("Bundesliga match URL has no team pair")
    return home, away


def _players(values: list[str], *, team: str) -> list[dict]:
    result = []
    for index, name in enumerate(values):
        player_key = hashlib.sha1(f"{team}:{name}".encode("utf-8")).hexdigest()[:16]
        result.append(
            {
                "player_id": f"bundesliga:{player_key}",
                "provider_player_id": player_key,
                "name": name,
                "position": None,
                "starter": True,
                "substitute": False,
                "status": "starter",
                "expected_minutes": None,
                "ordinal": index + 1,
            }
        )
    return result


def parse_bundesliga_match_page(
    payload: bytes,
    *,
    fixture_id: str,
    kickoff_at: str,
    retrieved_at: datetime,
    url: str,
    expected_home: str | None = None,
    expected_away: str | None = None,
) -> dict:
    """Parse a public lineup page with strict time/team and XI checks."""

    _validate_url(url, kind="match")
    observed = _utc(retrieved_at)
    kickoff = datetime.fromisoformat(str(kickoff_at).replace("Z", "+00:00"))
    if kickoff.tzinfo is None:
        raise ValueError("Bundesliga kickoff must be timezone-aware")
    kickoff = kickoff.astimezone(timezone.utc)
    official_kickoff = _official_kickoff(payload)
    if abs((official_kickoff - kickoff).total_seconds()) > 90:
        raise ValueError("Bundesliga official kickoff does not match canonical fixture")
    home_slug, away_slug = _slug_parts(url)
    if expected_home and not _team_matches(expected_home, home_slug):
        raise ValueError("Bundesliga official home team does not match canonical fixture")
    if expected_away and not _team_matches(expected_away, away_slug):
        raise ValueError("Bundesliga official away team does not match canonical fixture")
    parser = _LineupParser()
    parser.feed(payload.decode("utf-8", errors="strict"))
    home_names, away_names = parser.players
    home_players = _players(home_names, team=home_slug)
    away_players = _players(away_names, team=away_slug)
    home_complete = len(home_players) == 11
    away_complete = len(away_players) == 11
    confirmed = bool(home_complete and away_complete)
    before_kickoff = observed < kickoff
    provider_last_updated_at = _provider_last_updated_at(payload)
    source = {
        "name": "Bundesliga official",
        "url": url.replace("/liveticker", "/lineup"),
        "retrieved_at": observed.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "source_kind": "official_lineup"
        if home_players or away_players
        else "official_lineup_status",
        "time_basis": "observed_at_no_published_at",
        "effective_at": None,
    }
    return {
        "fixture_id": fixture_id,
        "match_id": None,
        "official_fixture": {
            "id": fixture_id,
            "competition_id": "bundesliga",
            "kickoff_at": kickoff.isoformat(),
            "home_team": expected_home,
            "away_team": expected_away,
            "status": "upcoming",
            "source": source,
        },
        "lineups": {
            "confirmed": confirmed,
            "home": {
                "players": home_players,
                "available": bool(home_players),
                "missing_players": [],
            },
            "away": {
                "players": away_players,
                "available": bool(away_players),
                "missing_players": [],
            },
            "available": bool(home_players or away_players),
            "model_eligible": bool(confirmed and before_kickoff),
            "missing_fields": ["expected_minutes", "replacement_value"],
            "diagnostic": _lineup_diagnostic(
                home_players,
                away_players,
                confirmed=confirmed,
                before_kickoff=before_kickoff,
                provider_last_updated_at=provider_last_updated_at,
            ),
        },
        "injuries": {"home": [], "away": [], "available": False},
        "source": source,
    }


def _matchday_guess(kickoff: datetime) -> int:
    # The top division starts in late August.  ±1 pages are fetched to absorb
    # season-to-season scheduling shifts while keeping the request budget small.
    season_start = date(kickoff.year, 8, 25)
    return max(1, min(34, (kickoff.date() - season_start).days // 7 + 1))


def fetch_bundesliga_lineups(
    fixtures: Iterable[dict],
    *,
    now: datetime | None = None,
    horizon_hours: int = 48,
    max_pages: int = 10,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    """Fetch only near-term Bundesliga pages with a bounded request budget."""

    return rights_blocked_fetch_envelope(
        SourceId.OFFICIAL_BUNDESLIGA_LINEUPS,
        provider="Bundesliga official",
        now=now,
        empty_fields=("fixtures", "lineups"),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    reference = _utc(now or datetime.now(timezone.utc))
    candidates = []
    for fixture in fixtures:
        if fixture.get("competition_id") != "bundesliga" or fixture.get("status") != "upcoming":
            continue
        try:
            kickoff = datetime.fromisoformat(
                str(fixture["kickoff_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if reference <= kickoff <= reference + timedelta(hours=horizon_hours):
            candidates.append((fixture, kickoff))
    result = {
        "provider": "Bundesliga official",
        "retrieved_at": reference.isoformat(),
        "home_url": f"https://{BUNDESLIGA_HOST}/en/bundesliga/matchday",
        "fixtures": [],
        "lineups": [],
        "errors": [],
        "status": "not_requested" if not candidates else "ok",
    }
    if not candidates:
        return result
    real_opener = opener is None
    opener = opener or _safe_opener()
    matchday_groups: dict[tuple[str, int], list[tuple[dict, datetime]]] = {}
    for fixture, kickoff in candidates:
        season = str(fixture.get("season") or kickoff.year)
        matchday_groups.setdefault((season, _matchday_guess(kickoff)), []).append(
            (fixture, kickoff)
        )
    pages_read = 0
    for (season, guess), group in sorted(matchday_groups.items()):
        season_label = f"{int(season):04d}-{int(season) + 1:04d}"
        for matchday in sorted({max(1, guess - 1), guess, min(34, guess + 1)}):
            if pages_read >= max_pages:
                result["errors"].append("max_pages reached")
                break
            page_url = (
                f"https://{BUNDESLIGA_HOST}/en/bundesliga/matchday/{season_label}/{matchday}"
            )
            try:
                if real_opener and pages_read:
                    time.sleep(BUNDESLIGA_CRAWL_DELAY_SECONDS)
                payload = _read_html(
                    opener,
                    urllib.request.Request(
                        page_url, headers={"User-Agent": "MatchlineResearch/1.0"}
                    ),
                    kind="matchday",
                )
                pages_read += 1
                match_urls = discover_match_urls(payload, url=page_url)
            except Exception as exc:  # noqa: BLE001 - isolate one public source page
                pages_read += 1
                result["errors"].append(f"{page_url}: {type(exc).__name__}: {exc}")
                continue
            for fixture, kickoff in group:
                match_url = next(
                    (
                        item
                        for item in match_urls
                        if _team_matches(fixture.get("home_team"), _slug_parts(item)[0])
                        and _team_matches(fixture.get("away_team"), _slug_parts(item)[1])
                    ),
                    None,
                )
                if match_url is None or pages_read >= max_pages:
                    continue
                lineup_url = match_url.replace("/liveticker", "/lineup")
                try:
                    if real_opener:
                        time.sleep(BUNDESLIGA_CRAWL_DELAY_SECONDS)
                    lineup_payload = _read_html(
                        opener,
                        urllib.request.Request(
                            lineup_url, headers={"User-Agent": "MatchlineResearch/1.0"}
                        ),
                        kind="match",
                    )
                    pages_read += 1
                    row = parse_bundesliga_match_page(
                        lineup_payload,
                        fixture_id=str(fixture["id"]),
                        kickoff_at=str(fixture["kickoff_at"]),
                        retrieved_at=reference,
                        url=lineup_url,
                        expected_home=str(fixture.get("home_team") or ""),
                        expected_away=str(fixture.get("away_team") or ""),
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one public match page
                    pages_read += 1
                    result["errors"].append(f"{lineup_url}: {type(exc).__name__}: {exc}")
                    continue
                result["fixtures"].append(row["official_fixture"])
                result["lineups"].append(row)
            if pages_read >= max_pages:
                break
    result["status"] = "degraded" if result["errors"] else "ok"
    return result


__all__ = [
    "BUNDESLIGA_HOST",
    "BUNDESLIGA_MATCHDAY_PATH",
    "BUNDESLIGA_MATCH_PATH",
    "fetch_bundesliga_lineups",
    "parse_bundesliga_match_page",
    "discover_match_urls",
]
