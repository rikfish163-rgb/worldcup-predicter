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
import csv
import hashlib
import io
import json
import os
import re
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


CACHE_SCHEMA = "matchline.remote.facts.v1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_OPENLIGADB_ROWS = 2_000
MAX_OPENFOOTBALL_ROWS_PER_SOURCE = 2_000
MAX_WIKIDATA_ENTITIES = 12
# Wikimedia throttles generic/bare clients.  Keep the collector identifiable
# without sending credentials or changing the facts-only boundary.
USER_AGENT = "MatchlineFactsCollector/1.0 (+https://matchline-intelligence.willif57kbkd.chatgpt.site/; facts-only)"

OPENFOOTBALL_LICENSE_URL = "https://github.com/openfootball/football.json/blob/master/LICENSE.md"

# These are fixed, reviewable URLs rather than a user-controlled endpoint.
# OpenFootball is the CC0 canonical fixture lane; this VPS copy is a
# server-side facts snapshot only and never contains model output.
OPENFOOTBALL_CURRENT_SOURCES: tuple[dict[str, str], ...] = (
    {
        "sourceId": "openfootball_current_premier_league",
        "name": "OpenFootball Premier League",
        "competitionId": "premier-league",
        "competition": "Premier League",
        "season": "2026-27",
        "format": "json",
        "timezone": "Europe/London",
        "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
    },
    {
        "sourceId": "openfootball_current_championship",
        "name": "OpenFootball Championship",
        "competitionId": "championship",
        "competition": "Championship",
        "season": "2026-27",
        "format": "json",
        "timezone": "Europe/London",
        "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.2.json",
    },
    {
        "sourceId": "openfootball_current_bundesliga",
        "name": "OpenFootball Bundesliga",
        "competitionId": "bundesliga",
        "competition": "Bundesliga",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Berlin",
        "url": "https://raw.githubusercontent.com/openfootball/deutschland/master/2026-27/1-bundesliga.txt",
    },
    {
        "sourceId": "openfootball_current_la_liga",
        "name": "OpenFootball La Liga",
        "competitionId": "la-liga",
        "competition": "La Liga",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Madrid",
        "url": "https://raw.githubusercontent.com/openfootball/espana/master/2026-27/1-liga.txt",
    },
    {
        "sourceId": "openfootball_current_serie_a",
        "name": "OpenFootball Serie A",
        "competitionId": "serie-a",
        "competition": "Serie A",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Rome",
        "url": "https://raw.githubusercontent.com/openfootball/italy/master/2026-27/1-seriea.txt",
    },
    {
        "sourceId": "openfootball_current_ligue_1",
        "name": "OpenFootball Ligue 1",
        "competitionId": "ligue-1",
        "competition": "Ligue 1",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Paris",
        "url": "https://raw.githubusercontent.com/openfootball/europe/master/france/2026-27_fr1.txt",
    },
    {
        "sourceId": "openfootball_current_eredivisie",
        "name": "OpenFootball Eredivisie",
        "competitionId": "eredivisie",
        "competition": "Eredivisie",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Amsterdam",
        "url": "https://raw.githubusercontent.com/openfootball/europe/master/netherlands/2026-27_nl1.txt",
    },
    {
        "sourceId": "openfootball_current_primeira_liga",
        "name": "OpenFootball Primeira Liga",
        "competitionId": "primeira-liga",
        "competition": "Primeira Liga",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Lisbon",
        "url": "https://raw.githubusercontent.com/openfootball/europe/master/portugal/2026-27_pt1.txt",
    },
)

# The current-season lane is deliberately kept separate from this historical
# lane. These fixed, public OpenFootball Football.TXT files cover the three
# most recent completed seasons. They are used only to make team research and
# causal form windows evidence-backed; they never become prediction/model
# inputs, and they are not mixed into the upcoming fixture list.
_OPENFOOTBALL_HISTORY_LEAGUES: tuple[dict[str, str], ...] = (
    {
        "slug": "premier_league",
        "name": "Premier League",
        "competitionId": "premier-league",
        "competition": "Premier League",
        "repo": "england",
        "path": "1-premierleague.txt",
        "timezone": "Europe/London",
    },
    {
        "slug": "championship",
        "name": "Championship",
        "competitionId": "championship",
        "competition": "Championship",
        "repo": "england",
        "path": "2-championship.txt",
        "timezone": "Europe/London",
    },
    {
        "slug": "bundesliga",
        "name": "Bundesliga",
        "competitionId": "bundesliga",
        "competition": "Bundesliga",
        "repo": "deutschland",
        "path": "1-bundesliga.txt",
        "timezone": "Europe/Berlin",
    },
    {
        "slug": "la_liga",
        "name": "La Liga",
        "competitionId": "la-liga",
        "competition": "La Liga",
        "repo": "espana",
        "path": "1-liga.txt",
        "timezone": "Europe/Madrid",
    },
    {
        "slug": "serie_a",
        "name": "Serie A",
        "competitionId": "serie-a",
        "competition": "Serie A",
        "repo": "italy",
        "path": "1-seriea.txt",
        "timezone": "Europe/Rome",
    },
    {
        "slug": "ligue_1",
        "name": "Ligue 1",
        "competitionId": "ligue-1",
        "competition": "Ligue 1",
        "repo": "europe",
        "path": "france/{season}_fr1.txt",
        "timezone": "Europe/Paris",
    },
    {
        "slug": "eredivisie",
        "name": "Eredivisie",
        "competitionId": "eredivisie",
        "competition": "Eredivisie",
        "repo": "europe",
        "path": "netherlands/{season}_nl1.txt",
        "timezone": "Europe/Amsterdam",
    },
    {
        "slug": "primeira_liga",
        "name": "Primeira Liga",
        "competitionId": "primeira-liga",
        "competition": "Primeira Liga",
        "repo": "europe",
        "path": "portugal/{season}_pt1.txt",
        "timezone": "Europe/Lisbon",
    },
)
_OPENFOOTBALL_HISTORY_SEASONS = ("2025-26", "2024-25", "2023-24")


def _history_source(league: Mapping[str, str], season: str) -> dict[str, str]:
    path = league["path"].format(season=season)
    # The European repository keeps its league files directly below the
    # country directory, while the country repositories use a season folder.
    url_path = path if league["repo"] == "europe" else f"{season}/{path}"
    return {
        "sourceId": f"openfootball_history_{league['slug']}_{season.replace('-', '_')}",
        "name": f"OpenFootball {league['name']} {season} history",
        "competitionId": league["competitionId"],
        "competition": league["competition"],
        "season": season,
        "format": "txt",
        "timezone": league["timezone"],
        "url": f"https://raw.githubusercontent.com/openfootball/{league['repo']}/master/{url_path}",
    }


OPENFOOTBALL_HISTORY_SOURCES: tuple[dict[str, str], ...] = tuple(
    _history_source(league, season)
    for season in _OPENFOOTBALL_HISTORY_SEASONS
    for league in _OPENFOOTBALL_HISTORY_LEAGUES
)

_OPENFOOTBALL_ROUND_RE = re.compile(r"^\s*[▪»]\s*(?P<round>.+?)\s*$")
_OPENFOOTBALL_DATE_RE = re.compile(
    r"^\s{0,2}(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})(?:\s+(?P<year>\d{4}))?\s*$"
)
_OPENFOOTBALL_MATCH_RE = re.compile(
    r"^\s{4}(?:(?P<time>\d{1,2}:\d{2})\s{2})?"
    r"(?P<home>.+?)\s+v\s+(?P<away_and_score>.+?)\s*$"
)
# Completed historical Football.TXT files use a compact score-first grammar
# (``Home 2-1 Away``) rather than the current-season ``Home v Away 2-1``
# grammar.  Keep this parser explicit so a score is never inferred from a
# missing result.
_OPENFOOTBALL_HISTORICAL_MATCH_RE = re.compile(
    r"^\s{2}(?:(?P<time>\d{1,2}:\d{2})\s{2,})?"
    r"(?P<home>.+?)\s{2,}(?P<home_goals>\d+)-(?P<away_goals>\d+)"
    r"(?:\s+\((?P<ht_home>\d+)-(?P<ht_away>\d+)\))?\s{2,}"
    r"(?P<away>.+?)\s*$"
)
_OPENFOOTBALL_SCORE_RE = re.compile(
    r"\s+(?P<home>\d+)-(?P<away>\d+)"
    r"(?:\s+\([^)]*\))?\s*$"
)
_OPENFOOTBALL_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

OPENLIGADB_LEAGUES: dict[str, dict[str, str]] = {
    "bl1": {"name": "OpenLigaDB Bundesliga", "competitionId": "bundesliga"},
    "bl2": {"name": "OpenLigaDB 2. Bundesliga", "competitionId": "bundesliga-2"},
    "bl3": {"name": "OpenLigaDB 3. Liga", "competitionId": "bundesliga-3"},
    # OpenLigaDB also publishes a small set of non-German competitions.  They
    # use the same bounded fixture/result contract and are kept in the
    # independent display-only lane; they never enter the model snapshot.
    "pl": {"name": "OpenLigaDB Premier League", "competitionId": "premier-league"},
    "la1": {"name": "OpenLigaDB LaLiga", "competitionId": "la-liga"},
    "ucl": {"name": "OpenLigaDB Champions League", "competitionId": "champions-league"},
    "dfb": {"name": "OpenLigaDB DFB Pokal", "competitionId": "dfb-pokal"},
    "uel2026": {"name": "OpenLigaDB Europa League", "competitionId": "europa-league"},
    "nla": {"name": "OpenLigaDB Nations League A", "competitionId": "nations-league-a"},
    "wm26": {"name": "OpenLigaDB World Cup 2026", "competitionId": "world-cup"},
}
DEFAULT_OPENLIGADB_LEAGUES = ("bl1", "bl2", "bl3")
# The collector uses a wider allowlisted set than the backwards-compatible
# helper default above. Tests and one-off diagnostics can still request the
# original three German leagues explicitly.
REMOTE_OPENLIGADB_LEAGUES = (
    "bl1", "bl2", "bl3", "pl", "la1", "ucl", "dfb", "uel2026", "nla", "wm26",
)

OPENLIGADB_LICENSE_URL = "https://www.openligadb.de/lizenz"
WIKIDATA_LICENSE_URL = "https://www.wikidata.org/wiki/Wikidata:Licensing"
MET_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
MET_TERMS_URL = "https://api.met.no/doc/TermsOfService"

# Football-Data.co.uk publishes small, downloadable CSV files for the same
# top-flight competitions that the primary fixture lane covers.  We only
# retain a source-health/count row for this sidecar: the CSV contains betting
# columns, but no odds or raw rows are written to the Matchline facts object.
# This keeps the source useful for coverage diagnostics without accidentally
# turning an external odds archive into a model input.
FOOTBALL_DATA_DISCLAIMER_URL = "https://www.football-data.co.uk/disclaimer.php"
FOOTBALL_DATA_SOURCES: tuple[dict[str, str], ...] = (
    {"code": "E0", "name": "Premier League", "competitionId": "premier-league"},
    {"code": "E1", "name": "Championship", "competitionId": "championship"},
    {"code": "D1", "name": "Bundesliga", "competitionId": "bundesliga"},
    {"code": "SP1", "name": "La Liga", "competitionId": "la-liga"},
    {"code": "I1", "name": "Serie A", "competitionId": "serie-a"},
    {"code": "F1", "name": "Ligue 1", "competitionId": "ligue-1"},
    {"code": "N1", "name": "Eredivisie", "competitionId": "eredivisie"},
    {"code": "P1", "name": "Primeira Liga", "competitionId": "primeira-liga"},
)


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


def _http_payload(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[int, bytes]:
    """Read one fixed HTTPS source without following redirects."""

    request_headers = {"Accept": "*/*", "User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, method="GET", headers=request_headers)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            declared = response.headers.get("Content-Length")
            if declared and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
                return status, b""
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""
    return status, body if len(body) <= MAX_RESPONSE_BYTES else b""


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


def _collect_openligadb_league(
    season: int,
    retrieved_at: str,
    shortcut: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    config = OPENLIGADB_LEAGUES[shortcut]
    url = f"https://api.openligadb.de/getmatchdata/{shortcut}/{season}"
    source_id = "openligadb_secondary_results" if shortcut == "bl1" else f"openligadb_secondary_results_{shortcut}"
    status, body, payload = _http_json(url)
    if status != 200 or not isinstance(payload, list):
        return (
            _source_status(
                source_id=source_id,
                name=config["name"],
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
        raw_shortcut = value.get("leagueShortcut", value.get("LeagueShortcut"))
        if raw_shortcut is not None and str(raw_shortcut).strip().lower() != shortcut:
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
            "id": f"openligadb:{shortcut}:{match_token}",
            "provider": "OpenLigaDB",
            "league": shortcut,
            "competition": config["name"],
            "competitionId": config["competitionId"],
            "providerMatchId": match_token,
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
        source_id=source_id,
        name=config["name"],
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


def collect_openligadb(
    season: int,
    retrieved_at: str,
    league_shortcuts: Sequence[str] = DEFAULT_OPENLIGADB_LEAGUES,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    selected = tuple(dict.fromkeys(league_shortcuts))
    if not selected or any(shortcut not in OPENLIGADB_LEAGUES for shortcut in selected):
        raise ValueError("openligadb_league_not_allowlisted")
    sources: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for shortcut in selected:
        source, league_rows = _collect_openligadb_league(season, retrieved_at, shortcut)
        sources.append(source)
        for row in league_rows:
            identity = f"{row['league']}:{row['providerMatchId']}"
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(row)
    rows.sort(key=lambda item: (str(item["kickoffAt"]), str(item["id"])))
    return sources, rows


def _openfootball_date(value: object, *, season: str, previous: date | None = None) -> date | None:
    raw = text(value, 32)
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        match = re.fullmatch(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:\s+(\d{4}))?", raw)
        if not match:
            return None
        month = _OPENFOOTBALL_MONTHS[match.group(1)]
        if match.group(3) is not None:
            year = int(match.group(3))
        elif previous is not None:
            # Football.TXT omits the year for dates within a season.  Once an
            # explicit year (for example ``Jan 8 2027``) has appeared, keep
            # that year for the following same-month rows and only roll over
            # when the month moves backwards.
            year = previous.year + (1 if month < previous.month else 0)
        else:
            year = int(season[:4])
        return date(year, month, int(match.group(2)))
    except (KeyError, TypeError, ValueError):
        return None


def _openfootball_kickoff(
    scheduled_date: date | None,
    value: object,
    *,
    timezone_name: str,
) -> str | None:
    if scheduled_date is None:
        return None
    raw = text(value, 16)
    if not raw or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw) is None:
        return None
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        local = datetime.combine(scheduled_date, time(hour, minute), tzinfo=ZoneInfo(timezone_name))
        return local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError, KeyError):
        return None


def _openfootball_score(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    full_time = value.get("ft")
    if not isinstance(full_time, Sequence) or isinstance(full_time, (str, bytes)) or len(full_time) != 2:
        return None
    home, away = full_time
    if (
        isinstance(home, bool)
        or isinstance(away, bool)
        or not isinstance(home, int)
        or not isinstance(away, int)
        or home < 0
        or away < 0
    ):
        return None
    return {"home": home, "away": away}


def _openfootball_id(source_id: str, scheduled_date: str, home: str, away: str, round_name: str | None) -> str:
    token = "|".join((source_id, scheduled_date, home, away, round_name or ""))
    return f"openfootball:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:24]}"


def _openfootball_row(
    *,
    config: Mapping[str, str],
    source: Mapping[str, object],
    scheduled_date: date,
    kickoff_at: str | None,
    home: str,
    away: str,
    round_name: str | None,
    score: dict[str, int] | None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": _openfootball_id(config["sourceId"], scheduled_date.isoformat(), home, away, round_name),
        "provider": "OpenFootball",
        "sourceId": config["sourceId"],
        "sourceUrl": config["url"],
        "competitionId": config["competitionId"],
        "competition": config["competition"],
        "season": config["season"],
        "scheduledDate": scheduled_date.isoformat(),
        "kickoffAt": kickoff_at,
        "homeTeam": home,
        "awayTeam": away,
        "status": "finished" if score is not None else "scheduled",
        "round": round_name,
        "source": dict(source),
    }
    if score is not None:
        row["score"] = score
    return row


def _parse_openfootball_json(payload: bytes, config: Mapping[str, str], source: Mapping[str, object]) -> list[dict[str, object]]:
    try:
        parsed = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("openfootball_json_invalid") from exc
    matches = parsed.get("matches") if isinstance(parsed, Mapping) else None
    if not isinstance(matches, list):
        raise ValueError("openfootball_json_matches_missing")
    rows: list[dict[str, object]] = []
    current_date: date | None = None
    current_round: str | None = None
    for value in matches[:MAX_OPENFOOTBALL_ROWS_PER_SOURCE]:
        if not isinstance(value, Mapping):
            continue
        candidate_date = _openfootball_date(value.get("date"), season=config["season"], previous=current_date)
        if candidate_date is not None:
            current_date = candidate_date
        if current_date is None:
            continue
        candidate_round = text(value.get("round"), 80)
        if candidate_round:
            current_round = candidate_round
        home = text(value.get("team1"), 160)
        away = text(value.get("team2"), 160)
        if not home or not away:
            continue
        rows.append(
            _openfootball_row(
                config=config,
                source=source,
                scheduled_date=current_date,
                kickoff_at=_openfootball_kickoff(current_date, value.get("time"), timezone_name=config["timezone"]),
                home=home,
                away=away,
                round_name=current_round,
                score=_openfootball_score(value.get("score")),
            )
        )
    return rows


def _parse_openfootball_txt(payload: bytes, config: Mapping[str, str], source: Mapping[str, object]) -> list[dict[str, object]]:
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("openfootball_txt_invalid") from exc
    rows: list[dict[str, object]] = []
    current_date: date | None = None
    current_time: str | None = None
    current_round: str | None = None
    for line in lines[: MAX_OPENFOOTBALL_ROWS_PER_SOURCE * 2]:
        round_match = _OPENFOOTBALL_ROUND_RE.fullmatch(line)
        if round_match:
            current_round = text(round_match.group("round"), 80)
            current_time = None
            continue
        date_match = _OPENFOOTBALL_DATE_RE.fullmatch(line)
        if date_match:
            month = _OPENFOOTBALL_MONTHS[date_match.group("month")]
            explicit_year = date_match.group("year")
            if explicit_year is not None:
                year = int(explicit_year)
            elif current_date is not None:
                year = current_date.year + (1 if month < current_date.month else 0)
            else:
                year = int(config["season"][:4])
            try:
                current_date = date(year, month, int(date_match.group("day")))
            except ValueError:
                current_date = None
            current_time = None
            continue
        match = _OPENFOOTBALL_MATCH_RE.fullmatch(line)
        historical_match = (
            _OPENFOOTBALL_HISTORICAL_MATCH_RE.fullmatch(line)
            if match is None
            else None
        )
        if match is None and historical_match is None or current_date is None:
            continue
        parsed_match = match if match is not None else historical_match
        assert parsed_match is not None
        explicit_time = parsed_match.group("time")
        if explicit_time:
            current_time = explicit_time
        home = text(parsed_match.group("home"), 160)
        if not home:
            continue
        score: dict[str, int] | None
        if historical_match is not None:
            away = text(historical_match.group("away"), 160)
            score = {
                "home": int(historical_match.group("home_goals")),
                "away": int(historical_match.group("away_goals")),
            }
        else:
            assert match is not None
            away_and_score = text(match.group("away_and_score"), 200)
            if not away_and_score:
                continue
            score_match = _OPENFOOTBALL_SCORE_RE.search(away_and_score)
            if score_match:
                away = away_and_score[: score_match.start()].strip()
                score = {"home": int(score_match.group("home")), "away": int(score_match.group("away"))}
            else:
                away = away_and_score
                score = None
        if not away:
            continue
        rows.append(
            _openfootball_row(
                config=config,
                source=source,
                scheduled_date=current_date,
                kickoff_at=_openfootball_kickoff(current_date, current_time, timezone_name=config["timezone"]),
                home=home,
                away=away,
                round_name=current_round,
                score=score,
            )
        )
        if len(rows) >= MAX_OPENFOOTBALL_ROWS_PER_SOURCE:
            break
    return rows


def _collect_openfootball_source(
    config: Mapping[str, str],
    retrieved_at: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    url = config["url"]
    status, body = _http_payload(url)
    if status != 200 or not body:
        return (
            _source_status(
                source_id=config["sourceId"],
                name=config["name"],
                url=url,
                retrieved_at=retrieved_at,
                status="unavailable" if status == 0 else "failed",
                record_count=None,
                license_url=OPENFOOTBALL_LICENSE_URL,
                http_status=status or None,
                error_code="http_error" if status else "transport_error",
                body=body,
            ),
            [],
        )
    source = {
        "sourceId": config["sourceId"],
        "provider": "OpenFootball",
        "url": url,
        "retrievedAt": retrieved_at,
        "license": "CC0-1.0",
        "licenseUrl": OPENFOOTBALL_LICENSE_URL,
        "rawSha256": sha256_hex(body),
    }
    try:
        rows = (
            _parse_openfootball_json(body, config, source)
            if config["format"] == "json"
            else _parse_openfootball_txt(body, config, source)
        )
    except ValueError as exc:
        return (
            _source_status(
                source_id=config["sourceId"],
                name=config["name"],
                url=url,
                retrieved_at=retrieved_at,
                status="failed",
                record_count=None,
                license_url=OPENFOOTBALL_LICENSE_URL,
                http_status=status,
                error_code=str(exc),
                body=body,
            ),
            [],
        )
    rows.sort(key=lambda item: (str(item.get("kickoffAt") or item.get("scheduledDate") or ""), str(item["id"])))
    return (
        _source_status(
            source_id=config["sourceId"],
            name=config["name"],
            url=url,
            retrieved_at=retrieved_at,
            status="fresh" if rows else "observed_empty",
            record_count=len(rows),
            license_url=OPENFOOTBALL_LICENSE_URL,
            http_status=status,
            error_code=None if rows else "no_valid_matches",
            body=body,
        ),
        rows,
    )


def collect_openfootball(
    retrieved_at: str,
    configs: Sequence[Mapping[str, str]] = OPENFOOTBALL_CURRENT_SOURCES,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    sources: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for config in configs:
        source, source_rows = _collect_openfootball_source(config, retrieved_at)
        sources.append(source)
        for row in source_rows:
            row_id = str(row["id"])
            if row_id in seen:
                continue
            seen.add(row_id)
            rows.append(row)
    rows.sort(key=lambda item: (str(item.get("kickoffAt") or item.get("scheduledDate") or ""), str(item["id"])))
    return sources, rows


def _football_data_row_count(body: bytes) -> int:
    """Count parseable fixture rows without retaining CSV or odds columns."""

    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = body.decode("latin-1")
    count = 0
    reader = csv.DictReader(io.StringIO(decoded))
    for row in reader:
        if not isinstance(row, Mapping):
            continue
        raw_date = text(row.get("Date"), 20)
        home = text(row.get("HomeTeam"), 160)
        away = text(row.get("AwayTeam"), 160)
        if not raw_date or not home or not away:
            continue
        try:
            datetime.strptime(raw_date, "%d/%m/%Y")
        except ValueError:
            continue
        count += 1
    return count


def _collect_football_data_source(
    config: Mapping[str, str],
    season: int,
    retrieved_at: str,
) -> dict[str, object]:
    season_code = f"{season % 100:02d}{(season + 1) % 100:02d}"
    url = f"https://www.football-data.co.uk/mmz4281/{season_code}/{config['code']}.csv"
    source_id = f"football_data_current_{config['code'].lower()}"
    status, body = _http_payload(url, headers={"Accept": "text/csv, */*"})
    if status != 200 or not body:
        return _source_status(
            source_id=source_id,
            name=f"Football-Data.co.uk {config['name']}",
            url=url,
            retrieved_at=retrieved_at,
            status="unavailable" if status == 0 else "failed",
            record_count=None,
            license_url=FOOTBALL_DATA_DISCLAIMER_URL,
            terms_url=FOOTBALL_DATA_DISCLAIMER_URL,
            http_status=status or None,
            error_code="http_error" if status else "transport_error",
            body=body,
        )
    try:
        record_count = _football_data_row_count(body)
    except (csv.Error, ValueError):
        record_count = 0
    return _source_status(
        source_id=source_id,
        name=f"Football-Data.co.uk {config['name']}",
        url=url,
        retrieved_at=retrieved_at,
        status="fresh" if record_count > 0 else "observed_empty",
        record_count=record_count,
        license_url=FOOTBALL_DATA_DISCLAIMER_URL,
        terms_url=FOOTBALL_DATA_DISCLAIMER_URL,
        http_status=status,
        error_code=None if record_count > 0 else "no_parseable_fixtures",
        body=body,
    )


def collect_football_data(season: int, retrieved_at: str) -> list[dict[str, object]]:
    """Probe the current-season CSV sidecar and return only health metadata."""

    return [_collect_football_data_source(config, season, retrieved_at) for config in FOOTBALL_DATA_SOURCES]


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
    openfootball_sources, openfootball_matches = collect_openfootball(retrieved_at)
    # Historical rows are fetched into a separate bounded section.  The
    # public match board still reads only ``openfootball.matches`` (the
    # current season), while the team-research lane can use this completed
    # season for real form and W-D-L evidence.
    openfootball_history_sources, openfootball_history_matches = collect_openfootball(
        retrieved_at,
        configs=OPENFOOTBALL_HISTORY_SOURCES,
    )
    openligadb_sources, matches = collect_openligadb(
        season,
        retrieved_at,
        league_shortcuts=REMOTE_OPENLIGADB_LEAGUES,
    )
    football_data_sources = collect_football_data(season, retrieved_at)
    wikidata_source, entities = collect_wikidata(retrieved_at)
    met_source, weather = collect_met(retrieved_at, coordinates)
    sources = [
        *openfootball_sources,
        *openfootball_history_sources,
        *openligadb_sources,
        *football_data_sources,
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
        "openfootball": {
            "season": "2026-27",
            "sources": [
                {
                    "sourceId": source["sourceId"],
                    "status": source["status"],
                    "recordCount": source["recordCount"],
                }
                for source in openfootball_sources
            ],
            "matches": openfootball_matches,
            "history": {
                "seasons": sorted({str(row["season"]) for row in openfootball_history_matches}),
                "sources": [
                    {
                        "sourceId": source["sourceId"],
                        "status": source["status"],
                        "recordCount": source["recordCount"],
                    }
                    for source in openfootball_history_sources
                ],
                "matches": openfootball_history_matches,
            },
        },
        "openligadb": {
            "season": season,
            "leagues": [
                {
                    "shortcut": source["sourceId"].removeprefix("openligadb_secondary_results_")
                    if source["sourceId"] != "openligadb_secondary_results" else "bl1",
                    "sourceId": source["sourceId"],
                    "status": source["status"],
                    "recordCount": source["recordCount"],
                }
                for source in openligadb_sources
            ],
            "matches": matches,
        },
        # The sidecar deliberately has no match rows in this envelope. Its
        # source-status records prove server reachability and parseable
        # fixture counts while keeping the CSV's odds columns out of the
        # public facts payload and model lane.
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
        "openfootball": len(value["openfootball"]["matches"]),
        "openligadb": len(value["openligadb"]["matches"]),
        "footballData": sum(
            int(source.get("recordCount") or 0)
            for source in value["sources"]
            if str(source.get("sourceId", "")).startswith("football_data_")
        ),
        "wikidata": len(value["wikidata"]["entities"]),
        "metNorway": len(value["metNorway"]["observations"]),
        "factsOnly": value["factsOnly"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
